# Odoo SaaS Platform (Odoo.sh-style hosting)

A monorepo consolidating this project's components, each in its own folder,
following the microservices split documented in
[`control-plane/docs/architecture/MICROSERVICES-PLAN.md`](control-plane/docs/architecture/MICROSERVICES-PLAN.md).

This repo was assembled from two prior working repos (fresh copy, no shared
git history — see that plan doc's own history note): the existing Odoo.sh
control plane, and the Kubernetes operator built to become its new compute
layer. Both prior repos still exist at their original locations if their
history is ever needed; this repo is the going-forward source of truth.

## Layout

```text
.
├── control-plane/   Odoo 18 control plane: saas_core (models/business logic)
│                    + saas_website (public/portal API + QWeb templates that
│                    host the SPA). Owns tenant/billing records, auth, the
│                    Postgres-backed job queue, and the public
│                    /saas/api/v1/* JSON API. See control-plane/docs/.
│
├── frontend/        veltnex: the React/Vite/TypeScript SPA served by
│                    control-plane/saas_website. Talks only to
│                    /saas/api/v1/* — no direct infrastructure awareness,
│                    which is exactly what kept it unaffected by the
│                    compute-layer migration (see the plan, Phase 4).
│
├── compute/         The Kubernetes operator (OdooInstance CRD + controller):
│                    provisions/deletes/backs up/restores tenant instances.
│                    This is the new Compute microservice — the Control
│                    Plane talks to it exclusively through the Kubernetes
│                    API (create/patch/delete OdooInstance custom
│                    resources), never SSH. See compute/docs/architecture.md
│                    (English) / architecture.ar.md (Arabic).
│
├── docs/PRODUCTION-READINESS-PLAN.md   The master roadmap (read this first).
│
└── .github/workflows/ci.yml   One pipeline, one job per component.
```

## Where to start

- **The master plan**: [`docs/PRODUCTION-READINESS-PLAN.md`](docs/PRODUCTION-READINESS-PLAN.md)
  — a fresh, evidence-based review of the current codebase (dependencies,
  dead code, test coverage) plus the full phased roadmap to production
  readiness (hygiene → test coverage → security → compute migration →
  features → reliability → go-live gate). Read this first.
- **The compute-layer migration in detail**: [`control-plane/docs/architecture/MICROSERVICES-PLAN.md`](control-plane/docs/architecture/MICROSERVICES-PLAN.md)
  — target architecture, phased migration steps, acceptance criteria, and a
  progress log for turning the Kubernetes operator into the real Compute
  microservice. This is Phase D of the master plan above.
- **Control plane history/context**: `control-plane/docs/architecture/` (the
  pre-existing evolution docs this plan builds on) and
  `control-plane/SESSION_NOTES.md` (running work log; some entries there
  predate the reorg and reference paths from the old repo layout — treat
  those as historical).
- **Security/architecture audit findings**: `control-plane/docs/reviews/`.
- **Compute service**: `compute/README.md` (quick start, provisioning
  lifecycle) and `compute/docs/architecture.md` (full design rationale).

## Local development

- **Running as a persistent systemd service** (instead of `devctl.sh`'s
  `nohup`-based dev mode): see
  [`control-plane/docs/LOCAL-RUNTIME-SYSTEMD.md`](control-plane/docs/LOCAL-RUNTIME-SYSTEMD.md).
- **Control plane** (Odoo): `control-plane/scripts/devctl.sh` — see that
  script's header comment for the sibling-directory layout it expects
  (Odoo source, venv, Postgres cluster; all overridable via env vars). Its
  local `odoo.conf`'s `addons_path` must point at this repo's
  `control-plane/` directory (containing `saas_core` and `saas_website` as
  direct children), not the old repo root. Install Odoo core's own
  `requirements.txt` first, then `pip install -r control-plane/requirements.txt`
  for `saas_core`'s own external deps (paramiko/jinja2/boto3/
  google-cloud-storage) — pinned there specifically to stay compatible
  with Odoo core's own pins; see that file's header before changing
  versions.
- **Frontend** (SPA): `cd frontend/veltnex && npm ci && npm run dev` (proxies
  API calls to a locally running Odoo on `:8018` — see `vite.config.ts`).
  `npm run build` writes straight into
  `control-plane/saas_website/static/spa/`.
- **Compute** (operator): `cd compute/operator && make test` (unit tests +
  envtest); `make manifests generate fmt vet` after changing the CRD types;
  see `compute/README.md` for a full local-cluster (microk8s) walkthrough.

## CI

`.github/workflows/ci.yml` runs three independent jobs on every PR: SPA
typecheck+build (`frontend/veltnex`), the Odoo test suite
(`control-plane/{saas_core,saas_website}`), and the operator's build+vet+test
(`compute/operator`).

## Known gaps carried forward (not silently fixed by this reorg)

- The security/architecture audit findings in `control-plane/docs/reviews/`
  (auth bypass, plaintext secrets for legacy SSH-based tenants, root
  containers on the legacy Docker path, no DR) are **unresolved** by this
  reorganization — it only changes where the code lives, not what it does.
  `MICROSERVICES-PLAN.md` §2 (Phase 0) tracks closing the ones that block
  everything else.
- A full dead-code/unused-code audit of `control-plane` (a ~22,000-line Odoo
  module set) was **not** performed as part of this reorg — that's a
  separate, substantial task. This pass only removed things the move itself
  made stale (build caches, `__pycache__`, `node_modules`, `dist`, the
  operator's compiled `bin/`), and fixed the two path references the folder
  move actually broke (`frontend/veltnex/vite.config.ts`'s build output
  path, and `.github/workflows/ci.yml`'s checkout/addons paths) — both
  verified working (see below).
