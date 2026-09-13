"""KubernetesDriver — a SECOND ComputeDriver backend, proving the Phase-1 seam.

The whole architecture bet is: "scale to a new compute backend without a
Control-Plane rewrite." This file is the proof. It implements the SAME
``ComputeDriver`` interface as ``SshDockerDriver``; the business logic in
``saas.instance`` is untouched — ``_compute_driver()`` just returns this when a
server's ``compute_driver == 'kubernetes'``.

Mapping (Docker → Kubernetes):
  compose project  → a Deployment + Service in a per-tenant Namespace
  up -d            → kubectl apply / scale replicas=1
  stop             → scale replicas=0   (keeps the spec; no data loss)
  down             → delete deployment+service   (purge → delete the namespace)
  restart          → kubectl rollout restart
  exec             → kubectl exec
  logs             → kubectl logs
  health/inspect   → kubectl get pod -o jsonpath (phase + restartCount)

v1 drives ``kubectl`` over the server's existing SSH transport (the server
record models the cluster's access point), mirroring how SshDockerDriver drives
``docker`` — so the structure, connection-reuse, and tests are identical. A
later revision can swap the transport for the K8s API/client-go without touching
this interface. NOT YET run against a live cluster (no demand) — the command/
manifest building is unit-tested; running it is a deploy-time concern.
"""

from __future__ import annotations

import logging
import shlex
from contextlib import nullcontext

from .base import (ComputeDriver, ComputeSpec, ComputeHandle, ExecResult, HealthStatus)

_logger = logging.getLogger(__name__)


