from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestAuditLog(TransactionCase):
    """SEC-010: an append-only, tamper-evident internal audit trail."""

    def test_helper_records_actor_action_target(self):
        Log = self.env['saas.audit.log']
        Log._saas_audit('unit_action', model='saas.instance', res_id=42,
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
        Log._saas_audit('immutable_w')
        rec = Log.search([('action', '=', 'immutable_w')], limit=1)
        with self.assertRaises(UserError):
            rec.write({'detail': 'tampered'})

    def test_entries_are_immutable_no_unlink(self):
        Log = self.env['saas.audit.log']
        Log._saas_audit('immutable_u')
        rec = Log.search([('action', '=', 'immutable_u')], limit=1)
        with self.assertRaises(UserError):
            rec.unlink()

    def test_helper_never_raises_into_caller(self):
        # Oversized/odd input must not propagate — auditing can't break the
        # audited operation.
        self.env['saas.audit.log']._saas_audit('big', detail='x' * 10000)
        rec = self.env['saas.audit.log'].search([('action', '=', 'big')], limit=1)
        self.assertTrue(rec)
        self.assertLessEqual(len(rec.detail or ''), 4000, "detail is capped")
