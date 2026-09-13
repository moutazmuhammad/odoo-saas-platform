# As-Built Map — Provisioning & Deploy

**Date:** 2026-06-17 · **Branch:** `architecture-evolution` · Phase 0 steps 0.2.2 / 0.2.8.
Altitude: enough to plan Phase 1 safely. Line refs are to `saas_core/models/` at this commit.

---

## 1. Control Plane shape
Odoo 18 (`saas_core`) is the Control Plane. It drives remote Docker hosts over SSH. Long operations
run in a **background thread** (`utils.run_in_background(record, method)`), which commits the current
transaction, opens a fresh cursor, and runs the method — so the web request returns immediately.

## 2. SSH transport (already abstracted) ✅
- `saas_core/utils.py :: SSHConnection` — context manager over paramiko. Methods:
  `execute(cmd) -> (rc, out, err)`, `exec_command_streaming(cmd)`, `write_file(path, content)`,
  `read_file_bytes(path)`. Host-key pinning supported (`_PinnedHostKeyPolicy`).
- `saas_server._get_ssh_connection()` (saas_server.py:471) builds the `SSHConnection` from the server
  record (host/port/user/key). This is the ONE place a connection is created.
- **Implication:** the transport layer is clean. What is NOT abstracted is the **semantic operations**
  (docker/pg/nginx/config) issued through it — see `DRIVER-BOUNDARY.md`.

## 3. Base image (per Odoo version)
- `saas.odoo.version` has `docker_image` + `docker_image_tag`; `_get_docker_image()` returns
  `image:tag` (saas_odoo_version.py:41). Base images are built by `saas_core/docker/build-all.sh`.

## 4. Provisioning / deploy flow (the happy path)
Entry: `action_deploy()` (4353) → `run_in_background(rec, '_do_deploy')` (4403) → `_do_deploy()` (4435).
Within `_do_deploy`, using one `SSHConnection` (`with server._get_ssh_connection() as ssh:`):

1. **Validate** deploy fields (`_validate_deploy_fields` 3234) and SSH reachability (`_ensure_can_ssh` 3016).
2. **Allocate** compute + DB servers + ports (`_allocate_servers` 3029, `_allocate_db_server` 3106,
   `_auto_assign_ports` 3168).
3. **PostgreSQL**: create role + DB, or clone from the per-instance PG template
   (`_provision_postgresql` 2735; `_pg_ensure_db_with_grants` 2853; fast path `_pg_clone_db` 2944 via
   `CREATE DATABASE WITH TEMPLATE`). Filestore copied with `cp -a`.
4. **Fetch customer code**: `git clone --single-branch` of product repos onto the host
   (`_clone_product_repos` 4243, git clone at 4269; submodules `_update_repo_submodules` 4209;
   updates `_pull_product_repos` 4292).
5. **Render configs** with Jinja2 and `write_file` them to the host: `docker-compose.yml` + `odoo.conf`
   (`_render_and_write_configs` 3921; `_render_template`/`_JINJA_ENV` 2698; addons paths
   `_get_all_addons_paths` 2703).
6. **Start container**: `docker compose up -d` (multiple sites, e.g. 6392–6412), optional
   `docker compose run` for DB init.
7. **Ingress**: render + write nginx vhost and reload (`_provision_nginx` 7032, `_refresh_nginx_config`
   7228, `_remove_nginx` 7283).
8. **Webhooks / health**: register webhooks (`_ensure_webhooks_registered` 2618), refresh usage
   (`_refresh_usage_with_ssh` 3609).
Errors are caught by the background error handler; state moves to a failure/pending state, retried by
crons (`_cron_retry_pending_provision` 3403, `_cron_recover_stuck_provisioning` 3488).

## 5. DEPLOY MECHANISM — the key Phase-2 finding (0.2.8)
**Hybrid: immutable base image + mutable source mounted at runtime.**
- The **base image** (per version) is immutable and tagged — good.
- The **tenant layer** (customer/product code) is `git clone`'d onto the host at deploy and **mounted**
  into the container via `docker-compose` + `addons_path`. It is NOT baked into a per-tenant image
  tagged by commit SHA.
- Consequence for the spec: today "what is running" = host source tree state + base image. There is no
  per-tenant immutable artifact, so **rollback is not "redeploy a SHA"** and a host is **not disposable**
  (its source tree + local filestore are state). 
- **Phase 2 target** (unchanged, now confirmed concrete): bake the tenant layer into an image tagged
  `tenant-<ver>-<sha>` and pull-by-SHA; move filestore to object storage. The base-image build already
  exists, so Phase 2 *extends* it with a tenant layer rather than starting from scratch.

## 6. Backup / restore (DataService territory)
- Backups: restic (daily, deduplicated) streamed via `exec_command_streaming` to object storage
  (`saas_instance_backup.py`; restic install `_ensure_restic_installed` 1170; GCS creds staging 1141).
  On-demand = zip (ephemeral). DB restore paths: `_restore_snapshot` 4053, `_restic_restore_one_db`
  6468, `_restore_one_db_from_dump` 6569.
- These wrap cleanly into the Phase-1 `DataService` (`snapshot`/`materialize`).

## 7. Risks reconfirmed
- `saas_instance.py` = 11,334 lines / 248 methods; ~121 Docker call sites here alone (~140 across models).
- Per-tenant scale ceiling: one container / one host / one DB / local filestore (Phase 5 tier target).
