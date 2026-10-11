# Veltnex Cost-Reduction Launch Plan

Oct 11, 2026 · @Mostafa

**This is the plan to execute for launch.** It takes the security must-haves and every cost reduction from the full target, [future-plan/ARCHITECTURE-TARGET.md](future-plan/ARCHITECTURE-TARGET.md) and [future-plan/PLAN-implementation.md](future-plan/PLAN-implementation.md), and orders the reductions from the largest saving to the smallest. Item IDs (P0.x, P1.x, …) point to the full plan, where each item's files and checks are written out.

Status: plan only. Nothing is built or deployed. Implementation starts when the owner says go.

## Owner decisions applied (2026-10-11)

- **Odoo 14 to 20 stay supported.** This costs about 3–5 extra engineer-weeks, accepted.
- **SSH gateway deferred.** Launch has the web shell and the Odoo shell only.
- **Sleep is required**, because it saves money, but only in the form that really reduces paid resources (section 3).
- **Platform database** moves to its own VM, managed by the owner (P0.29).
- **Billing intelligence:** usage metering (P1.31), so every customer is billed and priced from measured consumption.
- **One control plane, two hosting types (owner, 2026-10-11):**
    - **Managed hosting:** Kubernetes and the existing operator, priced by resources.
    - **Bring Your Own Server:** Docker Compose and Traefik on the customer's server, with a Veltnex agent over mTLS, priced by subscription.

  Both sit behind one infrastructure interface (P1.36, P1.32), and the portal shows each type's capabilities (P1.37). The install script is a clean-room Veltnex script; oec.sh's code is not copied.
- **Pricing on the oec.sh model (P1.33).** US dollars per month, excluding server cost:
    - Free $0: 1 server, 2 projects;
    - Starter $19: up to 2 servers;
    - Pro $39: unlimited servers and projects within the published plan;
    - Agency $199: hosting companies and resellers (P1.35).
  It is freemium: connecting a server is free, and the paid features are Git auto-deploy, monitoring, teams and permissions, Veltnex automated backups and advanced monitoring, with Managed Database Services later.
- **Marketing on the oec.sh approach (P1.34),** with every claim backed by a published calculation and no invented social proof.

**How savings are estimated** (*assumptions*, replaced by metering, P1.31, once it runs):

- one DigitalOcean cell with 50 Production tenants (2 workers each) and 40 non-production environments;
- node prices about $24 for `s-2vcpu-4gb` and $48 for `s-4vcpu-8gb`;
- per-tenant figures from the corrected cost model (architecture, concern 6, revision 3).

Infrastructure savings are per month. Engineering savings are engineer-weeks not spent before launch.

## 1. Must-haves before launch (not cuts; launch blockers)

These are non-negotiable, whatever they cost. Each must pass its "Done when" in the full plan.

| # | Must-have | Item | Why |
| --- | --- | --- | --- |
| M1 | Database role with limited rights for every tenant, existing and new | P0.24 | Customer code can read today's superuser login |
| M2 | Least privilege for the control plane | P0.1 | One breach must not reach every cluster |
| M3 | Separate Build pool | P0.19 | Customer build code runs without a sandbox |
| M4 | Container registry isolation (per-tenant repositories and credentials) | P0.3 | One credential can pull every tenant's image today |
| M5 | Separate tenant domain | P0.20 | Customer apps must not set cookies on the portal |
| M6 | Off-site backups with restic | P1.5 (with P0.4, P0.5, P0.16, P0.17, P1.16) | Recovery after losing a volume, cell or region; backups in every plan |
| M7 | Alertmanager and alerts | P0.11 | Failures must reach people, not only logs |
| M8 | Usage metering (billing intelligence) | P1.31 | Price and bill from real consumption; prove every saving below |
| M9 | Platform database on its own VM, owner-managed | P0.29 | The control plane's data lives off the application VM |

The other launch work (Tier L in the full plan) also stays:

- custom domains, Mailpit, the mail relay, the test gate, the Odoo shell and `pg_slow_queries.log`;
- customer migration and notices, maintenance windows;
- the disaster-recovery drill, load test, pentest, legal documents, on-call and status page, and the automated walk-through.

## 2. Cost reductions, largest first

### 2a. Infrastructure, per month

