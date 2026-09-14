from datetime import date, timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBillingLifecycleCrons(TransactionCase):
    """B.3 group 1: the revenue-critical billing crons on saas.instance —
    dunning/suspension, auto-renew retry, renewal reminders, the
    paid-pending-change safety net, and trial expiry. None of these were
    referenced by name anywhere in the test suite before this file."""

    def setUp(self):
        super().setUp()
        self.Instance = self.env['saas.instance']
        self.partner = self.env['res.partner'].sudo().create(
            {'name': 'Cron Co', 'customer_rank': 1})
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or \
            self.env['saas.product'].sudo().create(
                {'name': 'TEST Cron Hosting', 'is_hosting': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'Cron Plan', 'is_custom': True, 'workers': 2,
            'storage_limit': 10, 'cpu_limit': 1.0, 'ram_limit': '1g',
            'price': 30.0, 'yearly_price': 288.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])],
        })
        self.domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create(
                {'name': 'cron.example.com'})
        # _retry_failed_payments bails out early unless the instance/partner
        # has a saved, active payment method — give the partner one so the
        # retry-schedule tests actually reach the charge-attempt call.
        provider = self.env['payment.provider'].sudo().create(
            {'name': 'Cron Test Provider', 'state': 'test'})
        token = self.env['payment.token'].sudo().create({
            'provider_id': provider.id,
            'payment_method_id': self.env.ref('payment.payment_method_unknown').id,
            'partner_id': self.partner.id,
            'provider_ref': 'tok-cron-%s' % self.partner.id,
        })
        self.env['saas.payment.method'].sudo().create({
            'partner_id': self.partner.id, 'provider_id': provider.id,
            'token_id': token.id, 'is_default': True,
        })
        # The cron loops commit/rollback per instance; the test cursor
        # forbids both (AssertionError) since a real commit would break
        # the enclosing test transaction. Same neutralisation as
        # test_job_queue.py's _no_commit.
        for op in ('commit', 'rollback'):
            p = patch.object(self.env.cr, op)
            p.start()
            self.addCleanup(p.stop)

    def _inst(self, sub, **kw):
        vals = {
            'subdomain': sub, 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'billing_period': 'monthly',
            'state': 'running',
        }
        vals.update(kw)
        return self.env['saas.instance'].sudo().create(vals)

    def _move(self, **kw):
        # account.move.create() forbids state='posted' directly (must be
        # created draft, then posted) — so split state/payment_state out
        # of the create vals and force them via write() afterwards. This
        # bypasses the real posting workflow (no journal/product plumbing
        # needed) since only the dunning/retry filters' own field reads
        # matter for these cron tests, not real invoice numbering.
        state = kw.pop('state', 'posted')
        payment_state = kw.pop('payment_state', 'not_paid')
        vals = {
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            # payment_state is computed from amount_residual — a
            # line-less (amount_total=0) move recomputes to 'paid'
            # regardless of what we write, silently invalidating the
            # dunning/retry filters. A non-zero line keeps it settable.
            'invoice_line_ids': [(0, 0, {
                'name': 'Cron test line', 'quantity': 1, 'price_unit': 100.0,
            })],
        }
        vals.update(kw)
        move = self.env['account.move'].sudo().create(vals)
        move.sudo().write({'state': state, 'payment_state': payment_state})
        return move

    # ---------------- _cron_check_overdue_invoices / _check_dunning ----------------

    def test_dunning_suspends_stopped_instance_past_grace(self):
        inst = self._inst('dun1', state='stopped')
        overdue = self._move(invoice_date_due=date.today() - timedelta(days=10))
        with patch.object(type(inst), '_get_all_invoices', lambda self: overdue), \
                patch.object(type(inst), '_is_optional_invoice', lambda self, m: False):
            inst._check_dunning(date.today())
        self.assertEqual(inst.state, 'suspended')

    def test_dunning_skips_optional_only_overdue_invoice(self):
        inst = self._inst('dun2', state='stopped')
        overdue = self._move(invoice_date_due=date.today() - timedelta(days=10))
        with patch.object(type(inst), '_get_all_invoices', lambda self: overdue), \
                patch.object(type(inst), '_is_optional_invoice', lambda self, m: True):
            inst._check_dunning(date.today())
        self.assertEqual(inst.state, 'stopped', "optional-only overdue must not suspend")

    def test_dunning_noop_when_nothing_overdue(self):
        inst = self._inst('dun3', state='stopped')
        with patch.object(type(inst), '_get_all_invoices',
                          lambda self: self.env['account.move']):
            inst._check_dunning(date.today())
        self.assertEqual(inst.state, 'stopped')

    def test_dunning_within_grace_sends_warning_once(self):
        inst = self._inst('dun4', state='stopped')
        overdue = self._move(invoice_date_due=date.today() - timedelta(days=1))
        with patch.object(type(inst), '_get_all_invoices', lambda self: overdue), \
                patch.object(type(inst), '_is_optional_invoice', lambda self, m: False):
            inst._check_dunning(date.today())
        self.assertEqual(inst.state, 'stopped', "within grace period: no suspension yet")
        self.assertTrue(inst.suspension_warning_sent)

    def test_cron_overdue_invoices_continues_past_one_instance_failure(self):
        # The cron's own search requires sale_order_id != False.
        so = self.env['sale.order'].sudo().create({'partner_id': self.partner.id})
        bad = self._inst('dunbad', state='stopped', sale_order_id=so.id)
        good = self._inst('dungood', state='stopped', sale_order_id=so.id)
        calls = []

        def fake_dunning(rec, today):
            calls.append(rec.subdomain)
            if rec.id == bad.id:
                raise ValueError("boom")
            rec.state = 'suspended'

        with patch.object(type(bad), '_check_dunning', fake_dunning):
            self.Instance.sudo().search([
                ('id', 'in', [bad.id, good.id]),
            ])  # sanity: both are visible to the cron's own search
            self.Instance._cron_check_overdue_invoices()
        self.assertIn('dunbad', calls)
        self.assertIn('dungood', calls)
        good.invalidate_recordset()
        bad.invalidate_recordset()
        self.assertEqual(good.state, 'suspended')
        self.assertEqual(bad.state, 'stopped', "the failing instance's own "
                          "transaction must have rolled back, not just logged")

    # ---------------- _cron_retry_failed_payments ----------------

    def test_retry_charges_invoice_on_schedule_day(self):
        inst = self._inst('retry1', auto_renew_subscription=True)
        due = date.today() - timedelta(days=3)
        inv = self._move(invoice_date_due=due)
        calls = []
        with patch.object(type(inst), '_get_all_invoices', lambda self: inv), \
                patch.object(type(inst), '_is_optional_invoice', lambda self, m: False), \
                patch.object(type(inst), '_try_auto_charge_invoice',
                             lambda self, invoice, kind: calls.append((invoice.id, kind))):
            inst._retry_failed_payments(date.today())
        self.assertEqual(calls, [(inv.id, 'retry')])

    def test_retry_skips_when_age_not_on_schedule(self):
        inst = self._inst('retry2', auto_renew_subscription=True)
        due = date.today() - timedelta(days=2)  # not in (1, 3, 5)
        inv = self._move(invoice_date_due=due)
        calls = []
        with patch.object(type(inst), '_get_all_invoices', lambda self: inv), \
                patch.object(type(inst), '_is_optional_invoice', lambda self, m: False), \
                patch.object(type(inst), '_try_auto_charge_invoice',
                             lambda self, invoice, kind: calls.append(invoice.id)):
            inst._retry_failed_payments(date.today())
        self.assertEqual(calls, [])

    def test_retry_skips_when_already_attempted_today(self):
        inst = self._inst('retry3', auto_renew_subscription=True)
        due = date.today() - timedelta(days=1)
        inv = self._move(invoice_date_due=due)
        self.env['saas.payment.attempt'].sudo().create({
            'move_id': inv.id, 'instance_id': inst.id, 'attempt_no': 1,
            'attempted_on': date.today(), 'state': 'failed',
        })
        calls = []
        with patch.object(type(inst), '_get_all_invoices', lambda self: inv), \
                patch.object(type(inst), '_is_optional_invoice', lambda self, m: False), \
                patch.object(type(inst), '_try_auto_charge_invoice',
                             lambda self, invoice, kind: calls.append(invoice.id)):
            inst._retry_failed_payments(date.today())
        self.assertEqual(calls, [])

    def test_retry_noop_without_auto_renew(self):
        inst = self._inst('retry4', auto_renew_subscription=False)
        inv = self._move(invoice_date_due=date.today() - timedelta(days=1))
        calls = []
        with patch.object(type(inst), '_get_all_invoices', lambda self: inv), \
                patch.object(type(inst), '_is_optional_invoice', lambda self, m: False), \
                patch.object(type(inst), '_auto_renew_method', lambda self: False), \
                patch.object(type(inst), '_try_auto_charge_invoice',
                             lambda self, invoice, kind: calls.append(invoice.id)):
            inst._retry_failed_payments(date.today())
        self.assertEqual(calls, [])

    # ---------------- _cron_send_renewal_reminders ----------------

    def test_renewal_reminders_fire_both_offsets_once(self):
        today = fields.Date.today()
        inst7 = self._inst('rem7', next_invoice_date=today + timedelta(days=7))
        inst1 = self._inst('rem1', next_invoice_date=today + timedelta(days=1))
        sent = []
        with patch.object(type(inst7), '_send_notification',
                           lambda self, tmpl: sent.append((self.subdomain, tmpl))):
            self.Instance._cron_send_renewal_reminders()
        self.assertEqual(len(sent), 2)
        inst7.invalidate_recordset()
        inst1.invalidate_recordset()
        self.assertTrue(inst7.renewal_reminder_7d_sent)
        self.assertTrue(inst1.renewal_reminder_1d_sent)

        # Running again the same day must not re-send (flags now set).
        sent.clear()
        with patch.object(type(inst7), '_send_notification',
                           lambda self, tmpl: sent.append(self.subdomain)):
            self.Instance._cron_send_renewal_reminders()
        self.assertEqual(sent, [])

    def test_renewal_reminder_skips_non_matching_date(self):
        today = fields.Date.today()
        inst = self._inst('remno', next_invoice_date=today + timedelta(days=3))
        sent = []
        with patch.object(type(inst), '_send_notification',
                           lambda self, tmpl: sent.append(self.subdomain)):
            self.Instance._cron_send_renewal_reminders()
        self.assertEqual(sent, [])

    # ---------------- _cron_apply_paid_pending_changes ----------------

    def test_paid_pending_change_applies_for_trial(self):
        inst = self._inst('pend1', is_trial=True, pending_plan_id=self.plan.id)
        paid_inv = self._move(payment_state='paid')
        applied = []
        with patch.object(type(inst), '_paid_pending_change_invoice',
                           lambda self: paid_inv), \
                patch.object(type(inst), '_apply_pending_upgrade',
                             lambda self: applied.append('upgrade')), \
                patch.object(type(inst), '_apply_pending_plan_change',
                             lambda self: applied.append('change')):
            self.Instance._cron_apply_paid_pending_changes()
        self.assertEqual(applied, ['upgrade'])

    def test_paid_pending_change_applies_for_non_trial(self):
        inst = self._inst('pend2', is_trial=False, pending_plan_id=self.plan.id)
        paid_inv = self._move(payment_state='paid')
        applied = []
        with patch.object(type(inst), '_paid_pending_change_invoice',
                           lambda self: paid_inv), \
                patch.object(type(inst), '_apply_pending_upgrade',
                             lambda self: applied.append('upgrade')), \
                patch.object(type(inst), '_apply_pending_plan_change',
                             lambda self: applied.append('change')):
            self.Instance._cron_apply_paid_pending_changes()
        self.assertEqual(applied, ['change'])

    def test_paid_pending_change_left_alone_when_unpaid(self):
        inst = self._inst('pend3', is_trial=False, pending_plan_id=self.plan.id)
        applied = []
        with patch.object(type(inst), '_paid_pending_change_invoice',
                           lambda self: self.env['account.move']), \
                patch.object(type(inst), '_apply_pending_upgrade',
                             lambda self: applied.append('upgrade')), \
                patch.object(type(inst), '_apply_pending_plan_change',
                             lambda self: applied.append('change')):
            self.Instance._cron_apply_paid_pending_changes()
        self.assertEqual(applied, [])

    def test_paid_pending_change_ignores_instances_without_pending_plan(self):
        self._inst('pend4', pending_plan_id=False)
        applied = []
        with patch.object(type(self.Instance), '_apply_pending_upgrade',
                           lambda self: applied.append('upgrade')):
            self.Instance._cron_apply_paid_pending_changes()
        self.assertEqual(applied, [])

    # ---------------- _cron_check_trial_expiry ----------------

    def test_trial_expiry_suspends_and_clears_pending_upgrade(self):
        self.partner.write({
            'saas_trial_used': True,
            'saas_trial_end_date': date.today() - timedelta(days=1),
        })
        inst = self._inst('trial1', is_trial=True, state='running',
                           pending_plan_id=self.plan.id,
                           pending_billing_period='yearly')
        suspend_calls = []
        with patch.object(type(inst), 'action_suspend',
                           lambda self: suspend_calls.append(self.subdomain)):
            self.Instance._cron_check_trial_expiry()
        self.assertEqual(suspend_calls, ['trial1'])
        inst.invalidate_recordset()
        self.assertFalse(inst.pending_plan_id)
        self.assertFalse(inst.pending_billing_period)

    def test_trial_expiry_ignores_non_expired_partner(self):
        self.partner.write({
            'saas_trial_used': True,
            'saas_trial_end_date': date.today() + timedelta(days=5),
        })
        self._inst('trial2', is_trial=True, state='running')
        suspend_calls = []
        with patch.object(type(self.Instance), 'action_suspend',
                           lambda self: suspend_calls.append(self.subdomain)):
            self.Instance._cron_check_trial_expiry()
        self.assertEqual(suspend_calls, [])

    def test_trial_expiry_ignores_partner_without_trial_flags(self):
        self.partner.write({
            'saas_trial_used': False,
            'saas_hosting_trial_used': False,
            'saas_trial_end_date': date.today() - timedelta(days=1),
        })
        self._inst('trial3', is_trial=True, state='running')
        suspend_calls = []
        with patch.object(type(self.Instance), 'action_suspend',
                           lambda self: suspend_calls.append(self.subdomain)):
            self.Instance._cron_check_trial_expiry()
        self.assertEqual(suspend_calls, [])
