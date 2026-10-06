# Architecture

How the platform works today: what runs where, what happens when a customer orders, and the design rules every tenant follows (one pod, one volume, vertical plan changes). Read this before the setup guides.

Code references are from the repo root.

---

## 1. The pieces

```
 customer browser
        │ https://saas.example.com          (portal SPA + JSON API + Odoo backend)
        ▼
 ┌──────────────── SaaS server (control plane) ────────────────┐
 │ Odoo 18 + saas_core, saas_billing, saas_website, saas_iam   │
 │ React SPA (frontend/veltnex → saas_website/static/spa)      │
 │ saas-jobs worker (durable jobs: deploy, restore, backup...) │
 │ PostgreSQL (the platform's own database)                    │
 └──────────────────────────────┬──────────────────────────────┘
                                │ kubeconfig (ServiceAccount token), one per cluster
                                ▼
 ┌──────────────── Kubernetes cluster (one or many) ───────────┐
 │ odoo-operator (odoo-system)  ← watches OdooInstance CRs     │
 │ Traefik (ingress) + cert-manager (Let's Encrypt)            │
 │ Prometheus (monitoring), read through the API proxy         │
 │                                                             │
 │ per tenant: namespace odoo-tenant-<name>                    │
 │   Odoo pod (web + cron sidecar [+ shell])                   │
 │   PostgreSQL pod                                            │
 │   one data PVC shared by both                               │
 │   Ingress + TLS certificate, backup CronJob → S3            │
 └─────────────────────────────────────────────────────────────┘
 customer's Odoo: https://<sub>.apps.example.com → Traefik → tenant pod
```

| Part | Code | Role |
|---|---|---|
| `saas_core` | `control-plane/saas_core` | Instances, clusters (`saas.server`), regions, base domains, Odoo versions, plans' resource package, durable job queue (`saas.job`), Kubernetes driver, builds, backups, availability. |
| `saas_billing` | `control-plane/saas_billing` | Pricing, invoices, wallet, payments, renewals. |
| `saas_website` | `control-plane/saas_website` | Portal routes, JSON API, registration, serves the built SPA. |
| `saas_iam` | `control-plane/saas_iam` | Project members, groups and per-environment grants (`docs/iam.md`). |
| `saas_demo_data` | `control-plane/saas_demo_data` | Optional test catalog (versions, product, plans). Not for production. |
| SPA | `frontend/veltnex` | Customer UI (React + Vite), built into `control-plane/saas_website/static/spa`. |
| Job worker | `control-plane/saas_core/cli/saas_jobs.py` (`odoo-bin saas-jobs`) | Runs queued jobs outside Odoo's request/cron time limits. Without it, jobs run inside the web server under its limits. |
| Operator | `compute/operator` (Go), chart `compute/charts/odoo-operator` | Turns each `OdooInstance` into a namespace, PostgreSQL, PVC, Odoo pod, Ingress, backup CronJob, init/update/restore Jobs. |
| Backup tool | `compute/tools/backup-tool` | Image used by backup and restore Jobs (pg_dump + filestore → object storage via rclone). |

The control plane never SSHes to a cluster. Everything goes through the Kubernetes API with the cluster's kubeconfig.

---

## 2. End-to-end flows

### 2.1 Order → running tenant

