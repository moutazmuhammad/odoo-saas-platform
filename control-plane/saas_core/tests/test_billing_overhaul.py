from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBillingV47(TransactionCase):
    """v47 regression: lot-based two-class wallet, capacity state machine
    (no overage), storage-block purchase, no promotions, due-date payment
    alignment, single billing methodology."""

    def setUp(self):
        super().setUp()
        self.engine = self.env['saas.pricing.engine']
        self.icp = self.env['ir.config_parameter'].sudo()
        self.icp.set_param('saas_master.hosting_worker_price', '10.0')
        self.icp.set_param('saas_master.hosting_storage_price_per_gb', '0.3')
        self.icp.set_param('saas_master.storage_block_gb', '50')
        self.icp.set_param('saas_master.storage_block_price', '5')
        self.icp.set_param('saas_master.storage_grace_days', '7')
        self.icp.set_param('saas_master.system_credit_expiry_months', '12')
        self.partner = self.env['res.partner'].sudo().create(
            {'name': 'V47 Co', 'customer_rank': 1})
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or \
            self.env['saas.product'].sudo().create(
                {'name': 'TEST Hosting', 'is_hosting': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'V47 Plan', 'is_custom': True, 'workers': 4,
            'storage_limit': 50, 'cpu_limit': 2.0, 'ram_limit': '2g',
            'price': 55.0, 'yearly_price': 528.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])],
        })
        self.domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create(
                {'name': 'v47.example.com'})

    def _wallet(self):
        return self.env['saas.wallet'].for_partner(self.partner)

    def _inst(self, sub, **kw):
        vals = {
            'subdomain': sub, 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'billing_period': 'monthly',
            'state': 'running',
        }
        vals.update(kw)
        return self.env['saas.instance'].sudo().create(vals)

    # ---------- A5: single methodology, no days/30 ----------
    def test_months_between(self):
        mb = self.engine.months_between
        self.assertEqual(mb(date(2026, 1, 1), date(2027, 1, 1)), 12)
        self.assertEqual(mb(date(2026, 1, 10), date(2026, 4, 20)), 4)
        self.assertEqual(self.engine.period_months('yearly'), 12)

    def test_yearly_discount_only_on_resources(self):
        Support = self.env['saas.support.plan'].sudo()
        Support.create({'name': 'V47 Sup', 'code': 'v47_sup',
                        'monthly_price': 20.0})
        q = self.engine.compute('hosting', 4, 50, 'yearly', support_code='v47_sup')
        resource_monthly = 4 * 10 + 50 * 0.3
        self.assertAlmostEqual(
            q['yearly'], round(resource_monthly * 12 * 0.8 + 20.0 * 12, 2), 2)

    # ---------- No promotions ----------
    def test_no_trial_promo_fields_or_full_price(self):
        # Promo fields are gone from the model.
        self.assertNotIn('trial_promo_pct', self.env['saas.instance']._fields)
        trial_plan = self.env['saas.plan'].sudo().create({
            'name': 'V47 Trial', 'is_trial_plan': True, 'workers': 2,
            'storage_limit': 5, 'cpu_limit': 1.0, 'ram_limit': '1g',
            'saas_product_ids': [(6, 0, [self.product.id])]})
        inst = self._inst('v47trial', plan_id=trial_plan.id, is_trial=True)
        inst.action_subscribe_from_trial(self.plan.id, 'monthly')
        so = self.env['sale.order'].sudo().search(
            [('origin', '=', 'SAAS:SUBSCRIPTION:%s' % inst.name)],
            order='id desc', limit=1)
        # Full price, no discount line.
        self.assertAlmostEqual(so.amount_total, 55.0, places=2)

    # ---------- Wallet: lots, two classes, priority, idempotency ----------
    def test_wallet_two_class_and_priority(self):
        w = self._wallet()
        w._credit(100.0, origin='upgrade_surplus')            # customer_funded
        w._credit(30.0, origin='goodwill', credit_class='system_issued')
        self.assertAlmostEqual(w.balance_funded, 100.0, 2)
        self.assertAlmostEqual(w.balance_bonus, 30.0, 2)
        self.assertAlmostEqual(w.balance, 130.0, 2)
        # Consuming spends BONUS first, then the customer's own money.
        move = self.env['account.move']
        spent = w._consume(40.0)
        self.assertAlmostEqual(spent, 40.0, 2)
        self.assertAlmostEqual(w.balance_bonus, 0.0, 2)       # bonus drained
        self.assertAlmostEqual(w.balance_funded, 90.0, 2)     # 10 from funded
        self.assertAlmostEqual(w.balance, 90.0, 2)

    def test_wallet_consume_idempotent_per_move(self):
        w = self._wallet()
        w._credit(50.0, origin='manual')
        inv = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id})
        a = w._consume(20.0, move=inv)
        b = w._consume(20.0, move=inv)          # same move → no double spend
        self.assertAlmostEqual(a, 20.0, 2)
        self.assertAlmostEqual(b, 20.0, 2)      # returns prior, doesn't re-charge
        self.assertAlmostEqual(w.balance, 30.0, 2)

    def test_wallet_refund_restores_original_class(self):
        w = self._wallet()
        w._credit(20.0, origin='goodwill', credit_class='system_issued')
        inv = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id})
        w._consume(20.0, move=inv)
        self.assertAlmostEqual(w.balance, 0.0, 2)
        # Cancelling returns the credit to its ORIGINAL bonus lot (not funded).
        r1 = w._refund_move(inv)
        r2 = w._refund_move(inv)                # idempotent
        self.assertAlmostEqual(r1, 20.0, 2)
        self.assertAlmostEqual(r2, 0.0, 2)
        self.assertAlmostEqual(w.balance_bonus, 20.0, 2)
        self.assertAlmostEqual(w.balance_funded, 0.0, 2)

    def test_wallet_never_negative_and_immutable_ledger(self):
        w = self._wallet()
        w._credit(10.0, origin='manual')
        self.assertAlmostEqual(w._consume(999.0), 10.0, 2)
        self.assertAlmostEqual(w.balance, 0.0, 2)
        with self.assertRaises(UserError):
            w.transaction_ids[:1].write({'amount': 5.0})

    def test_wallet_expiry_only_system_issued(self):
        w = self._wallet()
        funded = w._credit(40.0, origin='manual')               # never expires
        bonus = w._credit(15.0, origin='goodwill',
                          credit_class='system_issued',
                          expiry_date=date.today() - timedelta(days=1))
        # Balance counts only LIVE credit, so the already-expired bonus is
        # excluded immediately (the cron only flips its state, not the balance).
        self.assertAlmostEqual(w.balance, 40.0, 2)
        # Expired bonus is not spendable even before the cron runs.
        self.assertFalse(bonus._is_live())
        self.assertTrue(funded._is_live())
        self.env['saas.wallet']._cron_expire_credits()
        bonus.invalidate_recordset()
        self.assertEqual(bonus.state, 'expired')
        self.assertAlmostEqual(w.balance, 40.0, 2)              # only funded left

    # ---------- Capacity state machine (no overage) ----------
    def test_capacity_states(self):
        GB = 1024 ** 3
        inst = self._inst('v47cap', state='stopped')
        # 90% of 50 GB -> warn80
        inst.total_storage_bytes = 45 * GB
        self.assertEqual(inst._evaluate_capacity(), 'warn80')
        # 110% -> full, anchors grace
        inst.total_storage_bytes = 55 * GB
        self.assertEqual(inst._evaluate_capacity(), 'full')
        self.assertTrue(inst.storage_over_since)
        # Past grace -> restricted (paused)
        inst.storage_over_since = date.today() - timedelta(days=8)
        self.assertEqual(inst._evaluate_capacity(), 'restricted')
        self.assertEqual(inst.state, 'suspended')
        # Free space -> back to ok, grace cleared
        inst.total_storage_bytes = 10 * GB
        self.assertEqual(inst._evaluate_capacity(), 'ok')
        self.assertFalse(inst.storage_over_since)

    def test_capacity_summary_positive_framing(self):
        GB = 1024 ** 3
        inst = self._inst('v47copy', state='stopped', total_storage_bytes=55 * GB)
        inst._evaluate_capacity()
        cap = inst._capacity_summary()
        text = (cap['title'] + ' ' + cap['message']).lower()
        for banned in ('exceeded', 'violation', 'penalty', 'quota', 'overage'):
            self.assertNotIn(banned, text)
        self.assertIn('capacity', text)

    # ---------- Storage block purchase raises capacity ----------
    def test_storage_block_purchase(self):
        inst = self._inst('v47block')
        self.assertAlmostEqual(inst.effective_storage_limit_gb, 50.0, 2)
        inv = inst.action_purchase_storage_block(1)
        self.assertTrue(inv.id)
        self.assertEqual(inst.pending_storage_blocks, 1)
        inst._activate_pending_storage_blocks()
        self.assertEqual(inst.extra_storage_blocks, 1)
        # 50 GB plan + 1 × 50 GB block = 100 GB capacity.
        self.assertAlmostEqual(inst.effective_storage_limit_gb, 100.0, 2)

    # ---------- C4.2: more room lifts a capacity pause ----------
    def _paused(self, sub, used_gb, reason='capacity'):
        """A server paused for *reason* while *used_gb* of its 50 GB were
        used, past the grace period."""
        return self._inst(
            sub, state='suspended', suspension_reason=reason,
            total_storage_bytes=used_gb * 1024 ** 3, storage_state='restricted',
            storage_over_since=date.today() - timedelta(days=10))

    def _resume_jobs(self, inst):
        return self.env['saas.job'].sudo().search([
            ('model', '=', 'saas.instance'), ('res_id', '=', inst.id),
            ('method', '=', '_do_resume')])

    def _no_cluster(self, inst):
        driver = MagicMock()
        for target, attr, fn in (
                (type(inst), '_compute_driver', lambda rec, connection=None: driver),
                (type(inst), '_compute_handle', lambda rec: 'H'),
                (type(inst), '_update_container_resources', lambda rec: None),
                (type(inst), '_safe_refresh_usage', lambda rec: None),
                (self.env.cr, 'commit', lambda: None)):
            p = patch.object(target, attr, fn)
            p.start()
            self.addCleanup(p.stop)
        return driver

    def test_capacity_pause_records_its_reason(self):
        inst = self._inst('v47why', total_storage_bytes=55 * 1024 ** 3,
                          storage_over_since=date.today() - timedelta(days=8))
        self.assertEqual(inst._evaluate_capacity(), 'restricted')
        self.assertEqual(inst.suspension_reason, 'capacity')

    def test_storage_purchase_unpauses_a_paused_server(self):
        inst = self._paused('v47unpause', used_gb=55)
        driver = self._no_cluster(inst)
        inst.action_purchase_storage_block(1)
        inst._activate_pending_storage_blocks()      # the paid invoice's hook
        self.assertEqual(inst.storage_state, 'ok')   # 55 of 100 GB
        job = self._resume_jobs(inst)
        self.assertEqual(len(job), 1)
        self.assertEqual((inst.state, inst.pending_operation), ('provisioning', 'restart'))
        self.env.flush_all()
        self.env['saas.job'].sudo()._run_one(job.id)
        inst.invalidate_recordset()
        self.assertEqual((inst.state, inst.suspension_reason), ('running', False))
        driver.restart.assert_called_once()

    def test_not_enough_room_stays_paused(self):
        inst = self._paused('v47short', used_gb=120)
        self._no_cluster(inst)
        inst.action_purchase_storage_block(1)
        inst._activate_pending_storage_blocks()      # 120 of 100 GB
        self.assertEqual(inst.storage_state, 'restricted')
        self.assertEqual(inst.state, 'suspended')
        self.assertFalse(self._resume_jobs(inst))

    def test_capacity_never_lifts_another_suspension(self):
        inst = self._paused('v47other', used_gb=10, reason='nonpayment')
        self.assertEqual(inst._evaluate_capacity(), 'ok')
        self.assertEqual(inst.state, 'suspended')
        self.assertFalse(self._resume_jobs(inst))

    def test_storage_cron_unpauses_after_a_plan_upgrade(self):
        inst = self._paused('v47cron', used_gb=55)
        self._no_cluster(inst)
        inst.plan_id.storage_limit = 100             # upgraded meanwhile
        self.env['saas.instance']._cron_check_storage_limits()
        inst.invalidate_recordset()
        self.assertEqual(inst.state, 'provisioning')
        self.assertEqual(len(self._resume_jobs(inst)), 1)

    def test_paid_plan_change_unpauses_at_once(self):
        inst = self._paused('v47upg', used_gb=55)
        self._no_cluster(inst)
        bigger = self.plan.copy({'name': 'V47 Big', 'storage_limit': 100})
        inst.write({'pending_plan_id': bigger.id, 'pending_billing_period': 'monthly'})
        inst._apply_pending_plan_change()
        self.assertEqual(inst.plan_id, bigger)
        self.assertEqual(inst.state, 'provisioning')
        self.assertEqual(len(self._resume_jobs(inst)), 1)

    # ---------- Proration full credit (no clawback) ----------
    def test_proration_full_credit(self):
        today = fields.Date.today()
        inst = self._inst('v47pro',
                          last_invoice_date=today - timedelta(days=15),
                          next_invoice_date=today + timedelta(days=15))
        value, rem_days, total_days = inst._proration_credit(60.0)
        self.assertEqual(total_days, 30)
        self.assertEqual(rem_days, 15)         # no -2 clawback
        self.assertAlmostEqual(value, 30.0, 2)

    # ---------- Payment timing keyed off due date ----------
    def test_payment_due_date_helper(self):
        inv = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_date': date(2026, 1, 1),
            'invoice_date_due': date(2026, 1, 10)})
        self.assertEqual(
            self.env['saas.instance']._payment_due_date(inv), date(2026, 1, 10))
