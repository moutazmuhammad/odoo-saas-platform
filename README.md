# Odoo SaaS Platform

VELTNEX — a multi-tenant, Odoo.sh-style hosting platform. Customers self-serve
a fully managed Odoo instance (trial, paid hosting, or a vertical "service"
like a pharmacy/clinic product), with billing, wallets, backups, and a
customer portal, on top of two interchangeable deployment backends: legacy
SSH/Docker Compose hosts and a Kubernetes operator (the platform's target
end-state — see [`ROADMAP.md`](ROADMAP.md)).

**Start with [`ROADMAP.md`](ROADMAP.md) — the single source of truth** for
current state (by subsystem, confidence-tagged), target architecture, the
phased plan, risk register, and ADR summary.

**New to this repo and want to actually run it end-to-end** (control plane +
a real Kubernetes cluster + wiring them together)? See
[`SETUP-GUIDE.md`](SETUP-GUIDE.md).

This repo was assembled from two prior working repos (fresh copy, no shared
git history): the existing Odoo.sh control plane, and the Kubernetes operator
built to become its new compute layer. Both prior repos still exist at their
original locations if their history is ever needed; this repo is the
going-forward source of truth.

## What it does

- **Customer-facing**: marketing site, free trials, a slider/tier hosting
  configurator with live pricing (per-unit rates never reach the browser),
  checkout with a simulated or real payment provider, wallet-backed
  auto-renewal, and a portal (projects, environments, backups, invoices,
  metrics, shell/SQL access).
- **Admin-facing**: a Dashboard (profitability by plan/instance/backend), full
  instance lifecycle (provision/start/stop/suspend/migrate), plan/add-on/
  compute-tier/support-plan catalog management with real cost-vs-price
  visibility, and infrastructure registries (servers, regions, domains, Odoo
  versions).
- **Two deployment backends, one data model**: a `saas.server` is either a
  Docker Compose host (SSH-provisioned containers, restic backups, one
  container per tenant) or a Kubernetes cluster (via a custom `OdooInstance`
  operator — namespace-per-tenant, CloudNativePG/managed/external database
  modes, Gateway-API ingress, replica-based scale tiers). Which backend an
  instance runs on is a per-instance allocation decision, not a platform-wide
  switch — see `SETUP-GUIDE.md` for how a region/cluster is wired in.

## Layout

```text
.
├── control-plane/   Odoo 18 control plane: saas_core (models/business logic)
│                    + saas_billing (pricing engine, wallet, plans, add-ons,
│                    support plans, payment) + saas_website (public/portal
│                    API + QWeb templates that host the SPA). Owns
│                    tenant/billing records, auth, the Postgres-backed job
│                    queue, and the public /saas/api/v1/* JSON API.
│                    See control-plane/docs/.
│
├── frontend/        veltnex: the React/Vite/TypeScript SPA served by
│                    control-plane/saas_website. Talks only to
│                    /saas/api/v1/* — no direct infrastructure awareness.
│
├── compute/         The Kubernetes operator (OdooInstance CRD + controller):
│                    provisions/deletes/backs up/restores tenant instances.
│                    The Compute microservice — the control plane talks to
│                    it exclusively through the Kubernetes API (create/patch/
│                    delete OdooInstance custom resources) via a stored,
│                    encrypted-at-rest kubeconfig, never SSH — Kubernetes is
│                    the platform's only compute backend. CRD reference:
│                    compute/operator/api/v1alpha1/odooinstance_types.go.
│
├── ROADMAP.md       The single source of truth (read this first).
├── SETUP-GUIDE.md   Full run-it-yourself walkthrough: control plane, a real
│                    Kubernetes cluster, and connecting the two.
│
└── .github/workflows/ci.yml   One pipeline, one job per component.
```

## Where to start

- **The plan**: [`ROADMAP.md`](ROADMAP.md) — current state by subsystem
  (with confidence-level tags on every claim), target architecture, a phased
  roadmap with dependencies/priorities/acceptance criteria, a risk register,
  and a summary of the architecture decisions worth preserving.
- **Running it**: [`SETUP-GUIDE.md`](SETUP-GUIDE.md) — start-to-finish setup
  for the SaaS control plane, a client Kubernetes cluster, and linking them.
- **Architecture references**: `control-plane/docs/architecture/` (control
  plane's original spec + as-built deltas) and the compute microservice's
  CRD/controller source itself (`compute/operator/api/v1alpha1/`,
  `compute/operator/internal/controller/`) for its full design.

## Local development

- **Control plane** (Odoo): normally runs as two `systemctl --user` services
  (`saas-postgres`, `saas-control-plane`) — see
  [`TEST-CLUSTER-SETUP.md`](TEST-CLUSTER-SETUP.md) §1 for seeded demo
  accounts, every flow you can exercise locally (mock provisioning — no
  real hosts/cluster needed to test billing/portal/admin), and this
  runtime's layout. `control-plane/scripts/devctl.sh` also works standalone
  (a throwaway/reset-friendly `nohup`-based dev loop) — **don't run both
  against the same ports/database at once**.
- **Frontend** (SPA): `cd frontend/veltnex && npm ci && npm run dev` (proxies
  API calls to a locally running Odoo on `:8018` — see `vite.config.ts`).
  `npm run build` writes straight into
  `control-plane/saas_website/static/spa/`.
- **Compute** (operator): `cd compute/operator && make test` (unit tests +
  envtest); `make manifests generate fmt vet` after changing the CRD types;
  see `SETUP-GUIDE.md` for a full local-cluster (microk8s) walkthrough and
  wiring a running cluster into the control plane.

## CI

`.github/workflows/ci.yml` runs six independent jobs on every PR: `spa`
(typecheck+build), `odoo-tests` (the full `saas_core`/`saas_billing`/
`saas_website` test suite), `csrf-lint` (static safety check on `csrf=False`
routes), `secret-scan` (gitleaks), `compute` (operator build+vet+test), and
`image-scan` (trivy against the operator + backup-tool images).

## Known gaps

See [`ROADMAP.md`](ROADMAP.md) §3 for the full, current, tagged-by-confidence
list of open findings across security, scalability, provisioning
reliability, tenant isolation, and UX — and §5 for what's planned to close
each of them.
