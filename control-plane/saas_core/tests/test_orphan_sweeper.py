"""C3.5: the orphan sweeper re-queues a paid instance or a provisioning
operation that has no live job, and flags a repeat offender."""
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestOrphanSweeper(TransactionCase):

    def setUp(self):
        super().setUp()
        env = self.env
        self.Job = env['saas.job'].sudo()
        self.Instance = env['saas.instance'].sudo()
        product = env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or env['saas.product'].sudo().create(
            {'name': 'Sw Hosting', 'is_hosting': True, 'is_published': True})
        plan = env['saas.plan'].sudo().create({
            'name': 'Sw Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 10,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [product.id])]})
        partner = env['res.partner'].sudo().create({'name': 'Sw Cust'})
        domain = env['saas.based.domain'].sudo().search([], limit=1) \
            or env['saas.based.domain'].sudo().create({'name': 'sw.example.com'})
        server = env['saas.server'].sudo().create(
            {'name': 'sw-srv', 'compute_driver': 'kubernetes'})
        self.inst = self.Instance.create({
            'subdomain': 'swtest', 'domain_id': domain.id,
            'partner_id': partner.id, 'saas_product_id': product.id,
            'plan_id': plan.id, 'docker_server_id': server.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running'})
        self.driver = MagicMock()
        self.alerts = []
        for target, attr, value in (
                (type(self.inst), '_compute_driver',
                 lambda rec, connection=None: self.driver),
                (type(env['saas.alert']), '_notify',
                 lambda rec, event, message, **kw: self.alerts.append(event)),
                (env.cr, 'commit', lambda: None),
                (env.cr, 'rollback', lambda: None)):
            p = patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def _orphan(self, minutes_ago=11, **vals):
        """Put the instance in an orphan state last touched *minutes_ago*."""
        self.inst.write(vals)
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE saas_instance SET write_date = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(minutes=minutes_ago), self.inst.id))
        self.inst.invalidate_recordset()

    def _stuck(self, op, prev='running', minutes_ago=11):
        self._orphan(minutes_ago, state='provisioning', pending_operation=op,
                     pre_provisioning_state=prev)

    def _jobs(self, method=None):
        domain = [('model', '=', 'saas.instance'), ('res_id', '=', self.inst.id)]
        if method:
            domain.append(('method', '=', method))
        return self.Job.search(domain, order='id')

    def _sweep(self):
        n = self.Instance._cron_sweep_orphaned_operations()
        self.inst.invalidate_recordset()
        return n

    def _run(self, job):
        self.env.flush_all()
        self.Job._run_one(job.id)
        job.invalidate_recordset()
        self.inst.invalidate_recordset()

    # ------------------------------------------------------------- paid
    def test_orphaned_paid_instance_is_deployed_within_one_cycle(self):
        self._orphan(0, state='paid')
        self.assertEqual(self._sweep(), 1)
        job = self._jobs('_do_deploy_after_payment')
        self.assertEqual(len(job), 1)
        self.assertEqual(job.idempotency_key,
                         'sweep:inst%s:1:deploy-after-payment' % self.inst.id)
        self.assertEqual(job.lock_key, 'instance:%s' % self.inst.id)
        self.assertEqual(json.loads(job.on_error_args_json), ['failed'])
        deployed = []
        with patch.object(type(self.inst), 'action_deploy',
                          lambda rec: deployed.append(rec.id)):
            self._run(job)
        self.assertEqual(job.state, 'done')
        self.assertEqual(deployed, [self.inst.id])

    def test_paid_instance_with_a_live_job_is_left_alone(self):
        self._orphan(0, state='paid')
        self.inst._enqueue_deploy_after_payment()
        self.assertEqual(self._sweep(), 0)
        self.assertEqual(len(self._jobs()), 1)

    # ----------------------------------------------------- provisioning
    def test_stuck_stop_is_requeued_and_completes(self):
        self._stuck('stop')
        self.assertEqual(self._sweep(), 1)
        job = self._jobs('_do_stop')
        self.assertEqual(len(job), 1)
        self.assertEqual(json.loads(job.on_error_args_json), ['running'])
        self._run(job)
        self.assertEqual(job.state, 'done')
        self.assertEqual(self.inst.state, 'stopped')
        self.driver.stop.assert_called_once()

    def test_stuck_suspend_is_requeued_and_completes(self):
        self._stuck('suspend')
        self._sweep()
        job = self._jobs('_do_suspend')
        self._run(job)
        self.assertEqual(self.inst.state, 'suspended')

    def test_stuck_deploy_is_requeued_with_its_retry_budget(self):
        self._stuck('deploy', prev='failed')
        self._sweep()
        job = self._jobs('_do_deploy')
        self.assertEqual(len(job), 1)
        self.assertEqual(job.max_attempts, self.inst.max_deploy_retries + 1)
        self.assertEqual(json.loads(job.on_error_args_json), ['failed'])

    def test_stuck_cancel_reuses_its_backup_watermark(self):
        self.Job.create({
            'model': 'saas.instance', 'res_id': self.inst.id,
            'method': '_do_delete_instance', 'args_json': '[42]',
            'state': 'failed', 'channel': 'deploy'})
        self._stuck('cancel')
        self._sweep()
        job = self._jobs('_do_delete_instance').filtered(lambda j: j.state == 'pending')
        self.assertEqual(len(job), 1)
        self.assertEqual(job.args_json, '[42]')

    def test_recent_operation_is_not_swept(self):
        self._stuck('stop', minutes_ago=5)
        self.assertEqual(self._sweep(), 0)
        self.assertFalse(self._jobs())

    def test_operation_with_a_live_job_is_not_swept(self):
        self.inst.action_stop()
        self._orphan(11)
        self.assertEqual(self._sweep(), 0)
        self.assertEqual(len(self._jobs()), 1)

    def test_restore_and_delete_stay_with_the_recovery_cron(self):
        for op in ('restore', 'delete'):
            self._stuck(op)
            self.assertEqual(self._sweep(), 0, op)
        self.assertFalse(self._jobs())

    def test_operation_without_a_state_to_restore_is_not_swept(self):
        self._stuck('stop', prev=False)
        self.assertEqual(self._sweep(), 0)
        self.assertFalse(self._jobs())

    def test_operation_still_holding_its_lock_is_not_swept(self):
        self._stuck('stop')
        with patch.object(type(self.inst), '_operation_is_alive', lambda rec: True):
            self.assertEqual(self._sweep(), 0)
        self.assertFalse(self._jobs())

    # --------------------------------------------------- repeat offender
    def test_third_sweep_of_the_same_instance_alerts_once(self):
        for n in (1, 2, 3, 4):
            self._stuck('stop')
            self.assertEqual(self._sweep(), 1)
            job = self._jobs('_do_stop')[-1]
            self.assertTrue(job.idempotency_key.startswith(
                'sweep:inst%s:%d:' % (self.inst.id, n)))
            # The job ends without fixing the state: orphaned again.
            job.state = 'done'
            self.assertEqual(self.alerts.count('operation_orphaned'),
                             1 if n >= 3 else 0)