| Rank | Reduction | Saving per month (*assumed* scale) | Risk it adds | Item |
| --- | --- | --- | --- | --- |
| C0 | **Bring Your Own Server (Docker Compose driver) for customers who have servers:** most customers run on their own servers under the flat plans, so Veltnex buys no compute, volumes or load balancer for them | About **$12–14 per customer moved to BYOS** (the managed cost per Production environment, corrected model); about **$600 or more** at 50 customers. Infrastructure risk moves off Veltnex's books | Less control over customer servers (handled by the cell checks and per-customer isolation); support load per server, which metering covers | P1.32, P1.33 |
| C1 | **Run cells on `s-4vcpu-8gb` nodes, not `s-2vcpu-4gb`** | About **$590**: Production compute falls from about $22.60 to $10.85 per tenant. The DaemonSet share drops from 14% to about 6%, and the rollout surge pod fits beside the tenants | More tenants affected by one node failure; covered by P0.28 maintenance windows | New: C1 below |
| C2 | **Sleep for non-production** (dev 1 h, staging 2 h, trials 1 h) **with a Shared pool that scales to zero** | About **$135** at 40 environments: about $3.35 per environment, because Odoo is awake about 15% of the time; more as environments grow | A 30 s wake on a warm node, 1–3 minutes from a cold pool | P2.1, P2.5 |
| C3 | **One cell and one load balancer** until a paying customer needs another region | About **$76 or more per cell avoided**: cell overhead about $28 plus at least one node | None now; residency customers wait for a funded cell | Full plan "Later" tier |
| C4 | **No DOKS HA control plane** until an SLA is sold | About **$40** | Kubernetes API outages delay deploys; running tenants are unaffected | P0.23 |
| C5 | **Shared non-production database** instead of a dedicated PostgreSQL per environment | About **$40** at 40 environments: about $1 per environment | One shared failure stops non-production in the cell; Production is unaffected | P2.2 |
| C6 | **restic deduplication** instead of 7 full copies, with 7/4/6 retention | About **$26** at 50 tenants, growing with retention: 17 full copies would be about $2 per tenant | None; integrity checks catch damage | P1.5 |
| C7 | **One shared-database instance**, not 2, until about 30 non-production environments per cell | About **$20 per cell** | A non-production outage during a restore; Production is unaffected | P2.2 (A9 revised) |
| C8 | **Right-size the control-plane VM from measurement**; the platform database on its own small VM | About **$12** net (*assumed*: application VM down from $48 to $24, database VM about $12) | None; the separate database is stronger | P0.29 |
| C9 | **Required tests only on Production and staging branches;** dev branches opt in | About **$5–10** of Build-pool time | Dev regressions caught later | P1.11 |

### 2b. Engineering effort before launch

| Rank | Reduction | Engineer-weeks saved | Risk it adds | Item |
| --- | --- | --- | --- | --- |
| E1 | **Defer the multi-provider work** until a signed customer needs a second region. That means region and provider pricing (P1.22), residency enforcement (P1.21), the provider adapters beyond the per-cell backup target and rate card (P0.25), the full cell contract (P0.26; only the NetworkPolicy and metadata probes are kept), the Azure adapter, and the OpenTofu modules (P2.10, P2.11) | **5–8** | A refactor later; low risk with one provider | Full plan "Later" |
| E2 | **Defer the SSH gateway** (owner decision) | **2–5** | A developer convenience gap against Odoo.sh | P1.13 |
| E3 | **Performance v1 at launch is `pg_slow_queries.log` plus the Odoo request log;** the Advisor, findings and alerts come in L+ | **3+** | The advanced feature arrives later | P1.14 now, P2.7 later |
| E4 | **Launch on the Managed PostgreSQL** (with the limited role, M1); **CloudNativePG in L+**, qualified on one PostgreSQL major first | **2–4** | A later move of each tenant by dump and restore, at a customer-chosen time | P1.9a, P1.9b |
| E5 | **A stock mail relay** (Postfix with SASL and a rate-limit policy), a low SES account quota and a manual pause; the automated quota and abuse engine later | **2–3** | Abuse is handled by hand at first; the SES quota caps the damage | P1.8 |
| E6 | **Customer buckets with the P3 filestore, not at launch;** trials use a prefix, not buckets | **1–2** | None until P3; stays far below the 100-bucket limit | P1.4 |
| E8 | **Reuse the deleted `SshDockerDriver`** (commit `cb9038d`, 294 lines, Compose with shadow deploys) as the starting point of the `DockerComposeDriver` | **About 1–2** | Old code must be reviewed against today's security rules | P1.32 |
| E7 | **Build the registry once**, with per-tenant access in P0.3, instead of a single pull credential that is replaced later | **About 1** | None | P0.3 (P1.24 merged) |

**Added by the owner's architecture (one control plane, two runtimes):** the one infrastructure interface (P1.36), the Docker Compose driver with the agent (P1.32), the capability matrix (P1.37) and the conformance test (P1.38). Together they are *assumed* at 6–9 engineer-weeks: a second runtime costs more than a k3s cell would, and buys a lighter, more familiar BYOS for customers.

**Not cut, by owner decision:**

- **Odoo 14–20:** costs 3–5 weeks.
- **Sleep:** costs 2–3 weeks to build and pays back in C2.

**Free-plan cost guardrails:** the Free plan is BYOS only, so Veltnex pays no compute. Its backups go to the customer's own S3. It allows one account per verified person or company. Servers inactive for 90 days are detached. Community support only.

**Backup rule to confirm (reconciles A11 with the free tier):**

