from datetime import date

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestSnapshotBilling(TransactionCase):
    """Snapshot add-on billing follows the SUBSCRIPTION period.

    The daily-backup add-on no longer has a monthly cycle of its own: it is
    aligned to the plan's renewal date at activation and its charge is merged
    into the plan's renewal invoice for the SAME period — a monthly plan bills
    one month (qty 1), a yearly plan bills the whole year up-front (qty 12).
    The single ``daily_backup_next_invoice_date`` is kept locked in step with
    ``next_invoice_date`` so a period can never be billed twice.
    """

    def setUp(self):
        super().setUp()
        self.icp = self.env['ir.config_parameter'].sudo()
        self.icp.set_param('saas_master.snapshot_price_per_gb', '5.0')
        self.icp.set_param('saas_master.hosting_worker_price', '10.0')
        self.icp.set_param('saas_master.hosting_storage_price_per_gb', '0.3')
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1)
        if not self.product:
            self.product = self.env['saas.product'].sudo().create({
                'name': 'TEST Hosting', 'is_hosting': True, 'is_published': True,
            })
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'TEST Snap Plan', 'is_custom': True,
            'workers': 4, 'storage_limit': 50, 'cpu_limit': 2.0, 'ram_limit': '2g',
            'price': 55.0, 'yearly_price': 528.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])],
        })
        self.partner = self.env['res.partner'].sudo().search([], limit=1)
        self.domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create({'name': 'snap.example.com'})

    def _backup_product(self):
        return self.env['product.product'].sudo().search(
            [('default_code', '=', 'SAAS-BACKUP-ADDON')], limit=1)

    def _mk(self, sub, billing, snap_next, due=None):
        due = due or date(2026, 1, 1)
        inst = self.env['saas.instance'].sudo().create({
            'subdomain': sub, 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'billing_period': billing,
            'state': 'running', 'next_invoice_date': due,
            'last_invoice_date': (due.replace(year=due.year - 1)
                                  if billing == 'yearly'
                                  else date(2025, 12, 1)),
        })
        inst.write({
            'daily_backup_enabled': True,
            'daily_backup_next_invoice_date': snap_next,
            'total_storage_bytes': 50 * (1024 ** 3),
        })
        return inst

    def _renewal_snapshot_lines(self, inst):
        bp = self._backup_product()
        so = self.env['sale.order'].sudo().search(
            [('origin', '=', 'SAAS:RENEWAL:%s' % inst.name)],
            order='id desc', limit=1)
        return [l for l in so.order_line if bp and l.product_id == bp]

    def test_monthly_merges_one_month(self):
        """Monthly plan, backup aligned to the renewal: one snapshot line
        (qty 1) is merged and the backup date advances one month, staying
        aligned with the new next_invoice_date."""
        inst = self._mk('snapmo', 'monthly', date(2026, 1, 1))
        inst._generate_renewal_invoice()
        lines = self._renewal_snapshot_lines(inst)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].product_uom_qty, 1)
        self.assertEqual(inst.daily_backup_next_invoice_date, date(2026, 2, 1))
        self.assertEqual(inst.next_invoice_date, date(2026, 2, 1))

    def test_no_merge_when_due_after_renewal(self):
        """Backup due AFTER the plan renews (not yet aligned ahead): no
        snapshot line on this renewal, backup date untouched."""
        snap_next = date(2026, 2, 1)
        inst = self._mk('snapnd', 'monthly', snap_next, due=date(2026, 1, 1))
        inst._generate_renewal_invoice()
        self.assertFalse(self._renewal_snapshot_lines(inst))
        self.assertEqual(inst.daily_backup_next_invoice_date, snap_next)

    def test_yearly_merges_full_year(self):
        """Yearly plan, backup aligned to the annual renewal: the whole year
        is merged (qty 12, never one month), and the backup date advances a
        full year, staying aligned."""
        inst = self._mk('snapyr', 'yearly', date(2026, 1, 1))
        inst._generate_renewal_invoice()
        lines = self._renewal_snapshot_lines(inst)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].product_uom_qty, 12)  # full year, not 1
        self.assertEqual(inst.daily_backup_next_invoice_date, date(2027, 1, 1))
        self.assertEqual(inst.next_invoice_date, date(2027, 1, 1))

    def test_disabled_backup_omits_line(self):
        """Backups off: no snapshot line on the renewal."""
        inst = self._mk('snapxoff', 'monthly', date(2026, 1, 1))
        inst.daily_backup_enabled = False
        inst._generate_renewal_invoice()
        self.assertFalse(self._renewal_snapshot_lines(inst))

    def test_merged_renewal_counts_as_unpaid_backup(self):
        """An unpaid renewal carrying a snapshot line is recognised by
        _daily_backup_unpaid_invoices (so it can pause snapshots)."""
        inst = self._mk('snapunp', 'monthly', date(2026, 1, 1))
        inst._generate_renewal_invoice()
        unpaid = inst._daily_backup_unpaid_invoices()
        self.assertTrue(unpaid)
        bp = self._backup_product()
        self.assertTrue(any(
            any(l.product_id == bp for l in m.invoice_line_ids)
            for m in unpaid
        ))
