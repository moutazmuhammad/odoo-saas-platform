# Cluster Capacity Auto-Expansion Plan

Oct 11, 2026 · @Mostafa

## Summary

A new customer is never blocked by a full cluster: before placing a tenant, the platform checks real node capacity, and if the node pool is at its maximum it raises the maximum through the provider's API, up to a ceiling you set, so the autoscaler can add the node.

- **Live capacity, not typed numbers.** Today each cluster has hand-entered CPU/RAM limits. The platform will read the real nodes, what is already reserved on them, and the autoscaler's current and maximum node count.
- **Raise the maximum automatically.** When no node can fit the new tenant and the pool is at its maximum, the platform raises it in steps (for example +2) through the provider adapter. Raising the maximum costs nothing by itself; you pay only for nodes the autoscaler actually adds.
- **Warm spare capacity.** A small, lowest-priority placeholder keeps room for one typical tenant ready, so a new customer starts in seconds instead of waiting 3–5 minutes for a node to boot.
- **Hard ceiling and alerts.** A per-cluster node ceiling (and the provider's own account limits) caps cost; every raise and every near-limit event posts to Teams.
- **Any Kubernetes.** The logic is provider-neutral; a small adapter per provider changes the maximum: DOKS first, then Cluster API, Karpenter, EKS, GKE, AKS, and a manual mode for clusters without an API.

## How it works today

The platform decides placement from numbers typed into each cluster record, while the real limit is the node pool's autoscaler maximum, which nothing in the platform reads or changes.

| Item | Today | Source |
| --- | --- | --- |
| DOKS pool (fra1) | One pool `node`, size `s-2vcpu-4gb` (1.9 CPU usable per node), autoscaler min 0 / **max 2**, 1 node running | live `cluster-autoscaler-status` ConfigMap, node labels |
| Capacity check | `saas.server._has_capacity_for` compares the sum of tenant packages against hand-entered `max_cpu_cores` / `max_ram_gb` / `max_instances` | `saas_core/models/saas_server.py` |
| When full | Overcommit if allowed, otherwise the tenant waits in `pending_provision`; a cron retries with back-off and escalates after 24 h | `saas_instance.py` `_allocate_servers`, `_cron_retry_pending_provision` |
| Placement rule | A tenant's Odoo pods and its PostgreSQL share one `ReadWriteOnce` volume, so the whole tenant lives on **one node**; a rollout needs room for a second Odoo pod on that same node | `setup/01-ARCHITECTURE.md` §4.1 |

**What happened on 2026-10-10.** The pool was at its maximum and CPU requests reached 91% of the node. A new tenant's pod and a production rollout both sat `Pending` for about 27 minutes. The autoscaler logged “max node group size reached” for the new tenant, and “node(s) didn't match pod affinity rules” for the rollout, because that rollout must land on the node that holds the tenant's volume.

Two lessons shape the design:

- **A new tenant** needs room on any node, or a new node. Raising the maximum solves it.
- **A rollout of an existing tenant** needs room on its own node. A new node never helps it, so every node must keep rollout headroom.

## Design

A capacity controller in the control plane answers one question before every new tenant, resize or new environment: “is there a node this tenant fits on, or can the pool grow to make one?”, and acts on the answer.

**1. Read live capacity** (every minute, cached, per cluster):

- Nodes: allocatable CPU and memory per node, minus the requests already placed on it.
- Autoscaler: current node count, minimum and maximum per node pool, and whether a scale-up is in progress. Read from the standard `cluster-autoscaler-status` ConfigMap in `kube-system`, which DOKS and every cluster-autoscaler install publish, plus the provider adapter.
- Pending pods and why they are pending.

**2. Fit check for a new tenant.** The whole tenant package (Odoo pods, cron, PostgreSQL) must fit on **one** node, plus **rollout headroom**: room for one more Odoo pod, so a later update never gets stuck. The package sizes already exist (`saas.plan._package()`).

**3. Decide:**

1. **A node has room** → place the tenant now.
2. **No node has room, but the pool is below its maximum** → place the tenant; its pods wait a few minutes while the autoscaler adds a node. The customer sees “Preparing”.
3. **The pool is at its maximum, below your ceiling** → raise the maximum by one step (for example +2 nodes) through the provider adapter, post to Teams, then continue as in step 2.
4. **At the ceiling, or the provider refuses** (for example the account's Droplet limit) → the tenant waits in `pending_provision` exactly as today, and Teams gets a critical alert with the reason and the action needed.
5. **The package is larger than one node of this pool** → adding nodes cannot help; the tenant needs a pool with bigger machines. The platform refuses to sell that plan on that cluster and alerts.

**4. Warm spare capacity.** A Deployment of placeholder pods with the lowest priority (a negative `PriorityClass`) reserves room for one typical tenant. When a real tenant arrives, Kubernetes evicts the placeholder at once and the tenant starts immediately; the placeholder goes back to pending, so the autoscaler adds a node in the background. Size and count are a setting per cluster (default: one typical package; zero to switch off).

**5. Rollout headroom per node.** Every node keeps room for one extra Odoo pod of its largest tenant, enforced by the fit check in step 2. This is what fixes the stuck rollouts from 2026-10-10, which a bigger maximum alone would not.

**6. Shrinking.** The autoscaler removes empty nodes as today. The platform never lowers the maximum below the running node count; a weekly job can lower an unused raised maximum back toward its normal value, which changes nothing about cost (only nodes cost).

**Where it lives:** a new `saas.capacity` service in `saas_core`, called from `_allocate_servers` (new tenants), plan changes and environment creation, and from the existing pending-provision cron. The hand-entered `max_cpu_cores` / `max_ram_gb` remain as an optional extra cap.

## Provider adapters

Only one small piece is provider-specific: reading a pool's limits and changing its maximum. Everything else uses plain Kubernetes, so moving to another provider means writing one adapter.

Each cluster record (`saas.server`) gets a provider type, its credentials (encrypted with the existing helper), the node pool to grow and its ceiling. The adapter interface:

- `pools()` → for each pool: id, machine size, current, minimum and maximum nodes.
- `set_max(pool, new_max)` → raise the maximum; returns the provider's limit error if any.
- `limits()` → account limits that cap growth (for example DigitalOcean's Droplet limit).

| Provider | How the maximum is changed | Notes |
| --- | --- | --- |
| DigitalOcean DOKS (now) | DigitalOcean API: update the node pool with `auto_scale`, `min_nodes`, `max_nodes` (`PUT /v2/kubernetes/clusters/{cluster}/node_pools/{pool}`, as `doctl kubernetes cluster node-pool update --max-nodes`) | Capped by the account's Droplet limit (25 by default, minus running Droplets); needs a DigitalOcean API token. Send the full pool settings, since an update may replace omitted fields |
| Cluster API (any self-managed cluster) | Annotation `cluster.x-k8s.io/cluster-api-autoscaler-node-group-max-size` on the MachineDeployment | Works on bare metal and most clouds through Cluster API |
| Karpenter | Raise the NodePool's `spec.limits` (CPU, memory) | Karpenter picks machine sizes itself |
| AWS EKS managed node groups | `UpdateNodegroupConfig` with a new `maxSize` |  |
| Google GKE | Node pool autoscaling `maxNodeCount` |  |
| Azure AKS | Node pool `maxCount` |  |
| Manual (MicroK8s, no API) | Cannot grow; the platform only alerts with the exact action needed | Today's behaviour, now with a clear alert |

The DOKS endpoint path and body are confirmed through DigitalOcean's tools (`doctl`, Terraform, Ansible) but must be tested against the live account before Phase 2 ([doctl node-pool update](https://docs.digitalocean.com/reference/doctl/reference/kubernetes/cluster/node-pool/update), [Terraform node pool](https://docs.digitalocean.com/reference/terraform/reference/resources/kubernetes_node_pool/), [DOKS autoscaling](https://docs.digitalocean.com/products/kubernetes/how-to/autoscale/)).

## Safety limits, cost and alerts

The platform may raise the maximum on its own, but never past limits you set, and you see every change in Teams.

- **Node ceiling per cluster** (for example 10): the maximum is never raised above it.
- **Step size** (for example +2 per raise) and a **cool-down** (one raise per 10 minutes), so a burst of sign-ups cannot run away.
- **Monthly cost guard:** ceiling × node price is shown next to the setting (an `s-2vcpu-4gb` node costs about $24/month), so the worst case is known up front.
- **Never shrink below what runs:** the maximum never drops under the current node count.
- **Alerts to Teams:** every raise (old → new maximum, reason, tenant), reaching 80% of the ceiling, reaching the ceiling, a provider refusal (with the provider's message, for example the Droplet limit), a tenant waiting more than 15 minutes, and a stuck rollout.
- **Audit log:** every capacity decision and every change to a pool.
- **Report:** the existing cluster dashboard gains nodes in use, maximum, ceiling, spare room and pending tenants, from the Prometheus data already collected.

## Phases, tests and acceptance

1. **Phase 1 — See capacity.** Live capacity reader, fit check with rollout headroom, dashboard and alerts. No automatic changes yet; it already explains every pending tenant.
2. **Phase 2 — DOKS adapter and automatic raise.** Ceiling, step, cool-down, audit and Teams. Test on the DOKS cluster with a temporary low ceiling.
3. **Phase 3 — Warm spare capacity.** Placeholder pods with the lowest priority.
4. **Phase 4 — Other providers.** Cluster API and Karpenter first (they cover most self-managed and AWS setups), then EKS, GKE, AKS as needed.

**Tests:**

- [ ] A tenant that fits is placed immediately; one that does not triggers exactly one raise.
- [ ] At the ceiling: no raise, the tenant waits in `pending_provision`, Teams gets a critical alert.
- [ ] Provider refusal (Droplet limit): reported with the provider's message, nothing half-changed.
- [ ] Ten sign-ups within a minute: one raise per cool-down, never past the ceiling.
- [ ] With warm spare capacity on, a new tenant becomes ready without waiting for a new node.
- [ ] A rollout of an existing tenant never waits for capacity on its node (rollout headroom).
- [ ] A plan larger than one node is refused on that cluster with a clear message.
- [ ] The maximum is never lowered below the running node count.

**Acceptance:** with the pool at its maximum and the ceiling above it, a new customer signs up and reaches a running server with no manual step; Teams shows the raise; the pool's maximum in DigitalOcean matches what the platform reports.

## Open questions

1. **Ceiling for the DOKS cluster:** how many nodes at most (each `s-2vcpu-4gb` about $24/month)? And should we ask DigitalOcean to raise the account's Droplet limit (25 by default) now?
2. **Step size:** +1 or +2 nodes per raise?
3. **Warm spare capacity:** keep room for one typical tenant ready (one node's worth of cost while idle), or switch it off and accept a 3–5 minute wait for a new customer when the cluster is full?
4. **Bigger plans:** should the cluster get a second pool with larger machines for packages that do not fit on an `s-2vcpu-4gb` node, or should such plans be limited to what one node holds?
