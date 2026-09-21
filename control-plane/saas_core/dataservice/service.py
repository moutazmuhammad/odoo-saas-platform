"""DataService — shared polling primitives used by the Kubernetes deploy/
scale paths.

Historically this also held the ssh_docker-era snapshot()/materialize()
primitives and the legacy-ssh_docker -> Kubernetes migrate_to_kubernetes()/
cutover subsystem (see /ROADMAP.md §5 Phase 2 and git history for that
implementation) — both were removed when ssh_docker was retired as a
compute backend entirely (there is nothing left to snapshot/materialize
via restic-over-SSH, and nothing left to migrate FROM). What remains is
genuinely backend-agnostic and still used by the Kubernetes deploy/scale
code paths in ``saas.instance``.
"""

from __future__ import annotations

import time


class DataService:
    """Constructed with an Odoo Environment; operates on saas.instance records."""

    def __init__(self, env):
        self.env = env

    def _wait_until_healthy(self, driver, handle, timeout=1800):
        """Poll health() until the instance's web Deployment is actually
        serving, not just created/restored. A single post-create/restore
        health() check can catch it mid-rollout (status='restarting',
        phase='Provisioning') and wrongly read that as a failure.
        """
        _MIGRATION_POLL_INTERVAL = 5
        deadline = time.time() + timeout
        health = driver.health(handle)
        while not health.running:
            if time.time() > deadline:
                raise RuntimeError(
                    "instance did not become healthy within %ss "
                    "(status=%s, detail=%s)" % (timeout, health.status, health.detail))
            time.sleep(_MIGRATION_POLL_INTERVAL)
            health = driver.health(handle)