class KubernetesDriver(ComputeDriver):
    """ComputeDriver backed by Kubernetes (kubectl over the server's SSH).

    Same constructor shape as SshDockerDriver: a ``saas.server`` record (the
    cluster access point) and an optional open connection to reuse."""

    def __init__(self, server, connection=None):
        self.server = server
        self._conn = connection

    # -- helpers ------------------------------------------------------------
    def _ssh(self):
        if self._conn is not None:
            return nullcontext(self._conn)
        return self.server._get_ssh_connection()

    @staticmethod
    def _namespace(handle: ComputeHandle) -> str:
        # One namespace per tenant: container_name is already 'odoo_<sub>'.
        return ('saas-%s' % handle.container_name).replace('_', '-').lower()

    @staticmethod
    def _name(handle: ComputeHandle) -> str:
        return handle.container_name.replace('_', '-').lower()

    def _kubectl(self, handle, args, *, timeout=60):
        """Run `kubectl -n <ns> <args>` over the server connection."""
        cmd = 'kubectl -n %s %s' % (shlex.quote(self._namespace(handle)), args)
        with self._ssh() as ssh:
            rc, out, err = ssh.execute(cmd, timeout=timeout)
        return rc, out, err

    # -- lifecycle ----------------------------------------------------------
    def create(self, spec: ComputeSpec) -> ComputeHandle:
        """Apply a Namespace + Deployment + Service rendered from ``spec``."""
        ns = ('saas-%s' % spec.container_name).replace('_', '-').lower()
        name = spec.container_name.replace('_', '-').lower()
        manifest = self.render_manifest(spec)
        with self._ssh() as ssh:
            ssh.execute('kubectl create namespace %s 2>/dev/null || true' % shlex.quote(ns))
            # apply via stdin heredoc
            rc, out, err = ssh.execute(
                'kubectl -n %s apply -f - <<\'EOF\'\n%s\nEOF' % (shlex.quote(ns), manifest),
                timeout=300)
        if rc != 0:
            raise RuntimeError('kubectl apply failed (rc=%s): %s' % (rc, (out + err)[-500:]))
        return ComputeHandle(
            server_id=self.server.id, container_name=spec.container_name,
            instance_path=ns, host=spec.db_host, http_port=spec.http_port)

    def render_manifest(self, spec: ComputeSpec) -> str:
        """Build the Deployment+Service YAML for a tenant (one app pod). Kept
        small + deterministic so it's unit-testable without a cluster."""
        name = spec.container_name.replace('_', '-').lower()
        return (
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n  name: %(name)s\n  labels:\n    tenant_id: %(name)s\n"
            "spec:\n  replicas: 1\n  selector:\n    matchLabels:\n      app: %(name)s\n"
            "  template:\n    metadata:\n      labels:\n        app: %(name)s\n"
            "        tenant_id: %(name)s\n"
            "    spec:\n      containers:\n      - name: odoo\n        image: %(image)s\n"
            "        ports:\n        - containerPort: 8069\n"
            "---\n"
            "apiVersion: v1\nkind: Service\nmetadata:\n  name: %(name)s\n"
            "spec:\n  selector:\n    app: %(name)s\n  ports:\n  - port: 8069\n"
            "    targetPort: 8069\n"
        ) % {'name': name, 'image': spec.image}

    def start(self, handle: ComputeHandle) -> None:
        rc, out, err = self._kubectl(
            handle, 'scale deployment/%s --replicas=1' % shlex.quote(self._name(handle)),
            timeout=300)
        if rc != 0:
            raise RuntimeError('kubectl scale up failed (rc=%s): %s' % (rc, (out + err)[-500:]))

    def stop(self, handle: ComputeHandle) -> None:
        rc, out, err = self._kubectl(
            handle, 'scale deployment/%s --replicas=0' % shlex.quote(self._name(handle)),
            timeout=120)
        if rc != 0 and 'NotFound' not in (out + err):
            raise RuntimeError('kubectl scale down failed (rc=%s): %s' % (rc, (out + err)[-500:]))

    def destroy(self, handle: ComputeHandle, *, purge=False) -> None:
        if purge:
            # delete the whole namespace (incl. PVCs/secrets) — the K8s analogue
            # of `compose down -v --remove-orphans`.
            with self._ssh() as ssh:
                rc, out, err = ssh.execute(
                    'kubectl delete namespace %s --ignore-not-found' % shlex.quote(
                        self._namespace(handle)), timeout=300)
        else:
            rc, out, err = self._kubectl(
                handle, 'delete deployment,service %s --ignore-not-found' % shlex.quote(
                    self._name(handle)), timeout=120)
        if rc != 0 and 'NotFound' not in (out + err):
            raise RuntimeError('kubectl delete failed (rc=%s): %s' % (rc, (out + err)[-500:]))

    def restart(self, handle: ComputeHandle) -> None:
        rc, out, err = self._kubectl(
            handle, 'rollout restart deployment/%s' % shlex.quote(self._name(handle)),
            timeout=300)
        if rc != 0:
            raise RuntimeError('kubectl rollout restart failed (rc=%s): %s' % (rc, (out + err)[-500:]))

    # -- introspection / interaction ---------------------------------------
    def exec(self, handle, command, *, user=None, shell='sh', timeout=None) -> ExecResult:
        rc, out, err = self._kubectl(
            handle, 'exec deployment/%s -- %s -c %s' % (
                shlex.quote(self._name(handle)), shell, shlex.quote(command)),
            timeout=timeout or 60)
        return ExecResult(rc=rc, stdout=out, stderr=err)

    def logs(self, handle, *, tail=None) -> str:
        tflag = ('--tail=%d ' % int(tail)) if tail else ''
        rc, out, err = self._kubectl(
            handle, 'logs deployment/%s %s2>&1' % (shlex.quote(self._name(handle)), tflag))
        return out

    def endpoint(self, handle) -> tuple[str, int]:
        return (handle.host, handle.http_port)

    def health(self, handle) -> HealthStatus:
        # phase + restartCount of the (first) pod, in one jsonpath query.
        jp = ('{.items[0].status.phase}|'
              '{.items[0].status.containerStatuses[0].restartCount}')
        rc, out, err = self._kubectl(
            handle, 'get pods -l app=%s -o jsonpath=%s' % (
                shlex.quote(self._name(handle)), shlex.quote(jp)), timeout=30)
        raw = (out or '').strip()
        if rc != 0 or not raw:
            return HealthStatus(running=False, status='not_found', restart_count=0,
                                detail=(err or 'not_found').strip())
        parts = raw.split('|')
        phase = (parts[0] or 'not_found').strip()
        try:
            restart_count = int(parts[1])
        except (IndexError, ValueError):
            restart_count = 0
        # Normalize K8s phases to the ComputeDriver vocabulary the reconciler uses.
        status = {'Running': 'running', 'Succeeded': 'exited', 'Failed': 'dead',
                  'Pending': 'restarting'}.get(phase, phase.lower() or 'not_found')
        return HealthStatus(running=(status == 'running'), status=status,
                            restart_count=restart_count, detail=raw)
