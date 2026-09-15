# Odoo SaaS Platform (Odoo.sh-style hosting)

A monorepo consolidating this project's components, each in its own folder.
**Start with [`ROADMAP.md`](ROADMAP.md) — the single source of truth** for
current state, target architecture, and the phased plan going forward.

This repo was assembled from two prior working repos (fresh copy, no shared
git history): the existing Odoo.sh control plane, and the Kubernetes operator
built to become its new compute layer. Both prior repos still exist at their
original locations if their history is ever needed; this repo is the
going-forward source of truth.

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
│                    which is exactly what keeps it unaffected by the
│                    ongoing compute-layer migration (see ROADMAP.md §5).
│
├── compute/         The Kubernetes operator (OdooInstance CRD + controller):
│                    provisions/deletes/backs up/restores tenant instances.
│                    This is the new Compute microservice — the Control
│                    Plane talks to it exclusively through the Kubernetes
│                    API (create/patch/delete OdooInstance custom
│                    resources), never SSH. See compute/docs/architecture.md
│                    (English) / architecture.ar.md (Arabic).
│
├── ROADMAP.md   The single source of truth (read this first).
│
└── .github/workflows/ci.yml   One pipeline, one job per component.
```

## Where to start

- **The plan**: [`ROADMAP.md`](ROADMAP.md) — current state by subsystem
  (with confidence-level tags on every claim), target architecture
  (including the Prometheus/Grafana/Loki observability stack), a phased
  roadmap with dependencies/priorities/acceptance criteria, a risk register,
  and a summary of the architecture decisions worth preserving. Read this
  first.
- **Architecture references**: `control-plane/docs/architecture/` (control
  plane's original spec + as-built deltas) and `compute/docs/architecture.md`
  (English) / `architecture.ar.md` (Arabic) for the compute microservice's
  full design.
- **Compute service quick start**: `compute/README.md`.

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

`.github/workflows/ci.yml` runs six independent jobs on every PR: `spa`
(typecheck+build), `odoo-tests` (the full `saas_core`/`saas_website` test
suite), `csrf-lint` (static safety check on `csrf=False` routes),
`secret-scan` (gitleaks), `compute` (operator build+vet+test), and
`image-scan` (trivy against the operator + backup-tool images).

## Known gaps

See [`ROADMAP.md`](ROADMAP.md) §3 for the full, current, tagged-by-confidence
list of open findings across security, scalability, provisioning
reliability, tenant isolation, and UX — and §5 for what's planned to close
each of them.
