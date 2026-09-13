# PERFORMANCE & SCALABILITY AUDIT — Odoo 18 SaaS Platform

> Focus: query patterns, crons, provisioning, background processing at "thousands of paying
> customers" scale. File:line verified.
>
> Severity counts (this file): **Critical 0 · High 5 · Medium 7 · Low 2** (14 findings)

---

## PERF-001
- **Severity:** High
- **Component:** Backup cron — serial execution
- **File Path:** `saas_core/models/saas_instance_backup.py`
- **Line Numbers:** `_cron_backup_all_instances` `:1741-1781`
- **Description:** The backup cron iterates all running instances and backs them up **one at a
  time** (per-instance `pg_dump` + zip + upload), each potentially taking minutes to tens of
  minutes for large DBs. Wall-clock = sum of all backups.
- **Business Impact:** At thousands of instances the backup window cannot close within a day;
  backups silently fall behind, breaking the RPO promise and risking data loss.
- **Technical Impact:** No parallelism, no per-backup timeout, no sharding across workers.
- **Recommended Fix (impact: turns O(N) serial into O(N/parallelism)):** Enqueue backups to a
  job queue and run a bounded pool (e.g. 5-10) in parallel with hard per-job timeouts; shard
  by host; track lag as a metric.

---

## PERF-002
- **Severity:** High
- **Component:** Synchronous provisioning in request path
- **File Path:** `saas_website/controllers/api.py`
- **Line Numbers:** `:740-859` (instance_action / set_repo)
- **Description:** Instance actions launch heavy SSH work via in-process threads tied to the
  HTTP worker. Under load these threads consume worker capacity and memory; worker recycling
  drops in-flight jobs. No queue smooths bursts.
- **Business Impact:** Provisioning throughput is bounded by HTTP worker count; traffic spikes
  cause failed/dropped operations and degraded portal responsiveness.
- **Technical Impact:** Thread-per-operation in the web tier; no backpressure.
- **Recommended Fix:** Move to a durable job queue (ARCH-004); web tier only enqueues.

---

## PERF-003
- **Severity:** High
- **Component:** Unbounded `search()` in crons
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** `:3952` (retry pending), `:4063-4070` (recover stuck), `:8401-8406` (storage limits)
- **Description:** Multiple crons `search()` across all instances with no `limit`, loading the
  full set into memory and iterating in Python.
- **Business Impact:** Memory spikes and long cron runtimes at scale; cron stacking; DB lock
  pressure during peak.
- **Technical Impact:** O(N) memory; full scans; Python-side filtering of large recordsets.
- **Recommended Fix:** Batch with `limit`/offset or `search_read` of only needed fields;
  push filtering into the SQL domain; process in chunks.

---

## PERF-004
- **Severity:** High
- **Component:** Metrics sampler — sequential host SSH
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** `_cron_record_metrics` `:4467-4505`
- **Description:** Per-host `docker stats` sampling runs hosts sequentially. With many hosts on
  a 5-minute cadence, a run can exceed its window and overlap the next.
- **Business Impact:** Stale metrics degrade alerting/autoscaling guidance; overlapping runs
  waste SSH capacity and can throttle hosts.
- **Technical Impact:** No parallel fan-out; no per-run deadline.
- **Recommended Fix:** Parallelize across hosts (worker pool) with a hard deadline; or push
  metrics from a host agent to a TSDB.

---

## PERF-005
- **Severity:** High
- **Component:** Backup staging to local disk
- **File Path:** `saas_core/models/saas_instance_backup.py`
- **Line Numbers:** `:933-1005`
- **Description:** On-demand backups stage the full `pg_dump`/zip to host `/tmp` before
  uploading, with multiple fallback paths. Large DBs (multi-GB) must fully materialize on disk
  first; `/tmp` pressure causes confusing IO failures and competes with running tenants.
- **Business Impact:** Backup failures on the largest (highest-value) tenants; host disk
  contention impacts co-tenants.
- **Technical Impact:** No streaming to object storage; disk-bound.
- **Recommended Fix:** Stream `pg_dump` directly to object storage via presigned PUT; cap
  concurrency; alert on staging-disk pressure.

---

## PERF-006
- **Severity:** Medium
- **Component:** Invoice/billing aggregation
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** invoice gathering `~:1450-1482`
- **Description:** Per-instance invoice/order lookups with Python-side `.filtered()`/`.sorted()`
  produce N+1 patterns when rendering dashboards over many instances.
- **Business Impact:** Slow portal dashboards as customers/instances grow; poor perceived
  performance.
- **Technical Impact:** O(instances × invoices) work in Python instead of one grouped query.
- **Recommended Fix:** Single grouped `read_group`/SQL keyed by instance; cache per request.

---

## PERF-007
- **Severity:** Medium
- **Component:** Missing indexes on hot filter columns
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** `init()` `:1101-1190` (creates subdomain/port indexes only)
- **Description:** `init()` adds unique indexes for subdomain and ports, and a metrics
  composite index, but common cron/portal filters (`state`, `docker_server_id`, `partner_id`,
  `plan_id`) are not indexed, so those queries scan.
