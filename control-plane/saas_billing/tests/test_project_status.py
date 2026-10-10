"""Project status: the subscription state, never a server's runtime."""
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestProjectStatus(TransactionCase):

    def setUp(self):
        super().setUp()
        env = self.env
        product = env['saas.product'].sudo().search([('is_hosting', '=', True)], limit=1) \
            or env['saas.product'].sudo().create({'name': 'PS Hosting', 'is_hosting': True, 'is_published': True})
        plan = env['saas.plan'].sudo().create({
            'name': 'PS Plan', 'is_custom': True, 'workers': 2, 'storage_limit': 20,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 50.0, 'yearly_price': 480.0,
            'currency_id': env.company.currency_id.id, 'saas_product_ids': [(6, 0, [product.id])]})
        self.partner = env['res.partner'].sudo().create({'name': 'PS Cust', 'email': 'ps@example.com'})
        self.inst = env['saas.instance'].sudo().create({
            'subdomain': 'pstest',
            'domain_id': (env['saas.based.domain'].sudo().search([], limit=1)
                          or env['saas.based.domain'].sudo().create({'name': 'ps.example.com'})).id,
            'partner_id': self.partner.id, 'saas_product_id': product.id, 'plan_id': plan.id,
            'billing_period': 'monthly', 'environment': 'production', 'region_id': False,
            'state': 'running'})

    def _invoice(self, days_due):
        inv = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_date_due': fields.Date.today() + timedelta(days=days_due),
            'invoice_line_ids': [(0, 0, {'name': 'x', 'quantity': 1, 'price_unit': 5.0})]})
        inv.action_post()
        inv.invoice_date_due = fields.Date.today() + timedelta(days=days_due)
        return inv

    def _status_with(self, invoices, optional=False):
        cls = type(self.inst)
        with patch.object(cls, '_get_all_invoices', lambda rec: invoices), \
                patch.object(cls, '_is_optional_invoice', lambda rec, inv: optional):
            return self.inst._project_status()

    def test_server_lifecycle_does_not_change_project_status(self):
        for state in ('running', 'stopped', 'failed', 'provisioning'):
            self.inst.state = state
            self.assertEqual(self.inst._project_status(), 'active', state)

    def test_subscription_states(self):
        self.inst.state = 'suspended'
        self.assertEqual(self.inst._project_status(), 'suspended')
        self.inst.write({'state': 'provisioning', 'pending_operation': 'suspend'})
        self.assertEqual(self.inst._project_status(), 'suspended')
        self.inst.write({'state': 'pending_payment', 'pending_operation': False})
        self.assertEqual(self.inst._project_status(), 'unpaid')
        for state in ('cancelled', 'cancelled_by_client'):
            self.inst.state = state
            self.assertEqual(self.inst._project_status(), 'cancelled')

    def test_overdue_mandatory_invoice_is_unpaid(self):
        self.assertEqual(self._status_with(self._invoice(-1)), 'unpaid')

    def test_open_or_optional_invoices_stay_active(self):
        self.assertEqual(self._status_with(self._invoice(5)), 'active', 'not due yet')
        self.assertEqual(self._status_with(self._invoice(-1), optional=True), 'active',
                         'an optional upgrade invoice never blocks the project')

    def test_suspension_wins_over_unpaid(self):
        self.inst.state = 'suspended'
        self.assertEqual(self._status_with(self._invoice(-30)), 'suspended')
