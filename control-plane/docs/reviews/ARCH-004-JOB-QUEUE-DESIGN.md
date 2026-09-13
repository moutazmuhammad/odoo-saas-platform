# ARCH-004 — Durable Job Queue: Design Proposal

**Status:** proposed (for sign-off before implementation)
**Author:** remediation work, branch `architecture-evolution`
**Scope:** replace the fire-and-forget background-thread model with a durable,
restart-safe, retryable, pollable job queue — incrementally, behaviour-preserving.

---

## 1. Problem (what exists today)

Background work runs via `utils.run_in_background(record, method_name, …)`
(`saas_core/utils.py`): it commits, spawns a **daemon thread** with its own
cursor, runs `record.method(*args)`, and on failure calls an `error_method`.
~20 call sites use it — deploy, delete, db create/duplicate/drop/restore/
upgrade, backups, webhook auto-deploy, etc.

What's already good (built during this remediation):
- **Per-op durable record + heartbeat + reaper** for DB ops
  (`saas.instance.db.operation`: `last_heartbeat`, `_HEARTBEAT_STALE_MIN`,
  `_cron_reap_stuck_db_operations` — PROV-001).
- **Recovery crons** for deploy: `_cron_recover_stuck_provisioning` (re-queues
  stuck `provisioning`) and `_cron_retry_pending_provision` (back-off retry,
  now capacity-aware — PROV-004).
- **Failure path**: `_on_background_error` marks `failed`/retries and **alerts**
  (`saas.alert._notify`, SEC-009).
- **Per-resource serialization**: pg advisory locks (`_ALLOC_LOCK_NAMESPACE`,
  `_hosting_template_build_lock`, `pg_advisory_xact_lock`) + the deploy
  "operation lock" liveness check (`_operation_is_alive`).
- **Connect resilience**: SSH retry + circuit breaker (ARCH-010); compose-up
  retry.

Remaining gaps (the actual ARCH-004 ask):
1. **Not durable for arbitrary ops.** A worker restart kills the thread; only
   deploy and db-ops have recovery. Other ops (backups, webhook deploy) can die
   silently with no record to recover from.
2. **No unified retry/idempotency.** Retry logic is bespoke per op type;
   re-runs aren't guarded by a shared idempotency key.
3. **No single pollable status surface.** `db.operation` is the only generic
   one; deploy uses `state` + `provisioning_log`.
4. **Recovery is ad-hoc** (two op-specific crons) rather than one generic
   reaper.

---

## 2. Options considered

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **A. OCA `queue_job`** | Battle-tested; channels; retries; UI | New dependency + a dedicated job-runner process/config; its own conventions; migrating 20 sites to its decorators | Strong, but adds infra + a big-bang feel |
| **B. External broker (Celery/RQ + Redis)** | Scales horizontally | New infra (Redis) + serialization of ORM context; another SPOF; ops burden | Rejected — contradicts "no new infra" and the control-plane simplicity goal |
| **C. DB-backed `saas.job` + cron/thread worker** | No new infra; reuses our heartbeat/reaper/advisory-lock patterns; incremental; same DB transaction story we already rely on | We own the executor; Postgres is the queue (fine at this scale) | **Recommended** |

**Recommendation: Option C**, with the `enqueue()` API designed so the
executor could later be swapped for OCA `queue_job` behind the same interface if
scale demands it. C is the natural generalization of what `db.operation`
already does, so adoption is low-risk and incremental.

---

## 3. Design (Option C)

### 3.1 Model: `saas.job`

| field | type | purpose |
|---|---|---|
| `idempotency_key` | Char, indexed, unique-partial | dedupe; enqueue is a no-op if a live/succeeded job with this key exists |
| `model` / `res_id` | Char / Int | the target record (`env[model].browse(res_id)`) |
| `method` | Char | method to call on the target |
| `args_json` | Text | JSON-encoded positional args (JSON-serializable only) |
| `state` | Selection | `pending → running → done` / `failed` / `cancelled` |
| `priority` | Int | lower = sooner (default 10) |
| `channel` | Char | concurrency group (e.g. `deploy`, `backup`, `dbop`) |
| `lock_key` | Char | per-resource serialization key (e.g. `instance:<id>`) |
| `attempts` / `max_attempts` | Int | retry budget (default 3) |
| `run_after` | Datetime | scheduling / back-off gate |
| `last_heartbeat` | Datetime | liveness (reuse PROV-001 pattern) |
| `started_at` / `finished_at` | Datetime | timing |
| `error` | Text | sanitized failure (customer-safe), full traceback to log |
| `result_json` | Text | optional return payload for pollers |
| `progress` | Char/Int | optional, for client status |

Indexes: `(state, run_after, priority)` for claiming; partial-unique on
`idempotency_key WHERE idempotency_key IS NOT NULL`.

### 3.2 Public API

```python
env['saas.job']._enqueue(
    record, 'method_name', args=(...),
    idempotency_key='deploy:%s' % inst.id,   # optional
    channel='deploy', lock_key='instance:%s' % inst.id,
    max_attempts=3, eta=None, priority=10,
) -> saas.job
```

- Creates the job row (`pending`), **commits**, then best-effort kicks an
  **immediate in-process worker** (low latency) — but the row is the source of
  truth, so if the process dies the cron reclaims it.
- If `idempotency_key` matches a non-terminal or `done` job → returns that job,
  creates nothing (idempotent enqueue).

