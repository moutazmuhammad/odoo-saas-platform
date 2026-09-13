# Phase 0 — Audit Findings

**Date:** 2026-06-17
**Branch:** `architecture-evolution`
**Goal of Phase 0:** verify a local dev/test environment, map the as-built system, and establish a
green test baseline before any refactor. See `PHASE-BREAKDOWN.md` Phase 0 for the step list.

---

## 1. Environment — VERIFIED ✅
- venv: `/home/moutaz/Documents/Work/odoo18/.env` (Python 3.12) — all deps present (jinja2, boto3,
  paramiko, psycopg2, werkzeug, google-cloud-storage).
- Config: `odoo18/odoo.conf` — PostgreSQL 12 @ 5432, user `odoo18` (superuser), addons_path includes
  `custom/saas`. (A PG14 cluster also exists @ 5433.)
- Docker 29.3.1, Node v20 / npm 10.8 present.
- **No git token or remote access is needed for local build/test** — the code is on disk and runs locally.
- Run locally: `cd odoo18 && ./.env/bin/python odoo/odoo-bin -c odoo.conf -d saas_dev`.
- Disposable dev DB: **`saas_dev`** (do NOT touch `saas` / `saas_test`).

## 2. Module install — CLEAN ✅
Fresh `saas_dev` DB installed `base,saas_core,saas_website` with exit 0. Only harmless warnings:
duplicate field labels (`plan_count`/`plan_ids`; `db_server_id`/`is_db_server`; backup vs unsplash keys),
a couple of view-accessibility notices, and an `unaccent` field-param warning. Track these during the
god-model inventory (0.2.7); none block anything.

## 3. Test baseline — was RED, now GREEN ✅
Run command:
```
./.env/bin/python odoo/odoo-bin -c odoo.conf -d saas_dev \
  -u saas_core --test-enable --test-tags /saas_core --without-demo=all --stop-after-init
```
- **Before:** 3 failed of 54 tests.
- **After fixing stale tests:** **0 failed, 0 errors of 54 tests.**

### The 3 failures were STALE TESTS, not code regressions
| Test | Symptom | Root cause | Fix |
|---|---|---|---|
| `test_environment_order_lines_one_per_active_child` | 0 ≠ 2 | Billing moved to **slot-based** (`staging_slots`/`dev_slots` = purchased entitlement); test counted live children and never set slots | Rewrote to set slots; renamed `…_one_per_slot` |
| `test_renewal_invoice_includes_environment_lines` | 0 ≠ 1 | Same slot model + recurring line label changed from "server" to "**slot**" | Set `staging_slots=1`; filter on `'slot'` |
| `test_wallet_expiry_only_system_issued` | 40.0 ≠ 55.0 | `balance` is computed **live-only** (sums lots where `_is_live()`), so an already-expired credit is excluded immediately — not only after the cron | Assert `40.0` pre-cron with an explanatory comment |

## 4. As-built facts confirmed (feed the architecture work)
- **Odoo 18 itself is the Control Plane** — provisions Odoo containers on remote hosts over SSH/paramiko.
- **`saas_instance.py` = 11,334 lines / 248 methods** — the god-model; dominant refactor risk.
- **No ComputeDriver abstraction** — Docker/SSH calls are embedded in the model (Phase 1 target).
- **Environment billing is slot-based** (entitlement), children never self-bill (matches SESSION_NOTES rules).
- **Wallet balance = spendable/live credit only.**
- One instance = one container on one host + one PG DB + local `cp -a` filestore (per-tenant scale ceiling;
  Phase 5 target).

## 5. Still open in Phase 0
- 0.2.2–0.2.5: write the full as-built flow maps (provisioning, backup/restore, billing crons, environments).
- 0.2.6: catalog every Docker/SSH call site (the Phase-1 driver boundary).
- 0.2.7: bucket the 248 god-model methods into concerns.
- 0.2.8: confirm deploy is source-clone vs image-based (sets Phase-2 scope).
- 0.3.2/0.3.3: add provisioning (driver-mocked) + backup/restore characterization tests.

## 6. Next step
Continue Phase 0 mapping (0.2.x) and extend the safety net with provisioning + backup/restore tests,
then begin Phase 1 (ComputeDriver + DataService seams).
