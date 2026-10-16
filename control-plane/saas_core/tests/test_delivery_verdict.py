"""C6.11: the delivery verdict. An instance counts as delivered only when its
pods passed the self-check (storage, database, application) for the current
spec and its public URL answers over valid TLS."""
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestDeliveryVerdict(TransactionCase):

    def setUp(self):
        super().setUp()
        env = self.env
        product = env['saas.product'].sudo().search([('is_hosting', '=', True)], limit=1) \
            or env['saas.product'].sudo().create(
                {'name': 'DV Hosting', 'is_hosting': True, 'is_published': True})
        domain = env['saas.based.domain'].sudo().search([], limit=1) \
            or env['saas.based.domain'].sudo().create({'name': 'dv.example.com'})
        self.inst = env['saas.instance'].sudo().create({
            'subdomain': 'dvtest', 'domain_id': domain.id,
            'partner_id': env['res.partner'].sudo().create({'name': 'DV Cust'}).id,
            'saas_product_id': product.id, 'environment': 'production',
            'region_id': False, 'state': 'running'})
        self.driver = MagicMock()
        self.driver.delivery_status.return_value = {'ready': True, 'failure': None, 'detail': ''}
        self.driver.check_public_url.return_value = (True, '')
        for attr, value in (('_compute_driver', lambda rec, connection=None: self.driver),
                            ('_compute_handle', lambda rec: rec.subdomain)):
            p = patch.object(type(self.inst), attr, value)
            p.start()
            self.addCleanup(p.stop)

    def test_delivered(self):
        self.assertEqual(self.inst._delivery_verdict(), ('delivered', None, ''))
        self.driver.check_public_url.assert_called_once_with(self.inst.name)

    def test_self_check_failure_blocks(self):
        for reason in ('storage', 'database', 'app'):
            self.driver.delivery_status.return_value = {
                'ready': False, 'failure': (reason, 'boom'), 'detail': '%s: boom' % reason}
            self.assertEqual(self.inst._delivery_verdict(), ('blocked', reason, 'boom'))
        self.driver.check_public_url.assert_not_called()

    def test_still_rolling_is_pending(self):
        self.driver.delivery_status.return_value = {
            'ready': False, 'failure': None, 'detail': 'Odoo pods not all ready'}
        self.assertEqual(self.inst._delivery_verdict(),
                         ('pending', None, 'Odoo pods not all ready'))
        self.driver.check_public_url.assert_not_called()

    def test_public_url_failure_blocks_as_route(self):
        self.driver.check_public_url.return_value = (False, 'certificate verify failed')
        self.assertEqual(self.inst._delivery_verdict(),
                         ('blocked', 'route', 'certificate verify failed'))

    def test_verdict_never_acts_on_the_workload(self):
        self.inst._delivery_verdict()
        called = {c[0].split('.')[0] for c in self.driver.method_calls}
        self.assertEqual(called, {'delivery_status', 'check_public_url'})
