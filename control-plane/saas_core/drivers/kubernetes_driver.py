"""KubernetesDriver — a real, API-backed ComputeDriver, and the ONLY
ComputeDriver implementation (ssh_docker was fully retired — see
/ROADMAP.md §3.1/§5 Phase 2). Talks to the real Kubernetes API (the
official ``kubernetes`` PyPI client) and manages the Compute Service
operator's ``OdooInstance`` custom resource (``compute/operator``), the
same resource a human would ``kubectl apply -f``.

Mapping (ComputeDriver -> Kubernetes):
  create           -> create the OdooInstance CR (operator does the rest)
  destroy          -> delete the OdooInstance CR (the operator's finalizer
                       already tears down the whole tenant namespace —
                       there is no lesser "stop but keep data" delete)
  start/stop       -> patch spec.suspended = false/true (already built,
                       see the operator's reconcileSuspended)
  restart          -> stop then start (ABC default; no native rolling-
                       restart trigger is exposed on the CR yet)
  exec             -> Kubernetes pods/exec subresource against the web pod
                       (one-shot, non-interactive; see exec_interactive()
                       for the long-lived TTY session a terminal needs)
  logs             -> Kubernetes pods/log subresource
  health           -> OdooInstance.status.phase + the web pod's own
                       container restart count (for crash-loop detection)

The naming below (group/version/plural, the "odoo-tenant-" namespace
prefix, the Deployment always being literally named "odoo") mirrors the
operator's own Go constants exactly (api/v1alpha1/constants.go,
internal/resources/naming.go) byte for byte. There is no way to discover
these from the cluster at runtime — keeping the two sides in sync by hand
is a real, standing constraint on ever renaming either one.

Scope note: ``create()`` IS called by real tenant provisioning —
``saas.instance._do_deploy_locked`` calls this driver's ``create()``
directly; see ``_do_deploy_locked_kubernetes``.
"""

from __future__ import annotations

import base64
import json
import logging
import shlex
import time
import datetime
from typing import Optional

import yaml
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream as k8s_stream

from .base import ComputeDriver, ComputeSpec, ComputeHandle, ExecResult, HealthStatus
from .k8s_builds import ImageBuildMixin, read_pod_log, docker_config_json, _TENANT_PULL_SECRET

# Kubernetes exec subresource WebSocket sub-channel indices (v4/v5
# channel.k8s.io protocol) — STDIN=0/STDOUT=1/STDERR=2/ERROR=3/RESIZE=4.
# Not exported as named constants by the `kubernetes` package's public
# API; verified against the installed `kubernetes.stream.ws_client`
# source (STDIN_CHANNEL/RESIZE_CHANNEL module-level ints).
_RESIZE_CHANNEL = 4


class K8sExecChannel:
    """Adapts a Kubernetes exec WebSocket (``kubernetes.stream``'s
    ``WSClient``, opened with ``tty=True, binary=True``) to look like a
    paramiko ``Channel`` — just enough surface for
    ``saas_core/controllers/ssh_terminal.py``'s pump/relay loop (built
    originally for a real SSH channel) to drive it unchanged: ``recv_ready``/
    ``recv``/``sendall``/``closed``/``get_transport().is_active()``/
    ``resize_pty``/``close``/``fileno`` (the last is what makes this
    ``select()``-able — verified ``websocket.WebSocket.fileno()``, which
    ``WSClient.sock`` is an instance of, delegates to the real OS socket)."""

    def __init__(self, ws_client):
        self._ws = ws_client

    def fileno(self):
        return self._ws.sock.fileno()

    def recv_ready(self):
        self._ws.update(timeout=0)
        return bool(self._ws.peek_stdout() or self._ws.peek_stderr())

    def recv(self, n):
        # `n` (buffer size) is advisory only, matching how the caller
        # already treats paramiko's own `recv()` — a websocket frame is
        # already a discrete unit, there's no partial-read chunking to do.
        data = (self._ws.read_stdout(timeout=0) or b'') + \
               (self._ws.read_stderr(timeout=0) or b'')
        return data

    def sendall(self, data):
        self._ws.write_stdin(data)

    def resize_pty(self, width=None, height=None, **kwargs):
        if not self._ws.is_open():
            return
        self._ws.write_channel(
            _RESIZE_CHANNEL,
            json.dumps({'Width': width, 'Height': height}))

    @property
    def closed(self):
        return not self._ws.is_open()

    def get_transport(self):
        return self

    def is_active(self):
        return self._ws.is_open()

    def close(self):
        self._ws.close()


class K8sExecStdoutReader:
    """File-like (``.read(n)``) wrapper around a non-interactive
    Kubernetes exec WebSocket's stdout, for feeding a streaming
    consumer — an S3/GCS upload SDK's ``upload_fileobj``/resumable-
    upload call — without buffering the whole command's output in
    memory. This is the Kubernetes-exec counterpart of piping an SSH
    channel's stdout straight into the same uploader (see
    ``saas_instance_backup.py``'s ``_upload_stream_to_bucket``, written
    against exactly this ``.read(n)`` contract already).

    Stderr is captured separately (never mixed into the byte stream —
    unlike ``K8sExecChannel``, which deliberately interleaves them for
    terminal display) and available via ``.stderr``/``.returncode``
    once the stream is exhausted."""

    # Per-``read()``-call idle timeout: how long to wait for at least
    # one more byte before giving up, not a cap on the whole transfer —
    # a large dump that keeps producing output, even slowly, never hits
    # this; only a stall does.
    _POLL_TIMEOUT = 5.0

    def __init__(self, ws_client, timeout=3600):
        self._ws = ws_client
        self._idle_timeout = timeout
        self._stderr_chunks = []
        self.bytes_read = 0
        self._eof = False

    def read(self, size=-1):
        if self._eof:
            return b''
        want = None if (size is None or size < 0) else size
        chunks = []
        got = 0
        deadline = time.time() + self._idle_timeout
        while True:
            out = self._ws.read_stdout(timeout=self._POLL_TIMEOUT) or b''
            if out:
                chunks.append(out)
                got += len(out)
                deadline = time.time() + self._idle_timeout
            err = self._ws.read_stderr(timeout=0) or b''
            if err:
                self._stderr_chunks.append(err)
            if not self._ws.is_open():
                self._eof = True
                break
            if want is not None and got >= want:
                break
            if not out and time.time() > deadline:
                raise TimeoutError(
                    'exec_stream_out: no output for %ss' % self._idle_timeout)
        data = b''.join(chunks)
        self.bytes_read += len(data)
        return data

    @property
    def stderr(self):
        return b''.join(self._stderr_chunks).decode('utf-8', 'replace')

    @property
    def returncode(self):
        rc = self._ws.returncode
        return 0 if rc is None else rc

    def close(self):
        self._ws.close()


_logger = logging.getLogger(__name__)


class PrometheusUnavailable(RuntimeError):
    """The region has no reachable Prometheus (not configured, not
    installed, or the query failed). Callers degrade: CPU/RAM usage and
    history are simply unavailable."""


_PROMETHEUS_TIMEOUT = 10        # secs per Prometheus HTTP call
_PROMETHEUS_RATE_WINDOW = '1m'  # >= 4x the cAdvisor scrape interval (15s)

