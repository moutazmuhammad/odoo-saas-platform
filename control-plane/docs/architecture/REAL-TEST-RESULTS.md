# Real-Test Results (live integration on 165.245.245.196)

**Date:** 2026-06-17 · **Branch:** `architecture-evolution`
Validating the platform against a real server, not just local unit tests. Setup: see `REAL-TEST-SETUP.md`.

---

## ✅ Test 1 — End-to-end provisioning (PASS)
Registered control-plane records in `saas_dev` (script: `scripts/rt_register.py`) and ran the real
`saas.instance._do_deploy()` against the live server. Result: a real Odoo 18 tenant, provisioned entirely
through the platform's own code path.

Evidence (from the provisioning log + verification):
- PostgreSQL role `saas_rt1` created (hosting mode: customer creates DBs, so no DB auto-created).
- `docker-compose.yml` + `odoo.conf` rendered and written over SFTP.
- Container **`odoo_rt1` created, started, and reports `healthy`** (image `odoo:18.0`).
- Nginx reverse proxy written + reloaded; **Let's Encrypt SSL obtained for `rt1.odoo.odex.sa`**
  (DNS for the subdomain resolves to the server — no manual DNS needed).
- Instance state → **running**; host ports 32000 (http) / 32001 (longpolling), bound to 127.0.0.1.

Verification:
- `curl http://127.0.0.1:32000/web/login` → **HTTP 303** (Odoo alive; redirects to DB selector since
  no database exists yet — expected for a hosting instance).
- `curl https://rt1.odoo.odex.sa/web/login` → **HTTP 303** (nginx + SSL proxy working).
- **Idle resource baseline: CPU ~2.4%, RAM ~199 MiB** (container limit 1.3 GiB).

### What this proves
The real provisioning pipeline works on a clean Ubuntu 24.04 + Docker + PostgreSQL 16 host: SSH transport,
PG role creation, config rendering, container lifecycle, nginx + automatic TLS. This is the integration
confidence local unit tests cannot give.

## Registration records (in `saas_dev`)
ssh key `cp-ed25519` (ed25519) · region `fra1` · server `rt-fra1` (docker+db, 165.245.245.196, root, host-key
pinned) · version `18.0` (`odoo:18.0`) · domain `odoo.odex.sa` · product/plan `RT Hosting`/`RT Plan` ·
partner `RT Customer` · instance `rt1` (production, running).

## ✅ Test 2 — Database creation (PASS)
Switched the version to the faithful `odoo-light:18.0` image (built on the host) + cloned Odoo 18.0
source to `/opt/odoo-source/18.0`, redeployed, and ran `inst.hosting_db_create('rtdb', ...)`.
- First call built the per-instance template (`__odoo_template_rt1`) via a one-off container, then
  `CREATE DATABASE ... WITH TEMPLATE` → DB **`rt1_rtdb`** created.
- Verified: container runs `odoo-light:18.0` (healthy); **HTTP 200** on `/web/login` (local + HTTPS).
- Finding: the official `odoo:18.0` image works for an empty instance but **breaks the DB template
  build** (`--addons-path: no such directory: /opt/odoo/odoo/addons`) — the platform requires the
  `odoo-light` image + mounted source. Confirmed and resolved.

## ✅ Test 3 — DataService backup → restore round-trip (PASS — the critical one)
Configured DO Spaces (provider=digitalocean, bucket `odoo18saas`, region `fra1`; creds runtime-only,
never committed) and ran the real restic full-instance backup, then restored.
- Backup: `saas.instance.backup` id=1, **state=done**, full-instance (restic → DO Spaces).
- Integrity method: planted a marker table `zz_marker_after_backup` AFTER the backup, then restored.
- Restore (36s, 5 steps: stop → wipe → restic FS restore → DB via `restic dump→psql` → up → nginx):
  - marker table **gone** (`t`→`f`) ⇒ DB genuinely replaced with the backed-up version (real restore).
  - `res_users`=5 and company "My Company" **unchanged** ⇒ data intact.
  - **HTTP 200** after restore ⇒ tenant serves correctly.

### Phase 0 acceptance — MET (on real infrastructure)
"A tenant can be provisioned, backed up, destroyed, and restored" — proven end-to-end on a real server,
not mocked. Provision ✅ · DB ✅ · backup ✅ · restore-with-integrity ✅.

## Next tests
- [ ] Clone + neutralize (DataService clone primitive).
- [ ] Resource baseline under a light, defined load (k6/Locust).

## Notes
- Per-tenant baseline (idle ~200 MiB) already informs the margin model (Phase 4) and scale math (Phase 5).
- Object-storage creds for the backup test are temporary/disposable and are **NOT** committed to the repo.

## ✅ Test 4 — DataService seam round-trip (PASS)
Re-proved backup→restore THROUGH the new `DataService` primitives on live rt1:
`inst._data_service().snapshot(inst)` (restic→DO Spaces, backup id=2 done) → planted marker
`zz_ds_marker` → `materialize(snap)` (37.8s restore) → marker **gone**, res_users=5, HTTP 200.
Confirms the seam wraps the proven logic with no behavior change.
