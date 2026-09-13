from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestProrationUnify(TransactionCase):
    """BILL-V2-001: proration credits the FULL remaining cycle (no undocumented
    "-2"); the portal display now calls this same backend helper so the quote
    matches the invoice."""

    def setUp(self):
        super().setUp()
        product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or \
            self.env['saas.product'].sudo().create(
                {'name': 'PRO Hosting', 'is_hosting': True, 'is_published': True})
        plan = self.env['saas.plan'].sudo().create({
            'name': 'PRO Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 30.0, 'yearly_price': 288.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [product.id])]})
        domain = self.env['saas.based.domain'].sudo().search([], limit=1) or \
            self.env['saas.based.domain'].sudo().create({'name': 'pro.example.com'})
        partner = self.env['res.partner'].sudo().create({'name': 'PRO Cust'})
        self.instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'proinst', 'domain_id': domain.id, 'partner_id': partner.id,
            'saas_product_id': product.id, 'plan_id': plan.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running', 'is_hosting': True})

    def test_credits_full_remaining_days_no_minus_two(self):
        today = fields.Date.today()
        self.instance.write({
            'last_invoice_date': today - timedelta(days=10),
            'next_invoice_date': today + timedelta(days=20),
        })
        value, remaining_days, total_days = self.instance._proration_credit(30.0)
        self.assertEqual(total_days, 30)
        # The whole point of BILL-V2-001: the full 20 remaining days, not 18.
        self.assertEqual(remaining_days, 20,
                         "must credit the full remaining cycle (no -2)")
        self.assertAlmostEqual(value, 20.0, places=2)

    def test_missing_dates_are_safe(self):
        self.instance.write({'last_invoice_date': False, 'next_invoice_date': False})
        self.assertEqual(self.instance._proration_credit(30.0), (0.0, 0, 0))