1. The customer picks a product/plan, region, Odoo version and subdomain in the SPA and pays (or starts a trial).
2. The paid instance queues a deploy job (`saas.job`); the job worker picks it up.
3. **Allocation** (`saas.instance._allocate_servers`): the base domain pins the cluster (its wildcard DNS points at that cluster's load balancer); otherwise the least-loaded healthy cluster in the instance's region. Clusters marked unreachable are skipped. No capacity → the instance waits as pending.
4. The Kubernetes driver (`saas_core/drivers/kubernetes_driver.py`) creates the `OdooInstance` CR, sized from the plan's package (section 3.2).
5. The operator reconciles: namespace with `restricted` Pod Security + default-deny NetworkPolicy, ResourceQuota, PostgreSQL, the shared data PVC, the **init Job** (creates the database), then the Odoo Deployment, Service and Ingress. cert-manager issues the certificate.
6. The CR reaches phase `Ready`. The control plane then observes availability (section 6) and shows **Online** once the public health URL answers.

### 2.2 Git push → new image

1. A push (or Redeploy) queues a build. A Job in `odoo-builds` fetches the repos, builds `FROM` the version's Odoo image with BuildKit (no cache) and pushes `tenant-<sub>:<ver>-b<id>` to the cluster's registry.
2. The control plane patches the CR's image and the list of changed modules.
3. **Update gate:** the operator first runs an `odoo -u` Job against the new image. Only if it succeeds does it roll the Odoo pod; if it fails, the old image keeps serving.
4. The replacement pod starts beside the old one on the same node, and the old one stops once the new one is ready (zero-downtime update).

Details: [04-IMAGES-AND-REGISTRY.md](04-IMAGES-AND-REGISTRY.md), section 3.

### 2.3 Backups and restore

- Scheduled backups: a CronJob in the tenant namespace runs the backup tool and uploads a PostgreSQL dump + filestore archive to the object storage set in *Settings → SaaS Manager*.
- Restore: a restore Job on the instance (or a new instance restored from a backup). Customers can also upload a backup through a presigned URL.
- Object-storage backups are the recovery path when a volume or a cluster is lost.

---

## 3. One instance per tenant, vertical scaling

### 3.1 One Odoo pod

Each tenant runs **one Odoo pod** (web container + a smaller cron sidecar) and **one PostgreSQL instance**. There is no customer HA tier, no replica scaling, no autoscaling, and no separate cron Deployment.

- The driver always sets `spec.replicas: 1`; the CRD rejects raising replicas on an existing instance.
- A legacy instance with `replicas > 1` still works: the operator runs one pod, removes the old cron Deployment and PDB, and reports a `ReplicasIgnored` warning condition.
- Pod restarts, upgrades and node failures interrupt the tenant. Cluster HA (see the cluster guides) protects the platform, not a single tenant.

### 3.2 The package (plan sizing)

`saas.plan._package()` is the single source for deploy, resize, capacity, cost and customer metrics. From the plan's workers, CPU/RAM limits and storage it derives:

| Part | Sizing |
|---|---|
| Odoo web | the plan's CPU/RAM limits (defaults 1 CPU / 2 GiB) |
| Cron sidecar | 25% of the web limits, capped at 500m CPU / 512Mi |
| PostgreSQL | per worker (Settings → SaaS Manager → database per worker), with floors |
| Requests | 25% of each limit, with floors |
| Storage | the plan's storage + extra storage blocks |
| Namespace quota | the package + one surge Odoo pod + shell sidecar + Job headroom |

### 3.3 Cron sidecar

The web process runs with `--max-cron-threads=0`; the cron sidecar runs `--no-http --workers=0` with the configured cron thread count. Disabling cron threads removes the sidecar. Both share the image and the data directory; no extra PVC. The sidecar has no readiness probe, so a cron restart never takes the pod out of traffic. Package cost, quotas, capacity and usage metrics include it.

### 3.4 Plan changes

A plan change updates CPU/RAM requests/limits and Odoo worker settings on the existing workload (billing/resize path, no new image build). Storage can grow on the existing PVC but never shrink. The update rolls like any other: the node needs room for a second Odoo pod for a moment. That is rollout headroom, not an availability tier.

---

## 4. Storage

### 4.1 One data volume per tenant

New tenants get `spec.storage.sharedWithDatabase: true`. The operator creates **one** PVC (`odoo-filestore`) at `spec.storage.filestore.size` for the whole allowance:

- `postgres/` is mounted only in PostgreSQL (`/var/lib/postgresql/data`);
- `odoo/` is mounted in Odoo, shell, init, update, backup and restore containers.

A 10 GiB package requests one 10 GiB PVC. There is no fixed database/files split; WAL, sessions and other runtime files use the same space.

- The tenant storage quota is the allowance + 1 GiB headroom (a ceiling, not another disk).
- `ReadWriteOnce`: every consumer, including init/restore Jobs, runs on the node holding the volume. PostgreSQL's directory is mode 0700; all consumers use fsGroup 101 with `OnRootMismatch`. Verify your CSI driver keeps this.
- Supported only with the built-in `Managed` database. CloudNativePG and external databases keep their own storage. Helm-created tenants keep the separate layout unless `storage.sharedWithDatabase: true` is set at creation.
- The layout is immutable. Older tenants with separate volumes keep them. To migrate one: maintenance window, final backup, restore into a new shared-layout instance, verify, switch, and only then remove the old instance (check `Retain` PVs are not left billed).

### 4.2 Block-storage replicas

| Cluster | Storage | Replicas |
|---|---|---|
| MicroK8s HA | Longhorn | `REPLICAS` in the guide. **1** = low-cost policy: a volume on a lost node is down until the node returns, or lost with its disk (restore from S3). **2** survives one node; **3** survives one node during a rebuild. Each replica is a full copy. |
| MicroK8s single node | `hostpath-storage` or Longhorn with 1 replica | node-local |
| DOKS | `do-block-storage` | provider-managed network disks |

Provider minimum volume sizes, snapshots and backup storage are separate costs. Kubernetes cannot shrink a PVC.

### 4.3 Builds without cache

BuildKit scratch is a Job-local `emptyDir` capped at 20 GiB (requests 4 GiB ephemeral, limit 24 GiB). Builds use `--opt no-cache` and never import/export registry cache; scratch space goes away with the pod (Job TTL 1 hour). Keep that much free disk on nodes and limit concurrent builds. Tenants without Git repos deploy the plain Odoo image with no build.

Old installations may still have `buildkit-cache-*` PVCs in `odoo-builds`. Preview and delete them with:

```bash
python scripts/cleanup-legacy-build-caches.py --context <cluster-context>            # preview
python scripts/cleanup-legacy-build-caches.py --context <cluster-context> --delete   # delete unused ones
```

It only touches managed cache PVCs with no Pod/Job references, never tenant volumes.

---

## 5. Suspension and retained costs

Suspension scales the Odoo Deployment (with cron and shell sidecars) and the Managed PostgreSQL StatefulSet to zero.

- Backup schedules and unfinished backup jobs pause. Init/restore/update Jobs run to completion first; PostgreSQL stops only after them (the operator reports `Progressing` meanwhile).
- PVCs, database credentials and retained backups stay. StatefulSet claims use `Retain` on scale-down. CloudNativePG uses `cnpg.io/hibernation=on`. External databases are left running.
- The operator re-enforces suspension every 30 seconds.
- Resume: PostgreSQL first, then paused Jobs, backup schedules, and the Odoo pod. Interrupted backups restart under their retry policy.

Costs: the rate-card estimate gives a suspended tenant zero CPU/RAM cost but keeps storage cost (at least the package allowance). Fixed node/VPS charges do not fall; suspension frees capacity. No PVC is deleted automatically, so suspended tenants still pay for their volumes.

---

## 6. Availability (what "Online" means)

The instance `state` is lifecycle/billing intent. Customer-facing badges show **observed availability** (`saas.instance.runtime`, `saas_core/models/saas_instance_health.py`). A project's badge is its Production environment's.

| Status | Meaning |
|---|---|
| Online | A live Ready pod with the Odoo container ready **and** `GET /web/health?db_server_status=1` on the public URL returns 200 with `status: pass` and `db_server_status: true` |
| Starting / Stopping | Workload coming up / still shutting down |
| Unavailable | Missing/failed workload or a failed HTTP check |
| Unreachable | DNS, TLS or connection failure to the public URL |
| Unknown | Cluster API failure, or the observation is older than 120 s |
| Stopped / Suspended | Intended and confirmed |

- Checks verify TLS and never follow redirects.
- Status polls refresh with a 20-second cache; a one-minute cron refreshes up to 50 tenants (never-checked first).
- Observation never starts, stops or bills anything.

---

## 7. Upgrading from versions before saas_core 18.0.58 / operator 0.1.28

Those versions had replica tiers, separate PostgreSQL/Odoo volumes and BuildKit cache PVCs. The one-time migration (patching `replicas` to 1, applying quotas before the operator rollout, the module upgrade order, reviewing old tier invoices) is in git history:

```bash
git show 2750f58:setup/SINGLE-INSTANCE-ARCHITECTURE.md
git show 2750f58:setup/STORAGE-COST-FIX.md
```

New installations don't need it.
