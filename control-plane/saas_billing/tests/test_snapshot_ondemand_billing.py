"""On-demand snapshot billing: frozen per-GB price, ONE open invoice per
customer that is reissued when its lines change, prorated first periods,
renewals a month in advance, grace and deletion, independence from the
project; daily backups as % of the plan."""
import datetime
from unittest.mock import patch

from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

GB = 1024 ** 3


@tagged('post_install', '-at_install')
class TestSnapshotOndemandBilling(TransactionCase):

    def setUp(self):
        super().setUp()
        env = self.env
        self.icp = env['ir.config_parameter'].sudo()
        self.icp.set_param('saas_master.snapshot_price_per_gb', '0.40')
        self.icp.set_param('saas_master.snapshot_grace_days', '7')
        self.icp.set_param('saas_master.daily_backup_pct', '20')
        product = env['saas.product'].sudo().search([('is_hosting', '=', True)], limit=1) \
            or env['saas.product'].sudo().create({'name': 'SB Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = env['saas.plan'].sudo().create({
            'name': 'SB Plan', 'is_custom': True, 'workers': 2, 'storage_limit': 20,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 50.0, 'yearly_price': 480.0,
            'currency_id': env.company.currency_id.id, 'saas_product_ids': [(6, 0, [product.id])]})
        self.partner = env['res.partner'].sudo().create({'name': 'SB Cust', 'email': 'sb@example.com'})
        self.inst = env['saas.instance'].sudo().create({
            'subdomain': 'sbtest',
            'domain_id': (env['saas.based.domain'].sudo().search([], limit=1)
                          or env['saas.based.domain'].sudo().create({'name': 'sb.example.com'})).id,
            'partner_id': self.partner.id, 'saas_product_id': product.id, 'plan_id': self.plan.id,
            'billing_period': 'monthly', 'environment': 'production', 'region_id': False,
            'state': 'running'})
        self.Backup = env['saas.instance.backup'].sudo()
        self.sent = []
        p = patch.object(type(self.Backup), '_notify', lambda rec, tmpl: self.sent.append((rec.name, tmpl)))
        p.start(); self.addCleanup(p.stop)
        p = patch.object(type(self.Backup), '_delete_bucket_prefix', lambda rec, prefix: None)
        p.start(); self.addCleanup(p.stop)

    def _snapshot(self, name, size_gb=2.0, state='done'):
        return self.Backup.create({
            'instance_id': self.inst.id, 'partner_id': self.partner.id, 'name': name,
            'state': state, 'is_full_instance': True, 'format': 'operator', 'source': 'snapshot',
            'origin_subdomain': 'sbtest', 'size_mb': size_gb * 1024,
            'bucket_path': 'snapshots/sbtest/%s' % name})

    def _open_invoices(self):
        return self.env['account.move'].sudo().search([
            ('partner_id', '=', self.partner.id), ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted'), ('payment_state', 'not in', ('paid', 'in_payment')),
            ('invoice_line_ids.sale_line_ids.order_id.origin', 'like', 'SAAS:SNAPSHOT:%')])

    # -- pricing -----------------------------------------------------------
    def test_price_rounds_up_with_a_one_gb_floor(self):
        self.assertEqual(self.Backup._snapshot_price(0), (1, 0.40))
        self.assertEqual(self.Backup._snapshot_price(int(2.1 * GB)), (3, 1.20))

    def test_first_snapshot_opens_the_cycle_and_is_prepaid_for_a_month(self):
        snap = self._snapshot('first', size_gb=2.0)
        snap._on_snapshot_ready()
        today = fields.Date.today()
        self.assertEqual((snap.billable_gb, snap.monthly_price), (2, 0.80))
        self.assertEqual(self.partner.snapshot_billing_date, today + relativedelta(months=1))
        inv = self.partner.snapshot_pending_invoice_id
        self.assertTrue(inv and inv.state == 'posted')
        self.assertAlmostEqual(inv.amount_total, 0.80, 2)
        self.assertEqual(snap.pending_invoice_id, inv)
        self.assertEqual(snap.billing_state, 'pending')
        self.assertIn(('first', 'saas_billing.mail_template_saas_snapshot_invoice'), self.sent)
        # Payment (the account.move hook) prepays up to the cycle end.
        snap._apply_snapshot_payment()
        self.assertEqual(snap.paid_until, today + relativedelta(months=1))
        self.assertEqual(snap.billing_state, 'paid')
        self.assertFalse(self.partner.snapshot_pending_invoice_id)

    def test_second_snapshot_joins_the_same_open_invoice_prorated(self):
        """The customer never gets one invoice per snapshot: a new snapshot
        reissues the single open invoice with its prorated line added."""
        today = fields.Date.today()
        first = self._snapshot('first', size_gb=2.0)
        first._on_snapshot_ready()
        inv1 = self.partner.snapshot_pending_invoice_id
        self.partner.snapshot_billing_date = today + datetime.timedelta(days=10)
        second = self._snapshot('second', size_gb=1.0)   # 0.40/month
        second._on_snapshot_ready()
        inv2 = self.partner.snapshot_pending_invoice_id
        self.assertNotEqual(inv1, inv2)
        self.assertEqual(inv1.state, 'cancel', "the old open invoice is replaced, never a second one")
        self.assertEqual(len(self._open_invoices()), 1)
        cycle_end = self.partner.snapshot_billing_date
        total_days = (cycle_end - (cycle_end - relativedelta(months=1))).days
        expected = round(0.80 + round(0.40 * 10 / total_days, 2), 2)
        self.assertAlmostEqual(inv2.amount_total, expected, 2)
        self.assertEqual((first.pending_invoice_id, second.pending_invoice_id), (inv2, inv2))
        self.assertEqual(len(inv2.invoice_line_ids.filtered(lambda l: l.price_unit > 0)), 2)

    def test_wallet_pays_in_full_or_not_at_all(self):
        wallet = self.env['saas.wallet'].for_partner(self.partner, create=True)
        wallet._credit(5.0, origin='test', reason='seed')
        snap = self._snapshot('paid-by-wallet', size_gb=1.0)
        snap._on_snapshot_ready()
        self.assertEqual(snap.billing_state, 'paid', "wallet covered it: nothing open")
        self.assertAlmostEqual(wallet.balance, 4.60, 2)
        wallet._consume(4.50, origin='test')
        snap2 = self._snapshot('not-enough', size_gb=1.0)
        snap2._on_snapshot_ready()
        self.assertEqual(snap2.billing_state, 'pending')
        self.assertAlmostEqual(wallet.balance, 0.10, 2, "no partial wallet use")

    # -- renewal / dunning --------------------------------------------------
    def test_cron_renews_every_snapshot_on_one_invoice_a_month_in_advance(self):
        today = fields.Date.today()
        a = self._snapshot('a', size_gb=1.0)
        b = self._snapshot('b', size_gb=2.0)
        (a | b).write({'paid_until': today + datetime.timedelta(days=2)})
        a.write({'billable_gb': 1, 'monthly_price': 0.40})
        b.write({'billable_gb': 2, 'monthly_price': 0.80})
        self.partner.snapshot_billing_date = a.paid_until
        self.Backup._cron_bill_snapshots()
        inv = self.partner.snapshot_pending_invoice_id
        self.assertTrue(inv)
        self.assertAlmostEqual(inv.amount_total, 1.20, 2)
        self.assertEqual((a.pending_invoice_id, b.pending_invoice_id), (inv, inv))
        self.assertEqual(a.pending_period_end, a.paid_until + relativedelta(months=1))
        self.Backup._cron_bill_snapshots()
        self.assertEqual(self.partner.snapshot_pending_invoice_id, inv, "unchanged lines: same invoice")
        self.assertEqual(len(self._open_invoices()), 1)
        inv.payment_state = 'paid'
        self.Backup._cron_bill_snapshots()
        self.assertEqual(a.paid_until, today + datetime.timedelta(days=2) + relativedelta(months=1))
        self.assertEqual(self.partner.snapshot_billing_date, a.paid_until)

    def test_unpaid_invoice_is_warned_then_its_snapshots_deleted_after_grace(self):
        today = fields.Date.today()
        snap = self._snapshot('late', size_gb=1.0)
        snap.write({'billable_gb': 1, 'monthly_price': 0.40, 'paid_until': today})
        self.Backup._cron_bill_snapshots()
        inv = self.partner.snapshot_pending_invoice_id
        self.assertTrue(inv)
        inv.invoice_date_due = today - datetime.timedelta(days=1)
        self.Backup._cron_bill_snapshots()
        self.assertTrue(self.partner.snapshot_billing_warned)
        self.assertIn(('late', 'saas_billing.mail_template_saas_snapshot_overdue'), self.sent)
        self.assertTrue(snap.exists())
        inv.invoice_date_due = today - datetime.timedelta(days=9)
        self.Backup._cron_bill_snapshots()
        self.assertFalse(snap.exists())
        self.assertIn(('late', 'saas_billing.mail_template_saas_snapshot_deleted'), self.sent)
        self.assertEqual(inv.state, 'cancel')

    def test_deleting_a_snapshot_reissues_the_open_invoice_without_it(self):
        a = self._snapshot('a', size_gb=1.0)
        b = self._snapshot('b', size_gb=2.0)
        a._on_snapshot_ready(); b._on_snapshot_ready()
        inv = self.partner.snapshot_pending_invoice_id
        self.assertAlmostEqual(inv.amount_total, 1.20, 2)
        b.action_delete_snapshot()
        self.assertEqual(inv.state, 'cancel')
        inv2 = self.partner.snapshot_pending_invoice_id
        self.assertTrue(inv2 and inv2 != inv)
        self.assertAlmostEqual(inv2.amount_total, 0.40, 2)
        a.action_delete_snapshot()
        self.assertEqual(inv2.state, 'cancel')
        self.assertFalse(self.partner.snapshot_pending_invoice_id)

    # -- independence from the project -----------------------------------
    def test_snapshot_survives_project_unlink_and_keeps_its_owner(self):
        snap = self._snapshot('keep')
        other = self.Backup.create({'instance_id': self.inst.id, 'name': 'nightly', 'state': 'done',
                                    'is_full_instance': True, 'format': 'operator', 'source': 'scheduled'})
        self.inst.state = 'cancelled'
        self.inst.unlink()
        self.assertTrue(snap.exists())
        self.assertFalse(snap.instance_id)
        self.assertEqual(snap.partner_id, self.partner)
        self.assertEqual(snap.origin_subdomain, 'sbtest')
        self.assertFalse(other.exists(), "daily backups go with the project")

    # -- daily backups as a percentage of the plan -------------------------
    def test_daily_backup_price_is_a_percentage_of_the_plan(self):
        self.assertAlmostEqual(self.inst._get_daily_backup_price(), 10.0, 2)   # 20% of 50
        self.icp.set_param('saas_master.daily_backup_pct', '0')
        self.inst.invalidate_recordset()
        with patch.object(type(self.inst), '_snapshot_total_bytes', lambda rec: 3 * GB):
            self.assertAlmostEqual(self.inst._get_daily_backup_price(), 1.20, 2)  # per GB fallback

    # -- daily backups can be switched off at any time ---------------------
    def test_customer_can_switch_daily_backups_off_any_time(self):
        self.inst.write({'daily_backup_enabled': True,
                         'daily_backup_next_invoice_date': fields.Date.today() + datetime.timedelta(days=10)})
        nightly = self.Backup.create({'instance_id': self.inst.id, 'name': 'nightly', 'state': 'done',
                                      'is_full_instance': True, 'format': 'operator', 'source': 'scheduled',
                                      'bucket_path': 'backups/sbtest/nightly'})
        snap = self._snapshot('keep-me')
        synced = []
        self.inst.docker_server_id = self.env['saas.server'].sudo().create(
            {'name': 'sb-srv', 'compute_driver': 'kubernetes'})
        with patch.object(type(self.inst), '_sync_scheduled_backup', lambda rec: synced.append(rec.id)):
            self.inst.action_disable_daily_backup()
        self.assertFalse(self.inst.daily_backup_enabled)
        self.assertFalse(self.inst.daily_backup_next_invoice_date)
        self.assertEqual(synced, [self.inst.id], "the nightly job is removed")
        self.assertFalse(nightly.exists(), "automatic backups go with the add-on")
        self.assertTrue(snap.exists(), "snapshots are a separate service")
        self.assertIsNone(self.inst._snapshot_order_line('monthly'), "no add-on line on the next renewal")