- **Business Impact:** Cron and listing latency grows with the table; compounds PERF-003.
- **Technical Impact:** Sequential scans on frequently filtered columns.
- **Recommended Fix:** Add indexes on `(state)`, `(docker_server_id, state)`, `(partner_id)`,
  `(plan_id, state)`; validate with `EXPLAIN`.

---

## PERF-008
- **Severity:** Medium
- **Component:** SPA polling load
- **File Path:** `veltnex/src/context/InstancesContext.tsx`; `veltnex/src/pages/portal/InstanceDetail.tsx`
- **Line Numbers:** `InstancesContext.tsx:~87`; `InstanceDetail.tsx:97-108`
- **Description:** The SPA polls status endpoints on fixed short intervals (~4s) with no
  exponential backoff on errors and no pause when the tab is hidden. At many concurrent
  customers this is sustained avoidable API load that worsens during incidents (every client
  hammers a degraded backend).
- **Business Impact:** Self-inflicted load amplification during outages; higher infra cost.
- **Technical Impact:** No backoff/jitter; no visibility-based pausing.
- **Recommended Fix:** Exponential backoff + jitter on failure; pause on `document.hidden`;
  prefer SSE/websocket for live status where available.

---

## PERF-009
- **Severity:** Medium
- **Component:** Cron partial commits inside loops
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** `:3977, 3998, 4121, 4458, 4504`
- **Description:** Per-iteration commits in large cron loops increase WAL/commit overhead and
  lock churn at scale (in addition to the idempotency concern in ARCH-011).
- **Business Impact:** Longer cron runtimes and more lock contention as the fleet grows.
- **Technical Impact:** High commit frequency; reduced batch efficiency.
- **Recommended Fix:** Chunked commits (e.g. every N records) or per-item jobs.

---

## PERF-010
- **Severity:** Medium
- **Component:** Recurring-billing cron scope
- **File Path:** `saas_core/data/saas_recurring_billing_cron.xml`; `saas_core/models/saas_instance.py`
- **Line Numbers:** cron defs `:5-96`; `_cron_generate_recurring_invoices`, `_cron_check_overdue_invoices`, `_cron_retry_failed_payments`
- **Description:** Nine billing/lifecycle crons run on `saas.instance`/`saas.wallet`. Without
  explicit batching and given the unbounded-search pattern (PERF-003), monthly billing over
  thousands of instances runs as one long transaction-heavy sweep.
- **Business Impact:** Billing runs may overrun their window or contend with peak traffic;
  delayed invoicing affects cash flow.
- **Technical Impact:** No sharding of the billing run; single-process sweep.
- **Recommended Fix:** Batch + parallelize billing by due-date shards; make each invoice
  generation an idempotent job.

---

## PERF-011
- **Severity:** Medium
- **Component:** Advisory lock contention (throughput)
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** `:3584-3587, 3723-3762, 5114-5122`
- **Description:** Long-held per-server deploy/port locks (ARCH-009) serialize provisioning on
  each host, capping concurrent provisioning throughput per server to ~1.
- **Business Impact:** Onboarding bursts (e.g. a sales push) queue behind each other per host;
  slow time-to-first-instance.
- **Technical Impact:** Effective per-host concurrency = 1 during deploy.
- **Recommended Fix:** Shorten critical sections; allow concurrent deploys with
  index-enforced port uniqueness.

---

## PERF-012
- **Severity:** Medium
- **Component:** SQL Console / log result sizing
- **File Path:** `veltnex/src/pages/portal/SqlConsole.tsx`; `saas_core/models/saas_instance.py` (hosting_sql_query)
- **Line Numbers:** SPA `limit=1000`; backend query path `~:10617`
- **Description:** SQL console returns up to 1000 rows and logs render hundreds of lines
  client-side without pagination/virtualization; large results bloat payloads and freeze the
  browser.
- **Business Impact:** Customer-facing tool feels broken on large datasets.
- **Technical Impact:** No server-side pagination/streaming; full payload rendering.
- **Recommended Fix:** Server-side pagination + CSV export; virtualize large tables/log views.

---

## PERF-013
- **Severity:** Low
- **Component:** Home page 3D bundle
- **File Path:** `veltnex/src/pages/Home.tsx`; `veltnex/src/components/Globe.tsx`
- **Line Numbers:** `Home.tsx:32-34` (lazy import)
- **Description:** The Globe (three.js) is lazy-loaded but heavy (~200KB+) and blocks the hero
  visual on slow networks with only a null fallback.
- **Business Impact:** Higher mobile bounce / CLS, hurting marketing-page conversion.
- **Technical Impact:** Large dependency on the most-visited page.
- **Recommended Fix:** Skeleton placeholder; lighter/SVG fallback on slow connections.

---

## PERF-014
- **Severity:** Low
- **Component:** Broad exception handling masking slow paths
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** 85× `except Exception:`
- **Description:** Broad catches can hide retries/timeouts that quietly add latency, making
  performance regressions hard to attribute.
- **Business Impact:** Latent slowness goes undiagnosed.
- **Technical Impact:** Reduced observability of slow code paths.
- **Recommended Fix:** Narrow handling + timing/telemetry on remote calls.
