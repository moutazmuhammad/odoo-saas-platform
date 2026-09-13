# PROVISIONING RELIABILITY AUDIT — V2 (Second Pass)

> Re-audit assuming V1 reliability findings fixed. Only **NEW** provisioning-lifecycle issues
> (allocation → create → deploy → recover). File:line verified. The automated pass also
> re-raised the V1-cleared "trial concurrent create" race — **dropped** (the `create()` path
> already takes `SELECT ... FOR UPDATE` on the commercial partner, `saas_instance.py:1269-1278`).
>
> Severity counts: **Critical 0 · High 1 · Medium 3 · Low 2** (6 findings)

---

## PROV-001
- **Severity:** High
- **Component:** Background DB-operation thread — unguarded failure
- **File Path:** `saas_core/utils.py`; `saas_core/models/saas_instance_db_operation.py`
- **Line Numbers:** `utils.py:~64-110` (`run_in_background` `_target`); op records created in db_operation model
- **Description:** Long DB operations (create/duplicate/restore) run in a daemon thread whose
  target invocation lacks a try/except that fails the operation record on crash. If the worker
  thread dies (unhandled exception, dropped DB connection, worker recycle), the
  `saas.instance.db.operation` stays `running` until the 30-minute zombie reaper flips it. There
  is no heartbeat, so the customer watches an indefinite spinner with no error.
- **Business Impact:** "Creating database…" hangs for up to 30 minutes on any transient failure;
  customers assume the platform is broken and open tickets or abandon.
- **Technical Impact:** Failure is invisible until a coarse timeout; no fast-fail, no heartbeat.
- **Recommended Fix:** Wrap the background target in try/except that marks the op `failed` with
  the traceback immediately; add a `last_heartbeat` the reaper can use to fail stuck ops sooner.

---

## PROV-002
- **Severity:** Medium
- **Component:** Partial create leaves subdomain reserved (no rollback)
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** `create()` `:1251-1350`; unconditional subdomain unique index `:1116-1166`
- **Description:** The instance row (with its subdomain) is committed before/independently of
  successful allocation and deployment. If allocation, port assignment, or DB provisioning then
  fails, the subdomain is already locked by the unconditional unique index, so a customer
  retrying the same name collides with their own failed record and cannot reuse it without
  operator intervention.
- **Business Impact:** Failed first-time provisions strand the chosen subdomain; retries fail
  confusingly; support must clean up.
- **Technical Impact:** No compensating teardown/rename on create-time failure; non-atomic
  create+provision.
- **Recommended Fix:** Defer subdomain commit until allocation succeeds, or on failure
  auto-rename the failed record (e.g., `<sub>-failed-<id>`) to free the name (the migration
  already demonstrates this rename pattern).

---

## PROV-003
- **Severity:** Medium
- **Component:** Overcommit fallback uses stale health, no pending fallback
- **File Path:** `saas_core/models/saas_server.py`; alloc caller `saas_core/models/saas_instance.py`
- **Line Numbers:** `saas_server.py:530-553` (`_allocate_overcommit_server`); caller `~:3611`
- **Description:** Overcommit candidate selection filters on cached `health_state != 'unreachable'`
  (up to a health-cron interval stale) and live-probes only the chosen candidate. If the single
  regional overcommit host fails the probe, the method returns `None` and the deploy errors out
  instead of transitioning to `pending_provision`, so a recoverable capacity blip becomes a hard
  failure.
- **Business Impact:** Instances fail to deploy (customer sees stuck/failed) when they should
  have queued for the next capacity/health cycle.
- **Technical Impact:** Stale health gate + missing graceful-degrade path.
- **Recommended Fix:** Require `health_state == 'ok'` for overcommit; on probe failure transition
  the instance to `pending_provision` rather than failing the deploy.

---

## PROV-004
- **Severity:** Medium
- **Component:** Pending-provision retry not capacity-aware
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** `_cron_retry_pending_provision` `:3933-4011`; `_PENDING_MAX_WAIT_HOURS=24` `:3946`
- **Description:** Pending instances retry on a time-based exponential backoff capped at 2h per
  interval, up to 24h. When an operator adds capacity, queued instances are not retried
  immediately — they wait for their next (possibly hours-away) backoff slot — and only fail after
  a full 24h, with no customer notification in between.
- **Business Impact:** Customers experience multi-hour deploy delays even after capacity exists;
  silence until a 24h failure erodes trust at the most critical first-deploy moment.
- **Technical Impact:** Retry cadence is decoupled from capacity-availability signals.
- **Recommended Fix:** Trigger an immediate retry sweep when server capacity/health changes;
  shorten the hard-fail to a few hours; email the customer that the deploy is queued for capacity.

---

## PROV-005
- **Severity:** Low
- **Component:** Subdomain unique index blocks reuse/reactivation
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** `:1104`, `:1116-1166` (unconditional unique index incl. cancelled)
- **Description:** Subdomain uniqueness is enforced across all states including cancelled, so a
  cancelled instance permanently holds its subdomain. A customer cannot reclaim their own
  cancelled name, and a typo subdomain is locked until an operator renames the dead record.
- **Business Impact:** Reactivation/rename friction and avoidable support load as the cancelled
  population grows.
- **Technical Impact:** No self-reclaim path for a partner's own cancelled subdomain.
- **Recommended Fix:** Allow a partner to reclaim their own cancelled subdomain, or auto-rename
  on cancel to free the namespace while preserving audit history.

---

## PROV-006
- **Severity:** Low
- **Component:** SSH per-operation timeouts not differentiated
- **File Path:** `saas_core/utils.py`
- **Line Numbers:** `SSH_CONNECT_TIMEOUT`/`SSH_COMMAND_TIMEOUT` consts `:~111-112`
- **Description:** Beyond the V1 timeout/retry concern, the connect/command timeouts are global
  constants applied uniformly to trivial checks (`echo`) and heavy operations (image build, DB
  init). Slow-but-legitimate operations can hit the same ceiling as quick ones, and operators
  cannot tune per host/operation without code changes.
- **Business Impact:** Legitimate slow deploys time out on high-latency hosts; no per-host tuning.
- **Technical Impact:** One timeout for all operation classes.
- **Recommended Fix:** Parameterize timeouts per operation class and expose a per-server override
  field.
