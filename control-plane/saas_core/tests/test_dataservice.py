from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestDataService(TransactionCase):
    """DataService: only ``_wait_until_healthy`` remains (see
    dataservice/service.py) — the ssh_docker-era snapshot()/materialize()
    primitives and the migrate_to_kubernetes()/cutover subsystem were
    removed along with the ssh_docker backend itself. This is now the
    shared health-polling helper used by the Kubernetes deploy/scale
    paths in saas.instance."""

    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or self.env['saas.product'].sudo().create(
            {'name': 'DS Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'DS Plan', 'is_custom': True, 'workers': 2, 'storage_limit': 10,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 50.0, 'yearly_price': 480.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.partner = self.env['res.partner'].sudo().create({'name': 'DS Cust'})
        self.domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create({'name': 'ds.example.com'})

    def _instance(self, sub):
        return self.env['saas.instance'].sudo().create({
            'subdomain': sub, 'domain_id': self.domain.id, 'partner_id': self.partner.id,
            'saas_product_id': self.product.id, 'plan_id': self.plan.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running'})

    # -- _wait_until_healthy must tolerate the post-create/restore rollout
    # window (a single health() check can catch it mid-rollout: 'restarting'
    # /'Provisioning' right after create/restore). ---------------------------
    def test_wait_until_healthy_tolerates_post_restore_rollout(self):
        from unittest.mock import MagicMock
        from odoo.addons.saas_core.drivers.base import HealthStatus
        inst = self._instance('dshealth1')
        ds = inst._data_service()
        driver = MagicMock()
        driver.health.side_effect = [
            HealthStatus(running=False, status='restarting', detail='Provisioning'),
            HealthStatus(running=False, status='restarting', detail='Provisioning'),
            HealthStatus(running=True, status='running', detail='Ready'),
        ]
        with patch('odoo.addons.saas_core.dataservice.service.time.sleep'):
            ds._wait_until_healthy(driver, 'handle', timeout=5)  # must not raise
        self.assertEqual(driver.health.call_count, 3)

    def test_wait_until_healthy_raises_on_timeout(self):
        from unittest.mock import MagicMock
        from odoo.addons.saas_core.drivers.base import HealthStatus
        inst = self._instance('dshealth2')
        ds = inst._data_service()
        driver = MagicMock()
        driver.health.return_value = HealthStatus(
            running=False, status='restarting', detail='Provisioning')
        with patch('odoo.addons.saas_core.dataservice.service.time.sleep'):
            with self.assertRaises(RuntimeError):
                ds._wait_until_healthy(driver, 'handle', timeout=-1)
