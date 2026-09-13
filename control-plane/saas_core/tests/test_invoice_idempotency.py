from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestInvoiceIdempotency(TransactionCase):
    """BIZ-009: renewal invoicing is idempotent per cycle — a duplicate/
    overlapping generation for the same period produces no second invoice."""

    def setUp(self):
        super().setUp()
        product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or \
            self.env['saas.product'].sudo().create(
                {'name': 'IDP Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'IDP Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 25.0, 'yearly_price': 240.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [product.id])]})
        domain = self.env['saas.based.domain'].sudo().search([], limit=1) or \
            self.env['saas.based.domain'].sudo().create({'name': 'idp.example.com'})
        self.partner = self.env['res.partner'].sudo().create({'name': 'IDP Cust'})
        self.instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'idpinst', 'domain_id': domain.id, 'partner_id': self.partner.id,
            'saas_product_id': product.id, 'plan_id': self.plan.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running', 'is_hosting': True,
            'next_invoice_date': fields.Date.today() - timedelta(days=1),
            'last_invoice_date': fields.Date.today() - timedelta(days=31)})

    def _invoice_count(self):
        return self.env['account.move'].sudo().search_count([
            ('partner_id', '=', self.partner.id),
            ('move_type', '=', 'out_invoice')])

    def test_double_generation_bills_once(self):
        self.assertEqual(self._invoice_count(), 0)
        self.instance._generate_renewal_invoice()
        after_first = self._invoice_count()
        self.assertEqual(after_first, 1, "first run should bill the due cycle")
        next_after_first = self.instance.next_invoice_date
        self.assertGreater(next_after_first, fields.Date.today(),
                           "cycle must advance past today")
        # Flush so the advanced next_invoice_date is in the DB row — this is
        # the committed state a second, overlapping cron run would lock+read.
        self.env.flush_all()
        # That second generation for the same cycle must not create another
        # invoice (the FOR UPDATE re-read sees the advanced date and returns).
        self.instance._generate_renewal_invoice()
        self.assertEqual(self._invoice_count(), 1,
                         "duplicate generation must not double-bill")
        self.assertEqual(self.instance.next_invoice_date, next_after_first,
                         "cycle must not advance twice")
