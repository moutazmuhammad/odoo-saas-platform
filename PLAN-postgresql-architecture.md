# PostgreSQL Architecture Plan

Oct 11, 2026 · @Mostafa

## The answer

**Do not put all customers on one central database. Use a hybrid:** every Production server keeps its own dedicated PostgreSQL, as today, and all Staging/Development servers and trials share one central PostgreSQL cluster per Kubernetes cluster. Idle Development servers go to sleep and wake on the next visit.

- **Customer comfort:** Production is never slowed or stopped by another customer, and a customer's update, restore or upgrade touches only their own database. Development servers are created and copied from Production in seconds and stay out of the way when unused.
- **Stable performance:** each Production database has fixed, guaranteed CPU and memory sized from the plan; the shared cluster has per-customer limits so one heavy Development query cannot hurt the others.
- **Your margin:** Production cost is covered by the plan price. The waste today sits in Development servers and trials: each carries its own PostgreSQL pod and volume while mostly idle. Sharing their database and putting them to sleep cuts that cost sharply, so Development servers can be sold cheaply and still earn well.
- **Paid extras on top:** High Availability (a standby database) and point-in-time restore for Production, for customers who need them.

## The three options

A database per Production customer protects what customers pay for; one shared cluster is right only where a short slowdown is acceptable, which is Development.

|  | A. Dedicated PostgreSQL per server (today) | B. One central cluster for everyone | C. Hybrid (recommended) |
| --- | --- | --- | --- |
| Noisy neighbours | None: each database has its own CPU and memory | One heavy report or import slows every customer | None for Production; limited by per-customer caps on the shared cluster |
| Failure impact | One customer | **All customers at once** | One Production customer, or Development only |
| Security with customer Shell/SQL access | Full: a separate server | Weaker: every customer sees the list of all databases on the cluster; extensions and roles need care | Full for Production; acceptable for Development |
| Version upgrades | Per customer, at their time | All at once; old Odoo versions need old PostgreSQL | Per Production customer; one shared cluster per PostgreSQL major |
| Restore one customer | Simple, only their server | Per database is fine; point-in-time restore is cluster-wide | Simple for Production |
| Connections | Small and predictable | Hundreds of Odoo workers need a pooler; Odoo's live-chat/bus needs session-mode connections | Pooler only on the shared cluster |
| Cost for many small or idle servers | High: one PostgreSQL pod and volume each | Lowest | Low where it matters (Development, trials) |
| Fit for Veltnex | Good for Production, wasteful for Development | Risky for paying customers | **Best** |

## Production: a dedicated database per customer

Keep today's model, a PostgreSQL pod per Production server, and make it lighter, faster and safer.

1. **Sized from the plan.** CPU and memory come from the plan as today (`saas.plan._package()`), with guaranteed requests, so performance does not depend on neighbours.
2. **Tuned for Odoo, automatically per size:** `shared_buffers` about 25% of the database memory, `effective_cache_size` about 70%, a modest `work_mem`, `max_connections` matched to the plan's Odoo workers, `pg_stat_statements` on, and autovacuum tuned for Odoo's large tables (`mail_message`, `account_move_line`).
3. **Separate the database from Odoo.** Today Odoo and PostgreSQL share one volume, which forces them onto the same node. Once files move to Spaces (Spaces Filestore Backend Plan) and sessions move to the database, Odoo needs no volume at all. Then Odoo updates can run on any node with zero downtime, and moving a customer between pools only restarts the database (Cluster Capacity plan).
4. **Backups:** daily database backups with restic (Backup & Restore plan); the files are already in Spaces.
5. **Paid extras:**
   - **High Availability:** a standby copy of the database on another node (CloudNativePG, two instances); if the node fails, the standby takes over in under a minute. Priced on top of the plan, since it doubles the database cost.
   - **Point-in-time restore:** continuous archiving of the database's changes to Spaces, so the customer can restore to any minute of the last 7 days. Cheap to run, valuable to sell.
6. **Large customers** get a bigger dedicated database on the Large node pool (Cluster Capacity plan); nothing else changes.

## Development and Staging: one shared cluster, and sleep when idle

All Staging/Development servers and trials of a Kubernetes cluster keep their databases in one shared PostgreSQL cluster; each server gets its own database and its own login, and idle servers sleep.

**The shared cluster.**

