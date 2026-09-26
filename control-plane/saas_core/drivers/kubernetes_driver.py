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
  scale            -> patch spec.replicas (Kubernetes-only, not part of
                       the shared ComputeDriver interface — see scale())

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
from .k8s_builds import ImageBuildMixin, read_pod_log

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

# OdooInstance.status.phase (OdooInstancePhase enum) -> the driver-agnostic
# status vocabulary saas_instance.py's crash-loop/reconcile logic already
# depends on ('running'/'restarting'/'exited'/'dead'/'not_found' — see
# _cron_reconcile's use of HealthStatus.status). Approximate by design: a
# CR-level phase is a coarser signal than a container's own runtime state,
# refined below by also checking the pod's own CrashLoopBackOff reason.
_PHASE_TO_STATUS = {
    'Ready': 'running',
    'Updating': 'running',
    'Provisioning': 'restarting',
    'Pending': 'restarting',
    'Degraded': 'restarting',
    'Suspended': 'exited',
    'Deleting': 'exited',
    'Failed': 'dead',
}


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

    # -- API client / naming helpers -----------------------------------------
    def _client(self):
        if self._api_client is not None:
            return self._api_client
        region = self.server.region_id
        kc = region.kubeconfig_id if region else False
        kubeconfig = (kc._kubeconfig_yaml() or '').strip() if kc else ''
        if not kubeconfig:
            raise RuntimeError(
                "Server '%s' has compute_driver=kubernetes but its region "
                "(%s) has no kubeconfig configured." % (
                    self.server.name, region.name if region else 'none'))
        try:
            config_dict = yaml.safe_load(kubeconfig)
        except yaml.YAMLError as e:
            raise RuntimeError(
                "Server '%s': region kubeconfig is not valid YAML: %s"
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
                _GROUP, _VERSION, _PLURAL, name)
        except ApiException as e:
            if e.status == 404:
                return None
            raise RuntimeError(
                'Kubernetes API error reading OdooInstance %s: %s' % (name, e)) from e

    def _first_pod(self, namespace, cr_name):
        """The tenant's own web pod, or None if the namespace/pod doesn't
        exist yet (e.g. the CR was just created and the operator hasn't
        provisioned anything in it yet — not an error condition)."""
        try:
            pods = self._core_api().list_namespaced_pod(
                namespace, label_selector=_WEB_POD_LABEL_SELECTOR % cr_name)
        except ApiException as e:
            if e.status == 404:
                return None
            raise RuntimeError(
                'listing pods in %s failed: %s' % (namespace, e)) from e
        running = [p for p in pods.items if p.status.phase == 'Running']
        candidates = running or list(pods.items)
        return candidates[0] if candidates else None

    # -- lifecycle ------------------------------------------------------------
    def create(self, spec: ComputeSpec) -> ComputeHandle:
        """Create the OdooInstance CR. Idempotent against a retried call
        for the SAME instance (see the 409 branch below) — this matters
        because the durable job queue can and does retry ``create()``:
        Odoo's own cron-thread watchdog can kill/reload the whole server
        mid-poll on a slow deploy (image pull + CNPG/managed-Postgres
        bring-up + init Job legitimately take minutes, easily exceeding
        the default ``limit_time_real_cron`` ~120s — see
        `TEST-CLUSTER-SETUP.md` §10 for the config fix), orphaning the
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
        IS set explicitly (default 1) because a value > 1 has a required
        companion (the filestore's accessMode, below) this driver must set
        consistently, not something safe to leave to the CRD's own default.
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

        tls_enabled = bool(spec.env.get('tls_enabled', False))
        tls = {'enabled': tls_enabled}
        if tls_enabled and spec.env.get('tls_issuer_name'):
            tls['issuerRef'] = {
                'name': spec.env['tls_issuer_name'],
                'kind': spec.env.get('tls_issuer_kind') or 'ClusterIssuer',
            }

        filestore = {'size': spec.env.get('filestore_size') or '5Gi'}
        if replicas > 1:
            # Required companion of replicas > 1 — see FilestoreSpec's own
            # comment (odoo_types.go): RWO is the safe default and the
            # controller rejects RWO with replicas > 1 (Degraded condition)
            # rather than corrupting data. Requesting RWX here only ever
            # succeeds if the region's cluster actually has an RWX-capable
            # StorageClass (e.g. NFS/CephFS/EFS) — this driver cannot verify
            # that in advance; a cluster without one will surface as a real,
            # visible Degraded condition on the CR, which health() picks up.
            filestore['accessMode'] = 'ReadWriteMany'

        body = {
            'apiVersion': '%s/%s' % (_GROUP, _VERSION),
            'kind': 'OdooInstance',
            'metadata': {'name': name},
            'spec': {
                'version': odoo_version,
                'image': {'repository': repository, 'tag': tag},
                'domain': {
                    'hostname': domain,
                    'tls': tls,
                },
                'storage': {'filestore': filestore},
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

    def trigger_backup_now(self, handle: ComputeHandle) -> str:
        """Create a one-off Job cloned from the operator-managed backup
        CronJob's own template — the Kubernetes-native equivalent of
        ``kubectl create job --from=cronjob/odoo-backup``, for an
        on-demand "back up now" action outside the schedule. Requires
        ``spec.backup.enabled`` already (the CronJob only exists once
        the operator has reconciled it — see ``set_scheduled_backup``).
        Returns the created Job's name."""
        namespace = handle.instance_path or self._namespace_for(handle)
        batch = self._batch_api()
        try:
            cron = batch.read_namespaced_cron_job(_BACKUP_CRONJOB_NAME, namespace)
        except ApiException as e:
            if e.status == 404:
                raise RuntimeError(
                    'No scheduled backup is configured for this instance '
                    'yet — enable it first.') from e
            raise RuntimeError('reading backup CronJob failed: %s' % e) from e
        job_name = '%s-manual-%d' % (_BACKUP_CRONJOB_NAME, int(time.time()))
        job = k8s_client.V1Job(
            metadata=k8s_client.V1ObjectMeta(
                name=job_name,
                labels=(cron.spec.job_template.metadata.labels
                        if cron.spec.job_template.metadata else None),
            ),
            spec=cron.spec.job_template.spec,
        )
        try:
            batch.create_namespaced_job(namespace, job)
        except ApiException as e:
            raise RuntimeError(
                'creating manual backup Job failed: %s' % e) from e
        return job_name

    def scale(self, handle: ComputeHandle, replicas: int) -> None:
        """Patch this instance's pod replica count in place — the
        underlying primitive behind the compute-tier feature
        (saas.instance.compute_tier_id / saas.compute.tier, e.g.
        Standard=1 / HA=2 / Scale=4 replicas). Kubernetes-only; there is
        no equivalent concept for SshDockerDriver, so this is NOT part of
        the shared ComputeDriver interface — callers must check
        ``compute_driver == 'kubernetes'`` first.

        Going from 1 to 2+ only actually works if the instance's filestore
        was already provisioned with an RWX-capable StorageClass (see
        _build_odoo_instance) — this call cannot verify or change that
        after the fact (a PVC's access mode is not something the API lets
        you patch in place). A cluster without one will make the CR go
        Degraded, which the caller's post-scale health() poll surfaces as
        a real, visible failure rather than a silent no-op.
        """
        name = self._cr_name(handle)
        try:
            self._custom_api().patch_cluster_custom_object(
                _GROUP, _VERSION, _PLURAL, name, {'spec': {'replicas': int(replicas)}})
        except ApiException as e:
            raise RuntimeError(
                'patching OdooInstance %s replicas=%s failed: %s'
                % (name, replicas, e)) from e

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
                         cols: int = 120, rows: int = 32) -> 'K8sExecChannel':
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
            pod.metadata.name, namespace, container=_CONTAINER_NAME,
            command=command or ['/bin/bash', '-l'],
            stderr=True, stdin=True, stdout=True, tty=True,
            binary=True, _preload_content=False)
        channel = K8sExecChannel(ws)
        channel.resize_pty(cols, rows)
        return channel

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

    def endpoint(self, handle: ComputeHandle) -> tuple[str, int]:
        """Return where an external reverse proxy should connect to reach
        this tenant — NOT the tenant's own public domain (``status.url``,
        which a caller can't route to: that hostname's DNS still points at
        whatever fronted the tenant before, so "connecting" to it would
        loop back rather than reach the cluster). This is the cluster's own
        ingress front door, a manually-configured, per-region value (see
        ``saas.region.ingress_host`` — which controller/Service backs it
        isn't safely auto-discoverable from here; the operator supports
        both plain Ingress and Gateway API, chosen at the operator level).

        The caller is expected to route with the tenant's own domain
        (``status.url``'s hostname) sent as the Host header — that's what
        the cluster's ingress rule for this CR actually matches on.
        """
        region = self.server.region_id
        host = (region.ingress_host or '').strip() if region else ''
        if not host:
            return ('', 0)
        return (host, region.ingress_port or 80)

    def health(self, handle: ComputeHandle) -> HealthStatus:
        name = self._cr_name(handle)
        cr = self._get_cr(name)
        if cr is None:
            return HealthStatus(running=False, status='not_found', restart_count=0,
                                detail='OdooInstance not found')
        phase = ((cr.get('status') or {}).get('phase')) or 'Pending'
        status = _PHASE_TO_STATUS.get(phase, 'not_found')

        namespace = handle.instance_path or self._namespace_for(handle)
        pod = self._first_pod(namespace, name)
        restart_count = 0
        if pod is not None:
            for cs in (pod.status.container_statuses or []):
                if cs.name != _CONTAINER_NAME:
                    continue
                restart_count = cs.restart_count or 0
                waiting = cs.state.waiting if cs.state else None
                if waiting and waiting.reason == 'CrashLoopBackOff':
                    # A real, more precise crash-loop signal than the CR's
                    # own coarser phase — a pod can be CrashLoopBackOff
                    # while the CR still reports "Provisioning".
                    status = 'restarting'
                break
        return HealthStatus(running=(status == 'running'), status=status,
                            restart_count=restart_count, detail=phase)