# Run inside the web pod: filestore size + every database the tenant's
# role owns (odoo.conf is rendered at pod start with the real credentials;
# the image ships python3 + psycopg2).
_MEASURE_STORAGE_CMD = r'''
set -e
echo "filestore_bytes=$(du -sb /var/lib/odoo | cut -f1)"
python3 - <<'PY'
import configparser, psycopg2
c = configparser.ConfigParser()
c.read('/etc/odoo/odoo.conf')
o = c['options']
conn = psycopg2.connect(
    host=o.get('db_host'), port=int(o.get('db_port') or 5432),
    user=o.get('db_user'), password=o.get('db_password'),
    dbname=o.get('db_name'), connect_timeout=10)
cur = conn.cursor()
cur.execute("""SELECT coalesce(sum(pg_database_size(d.datname)), 0)
               FROM pg_database d JOIN pg_roles r ON r.oid = d.datdba
               WHERE r.rolname = current_user""")
print('db_bytes=%d' % cur.fetchone()[0])
PY
'''

# Mirrors compute/operator/api/v1alpha1/constants.go + groupversion_info.go.
_GROUP = 'saas.odoo.example.com'
_VERSION = 'v1alpha1'
_PLURAL = 'odooinstances'


# WorkersSpec.Count bounds (+kubebuilder:validation:Minimum/Maximum).
_MAX_WORKERS = 32


def _clamp_workers(workers) -> int:
    return max(0, min(int(workers or 0), _MAX_WORKERS))
_NAMESPACE_PREFIX = 'odoo-tenant-'
# Mirrors internal/resources/naming.go: OdooDeploymentName always returns
# the literal string "odoo", regardless of the tenant's own name.
_CONTAINER_NAME = 'odoo'
# The operator's secret-free container for customer terminals (spec.shell).
_SHELL_CONTAINER_NAME = 'shell'
# Operator Secret with the master password and the database-manager key.
_ADMIN_SECRET_NAME = 'odoo-admin-credentials'

# Staff cluster terminal: a toolbox pod with kubectl + helm running as the
# ServiceAccount saas-toolbox/saas-toolbox. An operator creates that
# namespace, ServiceAccount and its role binding by hand
# (setup/03a-MICROK8S-CLUSTER-SETUP.md step 12.4, setup/03b-DOKS-CLUSTER-SETUP.md
# step 7.2); the control plane never grants permissions,
# it only starts the pod. The pod ends after _TOOLBOX_LIFETIME and is
# recreated on the next open. Its image is the cluster's toolbox_image
# (keep its kubectl within one minor version of the cluster).
_TOOLBOX_NAMESPACE = 'saas-toolbox'
_TOOLBOX_NAME = 'saas-toolbox'
_TOOLBOX_PULL_SECRET = 'saas-toolbox-registry'
_TOOLBOX_LIFETIME = 8 * 3600
_TOOLBOX_START_TIMEOUT = 180
_POD_LABEL_SELECTOR = 'app.kubernetes.io/name=odoo,app.kubernetes.io/instance=%s'
# The serving web pods only — not the init/update Job pods, which carry the
# same instance labels (and may be the only Running pod during an update).
_WEB_POD_LABEL_SELECTOR = _POD_LABEL_SELECTOR + ',saas.odoo.example.com/role=web'
# Same key `kubectl rollout restart` uses.
_RESTARTED_AT_ANNOTATION = 'kubectl.kubernetes.io/restartedAt'
# Mirrors internal/resources/naming.go's BackupCronJobName() — always the
# literal string "odoo-backup", regardless of the tenant's own name.
_BACKUP_CRONJOB_NAME = 'odoo-backup'
# Name of the Secret holding this tenant's object-storage backup
# credentials, referenced by spec.backup.destination.objectStorageSecretRef
# (compute/operator/api/v1alpha1/odooinstance_types.go) — never inlined
# into the CR itself.
_BACKUP_SECRET_NAME = 'odoo-backup-object-storage'

_IMAGE_PULL_ERRORS = ('ErrImagePull', 'ImagePullBackOff')


class ToolboxNotSetUp(RuntimeError):
    """The cluster's saas-toolbox ServiceAccount hasn't been created yet."""


