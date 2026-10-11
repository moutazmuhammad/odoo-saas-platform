# Veltnex Architecture Review and Target Design

Oct 11, 2026 · @Mostafa · Revision 2 · Doc (revision 1 only): https://claude.ai/code/artifact/2e71a7cf-e77c-4e5f-bd39-a0854eee94ad

This is the architecture review and the target design. The work items are in [PLAN-implementation.md](PLAN-implementation.md). That file also keeps the four earlier plans (backup and restore, Spaces filestore, cluster capacity, PostgreSQL) unchanged as Appendices A–D.

Revision 2 answers eight review concerns, adds AWS SES for outgoing mail, and hardens the wildcard certificate and the custom-domain path. Every claim below is marked as one of these. Decisions and their approval status are in the next section.

- **Verified:** checked in the repository, on the live DOKS cluster (read-only Prometheus and kubectl reads on 2026-10-11), or in a provider document that was opened and is linked.
- **Assumption:** reasonable but not yet proven; each one has a test in the plan.

Nothing here is implemented. All decisions are approved subject to the launch acceptance criteria (next section). Implementation waits for the owner's go-ahead.

## Decisions and their status

All thirteen decisions were approved on 2026-10-11, subject to the launch acceptance criteria in the plan. Where the owner set a condition, it is a "Done when" in [PLAN-implementation.md](PLAN-implementation.md#launch-blockers-and-launch-acceptance-criteria).

| # | Decision | Status | Condition or note | Plan items |
| --- | --- | --- | --- | --- |
| A1 | CloudNativePG as the one PostgreSQL engine: dedicated 1 instance per Production environment, shared for non-production, HA by raising instances to 2 | **Approved with conditions** | Qualify backups, upgrades, failover and restore before adopting it for every environment. New Production uses it only after the qualification suite passes (P1.9a) | P0.22, P1.9a, P1.9b, P2.2, P3.3, P4.1, P4.2 |
| A2 | A Production bucket and a non-production bucket per customer, each with its own key | **Approved** | Test code can never reach Production files | P1.4, P3.1, P3.4 |
| A3 | Sessions in a per-environment PostgreSQL database | **Approved with conditions** | Test session behaviour, performance and the automatic cleanup of expired sessions | P3.2 |
| A4 | Tenant apps on their own registrable domain, on the Public Suffix List | **Approved (strongly)** | Protects the portal from cookies set by customer apps | P0.20, P0.7 |
| A5 | Custom-domain ownership by a permanent DNS TXT record | **Approved** | Add a clear procedure for when the customer deletes the record or loses control of the domain (concern 8, "Ownership lost") | P1.1 |
| A6 | Memory requests from measured usage | **Approved** | Pricing is based on usable node capacity, not theoretical node size | P0.21, P0.18 |
| A7 | Central mail relay to AWS SES | **Approved** | No AWS credentials in tenants; quotas, isolation and automatic pausing on abuse | P1.8, P0.13 |
| A8 | Production attachments copied to dev only on the customer's explicit request | **Approved** | Staging copies include them; dev does not unless asked | P1.4 |
| A9 | Shared non-production cluster with 2 instances per cell | **Approved with conditions** | Test failover, storage growth and resource contention | P2.2 |
| A10 | Basic performance monitoring free; history and advanced reports paid | **Approved** | — | P2.7, P4.7 |
| A11 | Automatic Production backups included in every plan | **Approved with conditions** | Plan-based retention and recovery features | P1.16 |
| A12 | Read-only window for the final major-version upgrade, chosen by the customer | **Approved with conditions** | A tested copy, a verified backup, a customer-selected window and a documented recovery procedure | P1.18 |
| A13 | State an uptime target on the website; SLA only with HA | **Approved with conditions** | A target only, until HA passes failure and recovery tests; a contractual SLA only once the platform consistently meets it | P0.23, P4.1 |

**Launch blockers.** The full list, with each acceptance check, is in the plan's "Launch blockers and launch acceptance criteria". The owner added these on 2026-10-11:

- no PostgreSQL superuser for any tenant (P0.24);
- no Production deploy without passing tests (P1.11);
- an isolated, auditable SSH gateway (P1.13);
- backups in every plan (P1.16);
- safe major upgrades (P1.18);
- an uptime target, not an SLA (P0.23).

Still open from revision 1: archive window, whether trials share the cluster, add-ons at launch, second backup region, and Enterprise hosting (see the end of this file).

## Summary

**Keep Kubernetes, the Go operator and one namespace per environment.** Production stays dedicated and always on. Staging, dev and trials share and sleep. Revision 2 changes six things:

1. **One PostgreSQL engine:** CloudNativePG for every new environment (A1). Production keeps a dedicated database. Shared Production shards save about $1 per tenant a month and multiply the blast radius, so they are rejected.
2. **The isolation boundary is the bucket, not the folder.** Spaces keys are per bucket only, so Production files get their own bucket and key (A2).
3. **A corrected cost model.** The old formula charged the node twice. The new one prices each tenant by the larger of its CPU and memory share, after system overhead and packing. For a 2-worker Production environment it gives about $14 a month, so the 60% price floor is about $35.
4. **One unit of consistency for backups and restores:** a database snapshot, plus exactly the files it references, plus the code that wrote it.
5. **Tenant safety at the edge:**
    - tenant apps on their own registrable domain;
    - the wildcard certificate locked down;
    - custom domains on a separate issuance path with TXT ownership proof and takeover rules;
    - exact host routing only.
6. **Builds are a launch security item, not a cost item.** They run customer code with seccomp and AppArmor turned off, so they move to their own tainted node pool before launch.

## Current state assessment

**Verified on 2026-10-11** in the repository and on the live cluster unless marked otherwise.

| Area | Status | Evidence | Main limitation |
| --- | --- | --- | --- |
| Tenant runtime | Implemented | operator `resources/deployment.go`, `tenancy.go` | One Odoo pod plus a cron sidecar and a PostgreSQL StatefulSet on one RWO volume, pinned to one node (`deployment.go:282`) |
| Zero-downtime updates | Implemented | MaxSurge 1, update gate `controller/update.go` | The surge pod must fit on the same node; a node drain still means 1–3 minutes down |
| Pod hardening | Implemented | restricted Pod Security, non-root, read-only root, seccomp `RuntimeDefault`, no service-account token (`tenancy.go:36`) | Good baseline |
| Network isolation | Partial | `networkpolicy.go` | Ingress only from the gateway, which is good. Egress 443 reaches any address, including in-cluster; no SMTP egress |
| PostgreSQL | Implemented, dedicated | `resources/database.go`; CloudNativePG mode `cnpg.go` single-instance | The tenant login is the image's `POSTGRES_USER`, a superuser (`database.go:92`); no HA, PITR or major-version guard |
| SQL console | Implemented | `hosting_sql_query` runs through Odoo's own connection (`saas_instance.py:2191`) | Runs as that superuser |
| Shell | Implemented | `shellContainer` (`deployment.go:225`): no secrets, but the Odoo data dir is mounted read-write | Session files and the data-dir `addons/` folder are writable |
| Builds | Implemented | `drivers/k8s_builds.py` | Rootless BuildKit with seccomp and AppArmor `Unconfined` (`:288`); runs customer `pip install`; no node selector, so builds share tenant nodes; private ranges blocked on egress (good) |
| Registry credentials | Partial | `_upsert_pull_secret(namespace, …, registry_username, registry_password)` (`k8s_builds.py:412`) | The same push-capable login is copied into every tenant namespace |
| Image registry | Partial | Docker Hub `moutazmuhammad/*` | Free Docker Hub repositories are public |
| Filestore on Spaces | Planned | Appendix B | Files on block storage keep Odoo stateful |
| Environments | Implemented | `_create_env_child`, `_NEUTRALIZE_SQL`, `_PII_MASK_SQL` | PII masking covers database rows, not attachments; no sleep; no mail catcher |
| Backups | Implemented (full dumps) | `run-backup.sh`: `pg_dump` per database, then a tar of the filestore, then a manifest | The manifest records no image, commit, module or PostgreSQL version. One shared S3 key sits in every tenant namespace. PostgreSQL 16 client only. Every backup starts at 02:00 |
| Restore | Partial | `run-restore.sh:125`, `saas_instance.py:3400` | Drops the database before loading |
| Memory requests | Partial | `_package()` requests are 25% of limits; a 2-worker plan requests 448 MiB | Measured one-day average working sets were 0.46–1.4 GiB per environment, so memory can be overcommitted |
| Tenant domain | Partial | live CR host `ee.node.eagle-tech.info`; portal `main.eagle-tech.info` | Tenants and the portal share the registrable domain `eagle-tech.info` |
| TLS | Implemented | per-host certificate through the cluster issuer (`kubernetes_driver.py:525`) | Let's Encrypt allows 50 new certificates per registered domain per 7 days ([source](https://letsencrypt.org/docs/rate-limits/)) |
| Custom domains | Missing | only `subdomain.base_domain` | — |
| Outgoing mail | Missing as a platform service | — | Customers configure their own SMTP, which the network policy blocks today |
| Control plane to cluster | Over-privileged | bootstrap binds `cluster-admin` (`bootstrap.py:215`) | — |
| Control-plane scaling | Partial | `_cron_reconcile`, `_cron_verify_webhooks`, `_cron_sync_scheduled_backups` | Per-tenant API calls in unbounded loops |
| Platform database backup | Owner-managed | confirmed 2026-10-11 | Backed up outside the repository |
| Monitoring and alerts | Partial | Prometheus, no Alertmanager; alert webhook unset | — |
| CI/CD gates | Partial | `cd.yml` "temporary fast deployment mode" | — |

## Recommended target architecture

**A regional cell (one DOKS cluster) runs four classes of environment.** Production is dedicated and always on; everything else shares and sleeps.

| Class | Odoo | Database (A1) | Files (A2) | Idle | Backups | Priced by |
| --- | --- | --- | --- | --- | --- | --- |
| Production | 1 pod in a Production pool; never sleeps | Dedicated CloudNativePG, 1 instance | Customer's Production bucket | Always on | Daily snapshot set (DB + referenced files + code) with restic, 7/4/6; second-region copy | Workers + storage |
| Production HA (add-on) | 2 pods on different nodes | Same cluster raised to 2 instances; WAL archiving | Production bucket | Always on | Same + PITR 7 days | Base + HA fee |
| Staging | 1 small pod in the Shared pool | A database on the cell's shared CloudNativePG cluster | Customer's non-production bucket, own prefix | Sleeps after 2 idle hours | Manual snapshots | Slot fee; awake option |
| Development | Same | Same | Non-production bucket, own prefix | Sleeps after 1 idle hour; archived after long idle | None | Slot fee |
| Trial | Same | Same | Its own bucket pair (it is a customer) | Sleeps after 1 idle hour | None | Free, capped |

**Node pools by phase.** Revision 1 placed Shared and Build pools at launch, before the phases that build them; this table corrects that.

| Pool | Holds | Taint | Arrives |
| --- | --- | --- | --- |
| Standard | Production (and everything else until P2) | none | Today (pool `node`) |
| Build | BuildKit Jobs only | `saas.platform/pool=build:NoSchedule` | P0, as a security fix |
| Shared | Staging, dev, trials, shared database clusters, Mailpit | `saas.platform/pool=shared:NoSchedule` | P2 |
| Large | Production above the Standard node size | `saas.platform/pool=large:NoSchedule` | When the first large customer arrives (P3–P4) |

HA is not a pool. An HA tenant runs in its normal Production pool with pod anti-affinity, so that pool needs at least 2 nodes.

## Architecture diagram

```mermaid
flowchart TB
    CUST["Customers<br/>Portal (portal domain) · Odoo (tenant domain, A4) · custom domains"]
    GIT["Git providers<br/>signed push webhooks"]
    CP["Control plane (Odoo 18)<br/>saas_core, billing, website, IAM · saas-jobs worker<br/>batched cluster reads · least-privilege kubeconfig"]
    PDB["Platform database<br/>owner-managed backups"]
    SES["AWS SES<br/>one credential, held only by the relay"]
    GIT --> CP --> PDB
    subgraph CELL["Regional cell: one DOKS cluster (fra1 first)"]
        EDGE["Edge<br/>Traefik · exact host routes only · default 404<br/>wildcard cert (tenant domain, DNS-01) · custom-domain certs (HTTP-01, separate ACME account)"]
        PS["Platform services<br/>odoo-operator (2 replicas) · CloudNativePG operator · wake activator<br/>Prometheus + Alertmanager · Mailpit · private registry"]
        MR["Mail relay (platform namespace)<br/>per-environment SMTP auth · sender-domain check<br/>rate limit · quota · abuse pause"]
        STD["Standard pool<br/>Production: 1 Odoo pod<br/>dedicated CloudNativePG (1 instance; 2 with HA)<br/>never sleeps"]
        SH["Shared pool (P2)<br/>Staging, dev, trials: Odoo sleeps 1–2 h idle<br/>shared CloudNativePG per PG major (2 instances)"]
        BD["Build pool (P0, tainted, min 0)<br/>BuildKit Jobs · layer cache<br/>push-only credential"]
        BD --> PS
    end
    CUST -- "HTTPS" --> EDGE
    CP -- "Kubernetes API" --> CELL
    STD -- "SMTP, in-cluster" --> MR
    MR -- "HTTPS 443 (SES API)" --> SES
    SP["Spaces (same region)<br/>per customer: Production bucket + non-production bucket<br/>backup bucket (restic, WAL) · registry storage · cold archive"]
    SR["Second region<br/>nightly copy of Production backups"]
    CELL -- "files, backups, WAL" --> SP --> SR
```

## Review of the eight concerns

### 1. Dedicated PostgreSQL per Production versus shards

**Evidence:**

- **Verified (Prometheus, one-day averages on the live cluster):**
    - an idle tenant PostgreSQL used 58–193 MiB of memory, against 230–1,280 MiB for Odoo plus cron in the same tenant;
    - the highest 5-minute CPU rate of any tenant database over the day was 0.165 cores; the average across tenants was 0.019.
- **Verified (code):**
    - a 2-worker plan reserves 125m CPU and 128 MiB for the database (`saas_plan._recommended_db_resources`, `_package()`);
    - the Odoo login is a superuser in its own database server (`database.go:92`).
- **Verified (node, DigitalOcean pricing):** an `s-2vcpu-4gb` node has 1,900m CPU and 3,003 MiB allocatable, of which DaemonSets reserve 412m and 412 MiB. The node costs about $24 a month (repo docs).

**Options:**

| Option | Cost per Production DB per month (assumed sizes) | Isolation and noisy neighbours | Failure domain | Operations |
| --- | --- | --- | --- | --- |
| A. Dedicated Managed StatefulSet (today) | About $2: 150 MiB request, memory-bound on the corrected model, plus a 1–2 GiB volume | Full; one tenant per server | One tenant | Custom code for backup, PITR and HA, none of which exist |
| B. Dedicated CloudNativePG, 1 instance (recommended) | About $2.10: A plus the instance manager, assumed at about 30 MiB | Full | One tenant | One engine for every class; PITR and HA become settings; base backups and WAL handled by the engine |
| C. Production shards (one CloudNativePG cluster with 2 instances for about 50 tenants) | About $1: 2 instances × 2 GiB, plus 25% packing loss, divided by 50 | Shared buffers, CPU and IO; one heavy report slows 50 customers; superuser impossible, so extensions and the SQL console are restricted | 50 tenants per failure; PITR restores the whole shard | Shard placement, rebalancing, per-shard major upgrades |
| D. Hybrid: dedicated for paid plans, shards for a micro "Starter" plan | Mix | Mixed | Mixed | Both systems |

**Trade-offs:**

- **Cost:** shards save about $1 per tenant a month. That is about 3% of a $35 Production price floor.
- **Risk:** shards raise the failure domain from one tenant to about 50.
- **Restore:** shards make PITR a whole-shard operation.
- **Upgrades:** shards force every tenant in a shard onto one major version, which conflicts with selling Odoo 14–20.

**Recommendation: B.** Every Production environment gets a dedicated CloudNativePG cluster with 1 instance, and its requests come from measurement (A6). Reject C for Production. Revisit D only if a sub-$20 Starter plan is ever priced, and then only for trials converted to a "lite" tier. Existing tenants on the Managed StatefulSet move by dump and restore at a customer-chosen time.

*Assumption:* every version sold, Odoo 14–20, runs on PostgreSQL 13 or later. That is the CRD minimum and the oldest major current CloudNativePG images ship. Odoo 14, the oldest, is tested first in P1.7. No PostgreSQL 12 legacy engine is needed.

### 2. Tenant isolation

**Verified** in code unless marked.

| Vector | Today | Risk | Target |
| --- | --- | --- | --- |
| Customer Python addons in Odoo | Non-root, read-only root, seccomp `RuntimeDefault`, no service-account token. The code can read `odoo.conf`: its own database password, which is a superuser login, and `admin_passwd` | Contained to the tenant's own database server. A superuser can run `COPY … TO PROGRAM` inside its own PostgreSQL container | **Launch blocker (P0.24):** every environment, existing and new, uses a limited owner role. `CREATEDB` is allowed on a dedicated server only. The superuser login exists only in a Secret that platform Jobs use, never in Odoo or the shell |
| Builds (`pip install`, `setup.py`) | seccomp and AppArmor `Unconfined`, any node, private ranges blocked on egress | A kernel escape from a build lands on a node running other tenants | Tainted Build pool (P0). The push credential lives only in `odoo-builds`. BuildKit does not mount the client's `config.json` into `RUN` steps (*assumption* from BuildKit's design; test in P0) |
| Registry credentials | The same push-capable login is in every tenant namespace | Anyone reading one tenant Secret could overwrite any tenant's image | A read-only pull credential in tenant namespaces; push only from the Build pool. Per-repository tokens need a token-auth registry, which comes later |
| Shell | No secrets, but the data dir is read-write: session files and the data-dir `addons/` folder, which Odoo adds to its addons path | A shell user can hijack other users' sessions of the same customer, or plant a module that runs in Odoo. This is privilege escalation inside one customer, not across customers | The shell gets the code and a read-only filestore only. Sessions move to the database (A3). Shell access requires a full-developer role with 2FA |
| Kubernetes API | No token mounted (`automountServiceAccountToken: false`) | Low | Keep. The control plane's own access becomes least-privilege (P0.1) |
| Network | Ingress only from gateway pods on 8069, 8072 and 8089 (cross-tenant blocked). Egress 443 to any address | In-cluster services on 443, such as the API server, are reachable without credentials | Deny private ranges on all egress, reusing the builds' `_BLOCKED_CIDRS`. Outgoing mail goes only to the in-cluster relay; direct SMTP to the internet stays closed, so no tenant can send around the quotas. IMAP and POP3 over TLS (993, 995) are allowed for Odoo's incoming mail servers. No inbound mail port is opened |
| Database credentials | One random password per tenant, held in Secrets and `odoo.conf` | Fine per tenant | Non-superuser roles. On shared clusters: per-login connection limit, `statement_timeout`, `temp_file_limit`, and `REVOKE CONNECT … FROM PUBLIC` |
| Object-storage credentials | None in Odoo pods today. The backup key sits in tenant namespaces (P0.5) | See concern 3 | The Production key only in Production namespaces; the non-production key only in non-production ones |
| Cookies | Tenant hosts and the portal share `eagle-tech.info` | Customer code on a tenant host can set cookies for the parent domain, which the portal then receives (cookie tossing or session fixation) | A4: tenants on their own registrable domain, listed on the Public Suffix List |

