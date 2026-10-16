"""C6.4: the instance payload carries the paid-upgrade status in customer
terms only (no cluster, node or pod words)."""
from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.saas_website.controllers.api import SaasApi

INFRA = ('cluster', 'node', 'pod', 'kubernetes', 'operator', 'namespace')


@tagged('post_install', '-at_install')
class TestUpgradeStatusApi(TransactionCase):

    def setUp(self):
        super().setUp()
        env = self.env
        product = env['saas.product'].sudo().search([('is_hosting', '=', True)], limit=1) \
            or env['saas.product'].sudo().create({'name': 'Us Hosting', 'is_hosting': True})
        domain = env['saas.based.domain'].sudo().search([], limit=1) \
            or env['saas.based.domain'].sudo().create({'name': 'us.example.com'})
        self.partner = env['res.partner'].sudo().create({'name': 'Us Cust'})
        self.inst = env['saas.instance'].sudo().create({
            'subdomain': 'ustest', 'domain_id': domain.id, 'partner_id': self.partner.id,
            'saas_product_id': product.id, 'environment': 'production',
            'region_id': False, 'state': 'running'})

    def test_no_upgrade_is_false(self):
        self.assertIs(SaasApi._serialize_upgrade(self.inst), False)

    def test_status_with_utc_times_and_no_infrastructure_words(self):
        invoice = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_line_ids': [(0, 0, {'name': 'up', 'quantity': 1, 'price_unit': 5.0})]})
        invoice.action_post()
        now = fields.Datetime.now()
        invoice.sudo().write({'saas_funds_secured_at': now, 'saas_capture_mode': 'manual'})
        self.inst.write({'upgrade_invoice_id': invoice.id, 'upgrade_authorized_at': now,
                         'upgrade_target_at': now + timedelta(minutes=15),
                         'upgrade_deadline_at': now + timedelta(minutes=120)})
        payload = SaasApi._serialize_upgrade(self.inst)
        self.assertEqual(payload['status'], 'in_progress')
        self.assertTrue(payload['target_at'].endswith('Z'))
        self.assertEqual(payload['completed_at'], '')
        self.assertFalse(payload['charged'])
        text = repr(payload).lower()
        for word in INFRA:
            self.assertNotIn(word, text)
