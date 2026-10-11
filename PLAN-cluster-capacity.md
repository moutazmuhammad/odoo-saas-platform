# Cluster Capacity Auto-Expansion Plan

Oct 11, 2026 · @Mostafa

## Summary

**Goal:** a new customer is provisioned automatically as long as capacity can be added; an existing customer can grow their resources without the system failing just because their current node is small; and cost stays under control, with larger nodes running only when a customer needs them (the Large pool scales from zero and the autoscaler removes nodes that become empty).

No new customer is blocked while the cluster can still grow, every existing customer always has room to update, and customers run on node pools sized for them; the autoscaler adds nodes on its own, so the platform only has to place tenants correctly and warn you early.

- **Set each pool's maximum once** to the most you are willing to pay for. Raising it automatically would do the same thing as setting it to that ceiling from the start, because the autoscaler only adds a node when a pod is waiting for one.
- **Real capacity, not typed numbers:** the platform reads the nodes and the autoscaler and stops refusing customers because of hand-entered limits.
- **Room to update, always:** every node keeps space for an update of the tenants on it.
- **Two pools by size, Standard and Large:** the platform picks the pool from the customer's plan; a customer who outgrows their node is moved to one that fits.
- **Four alerts** to Teams: provisioning stuck over 15 minutes, a stuck update, nodes near the maximum, and no pool that fits a customer.

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

## 1. Real capacity instead of typed numbers

The platform stops deciding from hand-entered CPU and RAM; it looks at what the cluster really has and at what the autoscaler can still add.

**What it reads** (every minute per cluster, cached):

- Each node: allocatable CPU and memory, minus the requests already placed on it, and its pool.
- Each pool: current, minimum and maximum node count and whether a scale-up is running, from the standard `cluster-autoscaler-status` ConfigMap in `kube-system`. DOKS publishes it, and so does any cluster that runs the Kubernetes cluster autoscaler.
- Pending pods and the reason they wait.

**How a new customer is placed** (`_allocate_servers`, through a new `saas.capacity` service):

1. Pick the pool for the plan (section 3).
2. **A node in that pool has room** for the whole package plus its update room (section 2) → provision now.
3. **No node has room, but the pool is below its maximum** → provision anyway. The pods wait a few minutes while the autoscaler adds a node, and the customer sees “Preparing”. This is the change: today such a customer is refused into `pending_provision`.
4. **The pool is at its maximum** → the customer waits in `pending_provision` as today, and you get the “nodes at the maximum” alert so you can raise it.
5. **No pool has nodes big enough for the package** → the customer is not provisioned and you get the “no node fits” alert.

The hand-entered `max_cpu_cores` / `max_ram_gb` stay only as an optional extra cap, empty by default.

## 2. Room to update an existing customer

An update (new code, plan change, platform add-on change) starts a second Odoo pod next to the old one on **the same node**, because the tenant's volume is attached there. A new node cannot help it, so the room must already be on that node.

- **Update room per node:** a node only accepts a new tenant if, after placing it, it still has room for one extra Odoo pod of its largest tenant. The fit check in section 1 enforces this.
- **Limit simultaneous updates:** some changes (a new platform add-on, an operator upgrade) update every tenant at once. The operator will roll at most one tenant per node at a time, and the next only after the previous is ready, so the update room is never needed twice on one node.
- **If an update still waits for room** (for example after a manual change), the “stuck update” alert fires after 10 minutes with the node and tenant named. The old pod keeps serving meanwhile, so the customer has no downtime.

## 3. Node pools by size

A few pools by size class, not a pool per customer; the platform places each customer in the class that fits their plan.

