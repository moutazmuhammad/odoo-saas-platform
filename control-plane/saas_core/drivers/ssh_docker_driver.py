"""SshDockerDriver — the single v1 implementation of ComputeDriver.

Wraps the existing Docker-over-SSH operations (the ~140 inline `ssh.execute('docker …')`
call sites cataloged in docs/architecture/DRIVER-BOUNDARY.md) behind the stable interface.
The transport is the existing `SSHConnection` (utils.py), obtained from the target
`saas.server` record's `_get_ssh_connection()`.

Phase 1 wires call sites onto this incrementally; behavior must stay identical (same commands,
same paths) so the green test baseline and real-infra behavior are unchanged.
"""

from __future__ import annotations

import logging
import random
import shlex
import time
from contextlib import nullcontext

from .base import (ComputeDriver, ComputeSpec, ComputeHandle, ExecResult, HealthStatus)

_logger = logging.getLogger(__name__)


class SshDockerDriver(ComputeDriver):
    """Docker Compose over SSH. Constructed with the target `saas.server` record,
    which provides the SSH connection (and thus host-key pinning, key auth, etc.).

    Pass ``connection`` (an already-open SSHConnection) to REUSE it instead of
    opening a new one — lets fine-grained call sites inside an existing
    ``with server._get_ssh_connection() as ssh:`` block route through the driver
    without losing connection reuse. When reusing, the driver never closes it
    (the owning ``with`` block does)."""

    def __init__(self, server, connection=None):
        # `server` is a saas.server record exposing _get_ssh_connection().
        self.server = server
        self._conn = connection

    # -- helpers ------------------------------------------------------------
    def _ssh(self):
        # Reuse a caller-provided connection (no-op close), else open a fresh one.
        if self._conn is not None:
            return nullcontext(self._conn)
        return self.server._get_ssh_connection()

    def service_exec(self, handle, command, *, service='odoo', env=None, timeout=60):
        """Run ``command`` inside a compose SERVICE via `docker compose exec -T`,
        with optional ``env`` (passed as `-e K=V`). Matches the god-model's
        `cd <path> && docker compose exec -T <env> <service> <command>` exactly,
        so it can replace those inline calls faithfully. ``command`` is taken
        verbatim (caller does any quoting / heredoc)."""
        env_flags = ''
        if env:
            env_flags = ' '.join(
                '-e %s=%s' % (k, shlex.quote(str(v))) for k, v in env.items())
        cmd = 'cd %s && docker compose exec -T %s %s %s' % (
            shlex.quote(handle.instance_path), env_flags, service, command)
        with self._ssh() as ssh:
            rc, out, err = ssh.execute(cmd, timeout=timeout)
        return ExecResult(rc=rc, stdout=out, stderr=err)

    def _compose(self, ssh, instance_path, verb, timeout=120):
        """Run `cd <path> && docker compose <verb>` exactly as the god-model does."""
        return ssh.execute(
            'cd %s && docker compose %s 2>&1' % (shlex.quote(instance_path), verb),
            timeout=timeout,
        )

    # -- lifecycle ----------------------------------------------------------
    def create(self, spec: ComputeSpec) -> ComputeHandle:
        # Full provisioning (render configs + first `up`) still lives in the
        # god-model's _do_deploy; it will be routed here in a later Phase-1 step.
        raise NotImplementedError(
            "SshDockerDriver.create is not wired yet — provisioning still runs "
            "through saas.instance._do_deploy (Phase 1, later increment)."
        )

    # `docker compose up -d` occasionally fails transiently — a daemon
    # networking hiccup, or the brief window during a recreate where the old
    # container still holds the published port ("address already in use").
    # Since `up -d` is idempotent we retry it a few times with backoff+jitter
    # so provisioning self-heals instead of failing the whole deploy on a blip
    # (observed on a real provision; ARCH-008/010). A genuinely permanent error
    # still surfaces after the budget is exhausted.
    _COMPOSE_UP_ATTEMPTS = 3
    _COMPOSE_UP_BACKOFF = 2.0  # seconds: ~2s, ~4s (+ up to 1s jitter)

    def start(self, handle: ComputeHandle) -> None:
        # `up -d` is idempotent: creates if missing, recreates on config drift,
        # starts if stopped — matches the god-model's start path.
        last = ''
        for attempt in range(1, self._COMPOSE_UP_ATTEMPTS + 1):
            with self._ssh() as ssh:
                rc, out, err = self._compose(
                    ssh, handle.instance_path, 'up -d', timeout=300)
            if rc == 0:
                return
            last = (out + err)[-500:]
            if attempt < self._COMPOSE_UP_ATTEMPTS:
                delay = self._COMPOSE_UP_BACKOFF * attempt + random.uniform(0, 1)
                _logger.warning(
                    "docker compose up failed (rc=%s, attempt %d/%d) for %s — "
                    "retrying in %.1fs: %s", rc, attempt,
                    self._COMPOSE_UP_ATTEMPTS, handle.instance_path, delay,
                    last[-200:])
                time.sleep(delay)
        raise RuntimeError(
            'docker compose up failed after %d attempts (rc=%s): %s'
            % (self._COMPOSE_UP_ATTEMPTS, rc, last))

    def stop(self, handle: ComputeHandle) -> None:
        # Container-level op (works for both compose-managed and raw containers,
        # and matches the god-model's original `docker stop <container>`).
        with self._ssh() as ssh:
            rc, out, err = ssh.execute(
                'docker stop %s' % shlex.quote(handle.container_name), timeout=120)
        if rc != 0 and 'No such container' not in (out + err):
            raise RuntimeError('docker stop failed (rc=%s): %s' % (rc, (out + err)[-500:]))

    def destroy(self, handle: ComputeHandle, *, purge=False) -> None:
        # Removes the compose project (network + container); needs instance_path.
        # purge=True also drops named volumes + orphans (`-v --remove-orphans`).
        verb = 'down -v --remove-orphans' if purge else 'down'
        with self._ssh() as ssh:
            rc, out, err = self._compose(ssh, handle.instance_path, verb, timeout=120)
        if rc != 0 and 'No such' not in (out + err):
            raise RuntimeError('docker compose down failed (rc=%s): %s' % (rc, (out + err)[-500:]))

    def restart(self, handle: ComputeHandle) -> None:
        # Container-level op (matches the god-model's original `docker restart`).
        with self._ssh() as ssh:
            rc, out, err = ssh.execute(
                'docker restart %s' % shlex.quote(handle.container_name), timeout=300)
        if rc != 0:
            raise RuntimeError('docker restart failed (rc=%s): %s' % (rc, (out + err)[-500:]))

    # -- introspection / interaction ---------------------------------------
    def exec(self, handle, command, *, user=None, shell='sh', timeout=None) -> ExecResult:
        # ``shell`` picks the in-container interpreter (`sh -c` default; `bash`
        # for the pip helpers that rely on bashisms). ``command`` is the literal
        # script to run — quoting is handled here, callers pass it unescaped.
        uflag = ('-u %s ' % shlex.quote(user)) if user else ''
        with self._ssh() as ssh:
            rc, out, err = ssh.execute(
                'docker exec %s%s %s -c %s' % (
                    uflag, shlex.quote(handle.container_name),
                    shell, shlex.quote(command)),
                timeout=timeout or 60,
            )
        return ExecResult(rc=rc, stdout=out, stderr=err)

    def logs(self, handle, *, tail=None) -> str:
        tflag = ('--tail %d ' % int(tail)) if tail else ''
        with self._ssh() as ssh:
            rc, out, err = ssh.execute(
                'docker logs %s%s 2>&1' % (tflag, shlex.quote(handle.container_name)),
                timeout=60,
            )
        return out

    def endpoint(self, handle) -> tuple[str, int]:
        return (handle.host, handle.http_port)

    def stats(self, handle, *, fmt='{{.CPUPerc}}||{{.MemUsage}}||{{.MemPerc}}',
              timeout=None) -> ExecResult:
        """Raw `docker stats --no-stream` for the container; caller parses the output."""
        with self._ssh() as ssh:
            rc, out, err = ssh.execute(
                'docker stats --no-stream --format %s %s' % (
                    shlex.quote(fmt), shlex.quote(handle.container_name)),
                timeout=timeout or 60)
        return ExecResult(rc=rc, stdout=out, stderr=err)

    def stats_many(self, container_names, *,
                   fmt='{{.Name}}||{{.CPUPerc}}||{{.MemUsage}}', timeout=30) -> ExecResult:
        """One `docker stats --no-stream` over MANY containers in a SINGLE call
        (the host-batch live-metrics sampler). Caller parses the multi-line
        output (one line per container). Matches the god-model's batched cmd."""
        with self._ssh() as ssh:
            rc, out, err = ssh.execute(
                'docker stats --no-stream --format %s %s' % (
                    shlex.quote(fmt),
                    ' '.join(shlex.quote(n) for n in container_names)),
                timeout=timeout)
        return ExecResult(rc=rc, stdout=out, stderr=err)

    def health(self, handle) -> HealthStatus:
        # status + restart count + (optional) healthcheck state in one inspect.
        # RestartCount lets callers tell a one-off stop apart from a crash-loop.
        fmt = "{{.State.Status}}|{{.RestartCount}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}"
        with self._ssh() as ssh:
            rc, out, err = ssh.execute(
                "docker inspect -f %s %s 2>/dev/null" % (
                    shlex.quote(fmt), shlex.quote(handle.container_name)),
                timeout=30,
            )
        raw = (out or '').strip().strip('"')
        if rc != 0 or not raw:
            # No such container / unreachable -> treat as 'not_found' (matches
            # the god-model's `|| echo "not_found|0"` fallback).
            return HealthStatus(running=False, status='not_found', restart_count=0,
                                detail=(err or out or 'not_found').strip())
        parts = raw.split('|')
        status = (parts[0] or 'not_found').strip()
        try:
            restart_count = int(parts[1])
        except (IndexError, ValueError):
            restart_count = 0
        return HealthStatus(running=(status == 'running'), status=status,
                            restart_count=restart_count, detail=raw)

    def wait_until_running(self, handle, *, attempts=30, interval=2) -> HealthStatus:
        """Poll health() until the container is 'running', hits a terminal
        ('exited'/'dead') state, or attempts run out. Returns the final
        HealthStatus. Replaces the inline remote `for i in seq 1 30 …` bash
        wait-loop with a health-based poll (same 30×2s ceiling)."""
        last = self.health(handle)
        for _ in range(attempts - 1):
            if last.status in ('running', 'exited', 'dead'):
                return last
            time.sleep(interval)
            last = self.health(handle)
        return last