### 3. Bucket per customer with one key across environments

**Verified** in DigitalOcean's [key guide](https://docs.digitalocean.com/products/spaces/how-to/manage-access/) and [API reference](https://docs.digitalocean.com/reference/api/reference/spaces-keys/):

- a limited key grants Read or Read/Write/Delete per bucket;
- grants are per bucket only, with no prefix or folder scoping;
- keys can be created with `POST /v2/spaces/keys`; the guide still says Control Panel only, but the API reference documents the endpoint.

The account limits are 100 buckets and 200 keys, raised through support ([limits](https://docs.digitalocean.com/products/spaces/details/limits/)).

**Conclusion (verified):** with one bucket and one key per customer, any staging or dev code, which is customer code under test, can read, overwrite and delete Production files. A folder per environment does not help.

**Options:**

| Option | Buckets per customer | Isolation | Cost and limits |
| --- | --- | --- | --- |
| One bucket per customer (revision 1) | 1 | None between environments | Fewest buckets |
| Production plus non-production bucket (recommended) | 2 | Production fully isolated; staging and dev share a bucket | The 100-bucket limit allows 50 customers, so ask support for an increase early |
| Bucket per environment | 3–5 | Full | Limits reached at 20–30 customers; more pool recycling |
| Storage gateway issuing prefix-scoped access | 1 | Full | An always-on service, rejected earlier |

**Recommendation (A2):**

- **Buckets and keys:** each customer gets a Production bucket and a non-production bucket. The Production key is placed only in the Production namespace, and the non-production key only in staging and dev namespaces.
- **Copying files to staging:** copying Production to staging is a server-side copy by a platform Job with a platform key. Neither environment's key ever touches the other bucket.
- **Backups** stay in the platform backup bucket, which no customer key can reach.

**Remaining exposure:** dev code can read staging's copied files, which are real Production attachments. Database PII masking does not cover attachments (verified: `_PII_MASK_SQL` only updates rows).

**A8:** staging copies include attachments, and dev copies include them only when the customer chooses. Otherwise dev gets the database without files, and missing files show as broken links.

### 4. Consistency model for backups, PITR and full restore

**Verified:**

- `run-backup.sh` dumps the main database, then each extra database (each with its own snapshot), and only then tars the filestore;
- the manifest has no image, commit, module, Odoo or PostgreSQL version.

**Assumptions** from Odoo's attachment design:

- filestore files are content-addressed, written once and never modified;
- Odoo deletes unreferenced files later in its garbage collection, which can run between the dump and the tar.

**Defined model:** a restore point is one database snapshot, plus exactly the files that snapshot references, plus the code identity that wrote it.

| Operation | How consistency is reached |
| --- | --- |
| Daily logical backup | Open a transaction, export its snapshot, and in that same snapshot list `ir_attachment.store_fname`. Run `pg_dump --snapshot=<id>`, then copy exactly the listed files. Files are immutable, so copying them later is correct. Garbage-collected files go to `trash/` (Spaces lifecycle, 14 days) instead of being deleted, so a file referenced by the snapshot still exists while the copy runs. Missing files are counted in the manifest. |
| PITR (HA or PITR add-on) | CloudNativePG base backup plus WAL to the backup bucket. Restoring to time T rebuilds the database at T. Every file referenced at T exists, live or in `trash/`, if trash retention (14 days) exceeds the PITR window (7 days); the restore job moves needed objects back from `trash/`. |
| Several databases in one environment | Each has its own snapshot. Odoo databases are independent, so this is acceptable and documented. |
| Full environment restore | Manifest v2 records database names, PostgreSQL major, Odoo version, image digest, build id and commit, module fingerprint, plan, and the referenced-file count. Restore offers two modes: "exact", which uses the recorded image, or "current code", which runs the update gate after loading. |
| Verification | After every restore: `/web/health`, an admin login, and zero missing referenced files. A monthly drill restores a random Production backup into a scratch environment. |
| restic efficiency | Pipe an uncompressed `pg_dump -Fc -Z0` into `restic backup --stdin`, so restic's chunking deduplicates day to day and its own compression applies. *Assumption:* to be measured in P1.5 against the 1.3× target. |

Today, while files are still on the volume, the same model applies with "copy the listed files from the volume". Garbage collection is paused during the backup window instead of using `trash/`.

### 5. Stateless Odoo

**Verified** in DigitalOcean's [limits page](https://docs.digitalocean.com/products/spaces/details/limits/): 800 operations a second per new bucket, 5 GB single PUT, multipart up to 5 TB, a 4 KiB minimum billable object size, and versioning enabled only through the API.

**Not documented:** read-after-write consistency. It is an *assumption*, though content-addressed keys mean objects are never overwritten.

**Design and checks:**

- **Files:**
    - files go through the Appendix B module;
    - web asset bundles and files under 64 KiB stay in the database, to avoid one Spaces request per asset and the 4 KiB billing minimum (*assumed* threshold);
    - each pod has a read-through cache on an `emptyDir` of 1–2 GiB;
    - a read that returns 404 within 5 seconds of the write is retried with backoff.
- **Sessions (A3):**
    - **Options:**
        - a per-environment `…_sessions` database on the environment's own server (recommended: no new service);
        - Redis per cell with an ACL per tenant (faster, but a new stateful service to run);
        - an `emptyDir`, which logs everyone out on every rollout, so it is rejected.
    - **Versions:** the session store differs between Odoo generations (werkzeug's store in 14, Odoo's vendored copy from 15). That needs an adapter per generation, like the filestore. *Assumption*, to spike in P3.
    - **Cleanup:** a cron removes expired sessions.
- **Several replicas:**
    - the Odoo bus uses PostgreSQL `LISTEN/NOTIFY`, so long-polling and websocket requests can land on any replica;
    - cron jobs are taken with `SELECT … FOR UPDATE SKIP LOCKED`, so cron containers in two replicas never run the same job twice;
    - both points are from Odoo's source design and are re-verified per version in P3;
    - the database manager stays restricted to the platform.
- **Failure handling:**
    - Spaces errors on upload return a user-facing error;
    - reads fall back to the cache;
    - the readiness and liveness probes never call Spaces, so a Spaces outage cannot cause a restart storm;
    - objects orphaned by rolled-back transactions are cleaned by a monthly bucket-against-database reconcile.
- **Acceptance (P3):**
    - a node drain with zero failed requests;
    - sessions survive a pod replacement;
    - two replicas with the bus working across them;
    - one hour of a simulated Spaces outage with no pod restarts.

### 6. Corrected cost model

**What was wrong:** revision 1 priced CPU at the full node price per CPU and RAM at the full node price per GiB, then added both. That charges each node twice.

**Corrected model.** Verified inputs:

- node price about $24;
- allocatable 1,900m CPU and 3,003 MiB;
- DaemonSets 412m and 412 MiB per node, leaving 1,488m and 2,591 MiB usable;
- cell services: 320m and 650 MiB of requests (CoreDNS, monitoring, operator, toolbox, cert-manager, Traefik).

```latex
\text{compute} = \frac{P_{node} \cdot \max\left(\frac{CPU_{req}}{CPU_{usable}}, \frac{RAM_{req}}{RAM_{usable}}\right)}{u_{pack}}
```

```latex
\text{cost} = \text{compute} + \text{DB volume} + \text{Spaces GiB} \cdot 0.02 + \text{backup GiB} \cdot 0.02 \cdot c + \frac{\text{cell and platform overhead}}{N_{tenants}} + \text{mail}
```

Here `u_pack` is the packing efficiency, *assumed* at 0.75; it covers rollout headroom, autoscaler buffer and fragmentation. `c` is 2 when backups are copied to a second region.

**Realistic utilization (verified):**

- tenants averaged 0.037 cores of CPU against 437m requested, so CPU requests are about 12 times actual use;
- memory working sets matched or exceeded requests;
- so memory, not CPU, is the binding resource.

A6 sets requests to measured values. Per environment that means CPU about 150m and memory about the p95 working set. This is assumed at 950 MiB for a 2-worker Production environment, from the observed range.

**Worked example: a 2-worker Production environment in a cell of 50 Production tenants (assumptions marked).**

| Line | Today's requests | Right-sized requests (A6) |
| --- | --- | --- |
| Requests | 437m / 448 MiB, so CPU binds at 29.4% | 150m / 950 MiB, so memory binds at 36.7% |
| Compute, ÷ 0.75 | $9.40 | $11.73 |
| Database volume (2 GiB) | inside the 10 GiB shared volume, $1.00 | $0.20 |
| Files (5 GiB, Spaces) | inside the volume | $0.10 |
| Backups (8 GiB after dedup, ×2 regions) | 7 full copies, about $1.00 | $0.32 |
| Overhead ÷ 50 | $1.44 | $1.44 |
| **Monthly cost** | **about $12.8 (with eviction risk)** | **about $13.8** |
| Price at 60% margin (÷ 0.40) | $32 | **$34.5** |
| Price at 70% margin (÷ 0.30) | $43 | **$46** |

The overhead line is about $72 a month in total, all *assumed* or from published prices:

- load balancer, $12 ([DOKS pricing](https://www.digitalocean.com/pricing/kubernetes));
- cell services, about 0.25 of a node, $6;
- Spaces subscription, $5;
- the control-plane VM, assumed $48;
- Prometheus volume, $1.

**Non-production with sleep** (*assumed*: awake 15% of the month, 400 MiB requested):

- compute about $0.75;
- share of the 2-instance shared database cluster, about $19 per cell split over 50 environments, about $0.40;
- files and overhead, about $0.30;
- total about $1.5 a month, against about $8 always on.

The saving becomes money only when the autoscaler removes nodes; on a small cluster it shows first as room for more tenants. The figures in Appendix D ($5.30 and $1.10) are superseded by this model.

**Mail:** SES is billed per message (pass-through; see the SES item in the plan). It is priced as an included quota per plan plus usage.

### 7. Inconsistencies resolved

| Inconsistency in revision 1 | Resolution |
| --- | --- |
| Two PostgreSQL engines (Managed StatefulSet and CloudNativePG) with different backup, PITR and HA stories | A1: CloudNativePG for all new environments; Managed only for existing tenants until they are moved |
| PgBouncer in session mode in front of the shared cluster | Removed at launch. Session mode multiplexes nothing for Odoo, which holds connections per worker and needs session features (`LISTEN/NOTIFY`). Per-role `CONNECTION LIMIT` and Odoo's `db_maxconn` cap connections. Add a pooler only if a shared cluster nears `max_connections` |
| Non-production gets HA (2 instances) while Production has none | Intentional and now stated: one shared-cluster failure would stop every non-production environment in the cell, while a Production database failure affects one tenant. Cost about $19 per cell (A9) |
| "HA add-on" drawn inside the Large pool | HA is pool-independent; it needs at least 2 nodes in the tenant's pool |
| Launch said "Standard, Shared and Build pools and a shared cluster", but those were P2 items | The pool table above: Build moves to P0 (security), Shared and the shared cluster arrive in P2, Large on demand |
| The CloudNativePG operator is assumed present but not vendored | Vendor its chart in `compute/charts/vendor/` (repository rule) in P0, so new Production environments can use it |
| PITR is "add-on, CloudNativePG mode only" | With A1, PITR is a setting on any new Production environment |
| HA in P4 depends on stateless Odoo (P3) | Kept in that order: HA needs two Odoo replicas, and those need no RWO volume |

### 8. Custom domains, TLS and tenant-safe routing (launch plan)

The wildcard covers only the platform's own tenant domain, such as `*.main.eagle-tech.info` today, and not a customer domain such as `erp.customer.com`. The two follow separate paths.

**Wildcard protections:**

- **Separate domain (A4):** tenant apps get their own registrable domain, separate from the portal, which removes cookie tossing into the portal. Submit it to the Public Suffix List's private section, so browsers isolate cookies between tenants too.
- **Locked-down key:** the wildcard key lives only in Traefik's namespace. The control plane's least-privilege role (P0.1) cannot read Secrets there or in `cert-manager`.
- **Narrow DNS credential:** `_acme-challenge.<tenant-domain>` is a CNAME into a dedicated challenge zone. That zone sits in a separate DigitalOcean team whose token can change nothing else, so a leaked cert-manager token cannot repoint tenant traffic.
- **Issuance controls:** CAA records allow only Let's Encrypt and only the platform's ACME account (`accounturi`), for both `issue` and `issuewild`. Certificate Transparency is watched, with an alert on any certificate for the tenant domain that the platform did not order.
- **Renewal:** renewal starts 30 days before expiry (cert-manager), with an expiry alert at 14 days.
- **Subdomain reuse:** a cancelled tenant's subdomain is quarantined for 90 days (*assumed*), so old links and bookmarks never reach a new customer.

**Custom domains (separate path):**

- **Claim:** the customer adds `erp.customer.com` to a Production environment and gets two records to create:
    - a CNAME to their environment's host, or an A record for an apex domain;
    - a TXT record `_veltnex-challenge.erp.customer.com` holding a random token bound to that customer and that environment.
- **Ownership proof:**
    - the TXT token proves control of the customer's DNS zone;
    - the CNAME or A record proves routing;
    - both are required before any certificate is ordered;
    - an HTTP check alone is not enough, because a dangling CNAME left by an old owner would let anyone who claims the name pass it.
- **Issuance:** a separate ClusterIssuer with its own ACME account, using HTTP-01 per host. It is ordered only after both checks pass, which avoids Let's Encrypt's failed-validation limits.
- **Ongoing ownership (A5):** the TXT record stays in place. It is re-checked daily and before every renewal. If it is missing for 7 days, the host is disabled, its renewal stops and the customer is told. The daily check alone never proves ownership; it only detects loss of it.
- **Ownership lost (A5 procedure).** This applies when the TXT record is deleted or changed, the domain expires, or someone else takes control of the zone:
    - **Day 0:** the daily check, or the pre-renewal check, fails. The finding shows on the dashboard and a short email goes to project owners. The domain keeps serving on its current certificate.
    - **Days 1–7:** a grace period. The customer restores the record, or re-verifies with a fresh token, at any time, and the finding clears.
    - **Day 7:** the host leaves routing, renewal stops, and the certificate is revoked through ACME. If it was primary, `web.base.url` returns to the platform address. The customer is told, and the binding becomes "released".
    - **Someone else proves a new TXT token for that host at any point:** the DNS owner wins. The old binding is removed at once, its certificate is revoked, and the previous customer is told. Nothing transfers automatically in the other direction.
    - **Routing record removed but TXT still present:** the customer is warned that traffic no longer reaches Veltnex. There is no takeover risk, because nobody else can prove the TXT.
- **Release and reuse:** removing a domain, or cancelling, takes the host out of routing at once. The record of the release is kept:
    - the same customer can re-add the domain with their existing token;
    - any other customer must prove a new TXT token, so a claim is never transferred automatically.
- **Routing safety:**
    - Traefik routes exact hosts only, with no tenant as a catch-all; unknown hosts get the default 404;
    - a host is unique across the platform, enforced by a database constraint and checked again by the operator before it writes an Ingress;
    - platform domains and the portal domain are on a blocklist;
    - wildcard customer domains are not supported at launch.
- **Primary domain:** the control plane sets `web.base.url` and `web.base.url.freeze` in the Production database. The default address keeps working.

**Mail with AWS SES (decided and approved 2026-10-11, A7).** Nothing in the cluster receives SMTP from the internet. The only change is outbound mail toward SES.

- **No AWS credentials in tenant namespaces:**
    - one SES credential exists, in a Secret in the relay's platform namespace (`veltnex-mail`);
    - tenants never hold AWS credentials, IAM users or SES keys;
    - the relay sends to SES over its HTTPS API (443), so it needs no SMTP egress either.
- **The relay:** a small in-cluster SMTP relay with 2 replicas.
    - **Authentication:** each Production environment gets its own relay username and password. The platform issues them, stores them encrypted and can revoke them. They are worthless outside the cluster.
    - **Injection:** the control plane writes them into the Production database as the outgoing mail server.
    - **Neutralization:** staging and dev are neutralized to Mailpit and have no relay credential at all.
- **Usage isolated per customer:**
    - **Sender domains:** the relay accepts only `From` domains verified for that customer. These are the customer's own domain (SES DKIM records shown in the portal next to the custom-domain records) or a platform mail subdomain for customers without one.
    - **Tagging:** every message is tagged with the tenant id in an SES message tag.
- **No unlimited sending:**
    - per-environment rate limits (messages per minute and recipients per message) are enforced in the relay;
    - a daily and monthly quota per plan is enforced by the control plane from SES send events, and it disables the relay credential when exceeded; overage is billed only if the plan allows it;
    - a global limiter keeps the whole cell under the SES account's sending rate.
- **Spam and abuse:**
    - SES bounce and complaint events, through SNS to the control plane, pause a customer above set rates; *assumed* starting points are 5% bounces or 0.1% complaints;
    - new customers start with a low quota that rises with clean history;
    - message size and recipient caps apply;
    - every send is logged per tenant for support and billing.
- **Customers' own SMTP:** direct SMTP egress is closed by default, so nobody can bypass the quotas from platform IPs. A customer who must use their own provider gets a staff-approved egress exception for that host only.
- **Prerequisites:** take the SES account out of the sandbox, and choose the SES region (*assumed* eu-central-1).

## Performance monitoring per customer

**Goal:** find each customer's slow and expensive queries, show what they cost in database time and user-facing latency, and notify the customer only when it is worth their attention. The platform never changes a customer database by itself; it tells the customer, or their developer, what to fix. It is built at the lowest cost: no new always-on service, and reuse of what already runs.

**What is measured, at the lowest cost:**

| Signal | Why it matters | Source (cost) |
| --- | --- | --- |
| Query duration (mean, max) | Finds slow queries | `pg_stat_statements` (already in Production tuning plans; a few % CPU, *assumed*) |
| Total execution time | Finds queries that consume most database time | `pg_stat_statements.total_exec_time` as a share of the tenant total |
| Call count | Tells a rare slow query from an average one repeated thousands of times (N+1) | `pg_stat_statements.calls` |
| Rows read against rows returned | Hints at a missing or unused index | Blocks read per call against rows per call (`pg_stat_statements`); `seq_scan` and `seq_tup_read` against `idx_scan` (`pg_stat_user_tables`). Plans are on demand with `EXPLAIN (GENERIC_PLAN)` on PostgreSQL 16+, not `auto_explain` logging, so no log pipeline is needed |
| Lock waits | Finds queries blocked by other transactions | A custom query in the CloudNativePG metrics exporter: the longest current lock wait and the count of waits over 5 s, scraped by Prometheus every 30 s; plus `pg_stat_database.deadlocks` |
| Database CPU and connections | Ties slow queries to pressure | Container CPU against the limit (cAdvisor, already scraped); connections against `max_connections` (exporter) |
| Odoo request latency | What users actually feel | p50 and p95 per environment from Traefik's request-duration histogram, already in Prometheus at no extra cost. Per-endpoint detail is deferred, because it needs a log pipeline |

**One shared Veltnex service, not a stack per tenant.** Collection is a single CronJob per cell. Analysis, the Advisor and notifications run once in the control plane for all customers. No tenant gets its own exporter, Prometheus, dashboard server or agent.

**How it runs (lowest cost):**

- **Collector:** a CronJob in a platform namespace, not an always-on service. It runs every 15 minutes and reads each Production database with a read-only `pg_monitor` role that lives only in the platform namespace. Tenant network policies admit it on 5432 from that namespace only.
- **What it keeps:** the deltas of `pg_stat_statements`, of which it keeps only:
    - the top 10 fingerprints by total time;
    - anything above the environment's thresholds.

  Query text is normalized (constants become `$1`), so no customer values leave the database. On shared clusters, rows are filtered by database.
- **Storage:** in the control-plane database. There are two tables:
    - `saas.perf.finding`: one row per environment and query fingerprint, updated in place;
    - `saas.perf.sample`: hourly points, kept 14 days for basic insights and 90 for the paid tier.

  At 1,000 environments that is about 240,000 small rows a day (*assumed* sizing). Prometheus keeps only per-environment series; query ids never become labels.
- **Attribution:** a fingerprint's tables map to Odoo models (`ir_model`) and to the modules that define them (`ir_model_data`). A finding says "from your module X" or "from core module Y".
- **Database settings:**
    - `pg_stat_statements`, `track_io_timing`, and `log_lock_waits` with `deadlock_timeout` at 1 s;
    - new environments get them at creation;
    - existing tenants get them at their next customer-chosen maintenance window, because a preload library needs a restart.

**Performance Advisor.** A finding never says only "Slow query detected". Every finding answers six questions on the dashboard:

| Question | How the Advisor answers it |
| --- | --- |
| What is the problem? | A plain sentence plus the normalized query, its tables, the Odoo model and module behind it ("from your module X" or "from core module Y"), and the class: slow, heavy, repeated (N+1), possible missing index, lock contention, or database pressure |
| How often does it happen? | Calls per hour, the hours of the day it runs, and how long the pattern has existed (first seen, last seen) |
| Does it really affect performance? | Its share of the database's time; whether the database was CPU-, connection- or lock-bound when it ran; and the environment's p95 latency over the same windows. Each finding is labelled "affecting users", "using capacity" or "minor" |
| What are the likely causes? | Rule-based by class: a sequential scan on a large table (missing index); many calls with few rows each (N+1 from a loop in custom code); waits behind a long transaction (cron or import); high temporary-file use (sort or join over memory); growth in table size. The generic plan is shown where PostgreSQL 16+ allows |
| What should be done? | Concrete steps for the class, such as: add `index=True` to the field used in the filter; batch with `read_group` or prefetch; split the cron's transaction; move to a larger plan. Each step links to a short guide |
| Did it get better after the change? | The Advisor marks the deploy (build id) or the time the customer clicks "I changed something". It compares the fingerprint's mean duration, calls and share of database time over the 24–48 hours before and after, and shows Improved, No change or Worse with the numbers. A finding that improves enough closes as "Resolved by your change" |

**When the customer is notified.** Never an email per slow query, which would cause notification fatigue and cost without benefit. There are three levels:

| Level | Trigger (default; adjustable) | Where it shows |
| --- | --- | --- |
| 1 · Recurring slow query | A query above 2 s that keeps recurring (above the threshold in 2 consecutive hourly windows), or one that exceeds the environment's performance budget | Dashboard only, with similar findings grouped by fingerprint |
| 2 · High cumulative consumption | A short query repeated tens of thousands of times that takes a large share of database time (default: over 20% for 3 hours) | Dashboard, with an in-portal badge; the finding explains the impact (share of database time, calls per hour, effect on latency), not just the single run time |
| 3 · Noticeable degradation | Sustained rise for 15 minutes in p95 latency, database pressure (CPU above 80% of the limit, connections above 85%) or lock waits (over 30 s) | Portal banner, plus one short email only when it is marked "affecting users" (p95 latency also above budget). The email carries no recommendations, only a link to the dashboard |

**Anti-fatigue rules:**

- **Recommendations stay on the dashboard.** It is clearer and cheaper; no recommendation or digest is ever emailed.
- **One finding per fingerprint:** one open finding per fingerprint. It is highlighted again only if it gets at least 50% worse, or after a 7-day cooldown.
- **Auto-resolve:** a finding resolves itself after 24 hours below its threshold.
- **Email cap:** at most 1 Level 3 email per environment every 6 hours.
- **Acknowledge or snooze:** customers can acknowledge or snooze a finding.

**Adjustable thresholds:**

- **Starting defaults:** the numbers above are starting defaults, not limits suitable for every application.
- **Profiles:** each environment gets defaults from its database size class and a workload profile:
    - *Standard* (interactive ERP);
    - *Reporting-heavy*, with relaxed duration thresholds;
    - *Batch and imports*, with relaxed lock thresholds inside declared batch windows.
- **Overrides:** customers can override the performance budget, thresholds and quiet hours per environment in the portal.
- **Tuning:** defaults are tuned from the data collected in the first months.

**The dashboard finding card** (the approved layout; metrics are illustrative, not tenant data). The same header, without the recommended actions, is the short Level 3 email:

```text
Veltnex Performance Alert
Production · Database Performance
Needs attention

A database query is consuming excessive resources
We detected a query pattern that is contributing significantly to your database execution time.

Average duration     2.8 sec
Calls per hour       18,400

Recommended actions
- Review the query's execution plan.
- Check whether appropriate indexes exist.
- Investigate repeated queries and possible N+1 patterns.
- Review custom Odoo modules that may be generating unnecessary database calls.
```

The card opens the full finding: the normalized query, its tables and module, the trend, the generic plan where available, and the before-and-after check.

**Cost:** the CronJob costs a few CPU-minutes per cell every 15 minutes. Storage is a few GB in the control-plane database. Level 3 emails are rare and go through the mail relay. Basic insights are included in every Production plan; the 90-day history and the plan and index-hint views are the paid tier (A10).

## Key architecture decisions

| # | Decision | Rejected alternatives | Why it wins |
| --- | --- | --- | --- |
| D1 | One namespace per environment on shared clusters | Cluster per customer; one Odoo for many databases | Custom code per customer rules out a shared Odoo; a cluster each multiplies fixed cost |
| D2 | Dedicated CloudNativePG (1 instance) per Production environment (A1) | Production shards; the Managed StatefulSet; managed databases | About $2 per tenant a month buys a one-tenant failure domain, per-tenant versions and PITR and HA as settings (concern 1) |
| D3 | Shared CloudNativePG per PostgreSQL major for non-production, 2 instances, no PgBouncer at launch | Dedicated pods; a pooler in session mode | The saving is in idle non-production environments, and a pooler adds a hop with no gain (concern 7) |
| D4 | Production bucket and non-production bucket per customer, through one server-wide module | One bucket per customer; a bucket per environment; volumes | Keys are per bucket only (verified, concern 3) |
| D5 | Sessions in a per-environment PostgreSQL database (A3) | Redis; `emptyDir` | No new service; survives pod moves and replicas |
| D6 | Sleep non-production (dev 1 h, staging 2 h, trials 1 h); Production never | Always on; deleting dev environments after about 3 days as Odoo.sh does | Largest saving; the customer keeps their data |
| D7 | HA as a paid add-on | HA for all | It doubles the database and Odoo pods |
| D8 | PITR as a paid setting on CloudNativePG Production | PITR for all | WAL storage and restore drills cost money |
| D9 | Builds on a tainted Build pool from P0, with a push-only credential there and pull-only in tenants | Builds on tenant nodes | Unconfined sandboxes must not share nodes with tenants (concern 2) |
| D10 | Scale by cells | One giant cluster | Blast radius and upgrade risk |
| D11 | The control plane stays Odoo, with batched reads; platform database backups owner-managed | Rewrite | The bottleneck is per-tenant polling |
| D12 | Pools by phase: Standard now, Build in P0, Shared in P2, Large on demand | All pools at launch | Pay for a pool only when it has a job |
| D13 | Tenant apps on their own registrable domain, on the Public Suffix List (A4) | Subdomains of the portal domain | Cookie isolation between tenants and from the portal |
| D14 | Wildcard for platform hosts; custom domains on a separate issuance path with TXT ownership | HTTP-01 per platform host; HTTP-only verification for custom domains | Rate limits, and takeover safety (concern 8) |
| D16 | Per-customer performance insights: a CronJob collector reading `pg_stat_statements`, the exporter's lock metrics and Traefik latency, with findings stored in the control-plane database (A10) | A per-tenant monitoring stack; an always-on perf service; `auto_explain` with a log pipeline; query ids as Prometheus labels | Lowest cost; no new always-on service; recommendations on the dashboard, not by email, avoiding notification fatigue |
| D15 | Mail through AWS SES behind a platform relay that holds the only credential (A7) | An IAM user per customer; one shared SES credential in tenants; customers' own SMTP only | No AWS credentials in tenants; quotas, rate limits and abuse pauses per customer |

## Scalability analysis

**The first limits are not Kubernetes:**

- Let's Encrypt's limit on the tenant domain (fixed by P0.7);
- control-plane polling per tenant (P2.4);
- every backup starting at 02:00 (P0.16);
- pool ceilings (P0.12);
- Spaces account limits, which now matter twice as much: 100 buckets is 50 customers with A2, so request an increase before launch.

| Bottleneck | Trigger | Response |
| --- | --- | --- |
| Spaces buckets and keys (2 per customer) | 70% of 100 buckets or 200 keys | Ask support for an increase before launch; recycle buckets through the pool |
| Shared non-production cluster | 70% of connections, sustained CPU above 60%, about 200 databases, or 70% disk (*assumed*) | Open the next shared cluster in the cell |
| Dedicated Production databases | None shared; each scales with its plan | Larger plan or the Large pool |
| CloudNativePG operator | About 1,000 clusters per cell (*assumed*) | Watch reconcile latency; tune concurrency; open a new cell |
| Operator | Reconcile latency above 30 s | 2 replicas with leader election; narrower informers |
| Cell | About 1,500 environments or 60 nodes (*assumed*) | New cell |
| Control-plane crons | A cycle overruns its interval | Batched reads per cluster |

**How it grows:**

1. **Launch (P0 and P1):** one cell, the Standard pool and the Build pool. New Production environments run on dedicated CloudNativePG (after A1). Non-production still runs dedicated, always on.
2. **P2:** the Shared pool, shared clusters, sleep and archiving.
3. **P3 and P4:** stateless Odoo, the Large pool on demand, HA and PITR, then more cells.

## Security and reliability review

| Severity | Risk | Evidence (verified) | Safeguard |
| --- | --- | --- | --- |
| Critical | The control plane holds cluster-admin | `bootstrap.py:215` | P0.1 least privilege |
| Critical | Customer code images may be public | 04 docs; per-cluster registry | P0.3 private registry |
| High | Builds run customer code unconfined on tenant nodes | `k8s_builds.py:288`, no node selector | P0.19 tainted Build pool |
| High | A push-capable registry login sits in every tenant namespace | `k8s_builds.py:412` | P0.3 pull-only in tenants |
| High | Tenants and portal share a registrable domain | live CR host and portal host | P0.20 (A4) |
| High | Staging and dev keys could reach Production files under one bucket per customer | DigitalOcean keys are per bucket | P1.4 (A2) |
| High | One backup key in every tenant namespace | `saas_instance.py:3787` | P0.5 |
| High | Restore drops the database first | `run-restore.sh:125` | P0.4 |
| High | PostgreSQL 17 backups fail; major changes break the database | backup Dockerfile; `database.go:256` | P0.17, P0.6 |
| High | Rate limits can be bypassed with a forged header; secrets fall back to plaintext | `api.py:162`, `crypto.py` | P0.8 |
| High | No 2FA; CI does not gate; alerts reach only the log | — | P0.9–P0.11 |
| High | Memory requests are below working sets, so evictions are possible under pressure | `_package()` against Prometheus | P0.21 (A6) |
| Critical (launch blocker) | The tenant database login is a superuser for every environment; the SQL console uses it | `database.go:92`, `saas_instance.py:2191` | P0.24: a limited role for every environment, existing and new |
| Medium | Shell can write sessions and the data-dir addons | `deployment.go:225` | P1.10 |
| Medium | Egress 443 reaches in-cluster addresses; tenants have no outgoing mail path | `networkpolicy.go` | P0.13 and P1.8 |
| Medium | Backups do not record code identity, and garbage collection can race the file copy | `run-backup.sh` | P1.5 manifest v2 and snapshot file list |
| Medium | Cancel deletes after a 2-minute wait for the final backup | `finalize.go:211` | P0.14 |
| Medium | One operator replica | `deployment.yaml:45` | P0.15 |
| Resolved | Platform database backups | owner-managed | Monthly restore drill |

**Odoo 14–20 are sold (decided; revised from 11–20 on 2026-10-11).** Versions Odoo no longer patches carry a portal notice and an upgrade offer. Per-environment isolation contains a compromise to that customer.

**Failure domains:**

| Failure | Target |
| --- | --- |
| Odoo pod or node | Before P3: 1–3 minutes. After P3: rescheduled on any node; HA tenants fail over |
| Production database | Base plans: restore from the nightly snapshot set, losing up to 24 hours. PITR tenants: under 5 minutes lost |
| Shared non-production cluster | CloudNativePG standby takes over; Production unaffected |
| Cell | Scripted restore of each tenant into another cell from Spaces |
| Spaces region | Production backups copied nightly to the second region |
| SES or relay | Mail stays queued in Odoo and is retried; Production keeps working |
| Let's Encrypt or DNS | Existing certificates last up to 30 days past the first failed renewal; alert at 14 days |

## Required platform capabilities

**These must exist on the platform (owner's list, 2026-10-11).** Most already exist. The gaps get launch items, except stored log history and automated OpenUpgrade runs, which come right after launch. "Today" is verified in the code on 2026-10-11.

| Area | Capability | Today (verified) | Gap → plan item |
| --- | --- | --- | --- |
| Git and CI/CD | Connect GitHub | GitHub, GitLab, Gitea, Bitbucket; signed push webhooks (`controllers/webhook.py`) | — |
| | Build on push | BuildKit Job per push (`k8s_builds.py`, `saas_instance_build.py`) | Cache (P2.6), Build pool (P0.19) |
| | Branches mapped to dev, staging, Production | `_env_branch`, `action_link_environment_branch` | — |
| | Automated tests, build failure detection | Build status and the update gate; **no test runs** | P1.11 tests per push |
| Shell | Web shell | Browser terminal over WebSocket exec (`ssh_terminal.py`), into a shell container with no secrets | Hardening (P1.10) |
| | SSH | **None:** no key-based SSH | P1.13 SSH gateway |
| | Odoo shell and Python console | **Not usable:** the shell container has no `odoo.conf`, so `odoo shell` cannot connect | P1.12 |
| Dependencies | `requirements.txt` and Python dependencies | Merged from every repository root at build (`templates/build/inspect.py`), installed in the image | Show the resolved package list per build (P1.11) |
| Addons | Community and custom addons | Official Odoo images plus the customer's repositories | — |
| | Git submodules | **None** (`fetch.sh` is shallow and drops `.git`) | P1.2 |
| Logs and monitoring | Live logs | Live tail of the web container | P1.14 adds cron, install, build and pip logs |
| | Build and server status | Build status; runtime observation (`saas_instance_health.py`) | — |
| | Availability and performance indicators | Usage dashboard (Prometheus); **no uptime percentage** | P1.15 uptime; P2.7 performance |
| | `pg_slow_queries.log` | **None** | P1.14 |
| Backups | Automatic Production backups | Daily backups exist, but are a **paid add-on** (`saas_master.daily_backup_pct`) | P1.16: included in every Production plan (A11) |
| | Manual backups, database import | Snapshots; import through a presigned upload | — |
| | Restore | Per-database and full-instance restore | Swap-safe restore (P0.4) |
| | Database plus filestore | Yes | Consistency model (P1.5) |
| | Production copy in staging | Yes, neutralized and PII-masked | Attachment rule (A8) |
| Staging | Test on Production data | Yes | — |
| | Mail catcher | **None** | P1.3 |
| | Promote staging to Production | Merge on the provider, **GitHub only** (`saas_instance_repo.py:879`) | P1.17: merge for all providers, plus a pre-deploy snapshot |
| Platform | Automatic mail servers | **None** | P1.8 relay into Production, Mailpit for the rest |
| | Customer domain on Production, subdomains for dev | Subdomains only | P1.1 |
| | Server management: monitoring, backups, DNS | Platform-side, partial | P0.7, P0.11, P0.12, P1.1 |
| | Database upgrade with a test before the final run | **Module upgrades only** (`_run_upgrade` runs `odoo -u`) | P1.18 version upgrade workflow; P2.9 OpenUpgrade automation |
| | Access control by role and environment | Fine-grained permissions per environment (`saas_iam/roles.py`); **no persona roles** | P1.19 Developer, Tester and Project Manager presets |
| | History of builds, imports and restores | Builds and database operations recorded separately | P1.20 one activity timeline per environment |

**Two contradictions to fix:**

- **Backups:** automatic Production backups must be included, not sold separately (A11). Their cost is already in the cost model (about $0.32 a month for a 2-worker plan).
- **Website claims:** the website promises a "99.99% uptime SLA" (`frontend/veltnex/src/pages/Hosting.tsx:95`). A single Odoo pod is down 1–3 minutes on a node failure, and MAINTAINING.md forbids SLA promises. Change it to a target, or sell an SLA only with the HA add-on (A13, P0.23).

## Odoo.sh competitive gap analysis

Unchanged from revision 1, plus mail: Odoo.sh catches staging mail and sends Production mail itself. Veltnex matches that with Mailpit and SES. Sources: [branches](https://www.odoo.com/documentation/18.0/administration/odoo_sh/getting_started/branches.html) and [pricing](https://www.odoo.sh/pricing), read on 2026-10-11.

| Capability | Odoo.sh | Veltnex today | Verdict |
| --- | --- | --- | --- |
| Custom domains with TLS | Yes | Missing | Launch (P1.1) |
| Git submodules | Yes | Missing | Launch (P1.2) |
| Mail catcher | Yes | Missing | Launch (P1.3) |
| Outgoing mail service | Yes | Missing | Launch (P1.8, SES) |
| Backups in several locations | 7 daily, 4 weekly, 3 monthly; 3 locations | 7 dumps, one bucket | Launch (P1.5) |
| Logs | Stored log files, including `pg_slow_queries.log` | Live tail only | Launch: named logs including `pg_slow_queries.log` (P1.14); stored 7 days (P2.8) |
| Staging from neutralized Production | Yes | Yes, plus PII masking | Advantage |
| Dev builds with tests | Yes; kept about 3 days | Production copy; kept; no tests | Tests per push at launch (P1.11); persistence is an advantage |
| SSH and Odoo shell | Yes | Browser shell only | Launch (P1.12, P1.13) |
| Version upgrades | Upgrade service with a test database | Module upgrades only | Launch workflow (P1.18) |
| Git providers | GitHub | 4 providers | Advantage |
| HA and PITR | Not on the pricing page | Missing | Premium add-ons |
| Enterprise licence | Excluded from hosting price | Community images | Business decision |

## Prioritized roadmap

```mermaid
flowchart TD
    P0["P0 · Launch blockers (before launch)<br/>Least-privilege access · private registry, pull-only tenant credentials · tainted Build pool<br/>Restore swap · backup key scope · spread backups · tools per PG major · major-version guard<br/>Wildcard TLS with CAA, delegated DNS-01, CT watch · separate tenant domain + PSL<br/>Real client IP, fail-closed secrets · 2FA · CI gates · alerts · capacity reader<br/>Egress: private ranges denied, mail only via relay · right-sized memory · 60% price floor · vendored CloudNativePG"]
    P1["P1 · Launch features (before launch)<br/>Custom domains (TXT ownership, separate issuer, takeover rules) · submodules · Mailpit<br/>Production + non-production buckets · restic with manifest v2 and snapshot file list · second-region copy<br/>Mail relay to SES with quotas · CloudNativePG for new Production · non-superuser roles · shell hardening<br/>Odoo 14–20 matrix · tests per push · Odoo shell · SSH gateway · logs incl. pg_slow_queries.log<br/>Uptime % · backups included · promotion with pre-deploy snapshot · version upgrade workflow · persona roles · activity history"]
    P2["P2 · Cost engine<br/>Shared pool · shared CloudNativePG (2 instances, no pooler) · sleep 1 h / 2 h / 1 h<br/>Archive idle dev · batched control-plane reads · build cache · performance insights v1<br/>Stored logs (7 days) · OpenUpgrade automation"]
    P3["P3 · Stateless Odoo<br/>Spaces filestore with DB-stored assets and local cache · sessions database<br/>Drop node pinning · backups from a platform namespace · moves between pools"]
    P4["P4 · Premium and scale<br/>HA (2 instances + 2 Odoo pods) · PITR setting · Large and dedicated pools<br/>More cells · control plane on 2 servers · performance insights paid tier"]
    P0 --> P1
    P1 -- "Gate A · launch" --> P2
    P2 -- "Gate B · growth" --> P3
    P3 -- "Gate C · premium demand" --> P4
```

## Open questions and assumptions

**Decided on 2026-10-11:**

- Odoo 14 to 20 are sold (revised from 11–20).
- 60% gross margin at launch, 70% or more at scale, never at the cost of security or Production stability.
- Production never sleeps; dev sleeps after 1 idle hour, staging after 2, trials after 1.
- The owner backs up the platform database.
- Customers can connect custom domains such as `erp.customer.com` to Production.
- Outgoing mail goes through AWS SES.

**Status:** A1–A13 approved on 2026-10-11, subject to the launch acceptance criteria.

**Still open:**

1. **Archiving:** how many idle days before a dev environment is archived? *Assumed: 14.*
2. **Trials:** do they share the non-production cluster, and how long do they last? *Assumed: yes, 14 days.*
3. **Paid add-ons:** sell HA, PITR and dedicated pools at launch, or on demand?
4. **Second region** for backup copies. *Assumed: ams3.*
5. **Enterprise:** Enterprise hosting or Community only?
6. **New tenant domain name** (A4).

**Evidence still needed (each has a test in the plan):**

- read-after-write behaviour of Spaces;
- Odoo 14 on PostgreSQL 13;
- the relay's sustained throughput against the SES account rate;
- the restic deduplication ratio with uncompressed dumps;
- BuildKit not exposing the client credential to `RUN` steps;
- the real p95 memory per plan, which sets A6's numbers;
- which registry live clusters push to.
