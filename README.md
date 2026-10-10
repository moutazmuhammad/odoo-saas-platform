# VELTNEX: Odoo SaaS platform

VELTNEX is an Odoo hosting platform. Customers sign up, buy Odoo hosting (or a ready-made service built on Odoo), and get a **project** with a Production environment plus optional Staging and Development environments. Each environment is its own Odoo instance on Kubernetes. Customers deploy their own addons from Git, take and restore backups, invite teammates with per-environment permissions, and pay through invoices and a wallet.

This repo holds everything: the control plane (Odoo 18 addons + React SPA), the Kubernetes operator that runs tenants, and the setup guides.

---

## Architecture

```
 customer browser
     │ https://saas.example.com                        https://<sub>.apps.example.com
     ▼                                                                 │
 ┌──────────── SaaS server (control plane) ────────────┐               │
 │ Odoo 18 + saas_core, saas_billing,                  │               │
 │           saas_website, saas_iam                    │               │
 │ React SPA (frontend/veltnex, built into             │               │
 │   saas_website/static/spa)                          │               │
 │ saas-jobs worker (deploys, restores, backups)       │               │
 └───────────────────────┬─────────────────────────────┘               │
                         │ kubeconfig (one per cluster)                │
                         ▼                                             ▼
 ┌──────────── Kubernetes cluster(s) ──────────────────────────────────────────┐
 │ odoo-operator ── watches OdooInstance CRs                                   │
 │ Traefik ingress + cert-manager (Let's Encrypt)   Prometheus (metrics)       │
 │                                                                             │
 │ namespace odoo-tenant-<name>, one per environment:                          │
 │   Odoo pod (web + cron [+ shell])  PostgreSQL  one PVC  Ingress + TLS       │
 │   backup CronJob ──▶ S3 / GCS bucket                                        │
 └─────────────────────────────────────────────────────────────────────────────┘
```

The control plane only talks to the Kubernetes API. Full explanation: [setup/01-ARCHITECTURE.md](setup/01-ARCHITECTURE.md).

---

## Repository layout

| Path | What |
|---|---|
| `control-plane/saas_core` | Instances, clusters, regions, domains, plans' resource package, job queue, Kubernetes driver, builds, backups, availability |
| `control-plane/saas_billing` | Pricing, invoices, wallet, payments, renewals |
| `control-plane/saas_website` | Portal, JSON API, registration; serves the SPA from `static/spa` |
| `control-plane/saas_iam` | Project teams, roles, environment scopes |
| `control-plane/saas_demo_data` | Optional test catalog (not for production) |
| `control-plane/scripts` | `devctl.sh` (local dev), `seed_dev.py`, lints |
| `frontend/veltnex` | Customer SPA (React, Vite, Vitest); also the customer docs catalog |
| `compute/operator` | The Go operator (`OdooInstance` CRD + controller) |
| `compute/charts/odoo-operator` | Its Helm chart and CRDs |
| `compute/charts/vendor/*` | Vendored third-party charts: Traefik, cert-manager, Longhorn, Prometheus |
| `compute/charts/monitoring` | Prometheus values |
| `compute/charts/odoo-instance` | GitOps wrapper for one `OdooInstance` (optional; the control plane creates CRs directly) |
| `compute/tools/backup-tool` | Backup/restore Job image |
| `compute/examples` | Values for `doks/` and `test-cluster/`, a `test-registry/`, sample `OdooInstance` manifests |
| `scripts/` | Customer-docs generator, legacy build-cache cleanup |
| `setup/` | Setup and architecture guides (below) |
| `docs/` | IAM, frontend languages, Arabic glossary, generated customer docs |

---

## Key concepts

