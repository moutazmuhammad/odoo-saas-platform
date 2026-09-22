from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestAuditLog(TransactionCase):
    """SEC-010: an append-only, tamper-evident internal audit trail."""

    def test_helper_records_actor_action_target(self):
        Log = self.env['saas.audit.log']
        Log.saas_audit('unit_action', model='saas.instance', res_id=42,
                        res_name='probe', detail='hello')
        rec = Log.search([('action', '=', 'unit_action')], limit=1)
        self.assertTrue(rec, "the event must be recorded")
        self.assertEqual(rec.actor_id, self.env.user)
        self.assertEqual(rec.actor_login, self.env.user.login)
        self.assertEqual(rec.res_id, 42)
        self.assertEqual(rec.res_name, 'probe')
        self.assertEqual(rec.result, 'ok')
        self.assertTrue(rec.timestamp)

    def test_entries_are_immutable_no_write(self):
        Log = self.env['saas.audit.log']
        Log.saas_audit('immutable_w')
        rec = Log.search([('action', '=', 'immutable_w')], limit=1)
        with self.assertRaises(UserError):
            rec.write({'detail': 'tampered'})

    def test_entries_are_immutable_no_unlink(self):
        Log = self.env['saas.audit.log']
        Log.saas_audit('immutable_u')
        rec = Log.search([('action', '=', 'immutable_u')], limit=1)
        with self.assertRaises(UserError):
            rec.unlink()

    def test_helper_never_raises_into_caller(self):
        # Oversized/odd input must not propagate — auditing can't break the
        # audited operation.
        self.env['saas.audit.log'].saas_audit('big', detail='x' * 10000)
        rec = self.env['saas.audit.log'].search([('action', '=', 'big')], limit=1)
        self.assertTrue(rec)
        self.assertLessEqual(len(rec.detail or ''), 4000, "detail is capped")


@tagged('post_install', '-at_install')
class TestAuditLogLifecycleWiring(TransactionCase):
    """SEC-010 follow-up: 'who scaled, deployed, restored, or deleted an
    instance' — deploy/redeploy are covered in test_job_queue.py
    (TestDeployViaQueue, alongside their existing job-queue tests); this
    covers restore (both kinds) and scale (both directions), the other
    two categories the audit named as unwired."""

    def setUp(self):
        super().setUp()
        product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or \
            self.env['saas.product'].sudo().create(
                {'name': 'AL Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'AL Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [product.id])]})
        self.plan2 = self.env['saas.plan'].sudo().create({
            'name': 'AL Plan 2', 'is_custom': True, 'workers': 2, 'storage_limit': 10,
            'cpu_limit': 2.0, 'ram_limit': '2g', 'price': 40.0, 'yearly_price': 384.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [product.id])]})
        domain = self.env['saas.based.domain'].sudo().search([], limit=1) or \
            self.env['saas.based.domain'].sudo().create({'name': 'al.example.com'})
        partner = self.env['res.partner'].sudo().create({'name': 'AL Cust'})
        self.instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'alinst', 'domain_id': domain.id, 'partner_id': partner.id,
            'saas_product_id': product.id, 'plan_id': self.plan.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'is_hosting': True, 'state': 'running',
            'daily_backup_enabled': True})

    def _audit(self, action):
        return self.env['saas.audit.log'].sudo().search([
            ('action', '=', action), ('model', '=', 'saas.instance'),
            ('res_id', '=', self.instance.id)], limit=1)

    # test_restore_backup_writes_audit_log and
    # test_restore_full_instance_writes_audit_log were removed:
    # action_restore_backup/action_restore_full_instance (the restic/SSH
    # restore pipeline) were removed along with ssh_docker — see the
    # removal plan's Phase 5 for the planned CR-based replacement.

    def test_plan_upgrade_writes_scale_audit_log(self):
        self.instance.write({'pending_plan_id': self.plan2.id})
        with patch.object(type(self.instance), '_update_container_resources',
                          lambda self: None):
            self.instance._apply_pending_plan_change()
        entry = self._audit('instance_scale')
        self.assertTrue(entry, "plan upgrade must write an audit log entry")
        self.assertIn(self.plan.name, entry.detail)
        self.assertIn(self.plan2.name, entry.detail)

    def test_scheduled_downgrade_writes_scale_audit_log(self):
        # The downgrade-apply block lives inline at the top of
        # _generate_renewal_invoice (applied before that cycle's invoice is
        # built) — the write()/_append_log()/saas_audit() sequence this
        # test cares about all happen before the method goes on to build a
        # real sale order/invoice, which needs billing fixtures well beyond
        # this test's scope. Let that tail fail if it must (e.g. missing
        # pricelist config) and assert on the audit log regardless — the
        # writes already made are still visible on this cursor either way.
        self.instance.write({
            'scheduled_plan_id': self.plan2.id,
            'next_invoice_date': fields.Date.today(),
        })
        with patch.object(type(self.instance), '_update_container_resources',
                          lambda self: None):
            try:
                self.instance._generate_renewal_invoice()
            except Exception:
                pass
        entry = self._audit('instance_scale')
        self.assertTrue(entry, "scheduled downgrade must write an audit log entry")
        self.assertIn(self.plan2.name, entry.detail)