| Pool | Machine (DOKS example) | For | Rule |
| --- | --- | --- | --- |
| Standard | `s-2vcpu-4gb` (today's pool) | Small and medium plans | Package + update room fits in about half a node, so two or more tenants share a node |
| Large | for example `s-4vcpu-8gb` or `g-4vcpu-16gb` | Plans with more workers or RAM | Anything that does not fit the Standard rule |

- **Labels:** each pool's nodes carry a label such as `saas.platform/pool=standard` or `large`. DOKS sets node pool labels at creation; on any other Kubernetes the same label is set on the node group.
- **Placement:** the operator adds a node affinity for that label to all of the tenant's pods (Odoo, cron, PostgreSQL, Jobs), so the whole tenant lands in its class.
- **Choosing the class:** a new setting on each pool (size class and machine size) plus the package size from `saas.plan._package()`; the smallest class that fits wins.
- **Each pool has its own minimum and maximum.** The Large pool can have a minimum of 0, so it costs nothing until the first large customer arrives.

## 4. A customer who outgrows their node

When a customer resizes and the new package no longer fits their node (or belongs to a larger class), the platform moves them to a node that fits.

1. **Check first:** the target pool has room or can grow (section 1). If not, the resize is refused before payment with a clear message, and you get the “no node fits” alert.
2. **Tell the customer:** a move needs a short downtime, usually 1–3 minutes, because the volume holding the database can be attached to only one node at a time. The customer picks “now” or a time window, and gets an email before and after.
3. **Move:** the operator stops the tenant's pods, the volume detaches from the old node and attaches to a node in the target pool (block volumes attach to any node in the same region), and the pods start there with the new size. The platform polls the site until it answers.
4. **Small resizes that still fit** stay on the same node with today's zero-downtime rolling update.

**Later, zero downtime here too:** once files live in Spaces (Spaces Filestore Backend Plan) and sessions move off the volume, only PostgreSQL holds the volume. A move can then start the new Odoo pods first and limit the downtime to the database restart; a fully online move would need a PostgreSQL replica (CloudNativePG), a larger project kept out of this plan.

## 5. Alerts

All go to the Teams channel through the alert webhook, and each one names the cluster, pool, tenant and the action to take.

| Alert | When | What to do |
| --- | --- | --- |
| Provisioning stuck | A customer has been provisioning or waiting for more than 15 minutes | Check the reason shown (pool at maximum, no node fits, image pull) |
| Update stuck | An update has waited for room or failed to become ready for more than 10 minutes | Free room on that node, or move the tenant |
| Nodes near the maximum | A pool reaches 80% of its maximum node count, and again at 100% | Raise the pool's maximum (and the DigitalOcean Droplet limit if needed) |
| No node fits | A customer's package fits no pool, or a resize needs a class with no room | Add or enlarge a pool, or change the plan |

## 6. One-time settings

- **DOKS Standard pool:** raise the maximum from 2 to your ceiling (for example 6–10 nodes at about $24/month each for `s-2vcpu-4gb`).
- **DOKS Large pool:** create it with a minimum of 0 and a maximum you choose, with the size-class label.
- **DigitalOcean account:** the total node count is capped by the Droplet limit (25 by default); ask DigitalOcean to raise it before you need more.
- **Other Kubernetes later:** the same two pools and labels, with each provider's own node-group minimum and maximum; nothing in the platform is DigitalOcean-specific.

## 7. Phases, tests and acceptance

1. **Phase 1 — Capacity reader, placement and alerts.** Sections 1, 2 (update room) and 5. Raise the Standard pool's maximum.
2. **Phase 2 — Size classes.** Pool labels, node affinity in the operator, class choice in the platform, the Large pool.
3. **Phase 3 — Moving a customer between pools.** Section 4, with the customer's time window and emails.
4. **Phase 4 — Limited simultaneous updates** in the operator (one tenant per node at a time).

**Tests** (on the DOKS cluster):

- [ ] With the pool full but below its maximum, a new customer is provisioned and becomes ready once the autoscaler adds a node.
- [ ] With the pool at its maximum, the customer waits and the “nodes at the maximum” alert fires.
- [ ] After filling a node, an update of a tenant on it still completes with no downtime (update room).
- [ ] A platform add-on change rolls all tenants one per node at a time, none stuck.
- [ ] A large plan lands in the Large pool; a small one in Standard.
- [ ] Resizing across classes moves the tenant with downtime under 3 minutes and its data intact.
- [ ] A package that fits no pool triggers “no node fits” and is not provisioned.
- [ ] A provisioning stuck for 15 minutes triggers its alert.

**Acceptance:** customers are never refused while a pool can still grow, no update ever waits for room on its node, and each of the four alerts has fired once in a test.

## 8. Decisions (2026-10-11)

1. **Pool ceilings are set by hand** in the provider's UI (the DigitalOcean console for DOKS). The platform only reads each pool's maximum; it never changes it.
2. **The Large pool's machine size is also chosen by hand** in the provider's UI. The platform reads each node's real size and picks the pool that fits a package, so a later change of machine size needs no platform change.
3. **Moving a customer between pools may take 1–3 minutes of downtime,** at a time the customer picks, with an email before and after. This is an accepted exception to the zero-downtime rule until the move without the shared volume is possible.
