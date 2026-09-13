# Phase 1 Status — ComputeDriver + DataService seams

**Date:** 2026-06-17 · **Branch:** `architecture-evolution`
**Outcome:** **Phase 1 complete.** Every ComputeDriver-lifecycle/introspection code path runs through the
`ComputeDriver` / `DataService` seams — incl. the full tail (host-batch stats, pip `bash` exec, health-poll
deploy wait, crash-loop health cron) — each verified on **real infrastructure** (live tenants `rt1`, `rt2`).
The only inline `docker` left is one-shot `odoo`-CLI/image work owned by the Phase-2 Build seam (see below).

---

## What exists now
- `saas_core/drivers/base.py` — `ComputeDriver` ABC + Odoo-free descriptors (`ComputeSpec`,
  `ComputeHandle`, `ExecResult`, `HealthStatus`).
- `saas_core/drivers/ssh_docker_driver.py` — the single v1 impl. Capabilities:
  `start` / `stop` / `restart` / `destroy(purge=)` / `exec` / `service_exec(env=)` / `stats` / `logs` /
  `endpoint` / `health`, with **optional connection reuse** (pass an open `SSHConnection`).
- `saas_core/dataservice/service.py` — `DataService.snapshot()` + `materialize()` (delegate to the
  proven restic backup + 5-step restore).
- `saas.instance._compute_driver(connection=)` / `_compute_handle()` / `_data_service()`;
  `saas.docker.container._driver_handle()`.

## Call sites routed (~23) — all real-proven
provision (first `_do_deploy` `up`) · template build (`down`/`up`) · backup→restore round-trip ·
`_do_stop` / `_do_restart` / `_do_suspend` · redeploy down+up · config-refresh down+up ·
addons-restart down+up · restic-restore Step2/Step5 · zip-restore pre-stop · cancel/teardown
`down -v --remove-orphans` · `_docker_exec_sql` · `_docker_exec_python` · `_refresh_usage` stats ·
`saas_docker_container` admin stop/restart.

## Real-infra evidence (live server 165.245.245.196)
- Provisioned a fresh tenant `rt2` end-to-end (own port + Let's Encrypt SSL, HTTP 303→200).
- Created `rt2_rtdb` (template build) → HTTP 200.
- Backup→restore round-trips on `rt1` ×3 with tenant-owned marker integrity (marker vanishes on
  restore, `res_users` intact, HTTP 200).
- Lifecycle (stop/restart/suspend), destroy+start recreate, purge+recreate (data survives) — all verified.
- `rt1` + `rt2` both healthy and serving 200 after all churn.
- **66/66 unit tests** green on clean DB `saas_ci` (`test_compute_driver.py`, `test_dataservice.py`).

## Tail — ComputeDriver lifecycle/introspection (DONE, real-proven 2026-06-17)
All four enumerated tail items are routed through the driver and verified live on `rt1`/`rt2`:
1. ✅ `_sample_live_metrics_for_host` → new `driver.stats_many(container_names)` (host-batch `docker stats`
   in one SSH). Live: both tenants sampled in one call; cpu/ram written.
2. ✅ pip-install helpers (`_pre_restore` + `_apply_pip_packages`) → `driver.exec(..., shell='bash')`
   (new `shell=` option; default stays `sh -c`). Live: `bash -c` confirmed in-container; the
   `awk '{print $1}'` single-quote escaping survives the driver's `shlex.quote` (md5 stamped correctly).
3. ✅ deploy **wait-loop** → new `driver.wait_until_running(handle)` (health-based poll, same 30×2s ceiling);
   failure path now uses `driver.logs(handle, tail=50)`.
4. ✅ health-check cron `_cron_check_container_health` → `driver.health()` now returns `status` +
   `restart_count` (one-off stop vs. crash-loop); repair stop/up → `driver.stop`/`driver.start`.
   Live: `health` = `running|0|healthy`; cron ran clean over both healthy tenants.

Driver surface added: `stats_many`, `wait_until_running`, `exec(shell=)`, `health` now carries
`status`/`restart_count`. Unit suite **70/70** on clean DB `saas_ci` (+4 new tests).

## Residual `docker` grep hits = Build/DataService seam, NOT ComputeDriver (Phase 2)
The remaining inline `docker` command sites are **one-shot `odoo`-CLI / image-introspection** ops, which
are deliberately out of the compute-lifecycle boundary (a workload `handle` doesn't even apply to them).
They are owned by the Phase-2 Build Pipeline (immutable images replace "run `odoo -i/-u` on the host"), so
wrapping them in `ComputeDriver` now would be throwaway work Phase 2 rewrites:
- `_get_container_uid` (2638): `docker run --rm --entrypoint id <IMAGE>` — image introspection, no handle.
- first-deploy DB init (4528): `docker compose run --rm -T odoo odoo -d <db> -i base …`.
- module upgrade (9892/9912/9931): `compose stop odoo` + `compose run … odoo -u <mod>` + `compose up -d`,
  one atomic op with bespoke per-command output capture for the customer report.
- clone init (10641): `docker compose run --rm -T odoo odoo …`.

**Definition of done (revised):** every ComputeDriver-lifecycle/introspection call site routes through the
driver (✅), unit suite green (✅ 70/70), routed paths verified on the live server (✅). The remaining
`docker compose run odoo …` one-shots are Phase-2 Build-seam work. At this point a `KubernetesDriver`
is purely a new file for the lifecycle surface.
