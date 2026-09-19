# Setup guide: control plane + a real Kubernetes cluster, end to end

This is the one place that walks through **all three** things you need to
get a real, non-mocked instance running: the SaaS control plane (Odoo), a
client's Kubernetes cluster (the compute backend), and the step where you
link them together. Each piece also has its own deeper reference doc —
this guide's job is the connecting narrative, not to duplicate them.

If you only want to click around the admin/customer UI with realistic seed
data and don't care about a real cluster, stop after
[Part 1](#part-1--run-the-control-plane-mock-provisioning-only) — that's the
fast path and it's what most local development actually uses.

---

## Part 0 — Prerequisites

| For | You need |
|---|---|
| Control plane | Linux/macOS, Python 3.12, a local/userspace PostgreSQL, Odoo 18 source (a sibling checkout — see below) |
| Kubernetes cluster (this guide uses microk8s) | A Linux host (VM or bare metal), 2+ CPU / 4GB+ RAM free, root/sudo |
| Linking them | `kubectl` access to the cluster, Helm 3, the cluster's kubeconfig file |

Nothing here needs a cloud account — microk8s on a single VM (or the same
machine that runs the control plane, if it has the spare resources) is
enough to go through every step below for real.

---

## Part 1 — Run the control plane (mock provisioning only)

This gets the whole customer-facing site + admin backend running against
seed data, with **no real hosts or cluster** — deploy/start/stop actions
enqueue jobs that fail against a mock host, but billing, checkout, the
portal, and the admin UI are all fully exercisable. This is the fastest way
to get oriented before touching real infrastructure.

Full instructions, seeded accounts, and every flow you can test:
**[`control-plane/docs/LOCAL-TESTING.md`](control-plane/docs/LOCAL-TESTING.md)**.
Short version:

```bash
# sibling layout: ../odoo18 (Odoo 18 source), ../odoo18-venv, ../saas-pg, ../saas-odoo
control-plane/scripts/devctl.sh up        # start PostgreSQL + Odoo -> http://127.0.0.1:8069
control-plane/scripts/devctl.sh seed      # load demo data (idempotent)
control-plane/scripts/devctl.sh status
control-plane/scripts/devctl.sh down
```

Want this to survive logouts and restart on failure instead of `nohup`-based
dev mode? See
[`control-plane/docs/LOCAL-RUNTIME-SYSTEMD.md`](control-plane/docs/LOCAL-RUNTIME-SYSTEMD.md)
(two `systemctl --user` services). **Don't run both `devctl.sh` and the
systemd services against the same ports/database at once.**

Log in at `http://127.0.0.1:8069/web` (`admin` / `admin` on a freshly seeded
DB) and open the **SaaS Manager** app — that's the admin backend the rest of
this guide operates in.

---

## Part 2 — Stand up a real Kubernetes cluster (the client cluster)

"Client cluster" here means whatever Kubernetes cluster will actually host
tenant Odoo instances — a customer's own cluster, or one you run yourself.
This guide uses **microk8s** as a concrete, single-VM example; any
conformant cluster works the same way from the operator's point of view.

### 2.1 Install microk8s and the addons the operator needs

```bash
sudo snap install microk8s --classic
sudo usermod -a -G microk8s $USER && newgrp microk8s
microk8s status --wait-ready

# DNS (required), storage (for PVCs), and one ingress path:
microk8s enable dns hostpath-storage
# Gateway API path (preferred by the operator):
microk8s enable gateway-api
# — or, if you'd rather use classic Ingress instead of Gateway API:
microk8s enable ingress

microk8s kubectl get nodes     # sanity check
```

### 2.2 Build and push the operator image

```bash
cd compute/operator
make docker-build IMG=registry.example.com/odoo-saas/operator:v0.1.0
make docker-push  IMG=registry.example.com/odoo-saas/operator:v0.1.0
# No registry handy? For a single-node microk8s, `microk8s ctr image import`
# after `docker save` works too — see microk8s's own image-import docs.
```

### 2.3 Install the operator (CRD + RBAC + Deployment) via Helm

```bash
cd compute
microk8s helm3 upgrade --install odoo-operator charts/odoo-operator \
  --namespace odoo-system --create-namespace \
  --set image.repository=registry.example.com/odoo-saas/operator \
  --set image.tag=v0.1.0
```

### 2.4 Sanity-check with a manual tenant (bypassing the control plane)

Before wiring the control plane in, prove the cluster/operator work on
their own — this isolates "is my cluster set up right" from "is the control
plane talking to it right":

```bash
microk8s kubectl apply -f compute/examples/odoo-instance-acme.yaml
microk8s kubectl get odooinstance customer-acme -w   # watch it reach Ready
microk8s kubectl describe odooinstance customer-acme
microk8s kubectl delete odooinstance customer-acme   # tear it back down
```

If that reaches `Ready` with a real URL, the cluster side is done. Full
detail (CRD fields, reconciliation flow, networking/database/backup design,
every trade-off): **[`compute/docs/architecture.md`](compute/docs/architecture.md)**
(also available in Arabic: `architecture.ar.md`); day-to-day operator
commands: **[`compute/README.md`](compute/README.md)**.

### 2.5 Get the cluster's kubeconfig

```bash
microk8s config > /path/to/cluster.kubeconfig
```

For a customer-owned cluster instead of microk8s, this is just whatever
kubeconfig they hand you (scoped to a service account with the RBAC the
operator's `ClusterRole` needs — see `compute/charts/odoo-operator` for the
exact permissions, no wildcards/cluster-admin required).

---

## Part 3 — Connect the two: register the cluster in the control plane

This is the step that's specific to this platform (not documented
elsewhere): telling the Odoo control plane that a Kubernetes cluster exists
and how to reach it, so `saas.instance` records can actually be deployed
onto it via the `KubernetesDriver`.

