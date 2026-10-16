"""C6.1: package convergence. A package the cluster rejected stays desired;
the reconciler retries it with back-off until it is applied."""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPackageConvergence(TransactionCase):

    def setUp(self):
        super().setUp()
        env = self.env
        self.Job = env['saas.job'].sudo()
        self.Instance = env['saas.instance'].sudo()
        product = env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or env['saas.product'].sudo().create(
            {'name': 'Pk Hosting', 'is_hosting': True, 'is_published': True})
        self.small, self.big = (env['saas.plan'].sudo().create({
            'name': name, 'is_custom': True, 'workers': workers, 'storage_limit': 10,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [product.id])]})
            for name, workers in (('Pk Small', 1), ('Pk Big', 3)))
        partner = env['res.partner'].sudo().create({'name': 'Pk Cust'})
        domain = env['saas.based.domain'].sudo().search([], limit=1) \
            or env['saas.based.domain'].sudo().create({'name': 'pk.example.com'})
        self.server = env['saas.server'].sudo().create(
            {'name': 'pk-srv', 'compute_driver': 'kubernetes'})
        self.version = env['saas.odoo.version'].sudo().search(
            [('docker_image', '!=', False), ('docker_image_tag', '!=', False)], limit=1) \
            or env['saas.odoo.version'].sudo().create(
                {'name': 'Pk 18', 'docker_image': 'odoo', 'docker_image_tag': '18.0'})
        self.inst = self._mk('pktest', partner, product, domain)
        self.driver = MagicMock()
        # C6.3: the rollout verifies, and the C6.11 delivery verdict passes.
        self.driver.package_rollout.return_value = ('applied', None, '')
        self.driver.delivery_status.return_value = {'ready': True, 'failure': None, 'detail': ''}
        self.driver.check_public_url.return_value = (True, '')
        self.alerts = []
        for target, attr, value in (
                (type(self.inst), '_compute_driver',
                 lambda rec, connection=None: self.driver),
                (type(self.inst), '_compute_handle', lambda rec: rec.subdomain),
                (type(env['saas.alert']), '_notify',
                 lambda rec, event, message, **kw: self.alerts.append(event)),
                (type(self.Job), '_spawn_worker', lambda rec: None),
                (type(self.Job), '_spawn_worker_after_commit', lambda rec: None),
                (env.cr, 'commit', lambda: None),
                (env.cr, 'rollback', lambda: None)):
            p = patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def _mk(self, sub, partner, product, domain):
        return self.Instance.create({
            'subdomain': sub, 'domain_id': domain.id,
            'partner_id': partner.id, 'saas_product_id': product.id,
            'plan_id': self.small.id, 'docker_server_id': self.server.id,
            'odoo_version_id': self.version.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running'})

    def _fault(self, on=True):
        self.driver.set_package.side_effect = (
            RuntimeError('admission webhook denied') if on else None)

    def _fail_once(self, inst=None):
        # Not assertRaises: it rolls back a savepoint, and with it the
        # recorded failure (callers catch and keep it).
        inst = inst or self.inst
        try:
            inst._update_container_resources()
        except RuntimeError:
            return
        self.fail('the apply should have failed')

    def _due(self, inst=None):
        """Move the next retry into the past."""
        (inst or self.inst).package_next_retry = fields.Datetime.now() - timedelta(seconds=1)

    def _jobs(self, inst=None):
        return self.Job.search([
            ('model', '=', 'saas.instance'), ('res_id', '=', (inst or self.inst).id),
            ('method', '=', '_do_converge_package')], order='id')

    def _reconcile(self):
        self.Instance._converge_packages()
        self.env.flush_all()

    def _run(self, job):
        self.env.flush_all()
        self.Job._run_one(job.id)
        self.env.invalidate_all()

    def _key(self, inst=None):
        inst = inst or self.inst
        return inst._package_key(inst._k8s_plan_resources())

    # ------------------------------------------------------------- tests
    def test_apply_records_desired_and_applied(self):
        self.inst._update_container_resources()
        self.assertEqual(self.inst.package_status, 'applying', "accepted, not yet verified (C6.3)")
        self.assertEqual(self.inst.package_desired, self._key())
        self.assertFalse(self.inst.package_applied)
        self.driver.set_package.assert_called_once()
        self.inst._verify_package()
        self.assertEqual(self.inst.package_status, 'in_sync')
        self.assertEqual(self.inst.package_applied, self._key())

    def test_failure_keeps_package_desired_with_backoff(self):
        self.inst._update_container_resources()
        applied = self.inst.package_applied
        self.inst.plan_id = self.big
        self._fault()
        before = fields.Datetime.now()
        self._fail_once()
        self.assertEqual(self.inst.package_status, 'pending')
        self.assertEqual(self.inst.package_attempts, 1)
        self.assertEqual(self.inst.package_desired, self._key())
        self.assertEqual(self.inst.package_applied, applied, "applied is what ran")
        self.assertNotEqual(self.inst.package_desired, self.inst.package_applied)
        self.assertIn('admission webhook denied', self.inst.package_error)
        self.assertGreaterEqual(self.inst.package_next_retry, before + timedelta(minutes=5))
        self.assertLessEqual(self.inst.package_next_retry,
                             fields.Datetime.now() + timedelta(minutes=5))
        self.assertTrue(self.inst.package_pending_since)

    def test_no_retry_before_backoff_elapses(self):
        self._fault()
        self._fail_once()
        self._reconcile()
        self.assertFalse(self._jobs())

    def test_injected_fault_converges_after_it_clears(self):
        """Acceptance: plan changed, patch fails, retries fail while the fault
        lasts, then one retry applies the paid package."""
        self.inst.plan_id = self.big
        self._fault()
        self._fail_once()
        since = self.inst.package_pending_since
        # A retry while the fault lasts: next back-off step (10 min).
        self._due()
        self._reconcile()
        job = self._jobs()
        self.assertEqual(len(job), 1)
        self._run(job)
        self.assertEqual(job.state, 'done', "a failed retry is recorded, not raised")
        self.assertEqual(self.inst.package_attempts, 2)
        self.assertEqual(self.inst.package_status, 'pending')
        self.assertEqual(self.inst.package_pending_since, since)
        self.assertGreater(self.inst.package_next_retry,
                           fields.Datetime.now() + timedelta(minutes=9))
        # The fault clears; the next due retry converges.
        self._fault(False)
        self._due()
        self._reconcile()
        jobs = self._jobs()
        self.assertEqual(len(jobs), 2)
        self._run(jobs[-1])
        self.assertEqual(self.inst.package_status, 'applying')
        self.inst._verify_package()
        self.assertEqual(self.inst.package_status, 'in_sync')
        self.assertEqual(self.inst.package_applied, self._key())
        self.assertEqual(self.inst.package_attempts, 0)
        self.assertFalse(self.inst.package_next_retry or self.inst.package_error
                         or self.inst.package_pending_since)
        applied = self.driver.set_package.call_args.args[1]
        self.assertEqual(applied['workers'], self.inst._k8s_plan_resources()['workers'])
        self.assertIn('Package converged', self.inst.provisioning_log)

    def test_one_retry_job_per_failure(self):
        self._fault()
        self._fail_once()
        self._due()
        self._reconcile()
        self._reconcile()
        self.assertEqual(len(self._jobs()), 1, "the same attempt is queued once")

    def test_retry_uses_the_instance_lock(self):
        self._fault()
        self._fail_once()
        self._due()
        self._reconcile()
        job = self._jobs()
        self.assertEqual(job.lock_key, 'instance:%s' % self.inst.id)
        self.assertEqual(job.channel, 'deploy')

    def test_backoff_doubles_and_caps(self):
        self._fault()
        delays = []
        for _ in range(9):
            now = fields.Datetime.now()
            self._fail_once()
            delays.append(round((self.inst.package_next_retry - now).total_seconds() / 60))
        self.assertEqual(delays, [5, 10, 20, 40, 80, 160, 320, 360, 360])

    def test_alerts_once_at_the_third_failure(self):
        self._fault()
        for _ in range(5):
            self._fail_once()
        self.assertEqual(self.alerts.count('package_not_applied'), 1)

    def test_retry_waits_for_a_running_operation(self):
        self._fault()
        self._fail_once()
        self._due()
        self.inst.write({'state': 'provisioning', 'pending_operation': 'restart'})
        self._reconcile()
        self.assertFalse(self._jobs())
        self.inst.write({'state': 'running', 'pending_operation': False})
        self._reconcile()
        self.assertEqual(len(self._jobs()), 1)

    def test_retry_job_is_noop_once_in_sync(self):
        self._fault()
        self._fail_once()
        self._due()
        self._reconcile()
        self._fault(False)
        self.inst._update_container_resources()   # e.g. a later plan change
        calls = self.driver.set_package.call_count
        self._run(self._jobs())
        self.assertEqual(self.driver.set_package.call_count, calls)

    def test_retries_per_run_are_capped(self):
        others = self.Instance.browse()
        for i in range(3):
            others |= self._mk('pkcap%d' % i, self.inst.partner_id,
                               self.inst.saas_product_id, self.inst.domain_id)
        self._fault()
        for inst in self.inst | others:
            self._fail_once(inst)
            self._due(inst)
        with patch.object(type(self.Instance), '_PACKAGE_RETRIES_PER_RUN', 2):
            self.assertEqual(self.Instance._converge_packages(), 2)

    def test_reconcile_cron_queues_retries(self):
        self._fault()
        self._fail_once()
        self._due()
        self.driver.health.return_value = MagicMock(status='running', restart_count=0)
        self.Instance._cron_reconcile()
        self.assertEqual(len(self._jobs()), 1)

    def test_deploy_is_verified_before_in_sync(self):
        self._fault()
        self._fail_once()
        self.inst.write({'state': 'provisioning', 'pending_operation': 'deploy'})
        self.server.tls_cluster_issuer = 'letsencrypt'
        self.driver.create.return_value = 'pktest'
        with patch.object(type(self.inst), '_data_service', lambda rec: MagicMock()), \
                patch.object(type(self.inst), '_hosting_prepare_production_database',
                             lambda rec: None), \
                patch.object(type(self.inst), '_hosting_seed_environment_database',
                             lambda rec: None), \
                patch.object(type(self.inst), '_safe_refresh_usage', lambda rec: None), \
                patch.object(type(self.inst), '_send_notification', lambda rec, t: None):
            self.inst._do_deploy_locked_kubernetes()
        self.assertEqual(self.inst.state, 'running')
        self.assertEqual(self.inst.package_status, 'applying', "healthy is not yet delivered (C6.12)")
        self.inst._verify_package()
        self.assertEqual(self.inst.package_status, 'in_sync')
        self.assertEqual(self.inst.package_applied, self._key())