| Term | Meaning |
|---|---|
| **Region** (`saas.region`) | A location customers pick at checkout. Holds one or more clusters. |
| **Cluster** (`saas.server`) | One Kubernetes cluster: kubeconfig, TLS issuer, Prometheus, image registry. The UI calls it *Kubernetes Cluster*. |
| **Base domain** (`saas.based.domain`) | The tenant wildcard domain, e.g. `apps.example.com`. Its DNS points at one cluster's load balancer, so it pins instances to that cluster. |
| **Plan / package** (`saas.plan`) | Workers, CPU/RAM limits and storage. `_package()` turns them into the Odoo, cron and PostgreSQL resources, the volume and the namespace quota. |
| **Instance** (`saas.instance`) | One Odoo environment. A **project** is a Production instance plus its Staging/Development children (`parent_id`). |
| **OdooInstance** | The custom resource the control plane creates per instance; the operator reconciles it into the tenant namespace. |
| **Job worker** | `odoo-bin saas-jobs`: runs queued `saas.job` work (deploy, restore, backup, builds) outside Odoo's time limits. |
| **Availability** | What the customer sees: Online / Unavailable / Unreachable / Unknown / ..., observed from the pod and the public `/web/health` URL, separate from the billing state. |

---

## Documentation map

Read in this order:

1. [setup/01-ARCHITECTURE.md](setup/01-ARCHITECTURE.md): how it works, end-to-end flows, sizing, storage, suspension, availability.
2. [setup/02-SAAS-SERVER-SETUP.md](setup/02-SAAS-SERVER-SETUP.md): install the control plane on Ubuntu 24.04.
3. A tenant cluster, one of:
   - 3a. [setup/03a-MICROK8S-CLUSTER-SETUP.md](setup/03a-MICROK8S-CLUSTER-SETUP.md): your own servers, 3-node HA or the single-node test variant;
   - 3b. [setup/03b-DOKS-CLUSTER-SETUP.md](setup/03b-DOKS-CLUSTER-SETUP.md): DigitalOcean Kubernetes.
4. First tenant test: step 15 of 3a, step 12 of 3b.

References:

| Doc | For |
|---|---|
| [setup/04-IMAGES-AND-REGISTRY.md](setup/04-IMAGES-AND-REGISTRY.md) | Every image, building them, the tenant build pipeline, private registries |
| [setup/05-DEVELOPMENT.md](setup/05-DEVELOPMENT.md) | Local dev environment, tests, SPA build, customer docs |
| [setup/06-CICD.md](setup/06-CICD.md) | Automated builds/deployments, required GitHub secrets, server bootstrap and rollback |
| [setup/07-CICD-UI-WALKTHROUGH.md](setup/07-CICD-UI-WALKTHROUGH.md) | Step-by-step GitHub UI secrets setup and DigitalOcean/SaaS server commands |
| [docs/iam.md](docs/iam.md) | Project roles and permissions |
| [docs/frontend-languages.md](docs/frontend-languages.md) | English/Arabic in the SPA |
| [docs/arabic-localization-glossary.md](docs/arabic-localization-glossary.md) | Arabic terminology |
| [docs/customer/MAINTAINING.md](docs/customer/MAINTAINING.md) | Customer docs (generated from `frontend/veltnex/src/lib/docs-catalog.json`) |

---

## Versions

| Component | Version | Source |
|---|---|---|
| Odoo (control plane) | 18.0 | `__manifest__.py` versions |
| `saas_core` | 18.0.58.4.0 | `control-plane/saas_core/__manifest__.py` |
| `saas_billing` | 18.0.2.3.0 | `control-plane/saas_billing/__manifest__.py` |
| `saas_website` | 18.0.6.2.0 | `control-plane/saas_website/__manifest__.py` |
| `saas_iam` | 18.0.1.3.8 | `control-plane/saas_iam/__manifest__.py` |
| `saas_demo_data` | 18.0.1.0.0 | `control-plane/saas_demo_data/__manifest__.py` |
| Operator image | 0.1.29 | `compute/charts/odoo-operator/values.yaml` (`image.tag`) |
| Operator chart | 0.4.16 | `compute/charts/odoo-operator/Chart.yaml` |
| Backup tool image (default) | 0.1.5 | `compute/operator/internal/resources/backup.go` |
| Tenant Odoo versions accepted | 17.0, 18.0, 19.0, 20.0 | chart `supportedOdooVersions` |
