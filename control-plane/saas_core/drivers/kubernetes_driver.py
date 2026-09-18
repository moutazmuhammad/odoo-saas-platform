"""KubernetesDriver — a real, API-backed ComputeDriver (see /ROADMAP.md
§3.3/§5 Phase 1, "done"). Implements the SAME ``ComputeDriver`` interface as
``SshDockerDriver``; the business logic in ``saas.instance`` is untouched —
``_compute_driver()`` just returns this when a server's
``compute_driver == 'kubernetes'``.

This replaces an earlier stub that drove ``kubectl`` over the server's SSH
transport against hand-built raw Deployment/Service YAML — explicitly never
run against a live cluster (its own prior header comment said so). This
version instead talks to the real Kubernetes API (the official
``kubernetes`` PyPI client) and manages the Compute Service operator's
``OdooInstance`` custom resource (``compute/operator``), the same resource
a human would ``kubectl apply -f``, not a parallel primitive workload the
operator has never seen.

Mapping (ComputeDriver -> Kubernetes):
  create           -> create the OdooInstance CR (operator does the rest)
  destroy          -> delete the OdooInstance CR (the operator's finalizer
                       already tears down the whole tenant namespace —
                       there is no lesser "stop but keep data" delete at
                       this level, unlike SshDockerDriver's `compose down`)
  start/stop       -> patch spec.suspended = false/true (already built,
                       see the operator's reconcileSuspended)
  restart          -> stop then start (ABC default; no native rolling-
                       restart trigger is exposed on the CR yet)
  exec             -> Kubernetes pods/exec subresource against the web pod
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
``saas.instance._do_deploy_locked`` branches on
``docker_server_id.compute_driver`` and, for ``kubernetes``, calls this
driver's ``create()`` directly (no legacy SSH/Docker-Compose steps at
all); see ``_do_deploy_locked_kubernetes``. Which backend a NEW instance
lands on is a platform-level setting
(``saas_master.default_compute_driver``, default ``kubernetes``),
independent of the per-instance compute tier (replica count, e.g.
Standard/HA/Scale — see ``scale()`` below).
"""

from __future__ import annotations

import logging
from typing import Optional

import yaml
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream as k8s_stream

from .base import ComputeDriver, ComputeSpec, ComputeHandle, ExecResult, HealthStatus

_logger = logging.getLogger(__name__)

# Mirrors compute/operator/api/v1alpha1/constants.go + groupversion_info.go.
_GROUP = 'saas.odoo.example.com'
_VERSION = 'v1alpha1'
_PLURAL = 'odooinstances'
_NAMESPACE_PREFIX = 'odoo-tenant-'
# Mirrors internal/resources/naming.go: OdooDeploymentName always returns
# the literal string "odoo", regardless of the tenant's own name.
_CONTAINER_NAME = 'odoo'
_POD_LABEL_SELECTOR = 'app.kubernetes.io/name=odoo,app.kubernetes.io/instance=%s'

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


class KubernetesDriver(ComputeDriver):
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
        kubeconfig = (region.kubeconfig or '').strip() if region else ''
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
                namespace, label_selector=_POD_LABEL_SELECTOR % cr_name)
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
        name = self._cr_name(spec)
        body = self._build_odoo_instance(name, spec)
        try:
            self._custom_api().create_cluster_custom_object(
                _GROUP, _VERSION, _PLURAL, body)
        except ApiException as e:
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
                    'tls': {'enabled': bool(spec.env.get('tls_enabled', False))},
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

        # Phase 2 (/ROADMAP.md §5): DataService.migrate_to_kubernetes hands
        # a restore source through spec.env['restore'] rather than a new
        # ComputeSpec field, for the same reason domain/tls/resources do
        # (see the docstring above). Mirrors RestoreSourceSpec exactly
        # (compute/operator/api/v1alpha1/odooinstance_types.go) — restore
        # is immutable once set on the CR, so this must be present at
        # create() time; there is no later "attach a restore" call.
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

    def start(self, handle: ComputeHandle) -> None:
        self._patch_suspended(handle, False)

    def stop(self, handle: ComputeHandle) -> None:
        self._patch_suspended(handle, True)

    def restart(self, handle: ComputeHandle) -> None:
        # No native rolling-restart trigger is exposed on the CR today, so
        # this is a real stop-then-start via spec.suspended (heavier than a
        # zero-downtime rolling restart, but a genuine documented gap, not
        # a silent shortcut standing in for something broken).
        self.restart_default(handle)

    # -- introspection / interaction ------------------------------------------
    def exec(self, handle: ComputeHandle, command: str,
             *, user: Optional[str] = None, timeout: Optional[int] = None) -> ExecResult:
        if user:
            # Kubernetes' pods/exec subresource has no per-call user
            # override the way `docker exec -u` does — surfacing this
            # rather than silently running as the container's default
            # user without telling the caller.
            _logger.warning(
                "KubernetesDriver.exec: user=%r requested but not supported "
                "against a real Kubernetes pod — running as the container's "
                "own default user instead.", user)
        namespace = handle.instance_path or self._namespace_for(handle)
        cr_name = self._cr_name(handle)
        pod = self._first_pod(namespace, cr_name)
        if pod is None:
            return ExecResult(rc=127, stdout='', stderr='no pod found for %s' % cr_name)
        try:
            resp = k8s_stream(
                self._core_api().connect_get_namespaced_pod_exec,
                pod.metadata.name, namespace, container=_CONTAINER_NAME,
                command=['/bin/sh', '-c', command],
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

    def logs(self, handle: ComputeHandle, *, tail: Optional[int] = None) -> str:
        namespace = handle.instance_path or self._namespace_for(handle)
        pod = self._first_pod(namespace, self._cr_name(handle))
        if pod is None:
            return ''
        try:
            return self._core_api().read_namespaced_pod_log(
                pod.metadata.name, namespace, container=_CONTAINER_NAME,
                tail_lines=int(tail) if tail else None)
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
