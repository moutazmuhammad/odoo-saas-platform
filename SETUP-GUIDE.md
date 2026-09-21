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

Seeded accounts and every flow you can test: **[`TEST-CLUSTER-SETUP.md`](TEST-CLUSTER-SETUP.md)**
§1 (this repo's dev environment normally runs as two `systemctl --user`
services, not the `nohup`-based flow below — check
`systemctl --user status saas-postgres.service saas-control-plane.service`
before assuming nothing is running yet). If you genuinely need the
ad-hoc/no-systemd flow instead:

```bash
# sibling layout: ../odoo18 (Odoo 18 source), ../odoo18-venv, ../saas-pg, ../saas-odoo
control-plane/scripts/devctl.sh up        # start PostgreSQL + Odoo -> http://127.0.0.1:8069
control-plane/scripts/devctl.sh seed      # load demo data (idempotent)
control-plane/scripts/devctl.sh status
control-plane/scripts/devctl.sh down
```

**Don't run both `devctl.sh up`/`down` and the systemd services against the
same ports/database at once** — `devctl.sh down` will stop the systemd-managed
Postgres out from under a live Odoo process (see `TEST-CLUSTER-SETUP.md` §1).

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

"alias kubectl='microk8s kubectl'" >> ~/.bashrc
source ~/.bashrc

microk8s kubectl get nodes     # sanity check
```

### 2.2 Build and push the operator image

```bash

sudo apt update && sudo apt install -y docker.io
sudo usermod -aG docker $USER && newgrp docker

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
detail (CRD fields, reconciliation flow, networking/database/backup design)
now lives directly in the source, not a separate doc: the CRD schema is
`compute/operator/api/v1alpha1/odooinstance_types.go` (every field has a
doc comment), reconciliation logic is `compute/operator/internal/controller/`,
and generated resources are `compute/operator/internal/resources/`.

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
  does).
- **Everything else operator/cluster-side** — check the reconciler logs
  (`microk8s kubectl -n odoo-system logs deploy/odoo-operator`) and the
  CR's own `status.conditions` (`microk8s kubectl describe odooinstance <name>`)
  first; each condition's `message` names exactly what it's waiting on.
- **Everything else control-plane-side (mock provisioning, seed data,
  crons)** — see `TEST-CLUSTER-SETUP.md`.

## Where this fits in the bigger picture

This guide covers **today's** manual, admin-UI-driven way of connecting one
region to one cluster. Kubernetes is the platform's only compute backend
(ssh_docker/Docker Compose was fully removed); `ROADMAP.md` §5 covers
what's still ahead (automating cluster registration, multi-cluster/multi-
region scale-out).
