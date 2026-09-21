import json
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestJobQueue(TransactionCase):
    """ARCH-004 Phase 0: durable job engine — enqueue/idempotency, claim,
    success, retry/back-off, terminal failure, run_after gating, per-resource
    lock, and reaper recovery."""

    def setUp(self):
        super().setUp()
        self.Job = self.env['saas.job']
        self.partner = self.env['res.partner'].sudo().create({'name': 'Job Target'})

        def _job_ok(rec, *a):
            return {'ran': True, 'args': list(a)}

        def _job_boom(rec, *a):
            raise ValueError('boom')

        self._onerror_calls = []

        def _job_onerror(rec, exc, *a):
            self._onerror_calls.append((str(exc), list(a)))

        # Records how many jobs are in 'running' while THIS job executes —
        # used to prove the cron drains one job at a time.
        self._running_counts = []

        def _job_count_running(rec, *a):
            self._running_counts.append(
                rec.env['saas.job'].search_count([('state', '=', 'running')]))
            return True

        for name, fn in (('_job_ok', _job_ok), ('_job_boom', _job_boom),
                         ('_job_onerror', _job_onerror),
                         ('_job_count_running', _job_count_running)):
            p = patch.object(type(self.partner), name, fn, create=True)
            p.start()
            self.addCleanup(p.stop)
        # Suppress the immediate worker thread so execution is driven
        # deterministically via _cron_run_jobs / _run_one in the tests (and so
        # enqueue doesn't commit the test cursor).
        wp = patch.object(type(self.Job), '_spawn_worker', lambda self: None)
        wp.start()
        self.addCleanup(wp.stop)

    def _no_commit(self):
        # _cron_run_jobs / _execute / reaper commit per item; the test cursor
        # forbids commit/rollback, so neutralise them for the call.
        for op in ('commit', 'rollback'):
            p = patch.object(self.env.cr, op)
            p.start()
            self.addCleanup(p.stop)

    # ---- enqueue / idempotency ----
    def test_enqueue_creates_pending(self):
        job = self.Job.enqueue(self.partner, '_job_ok', args=(1, 2))
        self.assertEqual(job.state, 'pending')
        self.assertEqual(job.model, 'res.partner')
        self.assertEqual(job.res_id, self.partner.id)
        self.assertEqual(json.loads(job.args_json), [1, 2])

    def test_enqueue_idempotent(self):
        j1 = self.Job.enqueue(self.partner, '_job_ok', idempotency_key='k1')
        j2 = self.Job.enqueue(self.partner, '_job_ok', idempotency_key='k1')
        self.assertEqual(j1, j2, "same idempotency key must not create a 2nd job")

    # ---- backstop cron drains one job at a time (reaper-safety) ----
    def test_cron_runs_jobs_one_at_a_time(self):
        """Regression: the backstop cron must NOT flip a whole batch to
        'running' upfront. Doing so left not-yet-started jobs 'running' with a
        stale claim-time heartbeat, so a reaper tick during a long serial drain
        would requeue (double-run) or terminally fail them. During any job's
        execution exactly one job — itself — is 'running'."""
        self._no_commit()
        for i in range(4):
            self.Job.enqueue(self.partner, '_job_count_running',
                              idempotency_key='cnt%d' % i)
        self.Job._cron_run_jobs()
        self.assertEqual(len(self._running_counts), 4, "all jobs ran once")
        self.assertEqual(
            max(self._running_counts), 1,
            "no job may sit in 'running' while another job executes")

    # ---- run ----
    def test_run_success_stores_result(self):
        self._no_commit()
        job = self.Job.enqueue(self.partner, '_job_ok', args=(1, 2))
        self.Job._cron_run_jobs()
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.attempts, 1)
        self.assertEqual(json.loads(job.result_json), {'ran': True, 'args': [1, 2]})

    def test_run_with_lock_key(self):
        self._no_commit()
        job = self.Job.enqueue(self.partner, '_job_ok',
                                lock_key='res:%s' % self.partner.id)
        self.Job._cron_run_jobs()
        self.assertEqual(job.state, 'done')

    # ---- failure: retry then terminal ----
    def test_failure_requeues_with_backoff(self):
        self._no_commit()
        job = self.Job.enqueue(self.partner, '_job_boom', max_attempts=3)
        self.Job._cron_run_jobs()
        self.assertEqual(job.state, 'pending', "retries remain -> requeued")
        self.assertEqual(job.attempts, 1)
        self.assertIn('boom', job.error)
        self.assertGreater(job.run_after, fields.Datetime.now(),
                           "requeue is delayed by back-off")

    def test_failure_terminal_alerts(self):
        self._no_commit()
        job = self.Job.enqueue(self.partner, '_job_boom', max_attempts=1)
        self.Job._cron_run_jobs()
        self.assertEqual(job.state, 'failed')
        self.assertEqual(job.attempts, 1)
        self.assertIn('boom', job.error)

    def test_on_error_handler_invoked_on_failure(self):
        self._no_commit()
        job = self.Job.enqueue(self.partner, '_job_boom', max_attempts=1,
                                on_error='_job_onerror', on_error_args=('x',))
        self.Job._cron_run_jobs()
        self.assertEqual(job.state, 'failed')
        self.assertEqual(len(self._onerror_calls), 1,
                         "the target's on_error handler must run on failure")
        self.assertIn('boom', self._onerror_calls[0][0])
        self.assertEqual(self._onerror_calls[0][1], ['x'])

    def test_on_error_only_on_terminal_failure(self):
        # With retries remaining, a failed attempt reschedules WITHOUT firing
        # on_error; on_error fires once, only when attempts are exhausted.
        self._no_commit()
        job = self.Job.enqueue(self.partner, '_job_boom', max_attempts=2,
                                on_error='_job_onerror', on_error_args=('z',))
        self.Job._cron_run_jobs()  # attempt 1 -> reschedule
        self.assertEqual(job.state, 'pending')
        self.assertEqual(len(self._onerror_calls), 0,
                         "on_error must NOT fire on an intermediate retry")
        # Clear the back-off so the next claim is due. Flush so the raw-SQL
        # claim in _claim_jobs sees the new run_after (not just the ORM cache).
        job.run_after = fields.Datetime.now() - timedelta(seconds=1)
        self.env.flush_all()
        self.Job._cron_run_jobs()  # attempt 2 -> terminal
        self.assertEqual(job.state, 'failed')
        self.assertEqual(len(self._onerror_calls), 1,
                         "on_error fires exactly once, on terminal failure")

    def test_worker_loop_drains_channel(self):
        # A single worker drains all due pending jobs in its channel (the basis
        # for bounded-parallel throughput, e.g. nightly backups).
        self._no_commit()
        jobs = [self.Job.enqueue(self.partner, '_job_ok', channel='drain',
                                  run_now=False) for _i in range(3)]
        self.env.flush_all()
        self.Job._worker_loop(jobs[0].id, 'drain')
        for j in jobs:
            j.invalidate_recordset()
            self.assertEqual(j.state, 'done', "worker loop must drain the channel")

    def test_claim_next_in_channel_is_scoped(self):
        self._no_commit()
        mine = self.Job.enqueue(self.partner, '_job_ok', channel='c1', run_now=False)
        self.Job.enqueue(self.partner, '_job_ok', channel='c2', run_now=False)
        self.env.flush_all()
        claimed = self.Job._claim_next_in_channel('c1')
        self.assertEqual(claimed, mine, "must claim only within the channel")
        self.assertFalse(self.Job._claim_next_in_channel('c1'),
                         "channel drained → empty")

    def test_channel_concurrency_cap(self):
        # Soft per-channel cap bounds immediate workers (no thundering herd).
        self.env['ir.config_parameter'].sudo().set_param(
            'saas_master.job_channel_concurrency', '2')
        self.assertTrue(self.Job._should_spawn_now('capx'))
        j1 = self.Job.enqueue(self.partner, '_job_ok', channel='capx', run_now=False)
        j1.state = 'running'
        self.assertTrue(self.Job._should_spawn_now('capx'), "1 < cap -> spawn ok")
        j2 = self.Job.enqueue(self.partner, '_job_ok', channel='capx', run_now=False)
        j2.state = 'running'
        self.assertFalse(self.Job._should_spawn_now('capx'), "2 >= cap -> defer")
        # Cap is per-channel: a different channel still has capacity.
        self.assertTrue(self.Job._should_spawn_now('other'))

    # ---- claim gating ----
    def test_future_job_not_claimed(self):
        self._no_commit()
        job = self.Job.enqueue(self.partner, '_job_ok',
                                eta=fields.Datetime.now() + timedelta(hours=1))
        self.Job._cron_run_jobs()
        self.assertEqual(job.state, 'pending')
        self.assertEqual(job.attempts, 0, "a not-yet-due job must not be claimed")

    # ---- reaper ----
    def test_reaper_requeues_idempotent(self):
        self._no_commit()
        job = self.Job.enqueue(self.partner, '_job_ok', idempotent=True)
        job.write({'state': 'running', 'attempts': 1,
                   'last_heartbeat': fields.Datetime.now() - timedelta(minutes=30)})
        self.Job._cron_reap_jobs()
        self.assertEqual(job.state, 'pending', "dead idempotent job is requeued")

    def test_reaper_fails_non_idempotent(self):
        self._no_commit()
        job = self.Job.enqueue(self.partner, '_job_ok', idempotent=False)
        job.write({'state': 'running', 'attempts': 1,
                   'last_heartbeat': fields.Datetime.now() - timedelta(minutes=30)})
        self.Job._cron_reap_jobs()
        self.assertEqual(job.state, 'failed',
                         "dead non-idempotent job is failed for review")

    def test_reaper_leaves_fresh_running(self):
        self._no_commit()
        job = self.Job.enqueue(self.partner, '_job_ok', idempotent=True)
        job.write({'state': 'running', 'attempts': 1,
                   'last_heartbeat': fields.Datetime.now()})
        self.Job._cron_reap_jobs()
        self.assertEqual(job.state, 'running', "a freshly-beating job is left alone")


