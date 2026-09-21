from unittest.mock import patch, MagicMock

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestComputeDriver(TransactionCase):
    """ComputeDriver seam: god-model call-site routing (mocked driver).

    The ssh_docker driver implementation itself (and its dedicated
    command-building tests) was removed along with the ssh_docker
    backend — Kubernetes is now the only ``ComputeDriver`` implementation
    (see ``kubernetes_driver.py`` / ``test_kubernetes_driver.py``). What
    remains here is backend-agnostic: it only asserts that ``_do_stop``/
    ``_do_restart`` call through ``self._compute_driver()``, whatever
    driver that resolves to.
    """

    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or self.env['saas.product'].sudo().create(
            {'name': 'CD Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'CD Plan', 'is_custom': True, 'workers': 2, 'storage_limit': 10,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 50.0, 'yearly_price': 480.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.partner = self.env['res.partner'].sudo().create({'name': 'CD Cust'})
        self.domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create({'name': 'cd.example.com'})

    def _running_instance(self, sub):
        return self.env['saas.instance'].sudo().create({
            'subdomain': sub, 'domain_id': self.domain.id, 'partner_id': self.partner.id,
            'saas_product_id': self.product.id, 'plan_id': self.plan.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running'})

    # -------- god-model: _do_stop/_do_restart route to the driver --------
    def test_do_stop_routes_to_driver(self):
        inst = self._running_instance('cdstop')
        fake = MagicMock()
        with patch.object(type(inst), '_compute_driver', return_value=fake), \
             patch.object(type(inst), '_compute_handle', return_value='HANDLE'):
            inst._do_stop()
        fake.stop.assert_called_once_with('HANDLE')
        self.assertEqual(inst.state, 'stopped')
        self.assertFalse(inst.pending_operation)

    def test_do_restart_routes_to_driver(self):
        inst = self._running_instance('cdrestart')
        fake = MagicMock()
        with patch.object(type(inst), '_compute_driver', return_value=fake), \
             patch.object(type(inst), '_compute_handle', return_value='HANDLE'):
            inst._do_restart()
        fake.restart.assert_called_once_with('HANDLE')
        self.assertEqual(inst.state, 'running')

    def test_do_stop_wraps_driver_error_as_usererror(self):
        inst = self._running_instance('cderr')
        fake = MagicMock()
        fake.stop.side_effect = RuntimeError('boom')
        with patch.object(type(inst), '_compute_driver', return_value=fake), \
             patch.object(type(inst), '_compute_handle', return_value='H'):
            with self.assertRaises(UserError):
                inst._do_stop()