### 3.3 Worker / executor

A claim-execute loop, runnable both inline (post-enqueue thread) and from a
cron (`_cron_run_jobs`, every minute):

```
claim:  UPDATE saas_job SET state='running', started_at=now, last_heartbeat=now
        WHERE id IN (
          SELECT id FROM saas_job
          WHERE state='pending' AND run_after<=now
          ORDER BY priority, run_after, id
          FOR UPDATE SKIP LOCKED          -- no two workers grab the same job
          LIMIT <channel batch>
        ) RETURNING id
execute: hold pg advisory lock on hash(lock_key) for the run (per-resource
         serialization — replaces _operation_is_alive); a heartbeat thread
         stamps last_heartbeat every ~30s.
on success: state='done', result_json, finished_at.
on failure: attempts+1; if attempts<max -> state='pending',
            run_after = now + backoff(attempts)+jitter (reuse ARCH-010 style);
            else state='failed', error=sanitized, ALERT (SEC-009) + AUDIT (SEC-010).
```

- **Restart-safe**: `_cron_reap_jobs` requeues `running` jobs whose
  `last_heartbeat` is stale (worker died) — exactly the PROV-001 reaper,
  generalized. Stale running → back to `pending` (idempotent ops) or `failed`
  (non-idempotent, flagged per job).
- **Idempotency on execution**: jobs declare `idempotent=True/False`. Idempotent
  jobs (deploy/redeploy/restart/start/restore — the existing
  `_RECOVERABLE_OPERATIONS`) are safe to requeue; non-idempotent (db create/drop,
  delete, cancel) go to `failed` for manual review on crash (current behaviour).
- **Concurrency**: `channel` caps parallelism (config param per channel); the
  advisory `lock_key` serializes same-resource jobs (one deploy per instance at
  a time — what the operation lock does today).

### 3.4 Client-pollable status

The `saas.job` row IS the status (state/progress/error/result). The existing
`saas.instance.db.operation` becomes a thin view/wrapper over a `dbop`-channel
job (or is kept and backed by a job) so the Databases page polling is unchanged.

---

## 4. Migration plan (phased, behaviour-preserving)

Each phase is independently deployable and fully tested; nothing changes
customer-visible behaviour until the corresponding site is switched.

- **Phase 0 — foundation (no callers).** Add `saas.job` model, `_enqueue`,
  `_cron_run_jobs`, `_cron_reap_jobs`, advisory-lock + heartbeat reuse, ACL,
  config params (channel caps, reaper thresholds). Unit tests for claim
  (SKIP LOCKED), retry/back-off, idempotency-key dedupe, reaper requeue,
  per-resource lock. **Zero behaviour change.**
- **Phase 1 — backups first (lowest blast radius).** Route on-demand/daily
  backup jobs through `_enqueue` (channel `backup`). Backups are idempotent and
  not on the critical signup path → safe pilot. Verify on the real test host.
- **Phase 2 — DB operations.** Back `saas.instance.db.operation` with a `dbop`
  job (or migrate its `_run_*` onto `_enqueue`), keeping the portal polling API
  identical. The heartbeat/reaper already match the queue's model.
- **Phase 3 — deploy / delete.** Move `action_deploy`/`_do_delete_instance` onto
  `_enqueue` (channel `deploy`, `lock_key=instance:<id>`, idempotent=deploy).
  Retire `_cron_recover_stuck_provisioning` + fold `_cron_retry_pending_provision`
  (incl. PROV-004's `pending_retry_now`) into the generic reaper + `run_after`
  back-off. Keep `deploy_retry_count` semantics as `max_attempts`.
- **Phase 4 — webhook + remaining `run_in_background` sites.** Switch the rest;
  `run_in_background` becomes a thin shim over `_enqueue` (or is removed).

Roll-forward only; each phase leaves the previous mechanism working until cut
over. A feature flag (`saas_master.use_job_queue`) can gate `_enqueue`'s
immediate-thread vs cron-only execution during rollout.

---

## 5. Testing strategy

- **Unit (no infra):** claim concurrency (two workers, one job via SKIP LOCKED);
  idempotency-key dedupe; back-off scheduling; max-attempts → failed + alert +
  audit; reaper requeues stale-heartbeat running jobs; per-resource lock
  serializes. (All mockable like the existing `TestDbOperationReaper` /
  `TestPendingRetry`.)
- **Integration (local Odoo):** enqueue → cron runs → done; crash mid-run
  (kill heartbeat) → reaper requeues; full suite stays green.
- **Real host (per phase):** the same harness used for the live provision —
  enqueue a backup/deploy and confirm end-to-end.

## 6. Risks & mitigations

- **Double execution** → `FOR UPDATE SKIP LOCKED` claim + idempotency keys +
  per-resource advisory lock.
- **Poison jobs** → `max_attempts` then `failed` + alert; never infinite-loop.
- **Long jobs vs reaper** → heartbeat + generous stale threshold (as PROV-001).
- **Lost work on restart** → durable row + reaper requeue (idempotent) /
  fail-for-review (non-idempotent) — strictly better than today's silent death.
- **Scope creep** → strict phase boundaries; Phase 0 ships value (durable
  records) with zero behaviour change.

## 7. Estimated effort

Phase 0 ≈ 1 focused change (model + worker + reaper + tests). Phases 1–4 ≈ one
change each, gated and tested. Total ~5 reviewable PRs; no new infrastructure.