@tagged('post_install', '-at_install')
class TestBackupViaQueue(TransactionCase):
    """ARCH-004 Phase 1: portal backups are routed through the durable queue."""

    def setUp(self):
        super().setUp()
        product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or \
            self.env['saas.product'].sudo().create(
                {'name': 'BQ Hosting', 'is_hosting': True, 'is_published': True})
        plan = self.env['saas.plan'].sudo().create({
            'name': 'BQ Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [product.id])]})
        domain = self.env['saas.based.domain'].sudo().search([], limit=1) or \
            self.env['saas.based.domain'].sudo().create({'name': 'bq.example.com'})
        partner = self.env['res.partner'].sudo().create({'name': 'BQ Cust'})
        self.instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'bqinst', 'domain_id': domain.id, 'partner_id': partner.id,
            'saas_product_id': product.id, 'plan_id': plan.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running', 'is_hosting': True})

    def test_action_create_backup_enqueues_durable_job(self):
        # Suppress the immediate worker thread so no real backup runs.
        with patch.object(type(self.env['saas.job']), '_spawn_worker',
                          lambda self: None):
            self.instance.action_create_backup()
        backup = self.env['saas.instance.backup'].sudo().search(
            [('instance_id', '=', self.instance.id)], limit=1)
        self.assertEqual(backup.state, 'running',
                         "the backup record is created up-front")
        job = self.env['saas.job'].sudo().search([
            ('model', '=', 'saas.instance.backup'),
            ('res_id', '=', backup.id)], limit=1)
        self.assertTrue(job, "a durable job must be enqueued for the backup")
        self.assertEqual(job.method, '_run_portal_backup')
        self.assertEqual(job.channel, 'backup')
        self.assertEqual(job.lock_key, 'instance:%s' % self.instance.id)
        self.assertEqual(job.state, 'pending')


@tagged('post_install', '-at_install')
class TestDbOpViaQueue(TransactionCase):
    """ARCH-004 Phase 2: DB operations run through the queue; the job heartbeat
    also keeps the db.operation's heartbeat fresh."""

    def setUp(self):
        super().setUp()
        self.Job = self.env['saas.job']
        product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or \
            self.env['saas.product'].sudo().create(
                {'name': 'DQ Hosting', 'is_hosting': True, 'is_published': True})
        plan = self.env['saas.plan'].sudo().create({
            'name': 'DQ Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [product.id])]})
        domain = self.env['saas.based.domain'].sudo().search([], limit=1) or \
            self.env['saas.based.domain'].sudo().create({'name': 'dq.example.com'})
        partner = self.env['res.partner'].sudo().create({'name': 'DQ Cust'})
        self.instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'dqinst', 'domain_id': domain.id, 'partner_id': partner.id,
            'saas_product_id': product.id, 'plan_id': plan.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running', 'is_hosting': True})
        wp = patch.object(type(self.Job), '_spawn_worker', lambda self: None)
        wp.start()
        self.addCleanup(wp.stop)

    # test_drop_async_enqueues_dbop_job, test_create_async_enqueues_dbop_job,
    # test_create_async_rejects_existing_name,
    # test_duplicate_async_enqueues_dbop_job,
    # test_duplicate_async_rejects_missing_source, and the
    # _mock_hosting_db_list helper were removed: hosting_db_create_async/
    # _duplicate_async/_drop_async (and hosting_db_list) were ssh_docker/
    # docker-exec-only (see saas_instance.py's hosting_db_* family) and
    # were removed along with that backend — DB self-service is gone for
    # now (a later phase reimplements it via driver.exec()). The heartbeat
    # tests below are generic saas.job mechanics (using
    # saas.instance.db.operation only as a convenient target model) and
    # are unaffected.

    def test_heartbeat_tick_keeps_dbop_fresh(self):
        op = self.env['saas.instance.db.operation'].sudo().create({
            'instance_id': self.instance.id, 'db_name': 'd', 'operation': 'drop'})
        op.last_heartbeat = fields.Datetime.now() - timedelta(minutes=10)
        job = self.Job.enqueue(op, '_run_drop', channel='dbop', run_now=False)
        job.state = 'running'
        self.assertTrue(self.Job._heartbeat_tick(job.id))
        self.assertTrue(job.last_heartbeat)
        self.assertGreater(op.last_heartbeat,
                           fields.Datetime.now() - timedelta(minutes=1),
                           "the db.operation heartbeat must be refreshed too")

    def test_heartbeat_tick_target_without_field_is_safe(self):
        backup = self.env['saas.instance.backup'].sudo().create({
            'instance_id': self.instance.id, 'name': 'b', 'state': 'running'})
        job = self.Job.enqueue(backup, '_run_portal_backup', run_now=False)
        job.state = 'running'
        self.assertTrue(self.Job._heartbeat_tick(job.id))  # must not raise
        self.assertTrue(job.last_heartbeat)

    def test_heartbeat_tick_stops_when_not_running(self):
        op = self.env['saas.instance.db.operation'].sudo().create({
            'instance_id': self.instance.id, 'db_name': 'd2', 'operation': 'drop'})
        job = self.Job.enqueue(op, '_run_drop', run_now=False)  # state pending
        self.assertFalse(self.Job._heartbeat_tick(job.id),
                         "a non-running job stops the beater")


@tagged('post_install', '-at_install')
class TestDailyBackupViaQueue(TransactionCase):
    """Phase 5 redesign: the operator's own CronJob now drives the actual
    nightly backup once ``spec.backup`` is enabled — ``_cron_sync_scheduled_backups``
    only mirrors bucket contents into ``saas.instance.backup`` records for
    enabled + non-suspended instances (pure bucket listing, no job queue,
    no Kubernetes exec)."""

    def setUp(self):
        super().setUp()
        self.Job = self.env['saas.job']
        product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or \
            self.env['saas.product'].sudo().create(
                {'name': 'DB Hosting', 'is_hosting': True, 'is_published': True})
        plan = self.env['saas.plan'].sudo().create({
            'name': 'DB Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [product.id])]})
        domain = self.env['saas.based.domain'].sudo().search([], limit=1) or \
            self.env['saas.based.domain'].sudo().create({'name': 'db.example.com'})
        partner = self.env['res.partner'].sudo().create({'name': 'DB Cust'})
        self._base = {
            'domain_id': domain.id, 'partner_id': partner.id,
            'saas_product_id': product.id, 'plan_id': plan.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'is_hosting': True, 'state': 'running'}
        wp = patch.object(type(self.Job), '_spawn_worker', lambda self: None)
        wp.start()
        self.addCleanup(wp.stop)

    def _inst(self, sub, **vals):
        return self.env['saas.instance'].sudo().create(
            {**self._base, 'subdomain': sub, **vals})

    def test_cron_syncs_only_enabled_non_suspended_instances(self):
        on = self._inst('bkon', daily_backup_enabled=True)
        off_disabled = self._inst('bkoff', daily_backup_enabled=False)
        off_suspended = self._inst('bksusp', daily_backup_enabled=True,
                                   daily_backup_suspended=True)
        Backup = self.env['saas.instance.backup']
        seen_instances = []

        def _fake_list_stamps(self, cfg, prefix):
            return set()

        with patch.object(type(Backup), '_get_backup_config',
                          lambda self: {'provider': 's3', 'bucket': 'b',
                                        'access_key': 'a', 'secret_key': 's',
                                        'endpoint': '', 'region': ''}), \
             patch.object(type(Backup), '_list_backup_stamps',
                          lambda self, cfg, prefix: (
                              seen_instances.append(prefix) or set())), \
             patch.object(type(Backup), '_cleanup_old_backups', lambda self: None):
            Backup._cron_sync_scheduled_backups()

        self.assertIn(on._backup_bucket_prefix(), seen_instances,
                     "enabled, non-suspended instance must be synced")
        self.assertNotIn(off_disabled._backup_bucket_prefix(), seen_instances,
                        "disabled → not synced")
        self.assertNotIn(off_suspended._backup_bucket_prefix(), seen_instances,
                        "suspended → not synced")

    def test_cron_records_new_stamps_as_backups(self):
        on = self._inst('bkrec', daily_backup_enabled=True)
        Backup = self.env['saas.instance.backup']
        with patch.object(type(Backup), '_get_backup_config',
                          lambda self: {'provider': 's3', 'bucket': 'b',
                                        'access_key': 'a', 'secret_key': 's',
                                        'endpoint': '', 'region': ''}), \
             patch.object(type(Backup), '_list_backup_stamps',
                          lambda self, cfg, prefix: {'20260101T000000Z'}), \
             patch.object(type(Backup), '_bucket_object_size',
                          lambda self, key: 1024), \
             patch.object(type(Backup), '_cleanup_old_backups', lambda self: None):
            Backup._cron_sync_scheduled_backups()
        rec = Backup.search([
            ('instance_id', '=', on.id), ('is_full_instance', '=', True),
            ('format', '=', 'operator')])
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec.bucket_path,
                        '%s/20260101T000000Z' % on._backup_bucket_prefix())


@tagged('post_install', '-at_install')
class TestDeployViaQueue(TransactionCase):
    """ARCH-004 Phase 3: deploy + delete run through the queue, with
    _on_background_error wired as the job's on_error."""

    def setUp(self):
        super().setUp()
        self.Job = self.env['saas.job']
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or \
            self.env['saas.product'].sudo().create(
                {'name': 'DPQ Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'DPQ Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.domain = self.env['saas.based.domain'].sudo().search([], limit=1) or \
            self.env['saas.based.domain'].sudo().create({'name': 'dpq.example.com'})
        self.partner = self.env['res.partner'].sudo().create({'name': 'DPQ Cust'})
        wp = patch.object(type(self.Job), '_spawn_worker', lambda self: None)
        wp.start()
        self.addCleanup(wp.stop)

    def _instance(self, sub, **vals):
        base = {
            'subdomain': sub, 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': False, 'is_hosting': True,
        }
        base.update(vals)
        return self.env['saas.instance'].sudo().create(base)

    def test_action_deploy_enqueues_deploy_job(self):
        inst = self._instance('dpqdeploy', state='draft', is_trial=True)
        Inst = type(inst)
        with patch.object(Inst, '_allocate_servers', lambda self: True), \
                patch.object(Inst, '_validate_deploy_fields', lambda self: None):
            inst.action_deploy()
        job = self.Job.sudo().search([
            ('model', '=', 'saas.instance'), ('res_id', '=', inst.id),
            ('method', '=', '_do_deploy')], limit=1)
        self.assertTrue(job, "deploy must enqueue a durable job")
        self.assertEqual(job.channel, 'deploy')
        self.assertEqual(job.lock_key, 'instance:%s' % inst.id)
        self.assertEqual(job.on_error, '_on_background_error')
        self.assertEqual(json.loads(job.on_error_args_json), ['failed'])
        self.assertEqual(inst.state, 'provisioning')
        # Retry cutover: the queue owns retries (max_attempts) + crash recovery
        # (idempotent → reaper requeues a dead deploy).
        self.assertEqual(job.max_attempts, inst.max_deploy_retries + 1)
        self.assertTrue(job.idempotent)

    def test_deploy_terminal_failure_marks_instance_failed(self):
        # _on_background_error (the deploy job's on_error) now goes straight to
        # 'failed' — the queue already exhausted its retries; no pending dance.
        inst = self._instance('dpqfail', state='provisioning')
        inst._on_background_error(Exception("boom"), 'failed')
        self.assertEqual(inst.state, 'failed',
                         "terminal deploy failure must not re-queue to pending")

    def test_recover_stuck_defers_to_queue(self):
        # An instance with a live deploy job must be left to the queue reaper,
        # not recovered by the stuck-provisioning cron (no double-run).
        inst = self._instance('dpqstuck', state='provisioning')
        inst.pending_operation = 'deploy'
        inst.write({'pre_provisioning_state': 'failed'})
        job = self.Job.enqueue(inst, '_do_deploy', channel='deploy',
                                idempotent=True, run_now=False)
        job.state = 'running'
        # Make it look stuck (old write_date) for the cron's search window.
        self.env.cr.execute(
            "UPDATE saas_instance SET write_date = (now() at time zone 'UTC') "
            "- interval '30 minutes' WHERE id = %s", (inst.id,))
        inst.invalidate_recordset()
        self.env['saas.instance']._cron_recover_stuck_provisioning()
        inst.invalidate_recordset()
        self.assertEqual(inst.state, 'provisioning',
                         "instance with a live job must be left to the queue")

    def _lifecycle_job(self, inst, method):
        return self.Job.sudo().search([
            ('model', '=', 'saas.instance'), ('res_id', '=', inst.id),
            ('method', '=', method)], limit=1)

    def test_action_restart_enqueues_idempotent_job(self):
        inst = self._instance('dpqrestart', state='running')
        inst.action_restart()
        job = self._lifecycle_job(inst, '_do_restart')
        self.assertTrue(job, "restart must enqueue a durable job")
        self.assertEqual(job.channel, 'deploy')
        self.assertTrue(job.idempotent, "restart is crash-recoverable")
        self.assertEqual(job.max_attempts, 2)
        self.assertEqual(job.on_error, '_on_background_error')
        self.assertEqual(json.loads(job.on_error_args_json), ['running'])

    # test_action_redeploy_enqueues_idempotent_job and
    # test_action_redeploy_writes_audit_log were removed:
    # action_redeploy/_do_redeploy were ssh_docker-only (blue/green
    # docker-compose redeploy) and were removed along with that backend
    # (see test_redeploy_blue_green.py, also removed).

    def test_action_deploy_writes_audit_log(self):
        # SEC-010: deploy is one of the "who scaled, deployed, restored, or
        # deleted an instance" actions the audit found had no append-only
        # trail at all.
        inst = self._instance('dpqdeployaudit', state='draft', is_trial=True)
        Inst = type(inst)
        with patch.object(Inst, '_allocate_servers', lambda self: True), \
                patch.object(Inst, '_validate_deploy_fields', lambda self: None):
            inst.action_deploy()
        entry = self.env['saas.audit.log'].sudo().search([
            ('action', '=', 'instance_deploy'),
            ('model', '=', 'saas.instance'), ('res_id', '=', inst.id)], limit=1)
        self.assertTrue(entry, "action_deploy must write an audit log entry")
        self.assertEqual(entry.res_name, inst.subdomain)
        self.assertEqual(entry.result, 'ok')

    def test_action_delete_enqueues_job(self):
        inst = self._instance('dpqdel', state='running')
        inst.action_delete_instance()
        job = self.Job.sudo().search([
            ('model', '=', 'saas.instance'), ('res_id', '=', inst.id),
            ('method', '=', '_do_delete_instance')], limit=1)
        self.assertTrue(job, "delete must enqueue a durable job")
        self.assertEqual(job.channel, 'deploy')
        self.assertEqual(job.on_error, '_on_background_error')
        self.assertEqual(json.loads(job.on_error_args_json), ['running'])


@tagged('post_install', '-at_install')
class TestWebhookDeployViaQueue(TransactionCase):
    """ARCH-004 Phase 4: webhook auto-deploy runs through the queue; its
    success/error wrappers drive the build record + repo error handling."""

    def setUp(self):
        super().setUp()
        product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or \
            self.env['saas.product'].sudo().create(
                {'name': 'WD Hosting', 'is_hosting': True, 'is_published': True})
        plan = self.env['saas.plan'].sudo().create({
            'name': 'WD Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [product.id])]})
        domain = self.env['saas.based.domain'].sudo().search([], limit=1) or \
            self.env['saas.based.domain'].sudo().create({'name': 'wd.example.com'})
        partner = self.env['res.partner'].sudo().create({'name': 'WD Cust'})
        self.instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'wdinst', 'domain_id': domain.id, 'partner_id': partner.id,
            'saas_product_id': product.id, 'plan_id': plan.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running', 'is_hosting': True})
        self.repo = self.env['saas.instance.repo'].sudo().create({
            'instance_id': self.instance.id,
            'repo_url': 'https://github.com/acme/app.git', 'branch': 'main'})

    def _build(self):
        return self.env['saas.build'].sudo().create({
            'instance_id': self.instance.id, 'repo_id': self.repo.id,
            'branch': 'main', 'source': 'push', 'state': 'running'})

    def test_run_webhook_deploy_marks_build_success(self):
        build = self._build()
        with patch.object(type(self.repo), '_do_webhook_pull_and_restart',
                          lambda self: None):
            self.repo._run_webhook_deploy(build.id)
        self.assertEqual(build.state, 'success')

    def test_on_webhook_deploy_error_fails_build_and_handles_repo(self):
        build = self._build()
        calls = []
        with patch.object(type(self.repo), '_on_repo_background_error',
                          lambda self, e: calls.append(str(e))):
            self.repo._on_webhook_deploy_error(ValueError('boom'), build.id)
        self.assertEqual(build.state, 'failed')
        self.assertEqual(len(calls), 1, "repo background-error handler must run")
        self.assertIn('boom', calls[0])


@tagged('post_install', '-at_install')
class TestRepoOpsViaQueue(TransactionCase):
    """ARCH-004: repo clone/pull run through the durable queue."""

    def setUp(self):
        super().setUp()
        self.Job = self.env['saas.job']
        product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or \
            self.env['saas.product'].sudo().create(
                {'name': 'RO Hosting', 'is_hosting': True, 'is_published': True})
        plan = self.env['saas.plan'].sudo().create({
            'name': 'RO Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [product.id])]})
        domain = self.env['saas.based.domain'].sudo().search([], limit=1) or \
            self.env['saas.based.domain'].sudo().create({'name': 'ro.example.com'})
        partner = self.env['res.partner'].sudo().create({'name': 'RO Cust'})
        self.instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'roinst', 'domain_id': domain.id, 'partner_id': partner.id,
            'saas_product_id': product.id, 'plan_id': plan.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running', 'is_hosting': True})
        self.repo = self.env['saas.instance.repo'].sudo().create({
            'instance_id': self.instance.id,
            'repo_url': 'https://github.com/acme/app.git', 'branch': 'main'})
        wp = patch.object(type(self.Job), '_spawn_worker', lambda self: None)
        wp.start()
        self.addCleanup(wp.stop)

    def _job_for(self, method):
        return self.Job.sudo().search([
            ('model', '=', 'saas.instance.repo'),
            ('res_id', '=', self.repo.id), ('method', '=', method)], limit=1)

    def test_clone_enqueues_deploy_job(self):
        self.repo.action_clone_repo()
        job = self._job_for('_do_clone_and_restart')
        self.assertTrue(job)
        self.assertEqual(job.channel, 'deploy')
        self.assertEqual(job.lock_key, 'instance:%s' % self.instance.id)
        self.assertEqual(job.on_error, '_on_repo_background_error')
        self.assertTrue(job.idempotent)

    def test_pull_enqueues_deploy_job(self):
        self.repo.state = 'cloned'
        self.repo.action_pull_repo()
        job = self._job_for('_do_pull_repo')
        self.assertTrue(job)
        self.assertEqual(job.channel, 'deploy')
        self.assertEqual(job.on_error, '_on_repo_background_error')