class KubernetesDriver(ImageBuildMixin, ComputeDriver):
    """ComputeDriver backed by a real Kubernetes cluster.

    Same constructor shape as SshDockerDriver: a ``saas.server`` record and
    an optional connection to reuse. ``connection`` is accepted only for
    shape parity — the Kubernetes API client manages its own connection
    pooling, there is nothing to reuse across calls the way an open SSH
    session is reused."""

    def __init__(self, server, connection=None):
        self.server = server
        self._api_client = None
        # Tenant namespaces whose pull Secret this driver already wrote.
        self._pull_secret_synced = set()

    # -- API client / naming helpers -----------------------------------------
    def _client(self):
        if self._api_client is not None:
            return self._api_client
        kubeconfig = (self.server._kubeconfig_yaml() or '').strip()
        if not kubeconfig:
            raise RuntimeError(
                "Cluster '%s' has no kubeconfig configured." % self.server.name)
        try:
            config_dict = yaml.safe_load(kubeconfig)
        except yaml.YAMLError as e:
            raise RuntimeError(
                "Cluster '%s': kubeconfig is not valid YAML: %s"
                % (self.server.name, e)) from e
        configuration = k8s_client.Configuration()
        k8s_config.load_kube_config_from_dict(
            config_dict, client_configuration=configuration)
        self._api_client = k8s_client.ApiClient(configuration)
        return self._api_client

    def _custom_api(self):
        return k8s_client.CustomObjectsApi(self._client())

    def _core_api(self):
        return k8s_client.CoreV1Api(self._client())

    def _batch_api(self):
        return k8s_client.BatchV1Api(self._client())

    def _apps_api(self):
        return k8s_client.AppsV1Api(self._client())

    def require_cluster_ready(self, timeout=4):
        """Check controllers, not merely API reachability, before accepting work.

        Ready pods can conceal broken watches. A leader must still renew its
        lease. Managed clusters may hide control-plane leases; the operator
        deployment and (when enabled) its leader lease are always required.
        """
        deployments = self._apps_api().list_deployment_for_all_namespaces(
            label_selector='app.kubernetes.io/name=odoo-operator',
            _request_timeout=timeout).items
        if not deployments:
            raise RuntimeError('Cluster is not ready: Odoo operator is missing.')
        coordination = k8s_client.CoordinationV1Api(self._client())
        now = datetime.datetime.now(datetime.timezone.utc)

        def check_lease(namespace, name, required):
            try:
                lease = coordination.read_namespaced_lease(
                    name, namespace, _request_timeout=timeout)
            except ApiException as exc:
                if exc.status == 404 and not required:
                    return
                raise
            renewed = lease.spec.renew_time
            if renewed and renewed.tzinfo is None:
                renewed = renewed.replace(tzinfo=datetime.timezone.utc)
            age = (now - renewed).total_seconds() if renewed else None
            if not lease.spec.holder_identity or age is None or age > 120 or age < -30:
                raise RuntimeError(
                    'Cluster controllers are stalled: %s/%s has no recent '
                    'leader heartbeat. Provisioning will retry after recovery.'
                    % (namespace, name))

        for deployment in deployments:
            if not (deployment.status.available_replicas or 0):
                raise RuntimeError('Cluster is not ready: Odoo operator is unavailable.')
            managers = [c for c in deployment.spec.template.spec.containers
                        if c.name == 'manager']
            if not managers:
                raise RuntimeError('Cluster is not ready: Odoo operator manager is missing.')
            # Without an explicit false flag, require a lease (fail closed).
            if any('--leader-elect=false' not in (c.args or []) for c in managers):
                check_lease(deployment.metadata.namespace,
                            'odoo-instance-operator.saas.odoo.example.com', True)
        for name in ('kube-scheduler', 'kube-controller-manager'):
            check_lease('kube-system', name, False)

    @staticmethod
    def _cr_name(handle_or_spec) -> str:
        # Same normalization the old stub used (container_name is always
        # 'odoo_<sub>') — kept so an existing ComputeHandle/ComputeSpec
        # value derives the identical name whether the caller predates
        # this rewrite or not.
        return handle_or_spec.container_name.replace('_', '-').lower()

    @classmethod
    def _namespace_for(cls, handle_or_spec) -> str:
        return _NAMESPACE_PREFIX + cls._cr_name(handle_or_spec)

    def _get_cr(self, name):
        try:
            return self._custom_api().get_cluster_custom_object(
                _GROUP, _VERSION, _PLURAL, name, _request_timeout=10)
        except ApiException as e:
            if e.status == 404:
                return None
            raise RuntimeError(
                'Kubernetes API error reading OdooInstance %s: %s' % (name, e)) from e

    @staticmethod
    def _pod_ready(pod):
        if pod is None or pod.metadata.deletion_timestamp or pod.status.phase != 'Running':
            return False
        conditions = pod.status.conditions or []
        pod_ready = any(c.type == 'Ready' and c.status == 'True' for c in conditions)
        # Serving readiness is the web container's alone: the cron sidecar
        # has no readiness probe and must not take web traffic down with it.
        web = next((c for c in (pod.status.container_statuses or []) if c.name == _CONTAINER_NAME), None)
        return pod_ready and web is not None and web.ready is True

    def _first_pod(self, namespace, cr_name):
        """The tenant's own web pod, or None if the namespace/pod doesn't
        exist yet (e.g. the CR was just created and the operator hasn't
        provisioned anything in it yet — not an error condition)."""
        try:
            pods = self._core_api().list_namespaced_pod(
                namespace, label_selector=_WEB_POD_LABEL_SELECTOR % cr_name,
                _request_timeout=10)
        except ApiException as e:
            if e.status == 404:
                return None
            raise RuntimeError(
                'listing pods in %s failed: %s' % (namespace, e)) from e
        active = [p for p in pods.items if not p.metadata.deletion_timestamp]
        ready = [p for p in active if self._pod_ready(p)]
        running = [p for p in active if p.status.phase == 'Running']
        candidates = ready or running or active or list(pods.items)
        return candidates[0] if candidates else None

    def _sync_tenant_pull_secret(self, namespace, force=False):
        """Best-effort write of the tenant pull Secret (see
        _ensure_tenant_pull_secret); never fails the caller."""
        if namespace in self._pull_secret_synced and not force:
            return
        try:
            if self._ensure_tenant_pull_secret(namespace):
                self._pull_secret_synced.add(namespace)
        except Exception:
            _logger.warning("Writing the registry pull Secret in %s failed",
                            namespace, exc_info=True)

    # -- lifecycle ------------------------------------------------------------
    def create(self, spec: ComputeSpec) -> ComputeHandle:
        """Create the OdooInstance CR. Idempotent against a retried call
        for the SAME instance (see the 409 branch below) — this matters
        because the durable job queue can and does retry ``create()``:
        Odoo's own cron-thread watchdog can kill/reload the whole server
        mid-poll on a slow deploy (image pull + CNPG/managed-Postgres
        bring-up + init Job legitimately take minutes, easily exceeding
        the default ``limit_time_real_cron`` ~120s — see
        `limit_time_real_cron` in setup/02-SAAS-SERVER-SETUP.md step 4), orphaning the
        `saas.job` row at `state='running'` with no error recorded; the
        next pickup of that job re-runs `_do_deploy_locked_kubernetes`
        from the top, calling `create()` again against a CR that was
        already successfully created the first time. Without this,
        every such retry fails outright with a 409 "already exists"
        instead of resuming — this was hit repeatedly during real
        testing against a live cluster this project maintains.
        """
        name = self._cr_name(spec)
        body = self._build_odoo_instance(name, spec)
        try:
            self._custom_api().create_cluster_custom_object(
                _GROUP, _VERSION, _PLURAL, body)
        except ApiException as e:
            if e.status == 409:
                # Verify by actually reading the CR back rather than just
                # trusting the 409's wording — the same status code also
                # covers a genuine name collision with an unrelated
                # object, which should still fail loudly.
                existing = self._get_cr(name)
                if existing is None:
                    raise RuntimeError(
                        'creating OdooInstance %s failed: reported as '
                        'already existing, but a follow-up read found '
                        'nothing (raced with a delete?): %s' % (name, e)
                    ) from e
                _logger.warning(
                    "KubernetesDriver.create(%s): CR already exists — "
                    "treating as an idempotent retry, not a failure "
                    "(existing phase: %s).", name,
                    ((existing.get('status') or {}).get('phase')) or 'unknown')
            else:
                raise RuntimeError(
                    'creating OdooInstance %s failed: %s' % (name, e)) from e
        # The operator creates the namespace asynchronously, so this usually
        # finds none yet; health() keeps retrying until the Secret is in.
        self._sync_tenant_pull_secret(self._namespace_for(spec))
        return ComputeHandle(
            server_id=self.server.id, container_name=spec.container_name,
            instance_path=self._namespace_for(spec), host='',
            http_port=spec.http_port)

    def _build_odoo_instance(self, name: str, spec: ComputeSpec) -> dict:
        """Map a ComputeSpec onto a minimal, valid OdooInstance body.

        Kubernetes-specific fields the thin, backend-agnostic ComputeSpec
        has no dedicated slot for (domain, TLS, filestore size, resource
        limits, the Odoo version itself) are read from ``spec.env`` rather
        than adding new dataclass fields to the shared, frozen
        ``ComputeSpec`` — an earlier plan anticipated needing to extend
        that dataclass, but ``env`` (already documented
        as "extra environment / template context") covers this without
        touching a type ``SshDockerDriver`` and its ~25 call sites also
        share, which is less invasive for identical effect.

        Fields the CRD schema itself defaults server-side (workers,
        database mode, networking) are deliberately omitted rather than
        hand-duplicated here — see OdooInstanceSpec's own
        +kubebuilder:default markers in
        compute/operator/api/v1alpha1/odooinstance_types.go. ``replicas``
        is fixed at 1; capacity changes only through resource requests/limits.
        """
        domain = (spec.env.get('domain') or '').strip()
        if not domain:
            raise RuntimeError(
                "ComputeSpec.env['domain'] is required to create a "
                "Kubernetes-backed instance — this CRD has no "
                "domain-independent mode.")
        odoo_version = spec.env.get('odoo_version') or '18.0'
        repository, sep, tag = spec.image.rpartition(':')
        if not sep:
            repository, tag = spec.image, odoo_version
        replicas = int(spec.env.get('replicas') or 1)
        if replicas != 1:
            raise ValueError('Only one Odoo replica is supported; increase resource limits instead.')

        tls_enabled = bool(spec.env.get('tls_enabled', False))
        tls = {'enabled': tls_enabled}
        if tls_enabled and spec.env.get('tls_issuer_name'):
            tls['issuerRef'] = {
                'name': spec.env['tls_issuer_name'],
                'kind': spec.env.get('tls_issuer_kind') or 'ClusterIssuer',
            }

        filestore = {'size': spec.env.get('filestore_size') or '5Gi'}
        image = {'repository': repository, 'tag': tag}
        if self._registry_pull_auth():
            # Any image may come from the cluster's private registry; the
            # Secret itself is written once the namespace exists.
            image['pullSecretRefs'] = [{'name': _TENANT_PULL_SECRET}]
        body = {
            'apiVersion': '%s/%s' % (_GROUP, _VERSION),
            'kind': 'OdooInstance',
            'metadata': {'name': name},
            'spec': {
                'version': odoo_version,
                'image': image,
                'domain': {
                    'hostname': domain,
                    'tls': tls,
                },
                'storage': {'filestore': filestore, 'sharedWithDatabase': True},
                'replicas': replicas,
                'resources': {
                    'requests': {
                        'cpu': spec.env.get('cpu_request') or '250m',
                        'memory': spec.env.get('mem_request') or '512Mi',
                    },
                    'limits': {
                        'cpu': spec.env.get('cpu_limit') or '1',
                        'memory': spec.env.get('mem_limit') or '2Gi',
                    },
                },
            },
        }

        if spec.env.get('database_filter'):
            body['spec']['databaseFilter'] = spec.env['database_filter']
        database = self._database_spec(spec.env)
        if database:
            body['spec']['database'] = database
        if spec.env.get('quota'):
            body['spec']['tenancy'] = {'resourceQuota': dict(spec.env['quota'])}
        if spec.env.get('db_manager_prefix'):
            body['spec']['databaseManager'] = {'prefix': spec.env['db_manager_prefix'],
                                               'maxDatabases': int(spec.env.get('db_manager_max_databases') or 0)}
        if spec.env.get('shell'):
            body['spec']['shell'] = True

        if spec.env.get('workers') is not None:
            # Explicit, including 0 (dev mode) — see WorkersSpec.Count.
            body['spec']['workers'] = {
                'count': _clamp_workers(spec.env['workers']),
                'maxCronThreads': 1,
            }

        # A caller provisioning a brand-new instance FROM an existing
        # backup (not the in-place full-instance restore
        # `saas.instance.backup._do_restore_full_instance` uses for an
        # ALREADY-running instance — see that method) hands the source
        # through spec.env['restore'] rather than a new ComputeSpec field,
        # for the same reason domain/tls/resources do (see the docstring
        # above). Mirrors RestoreSourceSpec exactly
        # (compute/operator/api/v1alpha1/odooinstance_types.go) — restore
        # is immutable once set on the CR, so this must be present at
        # create() time; there is no later "attach a restore" call. No
        # current caller uses this path (kept as the documented mechanism
        # for whenever "provision fresh from a backup" is needed).
        restore_cfg = spec.env.get('restore')
        if restore_cfg:
            source = {
                'type': 'ObjectStorage',
                'bucket': restore_cfg['bucket'],
                'prefix': restore_cfg['prefix'],
                'objectStorageSecretRef': {'name': restore_cfg['secret_name']},
            }
            if restore_cfg.get('backup_id'):
                source['backupId'] = restore_cfg['backup_id']
            body['spec']['restore'] = {'source': source}

        return body

    def destroy(self, handle: ComputeHandle, *, purge=False) -> None:
        # `purge` has no separate meaning here (kept only for signature
        # parity with SshDockerDriver, whose ABC-adjacent kwarg it already
        # doesn't declare either): deleting the OdooInstance CR already
        # tears down the ENTIRE tenant namespace via the operator's own
        # finalizer (internal/controller/finalize.go) — there is no
        # lesser "remove the workload but keep the volumes" delete at the
        # CR level the way `compose down` (no -v) is for Docker.
        name = self._cr_name(handle)
        try:
            self._custom_api().delete_cluster_custom_object(
                _GROUP, _VERSION, _PLURAL, name)
        except ApiException as e:
            if e.status != 404:
                raise RuntimeError(
                    'deleting OdooInstance %s failed: %s' % (name, e)) from e

    def set_database_filter(self, handle: ComputeHandle, database_filter: str) -> None:
        """Set which databases the instance's Odoo serves (odoo.conf
        dbfilter; empty = only its own). A change rolls the pods (zero
        downtime); setting the current value is a no-op."""
        name = self._cr_name(handle)
        try:
            self._custom_api().patch_cluster_custom_object(
                _GROUP, _VERSION, _PLURAL, name,
                {'spec': {'databaseFilter': database_filter or None}})
        except ApiException as e:
            raise RuntimeError(
                'setting the database filter of %s failed: %s' % (name, e)) from e

    def set_hosting_access(self, handle: ComputeHandle, database_filter: str,
                           db_manager_prefix: str, max_databases: int = 0) -> None:
        """Hosting customers' access in one patch (one rollout): the
        databases Odoo serves, the database manager limited to
        ``db_manager_prefix``, and the secret-free ``shell`` container for
        their terminal. Setting the current values is a no-op."""
        name = self._cr_name(handle)
        try:
            self._custom_api().patch_cluster_custom_object(
                _GROUP, _VERSION, _PLURAL, name, {'spec': {
                    'databaseFilter': database_filter or None,
                    'databaseManager': {'prefix': db_manager_prefix, 'maxDatabases': max_databases} if db_manager_prefix else None,
                    'shell': True,
                }})
        except ApiException as e:
            raise RuntimeError(
                'setting the hosting access of %s failed: %s' % (name, e)) from e

    def hosting_access_ready(self, handle: ComputeHandle) -> bool:
        """Whether the serving pods already run with the database manager
        and the shell container (they roll after set_hosting_access)."""
        namespace, pod = self._resolve_pod(handle)
        if pod is None or not pod.status or pod.status.phase != 'Running':
            return False
        names = {c.name for c in (pod.spec.containers or [])}
        args = ' '.join(next(
            (c.args or [] for c in pod.spec.containers if c.name == _CONTAINER_NAME), []))
        return _SHELL_CONTAINER_NAME in names and '--load=' in args

    def database_manager_key(self, handle: ComputeHandle) -> str:
        """The key the tenant's database-manager addon verifies links
        with (operator Secret odoo-admin-credentials, key dbmanager-key)."""
        namespace = handle.instance_path or self._namespace_for(handle)
        try:
            secret = self._core_api().read_namespaced_secret(_ADMIN_SECRET_NAME, namespace)
        except ApiException as e:
            raise RuntimeError(
                'reading the database manager key of %s failed: %s'
                % (self._cr_name(handle), e)) from e
        raw = (secret.data or {}).get('dbmanager-key')
        if not raw:
            raise RuntimeError(
                'the database manager key of %s is not ready yet' % self._cr_name(handle))
        return base64.b64decode(raw).decode()

    def exists(self, handle: ComputeHandle) -> bool:
        """Whether the OdooInstance CR still exists — including while its
        finalizer is still tearing the tenant down after ``destroy()``."""
        return self._get_cr(self._cr_name(handle)) is not None

    def _patch_suspended(self, handle: ComputeHandle, suspended: bool) -> None:
        name = self._cr_name(handle)
        try:
            self._custom_api().patch_cluster_custom_object(
                _GROUP, _VERSION, _PLURAL, name, {'spec': {'suspended': suspended}})
        except ApiException as e:
            raise RuntimeError(
                'patching OdooInstance %s suspended=%s failed: %s'
                % (name, suspended, e)) from e

    def set_scheduled_backup(self, handle: ComputeHandle, *, enabled: bool,
                             schedule: str = '0 2 * * *', retention: int = 7,
                             bucket: Optional[str] = None,
                             prefix: Optional[str] = None,
                             access_key: Optional[str] = None,
                             secret_key: Optional[str] = None,
                             endpoint: Optional[str] = None) -> None:
        """Enable/configure/disable this instance's scheduled backup by
        patching ``spec.backup`` on its OdooInstance CR — the operator's
        own CronJob-based, cloud-agnostic pg_dump+filestore-tar mechanism
        (compute/operator/internal/resources/backup.go,
        internal/controller/backup.go's ``reconcileBackup``), continuously
        reconciled (NOT immutable-at-creation like ``spec.restore``), so
        this can toggle/reconfigure a live, already-running instance at
        any time. No new Kubernetes-side work needed — this is control-
        plane wiring onto an already-built mechanism.

        When enabling with object-storage credentials, first upserts a
        Secret the generated CronJob reads via
        ``spec.backup.destination.objectStorageSecretRef`` (endpoint/
        access-key/secret-key keys — credentials are never inlined into
        the CR itself, matching how the region's own kubeconfig is never
        pasted into a CR either)."""
        namespace = handle.instance_path or self._namespace_for(handle)
        name = self._cr_name(handle)
        backup_spec = {'enabled': bool(enabled)}
        if enabled:
            backup_spec['schedule'] = schedule
            backup_spec['retention'] = int(retention)
            if bucket:
                core = self._core_api()
                secret_body = k8s_client.V1Secret(
                    metadata=k8s_client.V1ObjectMeta(name=_BACKUP_SECRET_NAME),
                    string_data={
                        'endpoint': endpoint or '',
                        'access-key': access_key or '',
                        'secret-key': secret_key or '',
                    },
                )
                try:
                    core.replace_namespaced_secret(
                        _BACKUP_SECRET_NAME, namespace, secret_body)
                except ApiException as e:
                    if e.status == 404:
                        core.create_namespaced_secret(namespace, secret_body)
                    else:
                        raise RuntimeError(
                            'upserting backup credentials Secret for %s '
                            'failed: %s' % (name, e)) from e
                backup_spec['destination'] = {
                    'type': 'ObjectStorage',
                    'bucket': bucket,
                    'prefix': prefix or '',
                    'objectStorageSecretRef': {'name': _BACKUP_SECRET_NAME},
                }
        try:
            self._custom_api().patch_cluster_custom_object(
                _GROUP, _VERSION, _PLURAL, name, {'spec': {'backup': backup_spec}})
        except ApiException as e:
            raise RuntimeError(
                'patching OdooInstance %s backup config failed: %s'
                % (name, e)) from e

    def trigger_backup_now(self, handle: ComputeHandle,
                           wait_seconds: int = 60,
                           env: Optional[dict] = None) -> str:
        """Create a one-off Job cloned from the operator-managed backup
        CronJob's own template — the Kubernetes-native equivalent of
        ``kubectl create job --from=cronjob/odoo-backup``, for an
        on-demand "back up now" action outside the schedule. Requires
        ``spec.backup.enabled`` (see ``set_scheduled_backup``); the
        operator creates the CronJob asynchronously, so a just-enabled
        backup is waited for up to ``wait_seconds``. Returns the created
        Job's name."""
        namespace = handle.instance_path or self._namespace_for(handle)
        batch = self._batch_api()
        deadline = time.time() + max(0, wait_seconds)
        while True:
            try:
                cron = batch.read_namespaced_cron_job(_BACKUP_CRONJOB_NAME, namespace)
                break
            except ApiException as e:
                if e.status != 404:
                    raise RuntimeError('reading backup CronJob failed: %s' % e) from e
                if time.time() >= deadline:
                    raise RuntimeError(
                        'No scheduled backup is configured for this instance '
                        'yet — enable it first.') from e
                time.sleep(2)
        if cron.spec.suspend is True:
            raise RuntimeError('Backups are paused for this instance; resume it before creating a backup.')
        job_name = '%s-manual-%d' % (_BACKUP_CRONJOB_NAME, int(time.time()))
        spec = cron.spec.job_template.spec
        if env:
            # An on-demand snapshot writes under its own prefix with no
            # retention, so the nightly rotation never prunes it.
            for container in (spec.template.spec.containers or []):
                existing = {e.name: e for e in (container.env or [])}
                for key, value in env.items():
                    if key in existing:
                        existing[key].value = str(value)
                        existing[key].value_from = None
                    else:
                        container.env = (container.env or []) + [
                            k8s_client.V1EnvVar(name=key, value=str(value))]
        job = k8s_client.V1Job(
            metadata=k8s_client.V1ObjectMeta(
                name=job_name,
                labels=(cron.spec.job_template.metadata.labels
                        if cron.spec.job_template.metadata else None),
            ),
            spec=spec,
        )
        try:
            batch.create_namespaced_job(namespace, job)
        except ApiException as e:
            raise RuntimeError(
                'creating manual backup Job failed: %s' % e) from e
        return job_name


    @staticmethod
    def _database_spec(env: dict) -> dict:
        """spec.database from the package keys (resources, volume size);
        empty when none are given, so the CRD defaults apply."""
        db = {}
        if env.get('db_cpu_limit') and env.get('db_mem_limit'):
            db['resources'] = {
                'requests': {'cpu': env.get('db_cpu_request') or env['db_cpu_limit'],
                             'memory': env.get('db_mem_request') or env['db_mem_limit']},
                'limits': {'cpu': env['db_cpu_limit'], 'memory': env['db_mem_limit']},
            }
        if env.get('db_storage_size'):
            db['storage'] = {'size': env['db_storage_size']}
        return db

    def set_package(self, handle: ComputeHandle, res: dict) -> None:
        """Apply a whole package in one patch (instance._k8s_plan_resources
        keys): Odoo requests/limits and workers (pods roll, zero downtime),
        PostgreSQL resources (resized in place) and volume sizes (grown
        online; never shrunk), and the namespace quota."""
        name = self._cr_name(handle)
        spec = {
            'resources': {
                'requests': {'cpu': res['cpu_request'], 'memory': res['mem_request']},
                'limits': {'cpu': res['cpu_limit'], 'memory': res['mem_limit']},
            },
        }
        if res.get('workers') is not None:
            # Merge patch: maxCronThreads is left as-is.
            spec['workers'] = {'count': _clamp_workers(res['workers'])}
        database = self._database_spec(res)
        if database:
            spec['database'] = database
        if res.get('filestore_size'):
            spec['storage'] = {'filestore': {'size': res['filestore_size']}}
        if res.get('quota'):
            spec['tenancy'] = {'resourceQuota': dict(res['quota'])}
        try:
            self._custom_api().patch_cluster_custom_object(
                _GROUP, _VERSION, _PLURAL, name, {'spec': spec})
        except ApiException as e:
            raise RuntimeError(
                'applying the package to OdooInstance %s failed: %s' % (name, e)) from e

    def set_resources(self, handle: ComputeHandle, *, cpu_request: str,
                      cpu_limit: str, mem_request: str, mem_limit: str,
                      workers: Optional[int] = None) -> None:
        """Patch this instance's container requests/limits (and optionally
        its Odoo worker count) in place — what a plan upgrade/downgrade
        changes. The operator rolls the Deployment to apply it.
        Kubernetes-only, like ``scale``."""
        name = self._cr_name(handle)
        patch = {'spec': {'resources': {
            'requests': {'cpu': cpu_request, 'memory': mem_request},
            'limits': {'cpu': cpu_limit, 'memory': mem_limit},
        }}}
        if workers is not None:
            # Merge patch: maxCronThreads is left as-is.
            patch['spec']['workers'] = {'count': _clamp_workers(workers)}
        try:
            self._custom_api().patch_cluster_custom_object(
                _GROUP, _VERSION, _PLURAL, name, patch)
        except ApiException as e:
            raise RuntimeError(
                'patching OdooInstance %s resources failed: %s' % (name, e)) from e

    def start(self, handle: ComputeHandle) -> None:
        self._patch_suspended(handle, False)

    def stop(self, handle: ComputeHandle) -> None:
        self._patch_suspended(handle, True)

    def restart(self, handle: ComputeHandle) -> None:
        """Zero-downtime rolling restart (``kubectl rollout restart``).

        Stamps a pod-template annotation on the tenant's Deployments; the
        operator's RollingUpdate strategy (maxUnavailable=0, maxSurge=1)
        brings the new pod up and ready before the old one stops. The
        operator applies Deployments with server-side apply, which keeps
        fields owned by another manager, so the annotation isn't reverted.
        A suspended instance has no pods to roll — it's simply resumed.
        """
        name = self._cr_name(handle)
        cr = self._get_cr(name)
        if cr is None:
            raise RuntimeError('OdooInstance %s not found' % name)
        if (cr.get('spec') or {}).get('suspended'):
            self._patch_suspended(handle, False)
            return
        namespace = handle.instance_path or self._namespace_for(handle)
        apps = self._apps_api()
        try:
            deployments = apps.list_namespaced_deployment(
                namespace, label_selector=_POD_LABEL_SELECTOR % name).items
        except ApiException as e:
            raise RuntimeError(
                'listing Deployments in %s failed: %s' % (namespace, e)) from e
        if not deployments:
            raise RuntimeError(
                'no Deployment found for OdooInstance %s in %s' % (name, namespace))
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        patch = {'spec': {'template': {'metadata': {'annotations': {
            _RESTARTED_AT_ANNOTATION: stamp}}}}}
        for dep in deployments:
            try:
                apps.patch_namespaced_deployment(dep.metadata.name, namespace, patch)
            except ApiException as e:
                raise RuntimeError(
                    'rolling restart of %s/%s failed: %s'
                    % (namespace, dep.metadata.name, e)) from e

    # -- usage metrics (Prometheus + in-pod storage measurement) --------------
    def _prometheus_get(self, path, params):
        """GET ``/api/v1/<path>`` on the cluster's Prometheus through the
        Kubernetes API service proxy (the cluster kubeconfig is the only
        credential; Prometheus stays cluster-internal). Returns the
        response's ``data`` member. Raises ``PrometheusUnavailable`` when
        the cluster has no Prometheus configured or it can't be reached."""
        cluster = self.server.sudo()
        namespace = (cluster.prometheus_namespace or '').strip()
        service = (cluster.prometheus_service or '').strip()
        if not namespace or not service:
            raise PrometheusUnavailable(
                'no Prometheus configured for cluster %s' % cluster.name)
        try:
            resp = self._client().call_api(
                '/api/v1/namespaces/{namespace}/services/{service}/proxy/api/v1/' + path,
                'GET', path_params={'namespace': namespace, 'service': service},
                query_params=list(params.items()), auth_settings=['BearerToken'],
                _preload_content=False, _request_timeout=_PROMETHEUS_TIMEOUT)
            body = json.loads(resp.data)
        except (ApiException, ValueError) as e:
            raise PrometheusUnavailable('Prometheus query failed: %s' % e) from e
        except Exception as e:  # connection errors/timeouts (urllib3)
            raise PrometheusUnavailable('Prometheus unreachable: %s' % e) from e
        if body.get('status') != 'success':
            raise PrometheusUnavailable(
                'Prometheus error: %s' % (body.get('error') or body))
        return body['data']

    def prometheus_query(self, promql: str) -> list:
        """Instant query; the ``result`` vector."""
        return self._prometheus_get('query', {'query': promql})['result']

    def prometheus_query_range(self, promql: str, start: float, end: float,
                               step: int) -> list:
        """Range query (unix-second bounds); the ``result`` matrix."""
        return self._prometheus_get('query_range', {
            'query': promql, 'start': '%.3f' % start, 'end': '%.3f' % end,
            'step': str(int(step))})['result']

    @staticmethod
    def _odoo_container_selector(namespace_regex: str) -> str:
        # cAdvisor series of the tenant's Odoo containers: web and cron
        # containers — not the one-off init/update/restore Job pods, which share
        # the container name, nor the shell sidecar.
        return ('namespace=~"%s",container=~"%s|cron",pod=~"odoo-.+",'
                'pod!~"odoo-(init|update|restore)-.+"'
                % (namespace_regex, _CONTAINER_NAME))

    @staticmethod
    def _db_container_selector(namespace_regex: str) -> str:
        # The tenant's PostgreSQL: the Managed StatefulSet ("postgresql")
        # or a CloudNativePG instance ("postgres").
        return ('namespace=~"%s",container=~"postgresql|postgres",pod=~"postgresql-.+"'
                % namespace_regex)

    def _usage_promql(self, namespace_regex):
        """PromQL per component, summed per tenant namespace: the package
        is everything the tenant runs (all Odoo pods + its database)."""
        out = {}
        for part, sel in (('odoo', self._odoo_container_selector(namespace_regex)),
                          ('db', self._db_container_selector(namespace_regex))):
            out['%s_cpu_cores' % part] = (
                'sum by (namespace) (rate(container_cpu_usage_seconds_total{%s}[%s]))'
                % (sel, _PROMETHEUS_RATE_WINDOW))
            out['%s_mem_bytes' % part] = (
                'sum by (namespace) (container_memory_working_set_bytes{%s})' % sel)
        return out

    @staticmethod
    def _with_package_totals(usage: dict) -> dict:
        """Add cpu_cores / mem_bytes = Odoo + database."""
        usage['cpu_cores'] = usage.get('odoo_cpu_cores', 0.0) + usage.get('db_cpu_cores', 0.0)
        usage['mem_bytes'] = usage.get('odoo_mem_bytes', 0.0) + usage.get('db_mem_bytes', 0.0)
        return usage

    def usage_by_tenant(self, handle: Optional[ComputeHandle] = None) -> dict:
        """Current CPU (cores) and RAM (working-set bytes) per tenant on this
        cluster — or only ``handle``'s: ``{cr_name: {'cpu_cores',
        'mem_bytes', 'odoo_cpu_cores', 'odoo_mem_bytes', 'db_cpu_cores',
        'db_mem_bytes'}}``, the totals being Odoo + database. Tenants
        without running pods are absent."""
        # Namespace names are [a-z0-9-] only: safe as a literal regex.
        ns_re = (handle.instance_path or self._namespace_for(handle)) if handle \
            else _NAMESPACE_PREFIX + '.+'
        out = {}
        for key, promql in self._usage_promql(ns_re).items():
            for row in self.prometheus_query(promql):
                ns = row['metric'].get('namespace', '')
                if not ns.startswith(_NAMESPACE_PREFIX):
                    continue
                cr_name = ns[len(_NAMESPACE_PREFIX):]
                out.setdefault(cr_name, {})[key] = float(row['value'][1])
        return {k: self._with_package_totals(v) for k, v in out.items()}

    def usage_history(self, handle: ComputeHandle, start: float, end: float,
                      step: int) -> dict:
        """Time series for one tenant, ``[(ts, value)]`` per key: the
        per-component ``odoo_cpu_cores``/``odoo_mem_bytes``/``db_cpu_cores``/
        ``db_mem_bytes``, their totals ``cpu_cores``/``mem_bytes`` (Odoo +
        database), and ``volume_bytes`` (the tenant's PVCs, from kubelet
        volume stats; empty when the storage driver doesn't report them)."""
        namespace = handle.instance_path or self._namespace_for(handle)
        queries = dict(self._usage_promql(namespace))
        queries['volume_bytes'] = 'sum(kubelet_volume_stats_used_bytes{namespace="%s"})' % namespace
        out = {}
        for key, promql in queries.items():
            series = self.prometheus_query_range(promql, start, end, step)
            out[key] = [(float(t), float(v)) for t, v in
                        (series[0]['values'] if series else [])]
        for total, parts in (('cpu_cores', ('odoo_cpu_cores', 'db_cpu_cores')),
                             ('mem_bytes', ('odoo_mem_bytes', 'db_mem_bytes'))):
            summed = {}
            for part in parts:
                for ts, v in out[part]:
                    summed[ts] = summed.get(ts, 0.0) + v
            out[total] = sorted(summed.items())
        return out
    def measure_storage(self, handle: ComputeHandle) -> dict:
        """Filestore and database size, measured inside the web pod:
        ``{'filestore_bytes': int, 'db_bytes': int}``. The database figure
        sums every database the tenant's role owns (hosting instances can
        have several). Raises ``RuntimeError`` when it can't be measured."""
        res = self.exec(handle, _MEASURE_STORAGE_CMD, timeout=120)
        values = {}
        for line in (res.stdout or '').splitlines():
            key, _sep, val = line.partition('=')
            if key in ('filestore_bytes', 'db_bytes') and val.strip().isdigit():
                values[key] = int(val.strip())
        if res.rc != 0 or len(values) != 2:
            raise RuntimeError(
                'storage measurement failed for %s (rc=%s): %s' % (
                    self._cr_name(handle), res.rc,
                    (res.stderr or res.stdout or '').strip()[-500:]))
        return values

    def _resolve_pod(self, handle: ComputeHandle):
        """Return (namespace, pod) for handle's workload, or (namespace,
        None) if no pod exists yet."""
        namespace = handle.instance_path or self._namespace_for(handle)
        cr_name = self._cr_name(handle)
        return namespace, self._first_pod(namespace, cr_name)

    @staticmethod
    def _shell_command(command: str, env: Optional[dict] = None) -> list:
        """Build the ``['/bin/sh', '-c', ...]`` argv the exec subresource
        expects, with ``env`` exported as a POSIX prefix assignment
        (``K=V K2=V2 command``) — the pods/exec subresource has no
        per-call env parameter the way `docker exec -e` does, so this is
        the portable equivalent. Each value is shell-quoted; values never
        appear in the command line unquoted (avoids the injection class
        `docker compose exec -e`'s own callers were already careful
        about — see the deleted ``SshDockerDriver.service_exec``)."""
        if not env:
            return ['/bin/sh', '-c', command]
        prefix = ' '.join(
            '%s=%s' % (k, shlex.quote(str(v))) for k, v in env.items())
        return ['/bin/sh', '-c', '%s %s' % (prefix, command)]

    # -- introspection / interaction ------------------------------------------
    def exec(self, handle: ComputeHandle, command: str,
             *, user: Optional[str] = None, env: Optional[dict] = None,
             timeout: Optional[int] = None) -> ExecResult:
        if user:
            # Kubernetes' pods/exec subresource has no per-call user
            # override the way `docker exec -u` does — surfacing this
            # rather than silently running as the container's default
            # user without telling the caller.
            _logger.warning(
                "KubernetesDriver.exec: user=%r requested but not supported "
                "against a real Kubernetes pod — running as the container's "
                "own default user instead.", user)
        namespace, pod = self._resolve_pod(handle)
        if pod is None:
            return ExecResult(rc=127, stdout='', stderr='no pod found for %s' % self._cr_name(handle))
        try:
            resp = k8s_stream(
                self._core_api().connect_get_namespaced_pod_exec,
                pod.metadata.name, namespace, container=_CONTAINER_NAME,
                command=self._shell_command(command, env),
                stderr=True, stdin=False, stdout=True, tty=False,
                _preload_content=False)
            resp.run_forever(timeout=timeout or 60)
            stdout = resp.read_stdout() or ''
            stderr = resp.read_stderr() or ''
            resp.close()
            # The exec subresource reports the exit code out-of-band on the
            # WebSocket's own error channel, not as a plain process return
            # code — `returncode` is the client library's own decode of
            # that channel. None means run_forever's timeout elapsed before
            # the server ever closed the connection (e.g. a long-running
            # command cut short); treat that as success rather than
            # guessing failure, matching kubectl's own exec client.
            rc = resp.returncode
            return ExecResult(rc=0 if rc is None else rc, stdout=stdout, stderr=stderr)
        except ApiException as e:
            return ExecResult(rc=1, stdout='', stderr=str(e))

    def exec_stream_out(self, handle: ComputeHandle, command: str, *,
                        env: Optional[dict] = None,
                        timeout: Optional[int] = None) -> 'K8sExecStdoutReader':
        """Run ``command`` (e.g. ``pg_dump``) and return a file-like
        object (``.read(n)``) that progressively yields its stdout as the
        process produces it — bounded memory end to end, the Kubernetes
        equivalent of piping an SSH channel's stdout straight into an
        upload SDK's ``upload_fileobj``/resumable-upload call (see
        ``saas_instance_backup.py``'s ``_upload_stream_to_bucket``, which
        this is designed to feed directly, unchanged). Not part of the
        shared ``ComputeDriver`` ABC — same reasoning as
        ``exec_interactive()``: a genuinely new capability the ssh_docker
        backend never had a counterpart for.

        Non-goal: bidirectional/stdin streaming (e.g. piping data INTO a
        `pg_restore` process) — every current restore path instead runs
        `curl` *inside* the pod to pull from a presigned URL (matching
        how the deleted SSH-based restore worked: `curl` on the target,
        not a Python-side push), so a stdin-streaming primitive has no
        caller yet. Add one only when an actual caller needs it."""
        namespace, pod = self._resolve_pod(handle)
        if pod is None:
            raise RuntimeError('no pod found for %s' % self._cr_name(handle))
        ws = k8s_stream(
            self._core_api().connect_get_namespaced_pod_exec,
            pod.metadata.name, namespace, container=_CONTAINER_NAME,
            command=self._shell_command(command, env),
            stderr=True, stdin=False, stdout=True, tty=False,
            binary=True, _preload_content=False)
        return K8sExecStdoutReader(ws, timeout=timeout or 3600)

    def exec_interactive(self, handle: ComputeHandle, *,
                         command: Optional[list] = None,
                         cols: int = 120, rows: int = 32,
                         container: str = _CONTAINER_NAME) -> 'K8sExecChannel':
        """Open a long-lived, interactive TTY exec session against the
        workload's pod — the Kubernetes-native replacement for SSHing into
        a Docker host and running ``docker exec -it``. Returns a
        :class:`K8sExecChannel` wrapping the raw WebSocket stream with the
        same shape a paramiko ``Channel`` exposes (``recv_ready``/``recv``/
        ``sendall``/``closed``/``get_transport``/``resize_pty``/``close``/
        ``fileno``), so callers built around paramiko's interface (see
        ``saas_core/controllers/ssh_terminal.py``) don't need their own
        pump/relay logic rewritten for this backend.

        Not part of the shared ``ComputeDriver`` ABC (like ``scale()`` —
        this is Kubernetes-specific: there is no equivalent "attach an
        interactive shell" primitive on the abstract interface, since the
        original ssh_docker backend had no counterpart other than the
        deleted raw-SSH terminal)."""
        namespace, pod = self._resolve_pod(handle)
        if pod is None:
            raise RuntimeError('no pod found for %s' % self._cr_name(handle))
        ws = k8s_stream(
            self._core_api().connect_get_namespaced_pod_exec,
            pod.metadata.name, namespace, container=container,
            command=command or ['/bin/bash', '-l'],
            stderr=True, stdin=True, stdout=True, tty=True,
            binary=True, _preload_content=False)
        channel = K8sExecChannel(ws)
        channel.resize_pty(cols, rows)
        return channel

    # -- staff cluster terminal -----------------------------------------------
    def open_cluster_terminal(self, cols: int = 120, rows: int = 32) -> 'K8sExecChannel':
        """Interactive bash in this cluster's toolbox pod, with whatever
        the saas-toolbox ServiceAccount was granted. Not tenant-scoped:
        callers must restrict it to the Cluster Shell group."""
        pod = self._ensure_toolbox_pod()
        ws = k8s_stream(
            self._core_api().connect_get_namespaced_pod_exec,
            pod, _TOOLBOX_NAMESPACE, container='toolbox',
            command=['/bin/bash', '-l'],
            stderr=True, stdin=True, stdout=True, tty=True,
            binary=True, _preload_content=False)
        channel = K8sExecChannel(ws)
        channel.resize_pty(cols, rows)
        return channel

    def _ensure_toolbox_pod(self) -> str:
        """Start the toolbox pod when missing or finished; return its name
        once Running. Requires the hand-made namespace and ServiceAccount."""
        core = self._core_api()
        try:
            core.read_namespaced_service_account(_TOOLBOX_NAME, _TOOLBOX_NAMESPACE)
        except ApiException as e:
            if e.status == 404:
                raise ToolboxNotSetUp(
                    "Cluster '%s' has no %s/%s ServiceAccount yet. Create it once "
                    "(setup/03a-MICROK8S-CLUSTER-SETUP.md step 12.4, or "
                    "setup/03b-DOKS-CLUSTER-SETUP.md step 7.2)."
                    % (self.server.name, _TOOLBOX_NAMESPACE, _TOOLBOX_NAME)) from e
            raise
        deadline = time.time() + _TOOLBOX_START_TIMEOUT
        while True:
            try:
                pod = core.read_namespaced_pod(_TOOLBOX_NAME, _TOOLBOX_NAMESPACE)
            except ApiException as e:
                if e.status != 404:
                    raise
                pod = None
            phase = pod.status.phase if pod is not None and pod.status else None
            if phase == 'Running':
                return _TOOLBOX_NAME
            if pod is not None and phase in ('Succeeded', 'Failed'):
                # Past its lifetime, or crashed: replace it.
                core.delete_namespaced_pod(_TOOLBOX_NAME, _TOOLBOX_NAMESPACE, grace_period_seconds=0)
            elif pod is None:
                self._sync_toolbox_pull_secret(core)
                core.create_namespaced_pod(_TOOLBOX_NAMESPACE, self._toolbox_pod_body())
            if time.time() > deadline:
                raise RuntimeError(
                    'the cluster toolbox pod did not start within %ss (phase: %s)'
                    % (_TOOLBOX_START_TIMEOUT, phase or 'not created'))
            time.sleep(2)

    def _sync_toolbox_pull_secret(self, core):
        """The toolbox image's pull Secret, from the cluster's registry
        credentials (none = public toolbox image)."""
        auth = self._registry_pull_auth()
        if not auth:
            return
        body = k8s_client.V1Secret(
            metadata=k8s_client.V1ObjectMeta(name=_TOOLBOX_PULL_SECRET, labels={
                'app.kubernetes.io/managed-by': 'saas-control-plane'}),
            type='kubernetes.io/dockerconfigjson',
            string_data={'.dockerconfigjson': docker_config_json(*auth)})
        try:
            core.replace_namespaced_secret(_TOOLBOX_PULL_SECRET, _TOOLBOX_NAMESPACE, body)
        except ApiException as e:
            if e.status != 404:
                raise
            core.create_namespaced_secret(_TOOLBOX_NAMESPACE, body)

    def _toolbox_pod_body(self) -> dict:
        body = {
            'metadata': {'name': _TOOLBOX_NAME, 'labels': {
                'app.kubernetes.io/name': _TOOLBOX_NAME,
                'app.kubernetes.io/managed-by': 'saas-control-plane'}},
            'spec': {
                'serviceAccountName': _TOOLBOX_NAME,
                'restartPolicy': 'Never',
                'activeDeadlineSeconds': _TOOLBOX_LIFETIME,
                'terminationGracePeriodSeconds': 5,
                'containers': [{
                    'name': 'toolbox',
                    'image': self.server._toolbox_image(),
                    'command': ['sh', '-c',
                                "trap 'exit 0' TERM; while :; do sleep 3600 & wait $!; done"],
                    'env': [{'name': 'PS1', 'value': '[%s] \\w \\$ ' % self.server.name}],
                    'resources': {
                        'requests': {'cpu': '10m', 'memory': '64Mi'},
                        'limits': {'cpu': '1', 'memory': '512Mi'},
                    },
                }],
            },
        }
        if self._registry_pull_auth():
            body['spec']['imagePullSecrets'] = [{'name': _TOOLBOX_PULL_SECRET}]
        return body

    def logs(self, handle: ComputeHandle, *, tail: Optional[int] = None) -> str:
        namespace = handle.instance_path or self._namespace_for(handle)
        pod = self._first_pod(namespace, self._cr_name(handle))
        if pod is None:
            return ''
        try:
            return read_pod_log(self._core_api(), pod.metadata.name, namespace,
                                _CONTAINER_NAME, tail)
        except ApiException as e:
            if e.status == 404:
                return ''
            raise RuntimeError('reading logs for %s failed: %s' % (pod.metadata.name, e)) from e

    def open_log_stream(self, handle: ComputeHandle, *, tail: int = 100):
        """Follow the web pod's Odoo log (``kubectl logs -f --tail``).
        Returns the raw streaming HTTP response — iterate
        ``resp.stream(n)`` for byte chunks, ``resp.release_conn()`` when
        done — or None when the tenant has no pod. Opening the stream
        needs the ORM (kubeconfig); reading it doesn't, so a controller
        can open it inside the request and read it from a generator."""
        namespace = handle.instance_path or self._namespace_for(handle)
        pod = self._first_pod(namespace, self._cr_name(handle))
        if pod is None:
            return None
        try:
            return self._core_api().read_namespaced_pod_log(
                pod.metadata.name, namespace, container=_CONTAINER_NAME,
                follow=True, tail_lines=int(tail), _preload_content=False)
        except ApiException as e:
            if e.status == 404:
                return None
            raise RuntimeError(
                'opening the log stream for %s failed: %s'
                % (pod.metadata.name, e)) from e

    def health(self, handle: ComputeHandle) -> HealthStatus:
        name = self._cr_name(handle)
        cr = self._get_cr(name)
        if cr is None:
            return HealthStatus(running=False, status='not_found', restart_count=0,
                                detail='OdooInstance not found')
        phase = ((cr.get('status') or {}).get('phase')) or 'Pending'
        namespace = handle.instance_path or self._namespace_for(handle)
        pod = self._first_pod(namespace, name)
        suspended = bool((cr.get('spec') or {}).get('suspended')) or phase == 'Suspended'
        if pod is None and not suspended:
            # Still provisioning (create() ran before the namespace existed).
            self._sync_tenant_pull_secret(namespace)
        if pod is None:
            # The CR exists, so the operator owns recreating the pod (e.g.
            # after an eviction). 'missing' is not 'not_found': reconcile must
            # not call start() for a pod that is merely being rescheduled.
            return HealthStatus(running=False, status='exited' if suspended else 'missing',
                                detail='Suspended' if suspended else 'Tenant web pod is missing')
        if pod.metadata.deletion_timestamp:
            # Only a terminating pod is left: it is shutting down, not starting,
            # so a pending stop is not re-issued while it drains.
            return HealthStatus(running=False, status='terminating', detail='Terminating')
        status = 'running' if self._pod_ready(pod) else 'starting'
        if pod.status.phase == 'Failed':
            status = 'dead'
        elif pod.status.phase == 'Succeeded':
            status = 'exited'
        restart_count = 0
        detail = phase
        if any(cs.state and cs.state.waiting and cs.state.waiting.reason in _IMAGE_PULL_ERRORS
               for cs in (pod.status.container_statuses or [])):
            # Missing or outdated (rotated credentials) pull Secret: rewrite
            # it; the kubelet's next pull retry picks it up.
            self._sync_tenant_pull_secret(namespace, force=True)
        for cs in (pod.status.container_statuses or []):
            if cs.name not in (_CONTAINER_NAME, 'cron'):
                continue
            restart_count += cs.restart_count or 0
            waiting = cs.state.waiting if cs.state else None
            if waiting and waiting.reason:
                detail = waiting.reason
                if waiting.reason == 'CrashLoopBackOff':
                    status = 'restarting'
        return HealthStatus(running=(status == 'running'), status=status,
                            restart_count=restart_count, detail=detail)
