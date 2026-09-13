# SCALABILITY AUDIT — V2 (Second Pass)

> Re-audit assuming all V1 findings are fixed. Only **NEW** scalability issues, focused on
> the path to thousands of tenants across many hosts. File:line verified. V1 items
> (god-model, control-plane SPOF, no job queue, serial backup cron, unbounded crons,
> sequential metrics, missing indexes, long lock holds, etc.) are **excluded** as fixed.
>
> Severity counts: **Critical 0 · High 3 · Medium 5 · Low 3** (11 findings)

---

## SCALE-001
- **Severity:** High
- **Component:** Shared nginx proxy — concurrent reload
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** `:8040-8058`, `:8209-8223`, `:8246` (write vhost + `systemctl reload nginx`)
- **Description:** When several instances share one proxy server (`proxy_server_id`), each
  provision/restore writes a vhost and runs `systemctl reload nginx` with no per-proxy
  serialization. Concurrent provisions race on `nginx -t`/reload; a failed reload can leave
  the shared proxy serving a stale or partial routing table for all instances on it.
- **Business Impact:** A burst of signups on a shared proxy can interrupt routing for
  unrelated, already-live tenants — a multi-tenant blast radius from a single new deploy.
- **Technical Impact:** No mutex around the read-modify-reload of shared nginx state.
- **Recommended Fix:** Take a per-proxy advisory lock around vhost write + `nginx -t` +
  reload; coalesce rapid reloads (debounce) on busy proxies.

---

## SCALE-002
- **Severity:** High
- **Component:** Deploy critical section includes nginx + certbot
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** port lock `:3723-3762`; nginx/cert provisioning within `_do_deploy_locked` `:7985-8058`
- **Description:** The per-server port advisory lock is held for the whole deploy transaction,
  which includes nginx provisioning and certbot issuance (60-120s on slow ACME). Concurrent
  deploys on the same host therefore queue behind each other's certificate issuance, not just
  the millisecond port allocation. (Sharper, distinct instance of the general long-lock concern.)
- **Business Impact:** Effective per-host provisioning concurrency collapses to ~1 during
  certbot; bulk onboarding (region launch, marketing spike) serializes into minutes-per-instance.
- **Technical Impact:** Lock scope vastly exceeds the invariant it protects (unique port pair).
- **Recommended Fix:** Release the port lock immediately after ports are written to the row;
  move nginx/certbot outside the locked section; rely on the unique port index for safety.

---

## SCALE-003
- **Severity:** Medium
- **Component:** Non-atomic nginx config writes
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** `:8040-8048`, `:8209-8210` (`ssh.write_file(nginx_path, ...)`)
- **Description:** Vhost configs are written directly to the live path. An interrupted write
  (SSH drop, worker kill, disk full) leaves a partial/corrupt config; if the cleanup `rm` also
  fails, the next `nginx -t` breaks reloads for the whole shared proxy.
- **Business Impact:** One failed deploy can wedge nginx for all co-located tenants until
  manual cleanup.
- **Technical Impact:** No write-temp-then-atomic-rename; no validation before activation.
- **Recommended Fix:** Write to `<path>.new`, `nginx -t` it, then atomic-rename into place;
  keep the previous file until validation passes.

---

## SCALE-004
- **Severity:** Medium
- **Component:** Metrics table growth / retention
- **File Path:** `saas_core/models/saas_instance_metric.py`; pruning in `saas_core/models/saas_instance.py`
- **Line Numbers:** model `:1-38`; prune cron `~:4508-4514`
- **Description:** At N instances × ~288 samples/day × retention window, the metric table reaches
  tens of millions of rows. Pruning is a single raw `DELETE`; if the cron is disabled, slow, or
  the table is not regularly vacuumed/reindexed, inserts and dashboard reads degrade and disk
  can fill. There is no partitioning and no table-size observability.
- **Business Impact:** Metrics/dashboards slow down platform-wide as the fleet grows; disk-fill
  risk on the control DB.
- **Technical Impact:** Unpartitioned high-churn table; prune is all-or-nothing with no
  monitoring.
- **Recommended Fix:** Range-partition by time (drop old partitions instead of `DELETE`), or
  move metrics to a TSDB; add table-size/lag metrics and routine vacuum/reindex.

---

## SCALE-005
- **Severity:** High
- **Component:** Server capacity computation under concurrency
- **File Path:** `saas_core/models/saas_server.py`; allocation in `saas_core/models/saas_instance.py`
- **Line Numbers:** capacity compute `saas_server.py:311-358`; alloc invalidate `saas_instance.py:~3590`
- **Description:** `instance_count`/`allocated_cpu`/`allocated_ram_gb` are computed via
  `_read_group` and cached. If the cache is read before the per-region allocation lock is held
  (or invalidated before, not after, lock acquisition), two concurrent allocators can both see
  stale free capacity and pick the same host, overcommitting beyond `max_*` limits.
