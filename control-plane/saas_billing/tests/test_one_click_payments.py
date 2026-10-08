"""Saved-card capture and one-click purchases."""
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestOneClickPayments(TransactionCase):

    def setUp(self):
        super().setUp()
        env = self.env
        self.icp = env['ir.config_parameter'].sudo()
        self.icp.set_param('saas_master.daily_backup_pct', '20')
        product = env['saas.product'].sudo().search([('is_hosting', '=', True)], limit=1) \
            or env['saas.product'].sudo().create({'name': 'OC Hosting', 'is_hosting': True, 'is_published': True})
        plan = env['saas.plan'].sudo().create({
            'name': 'OC Plan', 'is_custom': True, 'workers': 2, 'storage_limit': 20,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 50.0, 'yearly_price': 480.0,
            'currency_id': env.company.currency_id.id, 'saas_product_ids': [(6, 0, [product.id])]})
        self.partner = env['res.partner'].sudo().create({'name': 'OC Cust', 'email': 'oc@example.com'})
        self.inst = env['saas.instance'].sudo().create({
            'subdomain': 'octest',
            'domain_id': (env['saas.based.domain'].sudo().search([], limit=1)
                          or env['saas.based.domain'].sudo().create({'name': 'oc.example.com'})).id,
            'partner_id': self.partner.id, 'saas_product_id': product.id, 'plan_id': plan.id,
            'billing_period': 'monthly', 'environment': 'production', 'region_id': False,
            'state': 'running'})

    def test_no_saved_card_means_no_charge_attempt(self):
        inv = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_line_ids': [(0, 0, {'name': 'x', 'quantity': 1, 'price_unit': 5.0})]})
        inv.action_post()
        self.assertFalse(self.inst._try_pay_with_saved_method(inv))

    def test_daily_backup_purchase_charges_the_saved_card_first(self):
        charged = []
        with patch.object(type(self.inst), '_auto_renew_method', lambda rec: object()), \
                patch.object(type(self.inst), '_try_auto_charge_invoice',
                             lambda rec, invoice, kind: charged.append((invoice.id, kind)) or True):
            inv = self.inst.action_purchase_daily_backup()
        self.assertEqual(charged, [(inv.id, 'subscription')])
        self.assertEqual(self.inst.daily_backup_pending_invoice_id, inv)
        self.assertAlmostEqual(inv.amount_total, 10.0, 2, "20% of a $50 plan, full month: no cycle yet")

    def test_storage_block_purchase_charges_the_saved_card_first(self):
        self.icp.set_param('saas_master.storage_block_gb', '10')
        self.icp.set_param('saas_master.storage_block_price', '2.0')
        charged = []
        with patch.object(type(self.inst), '_auto_renew_method', lambda rec: object()), \
                patch.object(type(self.inst), '_try_auto_charge_invoice',
                             lambda rec, invoice, kind: charged.append(invoice.id) or True):
            inv = self.inst.action_purchase_storage_block(1)
        self.assertEqual(charged, [inv.id])