The model chain is: **one region = one cluster = one kubeconfig.** A
`saas.server` record with `compute_driver = 'kubernetes'` is *not* a literal
machine — it's a capacity/allocation placeholder pointing at that region;
the real connection lives on the region.

### 3.1 Upload the kubeconfig onto a Region

In the admin backend: **SaaS Manager ▸ Infrastructure ▸ Regions**.

- Open (or create) the region this cluster serves, e.g. "EU" / "microk8s-dev".
- On the **Kubeconfig** field, create a new `saas.kubeconfig` record and
  upload `cluster.kubeconfig` from step 2.5.
- Save. The kubeconfig is captured and **encrypted at rest**
  (`saas.kubeconfig`, the same upload-only, encrypted-column pattern used
  for SSH private keys — the cleartext upload is cleared immediately after
  save; only the encrypted copy persists). Set `saas_secret_key` /
  `SAAS_SECRET_KEY` beforehand if you want this encrypted rather than
  stored as-is (see `control-plane/saas_core/fields.py`'s `EncryptedChar`).
- While you're on the region, also set **Ingress Host** / **Ingress Port**
  if this cluster's shared Gateway/Ingress isn't on the default (matches
  what you enabled in step 2.1 and whatever hostname/DNS routes to it).

### 3.2 Register a "compute" entry for the cluster

**SaaS Manager ▸ Infrastructure ▸ Compute ▸ Kubernetes Clusters** ▸ New:

- **Deployment Type**: `Kubernetes (Cluster)`.
- **Region**: the region from 3.1 (this is what ties the server record back
  to the kubeconfig — the Kubernetes Configuration/Network/Docker-specific
  fields on this form are hidden for this deployment type, since a
  Kubernetes entry's real connection details live on the region, not here).
- Save.

### 3.3 Deploy an instance onto it

Provision/deploy a `saas.instance` (via the portal's trial/checkout flow,
or directly in **SaaS Manager ▸ Instances**) with its Compute Target set to
the server from 3.2. `KubernetesDriver._client()` loads the region's
kubeconfig and creates the tenant's `OdooInstance` custom resource; the
operator takes it from there exactly as in step 2.4's manual test.
Check progress the same way:

```bash
microk8s kubectl get odooinstance -A -w
```

### 3.4 Moving an existing Compose-based tenant onto Kubernetes

If a tenant already exists on a Docker Compose host and you want to migrate
it onto the cluster instead of provisioning fresh, that's
`DataService.migrate_to_kubernetes` (`control-plane/saas_core/dataservice/service.py`) —
callable from the instance record once its target region has a working,
`kubeconfig_loaded` kubeconfig (the same precondition step 3.1 satisfies).
This direction (Compose → Kubernetes) is built and live-verified; the
reverse is not.

---

## Troubleshooting

- **"Kubernetes cluster hosting this region has no kubeconfig configured"**
  (or the deploy silently stays pending) — the region in 3.1 has no
  kubeconfig, or the upload didn't decrypt cleanly. Re-check
  `kubeconfig_loaded` on the `saas.kubeconfig` record and that
  `SAAS_SECRET_KEY` (if set) hasn't changed since the upload.
- **Operator RBAC `forbidden` errors** — the kubeconfig's service account is
  missing a permission the operator's `ClusterRole` expects; compare against
  `compute/charts/odoo-operator`'s generated RBAC.
- **PVC stuck `Pending`** — expected until a pod actually consumes it if
  your StorageClass uses `WaitForFirstConsumer` (microk8s's `hostpath-storage`
  does); see `compute/docs/architecture.md` §4 ("A gating deadlock this
  design avoids").
- **Everything else operator/cluster-side** — `compute/docs/architecture.md`
  §19 has a full failure/recovery table.
- **Everything else control-plane-side (mock provisioning, seed data,
  crons)** — see `control-plane/docs/LOCAL-TESTING.md`'s Caveats section.

## Where this fits in the bigger picture

This guide covers **today's** manual, admin-UI-driven way of connecting one
region to one cluster. `ROADMAP.md` §5 covers where this is headed
(automating cluster registration, multi-cluster/multi-region scale-out, and
eventually retiring the Docker Compose backend once Kubernetes carries
production traffic — see `ROADMAP.md`'s current-state confidence tags for
exactly how far along that migration is).