- **Business Impact:** Capacity ceilings silently breached under concurrent orders → host
  oversubscription, noisy-neighbor degradation, and OOM risk for paying tenants.
- **Technical Impact:** Read-decide-write on cached aggregates without a fully-serialized
  read-after-lock.
- **Recommended Fix:** Invalidate and re-read capacity strictly **after** acquiring the
  allocation lock; or compute live counts inside the locked section with a single SQL aggregate.

---

## SCALE-006
- **Severity:** Medium
- **Component:** Port pool exhaustion — no graceful degradation
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** `:3742-3752`
- **Description:** Port allocation linearly scans from `starting_port` to 65535 and raises a
  hard `ValidationError("No available port pair found")` when the (finite) per-host pool is
  exhausted, with no fallback to another host, overcommit, or `pending_provision`.
- **Business Impact:** A host nearing its port ceiling fails new deploys with an opaque error
  instead of routing elsewhere or queueing — capacity dead-ends instead of degrading.
- **Technical Impact:** No overflow path; worst-case scan is long while holding the lock.
- **Recommended Fix:** Cap the scan; on exhaustion fall back to another eligible host /
  overcommit, else transition to `pending_provision` with a clear operator alert.

---

## SCALE-007
- **Severity:** Medium
- **Component:** Live-metrics sampler — single global lock ceiling
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** `_LIVE_METRICS_LOCK_KEY` (global) `~:105`; sampler `:4423-4463`
- **Description:** A single global advisory lock guarantees only one sampler runs cluster-wide,
  with a ~50s run cap. Past a few dozen hosts the one sampler cannot complete a full cycle
  within the cadence, so it cannot be horizontally scaled — the design has a hard throughput
  ceiling independent of how many workers exist.
- **Business Impact:** Live metrics go stale for large fleets, degrading autoscaling guidance,
  alerting, and the customer-facing live charts.
- **Technical Impact:** Global mutex prevents sharded/parallel sampling.
- **Recommended Fix:** Shard sampling by host with per-host locks so multiple workers sample
  disjoint host sets in parallel; or push metrics from host agents to a TSDB.

---

## SCALE-008
- **Severity:** Medium
- **Component:** Template-DB build lock granularity
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** `_HOSTING_TEMPLATE_BUILD_LOCKS` `:26-38`
- **Description:** The in-process build lock is keyed by `instance.id`, but template DBs are
  shared per (version, server/region). Two instances of the same version building concurrently
  hold different locks and can race on the same `__odoo_template_*` DB/role creation, so the
  intended per-template serialization is lost. (Also: in-process locks don't coordinate across
  worker processes/hosts.)
- **Business Impact:** Concurrent first-of-version provisions can corrupt or collide on the
  shared template, causing flaky provisioning that's hard to reproduce.
- **Technical Impact:** Lock key doesn't match the shared resource; not cross-process.
- **Recommended Fix:** Key the lock by (odoo_version, server/region) and use a DB advisory lock
  so it holds across processes.

---

## SCALE-009
- **Severity:** Low
- **Component:** Regional allocation lock — global serialization without regions
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** `_ALLOC_LOCK_NAMESPACE` `:3540`; key `:3585-3586`
- **Description:** The allocation lock key is `(namespace, region.id or 0)`. With no regions
  configured, every allocation collapses to one global lock (`...,0`), serializing all
  provisioning platform-wide regardless of host.
- **Business Impact:** On single-region/region-less deployments, provisioning throughput is
  globally serialized.
- **Technical Impact:** Degenerate lock key when `region_id` is falsy.
- **Recommended Fix:** Fall back to per-server (not global) locking when region is absent;
  require/validate a region per instance.

---

## SCALE-010
- **Severity:** Low
- **Component:** DB-operation zombie reaper — unbatched loop
- **File Path:** `saas_core/models/saas_instance_db_operation.py`
- **Line Numbers:** `:350-379`
- **Description:** The reaper iterates all stuck operations one-by-one with per-row
  write+commit. After a large incident (DB outage) producing thousands of zombies, the run is
  slow and can overlap the next scheduled run.
- **Business Impact:** Post-incident cron pile-up and DB write storms.
- **Technical Impact:** No batched write/`limit`.
- **Recommended Fix:** Single bulk `write` per batch with a `limit`/chunk loop.

---

## SCALE-011
- **Severity:** Low
- **Component:** Sampler de-dup state is in-process
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** `_LIVE_SAMPLE_SEED_AT` `:108-109`; check `~:4348`
- **Description:** The "recently seeded" guard for background sampling lives in a process-local
  dict, lost on every worker restart/recycle. After restarts, near-simultaneous polls spawn
  duplicate background samplers (extra SSH/CPU). Correctness is fine; efficiency suffers under
  rolling restarts.
- **Business Impact:** Avoidable SSH/CPU spikes after deploys/restarts at scale.
- **Technical Impact:** Non-shared, non-durable de-dup state.
- **Recommended Fix:** Move the short TTL guard to a shared store (Redis key / DB flag with
  expiry).
