# Architecture & Evolution Docs

This folder is the **living reference** for evolving this platform toward a scalable, profitable
Odoo.sh-class hosting product. Keep these updated as work proceeds (especially the progress logs).

| Doc | Purpose |
|---|---|
| `MICROSERVICES-PLAN.md` | **Active plan**: splits the compute layer into a separate Kubernetes-backed microservice (reusing the operator built in `my-saas`), communicating with the Control Plane over the Kubernetes API. Supersedes `architecture-spec-v1.md` §1.4/§7 and `PHASE-BREAKDOWN.md` Phase 6 for the compute layer only. |
| `architecture-spec-v1.md` | The **target** architecture (Control Plane, storage, deploy/backup/restore/upgrade flows). |
| `IMPLEMENTATION-PLAN.md` | The **roadmap** — phases 0–7 that evolve the existing system toward the spec, ordered by dependency. Has a living progress log. |
| `PHASE-BREAKDOWN.md` | Every phase decomposed into **small, ID'd, verifiable steps** (0.1.1 … 7.2). The executable checklist. Includes the "start small / scale without rewrite" DO-NOW vs DEFER classification. |
| `PHASE-0-FINDINGS.md` | Results of the Phase 0 audit (env verified, module install, test baseline green, as-built facts). |
| `AS-BUILT.md` | The as-built provisioning/deploy flow + the deploy-mechanism finding (hybrid base-image + mounted source). |
| `DRIVER-BOUNDARY.md` | Catalog of the ~140 SSH/Docker call sites + the proposed `ComputeDriver` interface — the direct input to Phase 1. |

## Guiding intent
- **Start small, scale without a rewrite:** build cheap seams now (driver, DataService, object storage,
  observability), defer expensive infrastructure (scale-out tier, Kubernetes, upgrade automation) until
  real demand.
- **Profitability is a first-class goal:** the per-tenant margin dashboard (Phase 4) is how we know it.
- **The god-model (`saas_instance.py`, ~11k lines) is decomposed incrementally**, behind tests — never big-bang.

## How we work
- One step ≈ one commit; run the test suite before moving on.
- Update the progress log in `IMPLEMENTATION-PLAN.md` §10 after each step.
- Local disposable dev DB: `saas_dev` (Python venv at `odoo18/.env`, config `odoo18/odoo.conf`).