- CloudNativePG, two instances (primary and standby) on the Standard pool, with continuous archiving to Spaces. Starting size: 2 CPU and 4 GB; it grows as needed.
- One cluster per PostgreSQL major version that the Odoo versions need (for example PostgreSQL 12 for Odoo 11–13, PostgreSQL 16 for Odoo 14–20).
- A connection pooler (PgBouncer) in front, in session mode, which Odoo's live notifications require.

**Isolation per server.**

- Each server gets its own database and its own login, which can connect only to that database (`REVOKE CONNECT ... FROM PUBLIC`).
- Database names use the server's id, not the customer's name, because the list of database names is visible to everyone on a shared cluster.
- **Limits on each login** so no Development server can hurt the others: a connection limit (for example 10), `statement_timeout` (5 minutes), `idle_in_transaction_session_timeout`, `work_mem` and `temp_file_limit`.
- `pg_stat_statements` shows the heaviest servers; one that keeps hitting its limits is moved to a dedicated database automatically, and you get an alert.

**Lifecycle, all fast because nothing new starts up:**

- **Create:** a new database and login in seconds; the Production copy streams `pg_dump` from the customer's Production database into it, then is neutralized and PII-masked as today.
- **Delete:** drop the database and login; nothing to tear down.
- **Stop:** the database stays, using only disk.

**Sleep when idle.** A Development or Staging server with no visits for a set time (for example 2 hours) scales its Odoo pod to zero. The next visit shows “Waking up your server…” for about 20–30 seconds while Odoo starts; the database is already running in the shared cluster. The customer can turn sleep off per server for a fee, for example for a demo.

## Cost and how it fits the other plans

The saving comes almost entirely from Development servers and trials, which are the most numerous and the most idle.

**Rough estimate per Development server**, from today's requested sizes (PostgreSQL pod 0.125 CPU / 128 MiB, Odoo about 0.25 CPU) on `s-2vcpu-4gb` nodes at about $24/month; real numbers depend on your plans:

|  | Dedicated database, always on (today) | Shared database, always on | Shared database + sleep (idle 20 h a day) |
| --- | --- | --- | --- |
| Database | Own pod + own volume | Share of the shared cluster (about $0.50) | Same |
| Odoo | Running all month | Running all month | Running about 4 h a day |
| Approximate cost per server per month | about $5.30 | about $3.70 | about $1.10 |

With 50 Development servers that is roughly $265 → $55 a month for the same service, while Production stays exactly as reliable.

**How it fits the other plans:**

- **Spaces Filestore plan:** files leave the volume, so Odoo becomes stateless; this is what lets the database stand alone.
- **Backup & Restore plan:** Production backups stay per customer; the shared cluster is backed up as a whole by continuous archiving, plus a daily dump per Development database.
- **Cluster Capacity plan:** Production databases live in the Standard or Large pool by size; the shared cluster lives on the Standard pool.

## Phases, risks and open questions

1. **Phase 1 — Sleep for idle Development servers.** The biggest saving, small change: scale Odoo to zero after inactivity, wake on the next request. Works even before the shared cluster.
2. **Phase 2 — Shared cluster for new Development servers and trials.** CloudNativePG (vendored chart), per-server database and login, limits, pooler, Production copy by streaming.
3. **Phase 3 — Production tuning and separating Odoo from the database volume** (after the Spaces filestore lands).
4. **Phase 4 — Paid High Availability and point-in-time restore** for Production.

**Risks:**

| Risk | Mitigation |
| --- | --- |
| The shared cluster fails and every Development server stops | Two instances with automatic failover; Production is never on it |
| One Development server overloads the shared cluster | Per-login limits, top-query monitoring, automatic move to a dedicated database |
| A customer sees other database names | Names based on server ids; no customer data in names |
| Very old Odoo versions need old PostgreSQL | One shared cluster per PostgreSQL major in use |
| Customers dislike waiting for a sleeping server | Only Development/Staging sleep; a visible “waking up” page; sleep can be switched off for a fee |

**Open questions:**

1. Sleep after how long without visits: 1, 2 or 4 hours?
2. Should trials share the cluster too (recommended), or keep a dedicated database so the trial behaves exactly like the paid Production?
3. Do you want to sell High Availability and point-in-time restore as add-ons, and should Staging be allowed to stay awake by default (it is often used for testing with clients)?
