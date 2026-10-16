"""C6.3: an upgrade counts as applied only once the cluster verifiably runs
it. An accepted patch is 'applying'; a stuck rollout is 'blocked' with its
reason and keeps being polled; only a verified rollout writes
package_applied, sets in_sync and tells the customer it is complete."""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPackageVerification(TransactionCase):

    def setUp(self):
        super().setUp()
        env = self.env
        self.Job = env['saas.job'].sudo()
        self.Instance = env['saas.instance'].sudo()
        product = env['saas.product'].sudo().search([('is_hosting', '=', True)], limit=1) \
            or env['saas.product'].sudo().create(
                {'name': 'Pv Hosting', 'is_hosting': True, 'is_published': True})
        self.small, self.big = (env['saas.plan'].sudo().create({
            'name': name, 'is_custom': True, 'workers': workers, 'storage_limit': 10,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [product.id])]})
            for name, workers in (('Pv Small', 1), ('Pv Big', 3)))
        domain = env['saas.based.domain'].sudo().search([], limit=1) \
            or env['saas.based.domain'].sudo().create({'name': 'pv.example.com'})
        server = env['saas.server'].sudo().create({'name': 'pv-srv', 'compute_driver': 'kubernetes'})
        version = env['saas.odoo.version'].sudo().search(
            [('docker_image', '!=', False), ('docker_image_tag', '!=', False)], limit=1) \
            or env['saas.odoo.version'].sudo().create(
                {'name': 'Pv 18', 'docker_image': 'odoo', 'docker_image_tag': '18.0'})
        self.inst = self.Instance.create({
            'subdomain': 'pvtest', 'domain_id': domain.id,
            'partner_id': env['res.partner'].sudo().create({'name': 'Pv Cust'}).id,
            'saas_product_id': product.id, 'plan_id': self.small.id,
            'docker_server_id': server.id, 'odoo_version_id': version.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running'})
        self.driver = MagicMock()
        self.driver.package_rollout.return_value = ('applied', None, '')
        self.driver.delivery_status.return_value = {'ready': True, 'failure': None, 'detail': ''}
        self.driver.check_public_url.return_value = (True, '')
        self.alerts = []
        for target, attr, value in (
                (type(self.inst), '_compute_driver', lambda rec, connection=None: self.driver),
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
        # In sync on the small plan; its (now no-op) verify job is dropped.
        self.inst._update_container_resources()
        self.inst._verify_package()
        self._verify_jobs().unlink()

    def _key(self):
        return self.inst._package_key(self.inst._k8s_plan_resources())

    def _verify_jobs(self):
        return self.Job.search([('model', '=', 'saas.instance'), ('res_id', '=', self.inst.id),
                                ('method', '=', '_do_verify_package')], order='id')

    def _run(self, job):
        job.run_after = fields.Datetime.now() - timedelta(seconds=1)  # due now
        self.env.flush_all()
        self.Job._run_one(job.id)
        self.env.invalidate_all()

    def _upgrade(self):
        self.inst.plan_id = self.big
        self.inst._update_container_resources()

    def _messages(self):
        return ' '.join(self.inst.message_ids.mapped('body'))

    def test_accepted_patch_is_applying_not_applied(self):
        before = self.inst.package_applied
        self._upgrade()
        self.assertEqual(self.inst.package_status, 'applying')
        self.assertEqual(self.inst.package_desired, self._key())
        self.assertEqual(self.inst.package_applied, before, "nothing counts before verification")
        self.assertTrue(self.inst.package_pending_since)
        job = self._verify_jobs()
        self.assertEqual(len(job), 1, "a verify job follows the patch")
        self.assertEqual(job.lock_key, 'instance:%s' % self.inst.id)
        self.assertGreater(job.run_after, fields.Datetime.now() + timedelta(seconds=20))
        self.assertNotIn('Upgrade complete', self._messages())

    def test_verified_rollout_is_in_sync_and_tells_the_customer(self):
        self._upgrade()
        self._run(self._verify_jobs())
        self.assertEqual(self.inst.package_status, 'in_sync')
        self.assertEqual(self.inst.package_applied, self._key())
        self.assertFalse(self.inst.package_pending_since)
        self.assertIn('Upgrade complete', self._messages())
        self.assertEqual(len(self._verify_jobs()), 1, "no more polling once verified")

    def test_unschedulable_is_blocked_then_converges_without_a_manual_step(self):
        """Acceptance: a patch accepted but left Pending stays blocked and is
        never in_sync; once room exists it becomes in_sync on its own."""
        self._upgrade()
        self.driver.package_rollout.return_value = (
            'blocked', 'unschedulable', '0/1 nodes are available: Insufficient memory')
        self._run(self._verify_jobs())
        self.assertEqual(self.inst.package_status, 'blocked')
        self.assertEqual(self.inst.package_block_reason, 'unschedulable')
        self.assertIn('Insufficient memory', self.inst.package_error)
        self.assertEqual(self.alerts.count('package_blocked'), 1)
        jobs = self._verify_jobs()
        self.assertEqual(len(jobs), 2, "a blocked rollout keeps being polled")
        self._run(jobs[-1])                       # still blocked: no second alert
        self.assertEqual(self.alerts.count('package_blocked'), 1)
        self.assertNotIn('Upgrade complete', self._messages())
        self.driver.package_rollout.return_value = ('applied', None, '')   # room freed
        self._run(self._verify_jobs()[-1])
        self.assertEqual(self.inst.package_status, 'in_sync')
        self.assertFalse(self.inst.package_block_reason)
        self.assertIn('Upgrade complete', self._messages())

    def test_polling_backs_off_and_caps(self):
        self._upgrade()
        self.driver.package_rollout.return_value = ('rolling', None, 'Odoo is rolling out')
        delays = []
        for _ in range(6):
            job = self._verify_jobs()[-1]
            now = fields.Datetime.now()
            delays.append(round((job.run_after - now).total_seconds()))
            self._run(job)
        self.assertEqual(self.inst.package_status, 'applying')
        self.assertEqual([round(d, -1) for d in delays], [30, 60, 120, 240, 300, 300])

    def test_delivery_verdict_gates_applied(self):
        self._upgrade()
        self.driver.delivery_status.return_value = {
            'ready': False, 'failure': ('storage', 'cannot write'), 'detail': ''}
        self.inst._verify_package()
        self.assertEqual((self.inst.package_status, self.inst.package_block_reason),
                         ('blocked', 'storage'))
        self.driver.delivery_status.return_value = {'ready': False, 'failure': None,
                                                    'detail': 'Odoo pods not all ready'}
        self.inst._verify_package()
        self.assertEqual(self.inst.package_status, 'applying')
        self.driver.delivery_status.return_value = {'ready': True, 'failure': None, 'detail': ''}
        self.driver.check_public_url.return_value = (False, 'certificate verify failed')
        self.inst._verify_package()
        self.assertEqual(self.inst.package_block_reason, 'route')

    def test_stopped_tenant_needs_no_running_pods(self):
        self.inst.state = 'stopped'
        self._upgrade()
        self.driver.delivery_status.side_effect = AssertionError('no delivery check when stopped')
        self.inst._verify_package()
        self.assertEqual(self.inst.package_status, 'in_sync')

    def test_package_changed_during_verification_is_applied_first(self):
        self._upgrade()
        calls = self.driver.set_package.call_count
        self.inst.plan_id = self.small            # e.g. a scheduled downgrade lands
        self.inst._verify_package()
        self.assertEqual(self.driver.set_package.call_count, calls + 1)
        self.assertEqual(self.inst.package_desired, self._key())
        self.assertEqual(len(self._verify_jobs()), 1, "the existing chain keeps going")

    def test_reconcile_requeues_a_lost_chain_once(self):
        self._upgrade()
        self._verify_jobs().write({'state': 'failed'})       # the chain was lost
        self.Instance._converge_packages()
        self.Instance._converge_packages()
        pending = self._verify_jobs().filtered(lambda j: j.state == 'pending')
        self.assertEqual(len(pending), 1)

    def test_verification_never_acts_on_the_workload(self):
        self._upgrade()
        self.driver.reset_mock()
        self.inst._verify_package()
        used = {c[0].split('.')[0] for c in self.driver.method_calls}
        self.assertEqual(used, {'package_rollout', 'delivery_status', 'check_public_url'})

    def test_same_package_again_stays_in_sync(self):
        self.inst._update_container_resources()
        self.assertEqual(self.inst.package_status, 'in_sync')
        self.assertFalse(self._verify_jobs(), "nothing changed, nothing to verify")
