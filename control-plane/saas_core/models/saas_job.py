"""Durable job queue (ARCH-004, Phase 0).

A DB-backed queue: ``_enqueue`` persists a job row; ``_cron_run_jobs`` claims due
jobs with ``FOR UPDATE SKIP LOCKED`` and runs them; ``_cron_reap_jobs`` requeues
(idempotent) or fails (non-idempotent) jobs whose worker died (stale heartbeat).
Retries use exponential back-off; a per-resource advisory ``lock_key``
serialises same-resource jobs; ``idempotency_key`` dedupes enqueues. Reuses the
heartbeat/reaper (PROV-001), alert (SEC-009) and audit (SEC-010) patterns.

Phase 0 ships the engine with NO callers — pure addition, zero behaviour change.
See docs/reviews/ARCH-004-JOB-QUEUE-DESIGN.md.
"""
import datetime
import hashlib
import json
import logging
import random
import threading

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class SaasJob(models.Model):
    _name = 'saas.job'
    _description = 'SaaS Durable Job'
    _order = 'priority, run_after, id'

    # How many due jobs a single _cron_run_jobs tick claims.
    _RUN_BATCH = 50
    # Soft cap on concurrent immediate workers PER CHANNEL, so a burst of
    # enqueues can't spawn an unbounded number of threads (thundering herd).
    # Over-cap jobs stay pending and the cron drains them. Configurable via
    # ir.config_parameter 'saas_master.job_channel_concurrency'.
    _DEFAULT_CHANNEL_CONCURRENCY = 8
    # A running job whose heartbeat is older than this is presumed dead.
    _HEARTBEAT_STALE_MIN = 15
    # How often the in-flight worker stamps the heartbeat (job + target).
    _HEARTBEAT_TICK_SEC = 30
    # Retry back-off: BASE * 2**(attempts-1) minutes, capped, + jitter.
    _BACKOFF_BASE_MIN = 1
    _BACKOFF_CAP_MIN = 60

    idempotency_key = fields.Char(index=True, copy=False)
    model = fields.Char(required=True)
    res_id = fields.Integer()
    method = fields.Char(required=True)
    args_json = fields.Text(default='[]')
    state = fields.Selection(
        [('pending', 'Pending'), ('running', 'Running'),
         ('done', 'Done'), ('failed', 'Failed'), ('cancelled', 'Cancelled')],
        default='pending', required=True, index=True)
    priority = fields.Integer(default=10)
    channel = fields.Char(default='default', index=True)
    lock_key = fields.Char(help='Per-resource serialisation key (advisory lock).')
    idempotent = fields.Boolean(
        default=False,
        help='Safe to re-run if the worker dies mid-flight; such jobs are '
             'requeued by the reaper, others are failed for manual review.')
    on_error = fields.Char(
        help='Optional method on the target called as '
             '`record.on_error(exception, *on_error_args)` when the job fails '
             '(mirrors run_in_background error_method, e.g. _on_background_error '
             'which transitions an instance to failed/pending + alerts).')
    on_error_args_json = fields.Text(default='[]')
    attempts = fields.Integer(default=0)
    max_attempts = fields.Integer(default=3)
    run_after = fields.Datetime(default=fields.Datetime.now, index=True)
    last_heartbeat = fields.Datetime()
    started_at = fields.Datetime()
    finished_at = fields.Datetime()
    error = fields.Text(readonly=True)
    result_json = fields.Text(readonly=True)

    # ------------------------------------------------------------------ API
    @api.model
    def _enqueue(self, record, method, args=(), *, idempotency_key=None,
                 channel='default', lock_key=None, max_attempts=3,
                 idempotent=False, priority=10, eta=None, run_now=True,
                 on_error=None, on_error_args=()):
        """Persist a job and return its record. Idempotent on
        ``idempotency_key``: if a pending/running/done job already carries it,
        that job is returned and nothing new is created.

        ``run_now`` (default True, and only when not delayed by ``eta``) kicks
        an immediate background worker so the job starts promptly — like the old
        ``run_in_background`` — while the durable row + reaper guarantee it still
        runs if that worker (or the process) dies. The cron is the backstop."""
        if idempotency_key:
            existing = self.sudo().search([
                ('idempotency_key', '=', idempotency_key),
                ('state', 'in', ('pending', 'running', 'done')),
            ], limit=1)
            if existing:
                return existing
        job = self.sudo().create({
            'model': record._name,
            'res_id': record.id,
            'method': method,
            'args_json': json.dumps(list(args)),
            'idempotency_key': idempotency_key or False,
            'channel': channel,
            'lock_key': lock_key or False,
            'max_attempts': max_attempts,
            'idempotent': idempotent,
            'priority': priority,
            'run_after': eta or fields.Datetime.now(),
            'on_error': on_error or False,
            'on_error_args_json': json.dumps(list(on_error_args)),
        })
        if run_now and not eta:
            job._spawn_worker()
        return job

    @api.model
    def _channel_concurrency(self):
        """Max concurrent immediate workers per channel (config-overridable)."""
        val = self.env['ir.config_parameter'].sudo().get_param(
            'saas_master.job_channel_concurrency')
        try:
            return int(val) if val else self._DEFAULT_CHANNEL_CONCURRENCY
        except (TypeError, ValueError):
            return self._DEFAULT_CHANNEL_CONCURRENCY

    @api.model
    def _should_spawn_now(self, channel):
        """True while the channel has spare concurrency for an immediate worker.
        Over-cap jobs stay pending; the cron drains them as workers free up."""
        running = self.search_count([
            ('state', '=', 'running'), ('channel', '=', channel)])
        return running < self._channel_concurrency()

    def _spawn_worker(self):
        """Commit the job, then run it in a daemon thread with its own cursor.
        Mirrors utils.run_in_background, but the durable row means a crash is
        recovered by the cron/reaper rather than lost. Bounded per channel so a
        burst of enqueues can't spawn unbounded threads."""
        self.ensure_one()
        if not self._should_spawn_now(self.channel):
            _logger.info(
                "saas.job %s: channel '%s' at concurrency cap; left pending "
                "for the cron", self.id, self.channel)
            return
        job_id = self.id
        channel = self.channel
        dbname = self.env.cr.dbname
        self.env.cr.commit()  # publish the job so the new cursor sees it

        def _target():
            import odoo
            from odoo import api as odoo_api, SUPERUSER_ID
            try:
                reg = odoo.modules.registry.Registry(dbname)
                with reg.cursor() as cr:
                    env = odoo_api.Environment(cr, SUPERUSER_ID, {})
                    env['saas.job']._worker_loop(job_id, channel)
            except Exception:
                _logger.exception("saas.job %s worker thread crashed", job_id)

        threading.Thread(
            target=_target, name='saas_job_%s' % job_id, daemon=True).start()

    @api.model
    def _worker_loop(self, first_job_id, channel):
        """Run ``first_job_id``, then keep draining due pending jobs in the same
        channel until none remain. A fixed set of immediate workers (bounded by
        the per-channel cap) thus sustains parallel throughput — e.g. nightly
        backups run ~cap-at-a-time instead of cap-then-serial — and over-cap
        deferred jobs get picked up here rather than waiting for the cron."""
        self._run_one(first_job_id)
        while True:
            job = self._claim_next_in_channel(channel)
            if not job:
                break
            job._execute()

    @api.model
    def _claim_next_in_channel(self, channel):
        """Atomically claim ONE due pending job in *channel* (FOR UPDATE SKIP
        LOCKED) and return it (now running), or an empty recordset."""
        now = fields.Datetime.now()
        self.env.cr.execute(
            """
            UPDATE saas_job SET state='running', started_at=%s,
                   last_heartbeat=%s, attempts=attempts+1
            WHERE id = (
                SELECT id FROM saas_job
                WHERE state='pending' AND channel=%s AND run_after<=%s
                      AND attempts<max_attempts
                ORDER BY priority, run_after, id
                FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING id
            """, (now, now, channel, now))
        row = self.env.cr.fetchone()
        if not row:
            return self.browse()
        self.env.cr.commit()
        job = self.browse(row[0])
        job.invalidate_recordset()
        return job

    @api.model
    def _run_one(self, job_id):
        """Claim a single job by id (no-op if the cron/another worker already
        took it) and execute it."""
        now = fields.Datetime.now()
        self.env.cr.execute(
            """
            UPDATE saas_job SET state='running', started_at=%s,
                   last_heartbeat=%s, attempts=attempts+1
            WHERE id=%s AND state='pending' AND run_after<=%s
                  AND attempts<max_attempts
            RETURNING id
            """, (now, now, job_id, now))
        if not self.env.cr.fetchone():
            return  # already claimed elsewhere
        self.env.cr.commit()
        job = self.browse(job_id)
        job.invalidate_recordset()
        job._execute()

    # -------------------------------------------------------------- worker
    @api.model
    def _claim_next_any(self):
        """Atomically claim ONE due pending job across ALL channels
        (``FOR UPDATE SKIP LOCKED``) and return it (now running), or an empty
        recordset. Increments attempts so the count survives a crash."""
        now = fields.Datetime.now()
        self.env.cr.execute(
            """
            UPDATE saas_job SET state='running', started_at=%s,
                   last_heartbeat=%s, attempts=attempts+1
            WHERE id = (
                SELECT id FROM saas_job
                WHERE state='pending' AND run_after <= %s
                      AND attempts < max_attempts
                ORDER BY priority, run_after, id
                FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING id
            """, (now, now, now))
        row = self.env.cr.fetchone()
        if not row:
            return self.browse()
        self.env.cr.commit()
        job = self.browse(row[0])
        job.invalidate_recordset()
        return job

    @api.model
    def _cron_run_jobs(self):
        """Backstop drain. Claims and runs due jobs ONE AT A TIME (up to
        ``_RUN_BATCH`` per tick), so a job is only in the 'running' state while
        it is actually executing and carrying a live heartbeat.

        A previous version claimed the whole batch upfront (all → 'running'
        with a claim-time heartbeat) and executed them serially; jobs waiting
        their turn kept that stale heartbeat, so once the drain ran longer than
        ``_HEARTBEAT_STALE_MIN`` the reaper would treat the not-yet-started
        jobs as dead — requeuing (double run) or terminally failing them
        (spurious on_error/alert) even though this loop would still execute
        them. Claiming one at a time removes that window entirely."""
        for _i in range(self._RUN_BATCH):
            job = self._claim_next_any()
            if not job:
                break
            try:
                job._execute()
            except Exception:
                # _execute handles its own failures; this guards the loop.
                _logger.exception("saas.job %s executor crashed", job.id)
                self.env.cr.rollback()

    @staticmethod
    def _lock_int(key):
        """Stable signed-64-bit int for pg_advisory_xact_lock from a string."""
        h = hashlib.sha256(key.encode('utf-8')).digest()[:8]
        return int.from_bytes(h, 'big', signed=True)

    def _execute(self):
        self.ensure_one()
        if self.lock_key:
            # Serialise same-resource jobs for the duration of this txn.
            self.env.cr.execute(
                "SELECT pg_advisory_xact_lock(%s)", (self._lock_int(self.lock_key),))
        record = self.env[self.model].browse(self.res_id) if self.res_id else None
        if self.res_id and (record is None or not record.exists()):
            self._finish_failed("Target %s,%s no longer exists." % (self.model, self.res_id))
            return
        # Heartbeat the job (and any heartbeat-bearing target, e.g. a
        # db.operation) while the work runs, so neither reaper fails a live
        # long-running job.
        stop = threading.Event()
        beater = threading.Thread(
            target=self._heartbeat_loop, args=(self.id, self.env.cr.dbname, stop),
            name='saas_job_hb_%s' % self.id, daemon=True)
        beater.start()
        try:
            args = json.loads(self.args_json or '[]')
            result = getattr(record, self.method)(*args)
            payload = False
            try:
                payload = json.dumps(result)
            except (TypeError, ValueError):
                payload = False  # non-JSON result (e.g. recordset) — not stored
            self.write({
                'state': 'done', 'finished_at': fields.Datetime.now(),
                'result_json': payload, 'error': False,
            })
            self.env.cr.commit()
        except Exception as e:
            self.env.cr.rollback()
            _logger.exception("saas.job %s (%s.%s) failed", self.id, self.model, self.method)
            # on_error runs ONLY on terminal failure (in _finish_failed), not on
            # every intermediate retry — so a target that owns retries via
            # max_attempts (e.g. deploy) isn't marked failed/alerted mid-retry.
            self._reschedule_or_fail(str(e))
        finally:
            stop.set()
            beater.join(timeout=5)

    def _heartbeat_loop(self, job_id, dbname, stop):
        """Stamp the job's heartbeat (and a heartbeat-bearing target's) every
        ``_HEARTBEAT_TICK_SEC`` until the work ends. Own cursor per tick."""
        import odoo
        from odoo import api as odoo_api, SUPERUSER_ID
        while not stop.wait(self._HEARTBEAT_TICK_SEC):
            try:
                reg = odoo.modules.registry.Registry(dbname)
                with reg.cursor() as cr:
                    env = odoo_api.Environment(cr, SUPERUSER_ID, {})
                    if not env['saas.job']._heartbeat_tick(job_id):
                        break
                    cr.commit()
            except Exception:
                _logger.debug("saas.job %s heartbeat tick failed", job_id, exc_info=True)

    @api.model
    def _heartbeat_tick(self, job_id):
        """Stamp ``last_heartbeat`` on the job and, if the target record carries
        a ``last_heartbeat`` Datetime, on it too. Returns False once the job is
        no longer running (signals the beater to stop)."""
        job = self.browse(job_id)
        if not job.exists() or job.state != 'running':
            return False
        now = fields.Datetime.now()
        job.last_heartbeat = now
        target_model = self.env[job.model] if job.model else None
        if (job.res_id and target_model is not None
                and 'last_heartbeat' in target_model._fields
                and target_model._fields['last_heartbeat'].type == 'datetime'):
            rec = target_model.browse(job.res_id)
            if rec.exists():
                rec.last_heartbeat = now
        return True

    def _backoff_run_after(self):
        mins = min(self._BACKOFF_BASE_MIN * (2 ** max(0, self.attempts - 1)),
                   self._BACKOFF_CAP_MIN)
        delay = datetime.timedelta(minutes=mins, seconds=random.uniform(0, 30))
        return fields.Datetime.now() + delay

    def _reschedule_or_fail(self, message):
        """Retry with back-off while attempts remain, else fail terminally."""
        self.ensure_one()
        if self.attempts < self.max_attempts:
            self.write({
                'state': 'pending', 'run_after': self._backoff_run_after(),
                'error': (message or '')[:2000],
            })
            self.env.cr.commit()
            return
        self._finish_failed(message)

    def _finish_failed(self, message):
        self.ensure_one()
        self.write({
            'state': 'failed', 'finished_at': fields.Datetime.now(),
            'error': (message or '')[:2000],
        })
        self.env.cr.commit()
        # Terminal failure → run the target's on_error handler exactly once
        # (e.g. _on_background_error, which transitions the instance to failed +
        # alerts). Runs here, not per-attempt, so a retry-owning target isn't
        # failed mid-retry. Reached from both retry-exhaustion and the reaper.
        if self.on_error and self.res_id:
            record = self.env[self.model].browse(self.res_id)
            if record.exists():
                try:
                    getattr(record, self.on_error)(
                        Exception(message), *json.loads(self.on_error_args_json or '[]'))
                    self.env.cr.commit()
                except Exception:
                    self.env.cr.rollback()
                    _logger.exception(
                        "saas.job %s on_error handler failed", self.id)
        # Operational alert + immutable audit on terminal failure.
        try:
            self.env['saas.alert']._notify(
                'job_failed',
                'job %s (%s.%s) failed after %d attempts'
                % (self.id, self.model, self.method, self.attempts),
                level='error', detail=(message or '')[:500])
            self.env['saas.audit.log']._saas_audit(
                'job_failed', result='error', model=self.model,
                res_id=self.res_id, res_name=self.method,
                detail=(message or '')[:500])
        except Exception:
            _logger.exception("saas.job %s: alert/audit on failure failed", self.id)

    # --------------------------------------------------------------- reaper
    @api.model
    def _cron_reap_jobs(self):
        """Recover jobs whose worker died (stale heartbeat): requeue idempotent
        ones, fail the rest for review."""
        cutoff = fields.Datetime.now() - datetime.timedelta(
            minutes=self._HEARTBEAT_STALE_MIN)
        stuck = self.search([
            ('state', '=', 'running'),
            '|', ('last_heartbeat', '<', cutoff), ('last_heartbeat', '=', False),
        ])
        for job in stuck:
            if job.idempotent and job.attempts < job.max_attempts:
                job.write({'state': 'pending', 'run_after': fields.Datetime.now()})
                _logger.warning("saas.job %s requeued after stale heartbeat", job.id)
            else:
                job._finish_failed("Worker died (no heartbeat) and job is not "
                                   "safe to auto-retry.")
            self.env.cr.commit()
