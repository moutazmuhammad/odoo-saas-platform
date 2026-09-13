# Driver Boundary Catalog — Phase 1 input

**Date:** 2026-06-17 · **Branch:** `architecture-evolution` · Phase 0 step 0.2.6.
**Purpose:** identify exactly what the `ComputeDriver` seam must own, so Phase 1 is a mechanical
extraction, not a redesign. Line refs to `saas_core/models/` at this commit.

---

## 1. Layers today
```
  business logic (saas_instance.py, 248 methods)
        │  issues 'docker ...' / 'pg ...' / 'nginx ...' command STRINGS
        ▼
  ssh.execute(cmd)            ← semantic ops live HERE, inline, ~140 call sites  ❌ not abstracted
        ▼
  SSHConnection (utils.py)    ← transport: execute / streaming / write_file / read_file  ✅ abstracted
        ▼
  saas_server._get_ssh_connection()  ← the single connection factory  ✅
```
The transport is clean. The **semantic operations** (what we run over SSH) are scattered inline. Phase 1
puts a `ComputeDriver` between business logic and the command strings.

## 2. Inventory of Docker call sites (the boundary)
~140 total: **121 in `saas_instance.py`**, ~18 in `saas_instance_backup.py`, 1 in
`saas_docker_container.py`, 1 in `saas_instance_db_operation.py`. All go through `SSHConnection.execute`.

Grouped by intended ComputeDriver method:

| ComputeDriver method | What it does today | Representative sites (saas_instance.py) |
|---|---|---|
| `start` / `create` | `docker compose up -d` (+ `run` for init) | 6392–6412, 6876, 7385, 7742–7746, 9902–9990 |
| `stop` | `docker compose stop`, `docker stop` | 6181–6186, 6715, 7362, 8593, 9946–9951 |
| `destroy` | `docker compose down` (+ volume/dir cleanup) | 7734–7739 |
| `restart` | `down` then `up` | 7734–7746 |
| `exec` | `docker exec` / `docker compose exec` | 6935, 7777, 9295, 9473–9491, 9737; helpers `_docker_exec_python` 9444, `_docker_exec_sql` 9479 |
| `logs` | container logs read | `controllers/container_logs.py`; `saas_docker_container.action_view_logs` |
| `health` / `endpoint` | `docker inspect`, `docker stats` | 7336; metrics `_refresh_usage_with_ssh` 3609, `_sample_live_metrics_for_host` 3799 |
| `write_config` | render + `write_file` compose/odoo.conf | `_render_and_write_configs` 3921 |

## 3. What does NOT belong on ComputeDriver (separate seams)
- **DataService** (Phase 1, separate): PG provisioning + backup/restore.
  - PG: `_provision_postgresql` 2735, `_pg_ensure_db_with_grants` 2853, `_pg_clone_db` 2944,
    `_pg_drop_db` 2999, `_pg_db_exists` 2897.
  - Backup/restore: `saas_instance_backup.py` (restic), `_restore_snapshot` 4053,
    `_restic_restore_one_db` 6468, `_restore_one_db_from_dump` 6569.
- **Ingress/routing** (own seam later; keep as-is for now): nginx `_provision_nginx` 7032,
  `_refresh_nginx_config` 7228, `_remove_nginx` 7283.
- **Source fetch** (becomes the Build Pipeline in Phase 2): `_clone_product_repos` 4243,
  `_pull_product_repos` 4292, `_update_repo_submodules` 4209.

## 4. Proposed ComputeDriver interface (Phase 1.1.1)
```python
class ComputeDriver:
    def create(self, spec): ...        # write configs + first 'up' (image from spec.image)
    def destroy(self, handle): ...     # compose down + cleanup
    def start(self, handle): ...
    def stop(self, handle): ...
    def restart(self, handle): ...
    def exec(self, handle, cmd, *, user=None, timeout=None): ...   # -> (rc, out, err)
    def logs(self, handle, *, tail=None): ...
    def endpoint(self, handle): ...    # host/ports where ingress routes
    def health(self, handle): ...      # running? + basic stats
```
- **One implementation:** `SshDockerDriver` — internally uses `saas_server._get_ssh_connection()` +
  the command strings moved out of `saas_instance.py`.
- `spec`/`handle` = a plain descriptor (server, container name, ports, db, image). No Odoo coupling in
  the interface, so a future `KubernetesDriver` is a new file only.

## 5. Phase 1 extraction order (safe, test-after-each)
1. 1.1.1 define `ComputeDriver` (base.py) + `spec`/`handle` descriptors.
2. 1.2.x build `SshDockerDriver`; move command strings group by group (start/stop first, then exec,
   then health/inspect, then config write).
3. 1.3.2 replace call sites in `saas_instance.py` **one method per commit**; run the suite each time.
4. 1.3.4 assert no `docker ` string remains outside `SshDockerDriver`; baseline still green.
5. DataService (1.4.x) wraps PG + restic restore behind `snapshot`/`materialize`.

**Definition of done for the boundary:** `grep -n "docker " saas_core/models/saas_instance.py` returns
nothing (all moved to the driver), and the 54-test baseline is still green.
