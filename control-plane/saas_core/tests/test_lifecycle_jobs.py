"""C3.3: stop / suspend / cancel run as durable jobs queued in the caller's
transaction — a worker restart completes them exactly once, a failure puts
the server back to its previous state."""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestLifecycleJobs(TransactionCase):

    def setUp(self):
        super().setUp()
        env = self.env
        self.Job = env['saas.job'].sudo()
        self.Backup = env['saas.instance.backup'].sudo()
        product = env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or env['saas.product'].sudo().create(
            {'name': 'Lc Hosting', 'is_hosting': True, 'is_published': True})
        plan = env['saas.plan'].sudo().create({
            'name': 'Lc Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 10,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [product.id])]})
        partner = env['res.partner'].sudo().create({'name': 'Lc Cust'})
        domain = env['saas.based.domain'].sudo().search([], limit=1) \
            or env['saas.based.domain'].sudo().create({'name': 'lc.example.com'})
        server = env['saas.server'].sudo().create(
            {'name': 'lc-srv', 'compute_driver': 'kubernetes'})
        self.inst = env['saas.instance'].sudo().create({
            'subdomain': 'lctest', 'domain_id': domain.id,
            'partner_id': partner.id, 'saas_product_id': product.id,
            'plan_id': plan.id, 'docker_server_id': server.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running'})
        self.driver = MagicMock()
        self.driver.exists.return_value = False
        for target, attr, value in (
                (type(self.inst), '_compute_driver',
                 lambda rec, connection=None: self.driver),
                (type(self.Backup), '_delete_bucket_prefix', lambda rec, prefix: None),
                (type(self.Backup), '_delete_from_bucket', lambda rec: None)):
            p = patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def _patch_cr(self, **methods):
        for name, fn in methods.items():
            p = patch.object(self.env.cr, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def _forbid_commit(self):
        """Queuing must never commit the caller's transaction."""
        def boom():
            raise AssertionError('caller transaction committed')
        self._patch_cr(commit=boom)

    def _no_commit(self):
        self._patch_cr(commit=lambda: None, rollback=lambda: None)

    def _backup(self, name, source='final'):
        return self.Backup.create({
            'instance_id': self.inst.id, 'name': name, 'state': 'done',
            'is_full_instance': True, 'format': 'operator', 'source': source,
            'bucket_path': 'backups/lctest/%s' % name})

    def _job(self, method):
        return self.Job.search([
            ('model', '=', 'saas.instance'), ('res_id', '=', self.inst.id),
            ('method', '=', method)])

    def _run(self, job):
        # _run_one claims with raw SQL: flush ORM writes (requeues) first.
        self.env.flush_all()
        self.Job._run_one(job.id)
        job.invalidate_recordset()
        self.inst.invalidate_recordset()

    def _crash_and_restart(self, job):
        """The worker claimed *job* and died: no heartbeat since. The reaper
        of the restarted service must hand it back to the queue."""
        job.write({'state': 'running', 'attempts': 1,
                   'last_heartbeat': fields.Datetime.now() - timedelta(hours=1)})
        self.env.flush_all()
        self.Job._cron_reap_jobs()
        job.invalidate_recordset()
        self.assertEqual(job.state, 'pending')

    def _assert_queued(self, op, method):
        job = self._job(method)
        self.assertEqual(len(job), 1)
        self.assertTrue(job.idempotency_key.startswith(
            '%s:inst%s:' % (op, self.inst.id)))
        self.assertEqual(job.lock_key, 'instance:%s' % self.inst.id)
        self.assertTrue(job.idempotent)
        self.assertEqual(job.on_error, '_on_background_error')
        self.assertEqual(job.on_error_args_json, '["running"]')
        self.assertEqual(
            (self.inst.state, self.inst.pending_operation,
             self.inst.pre_provisioning_state),
            ('provisioning', op, 'running'))
        return job

    # ------------------------------------------------------------- enqueue
    def test_stop_queues_one_job_in_the_caller_transaction(self):
        self._forbid_commit()
        self.inst.action_stop()
        self._assert_queued('stop', '_do_stop')
        self.driver.stop.assert_not_called()

    def test_suspend_queues_one_job_in_the_caller_transaction(self):
        self._forbid_commit()
        self.inst.action_suspend()
        self._assert_queued('suspend', '_do_suspend')
        self.driver.stop.assert_not_called()

    def test_cancel_queues_one_job_carrying_the_backup_watermark(self):
        older = self._backup('nightly', source='scheduled')
        self._forbid_commit()
        self.inst.action_cancel()
        job = self._assert_queued('cancel', '_do_delete_instance')
        self.assertEqual(job.args_json, '[%d]' % older.id)
        self.driver.destroy.assert_not_called()

    # ------------------------------------------------- worker killed mid-op
    def test_worker_killed_mid_stop_completes_after_restart(self):
        self.inst.action_stop()
        job = self._job('_do_stop')
        self._no_commit()
        self._crash_and_restart(job)
        self._run(job)
        self.assertEqual(job.state, 'done')
        self.assertEqual(self.inst.state, 'stopped')
        self.assertFalse(self.inst.pending_operation)
        self.driver.stop.assert_called_once()

    def test_worker_killed_mid_suspend_completes_after_restart(self):
        self.inst.action_suspend()
        job = self._job('_do_suspend')
        self._no_commit()
        self._crash_and_restart(job)
        self._run(job)
        self.assertEqual(job.state, 'done')
        self.assertEqual(self.inst.state, 'suspended')
        self.assertFalse(self.inst.pending_operation)
        self.driver.stop.assert_called_once()

    def test_worker_killed_mid_cancel_completes_and_keeps_its_snapshot(self):
        # Retained by an earlier cancel the customer reactivated from.
        old_final = self._backup('old-final')
        self.inst.action_cancel()
        job = self._job('_do_delete_instance')
        self._no_commit()
        # The killed run had committed its final snapshot, then died.
        fresh_final = self._backup('fresh-final')
        self._crash_and_restart(job)
        with patch.object(type(self.Backup), '_create_full_instance_backup_sync',
                          side_effect=AssertionError('second final snapshot')):
            self._run(job)
        self.assertEqual(job.state, 'done')
        self.assertIn(self.inst.state, ('cancelled', 'cancelled_by_client'))
        self.driver.destroy.assert_called_once()
        self.assertTrue(fresh_final.exists(), "the interrupted run's snapshot is kept")
        self.assertFalse(old_final.exists())

    def test_cancel_keeps_only_its_fresh_final_snapshot(self):
        old_final = self._backup('old-final')
        self.inst.action_cancel()
        job = self._job('_do_delete_instance')
        self._no_commit()
        taken = []

        def snapshot(rec, instance):
            taken.append(self._backup('fresh-final'))
            return taken[-1]

        with patch.object(type(self.Backup), '_create_full_instance_backup_sync',
                          snapshot):
            self._run(job)
        self.assertEqual(job.state, 'done')
        self.assertEqual(len(taken), 1)
        self.assertTrue(taken[0].exists())
        self.assertFalse(old_final.exists())

    # ----------------------------------------------------- exactly once
    def test_rerun_of_a_finished_stop_never_stops_a_restarted_server(self):
        self.inst.action_stop()
        job = self._job('_do_stop')
        self._no_commit()
        self._run(job)
        self.assertEqual(self.inst.state, 'stopped')
        # The customer started it again; then the finished job runs again.
        self.inst.write({'state': 'running', 'pending_operation': False})
        job.write({'state': 'pending'})
        self.driver.reset_mock()
        self._run(job)
        self.assertEqual(job.state, 'done')
        self.driver.stop.assert_not_called()
        self.assertEqual(self.inst.state, 'running')

    # --------------------------------------------------- terminal failure
    def test_failed_stop_restores_the_previous_state(self):
        self.inst.action_stop()
        job = self._job('_do_stop')
        job.max_attempts = 1
        self.driver.stop.side_effect = RuntimeError('cluster unreachable')
        self._no_commit()
        self._run(job)
        self.assertEqual(job.state, 'failed')
        self.assertEqual(self.inst.state, 'running')
        self.assertFalse(self.inst.pending_operation)
        self.assertFalse(self.inst.pre_provisioning_state)
        self.assertIn('cluster unreachable', self.inst.last_error)