- **Every plan, Free included:** daily backups to the customer's own S3.
- **Pro and above:** Veltnex off-site automated backups, with retention, integrity checks, restore drills and a second location.
- **Veltnex-managed cells:** they always include Veltnex backups.

## 3. Sleep: savings that are real

Stopping pods saves nothing by itself, because the provider bills nodes and volumes. Sleep is built so that paid resources actually go away. The full rules are in the architecture: "Sleep: what it really saves" and "Behaviour during sleep".

**Real savings:**

1. **Placement:**
    - sleeping workloads, the non-production Odoo pods, run alone on a **Shared pool that can scale to zero**;
    - the shared database, Mailpit and the wake activator live on the Standard pool;
    - the scheduled minimum is 1 node in business hours for fast wakes, and 0 at night and on weekends (tunable).
2. **Nothing reserved:** a sleeping environment holds no pod and no requests. Its database is inside the shared cluster, with no pod of its own.
3. **Volumes:**
    - dev volumes are created at the minimum size;
    - dev copies carry no attachments unless asked;
    - after 14 idle days the environment is archived and its volume deleted;
    - after P3, files are billed per GiB stored.
4. **Proof:** metering (M8) reports Shared-pool node-hours and volume GiB-hours per day. Sleep counts as done only when node-hours measurably fall.

**Behaviour:**

| Topic | Rule |
| --- | --- |
| Wake-up | Any request to a sleeping host reaches the activator. Browsers see "Waking up…" and are redirected; API calls get `503` with `Retry-After`; there is a Wake button; wakes are rate-limited per cell |
| Cron jobs | Staging and dev crons are already disabled by neutralization. Trial crons pause and run each overdue job once on wake |
| Backups | Dev and trials have none. A manual staging snapshot runs without waking, by dumping from the shared cluster with a Job that mounts the volume. Archiving needs no wake |
| Builds | A push to a sleeping branch builds, tests and runs the update gate without waking the environment |
| Billing | A flat slot fee per non-production environment, asleep or awake; "always awake" is extra; trials are free. Awake hours are metered internally |
| Production and BYOS | Production never sleeps. BYOS servers do not sleep by default, because it saves the platform nothing |

## 4. Billing intelligence (usage metering, M8)

Every day, per environment, the platform records:

- CPU and memory requests over time, and the actual p95 and maximum;
- awake and asleep hours;
- volume GiB;
- object-storage and backup GiB after deduplication;
- build minutes;
- mail messages;
- egress where the provider reports it.

Each day is priced with the cell's rate card and the corrected cost function. That gives cost and margin per environment, customer, plan and cell.

**What it gives:**

- **Price floors:** set from real data, at 60% at launch and 70% at scale.
- **Usage billing:** storage over the plan and mail over the quota are billed from real records.
- **Alerts:** on unprofitable customers, and on plans whose real usage has drifted from their package.
- **Proof:** a measured check of every saving in section 2.
- **BYOS:** management fees covered against metered support time.

Its acceptance check: one month of metered cost on the test cell is within 15% of the DigitalOcean invoice (P1.31).

## 5. Execution order

1. **Security and safety must-haves:** M1–M7, M9, plus the related launch items (P0.4, P0.5, P0.6, P0.8, P0.9, P0.10, P0.13, P0.14, P0.16, P0.17).
2. **Metering (M8), then the largest savings:**
    - C1: move the cell to `s-4vcpu-8gb` nodes in a maintenance window (P0.27, P0.28);
    - C2, C5 and C7: Shared pool, sleep and one shared database instance;
    - C4, C6, C8 and C9 alongside.
3. **Launch features, BYOS and pricing:**
    - Bring Your Own Server (P1.32), the freemium plans with their feature gates (P1.33), the Agency plan with client organisations, and the marketing site (P1.34);
    - custom domains, submodules, Mailpit, the stock mail relay, the test gate;
    - Odoo shell, logs including `pg_slow_queries.log`, uptime, promotion;
    - the Odoo 14–20 matrix, the price floor, the website target.
4. **Launch readiness:** customer migration and notices, the disaster-recovery drill, load test, pentest, legal documents, operations and status page, and the automated walk-through. Then Gate A.
5. **After launch, L+, within about 90 days:**
    - CloudNativePG;
    - major-upgrade workflow;
    - persona roles, activity history;
    - Agency white-label and client billing (P1.35);
    - the performance Advisor, archive, control-plane scaling, build cache, stored logs, OpenUpgrade.

**C1 · Move to `s-4vcpu-8gb` nodes (new item):**

1. Add a pool of `s-4vcpu-8gb` nodes.
2. Cordon the old pool.
3. Drain it one node at a time in an announced window (pre-P3 tenants see 1–3 minutes each).
4. Delete the old pool.
5. Update the cell's rate card (P0.25) so pricing and metering use the new node.

Done when every tenant runs on the new pool, metering shows the lower compute cost per tenant, and no tenant exceeded its announced interruption.
