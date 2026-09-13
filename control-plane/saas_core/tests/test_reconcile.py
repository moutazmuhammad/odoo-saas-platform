from unittest.mock import patch, MagicMock

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestReconcile(TransactionCase):
    """Phase 3: the reconciler drives a container toward desired_state."""

    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or self.env['saas.product'].sudo().create(
            {'name': 'R Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'R Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.partner = self.env['res.partner'].sudo().create({'name': 'R Cust'})
        self.domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create({'name': 'r.example.com'})
        self.server = self.env['saas.server'].sudo().create({'name': 'r-srv'})

    def _inst(self, sub, state='running'):
        return self.env['saas.instance'].sudo().create({
            'subdomain': sub, 'domain_id': self.domain.id, 'partner_id': self.partner.id,
            'saas_product_id': self.product.id, 'plan_id': self.plan.id,
            'docker_server_id': self.server.id, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': False, 'state': state})

    def _health(self, status, restart_count=0):
        from odoo.addons.saas_core.drivers.base import HealthStatus
        return HealthStatus(running=(status == 'running'), status=status,
                            restart_count=restart_count, detail=status)

    def _reconcile_with_health(self, inst, status, restart_count=0):
        fake = MagicMock()
        fake.health.return_value = self._health(status, restart_count)
        with patch.object(type(inst), '_compute_driver', return_value=fake), \
             patch.object(type(inst), '_compute_handle', return_value='H'):
            action = inst.reconcile()
        return fake, action

    def test_desired_state_mapping(self):
        self.assertEqual(self._inst('rds1', 'running').desired_state, 'running')
        self.assertEqual(self._inst('rds2', 'stopped').desired_state, 'stopped')
        self.assertEqual(self._inst('rds3', 'suspended').desired_state, 'stopped')
        self.assertEqual(self._inst('rds4', 'draft').desired_state, 'ignore')

    def test_reconcile_restarts_a_down_container(self):
        inst = self._inst('rdown', 'running')
        fake, action = self._reconcile_with_health(inst, 'exited')
        fake.start.assert_called_once_with('H')
        self.assertEqual(action, 'started')
        self.assertEqual(inst.actual_state, 'exited')
        self.assertTrue(inst.last_reconcile)

    def test_reconcile_recreates_missing_container(self):
        inst = self._inst('rgone', 'running')
        fake, action = self._reconcile_with_health(inst, 'not_found')
        # not_found → `compose up -d` (driver.start) recreates from compose;
        # the action is reported as 'recreated' (escalates to redeploy only if
        # compose-up fails — see test_critical_fixes.test_b5_*).
        fake.start.assert_called_once_with('H')
        self.assertEqual(action, 'recreated')

    def test_reconcile_noop_when_already_running(self):
        inst = self._inst('rok', 'running')
        fake, action = self._reconcile_with_health(inst, 'running')
        fake.start.assert_not_called()
        fake.stop.assert_not_called()
        self.assertEqual(action, 'none')

    def test_reconcile_breaks_crash_loop(self):
        inst = self._inst('rloop', 'running')
        fake, action = self._reconcile_with_health(inst, 'exited', restart_count=7)
        fake.stop.assert_called_once_with('H')
        self.assertEqual(action, 'stopped_crashloop')
        self.assertEqual(inst.state, 'stopped')
        self.assertTrue(inst.last_error)

    def test_reconcile_stops_container_that_should_be_stopped(self):
        inst = self._inst('rstop', 'stopped')   # desired = stopped
        fake, action = self._reconcile_with_health(inst, 'running')
        fake.stop.assert_called_once_with('H')
        self.assertEqual(action, 'stopped')

    def test_reconcile_skips_when_operation_in_progress(self):
        inst = self._inst('rbusy', 'running')
        inst.pending_operation = 'deploy'
        with patch.object(type(inst), '_compute_driver') as drv:
            action = inst.reconcile()
        drv.assert_not_called()
        self.assertEqual(action, 'skipped')

    # -------- Phase 5.1: multi-container model seam --------
    def test_workloads_default_single_app_container(self):
        inst = self._inst('rwl1', 'running')
        self.assertEqual(inst._workloads(), [('app', 'odoo_rwl1')])
        self.assertEqual(len(inst._compute_handles()), 1)

    def test_workloads_explicit_scale_out_set(self):
        inst = self._inst('rwl2', 'running')
        self.env['saas.instance.container'].sudo().create([
            {'instance_id': inst.id, 'role': 'app', 'name': 'odoo_rwl2_app1'},
            {'instance_id': inst.id, 'role': 'app', 'name': 'odoo_rwl2_app2'},
            {'instance_id': inst.id, 'role': 'cron', 'name': 'odoo_rwl2_cron'},
        ])
        roles = [r for r, _n in inst._workloads()]
        self.assertEqual(roles, ['app', 'app', 'cron'])
        self.assertEqual(len(inst._compute_handles()), 3)
        self.assertEqual({h.container_name for h in inst._compute_handles()},
                         {'odoo_rwl2_app1', 'odoo_rwl2_app2', 'odoo_rwl2_cron'})
