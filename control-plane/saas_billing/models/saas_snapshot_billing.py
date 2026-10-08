"""On-demand snapshot billing (DigitalOcean-style, prepaid, per GB-month).

* A snapshot's price is frozen when it is taken: its size rounded UP to
  whole GB (1 GB minimum) × ``saas_master.snapshot_price_per_gb`` per month.
* The customer has ONE snapshot billing day. The first snapshot sets it
  (one month from today). A new snapshot is charged immediately for the
  days left until that day (prorated); afterwards every live snapshot is
  invoiced one month in advance, a few days before the billing day.
* One invoice per snapshot, so the accounting is exact: deleting a
  snapshot cancels its unpaid invoice and it simply never shows up again.
  The prepaid period is never refunded.
* Payment: wallet (all or nothing), then the customer's saved card,
  otherwise the invoice waits. Unpaid at the due date → warning email;
  after ``saas_master.snapshot_grace_days`` (default 7) the snapshot is
  deleted and the customer is told.
* Snapshots are a customer service: they outlive the project and keep
  billing until deleted.
"""
import datetime
import logging
import math
import threading

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

ORIGIN_SNAPSHOT = 'SAAS:SNAPSHOT:%s'
SNAPSHOT_BILLING_LEAD_DAYS = 3


class ResPartnerSnapshotBilling(models.Model):
    _inherit = 'res.partner'

    snapshot_billing_date = fields.Date(
        copy=False,
        help="The customer's snapshot billing day: every live snapshot is "
             "invoiced one month in advance up to this date.")


class SaasInstanceSnapshotQuote(models.Model):
    _inherit = 'saas.instance'

    def _snapshot_quote(self, size_bytes):
        """Monthly price of a snapshot of ``size_bytes`` and the prorated
        amount due now for the customer's current snapshot cycle."""
        self.ensure_one()
        Backup = self.env['saas.instance.backup']
        gb, monthly = Backup._snapshot_price(size_bytes)
        due_now, period_end = Backup._snapshot_first_charge(self.partner_id, monthly)
        return {'billable_gb': gb, 'monthly_price': monthly, 'due_now': due_now,
                'period_end': fields.Date.to_string(period_end)}


class SaasInstanceBackupBilling(models.Model):
    _inherit = 'saas.instance.backup'

    billable_gb = fields.Integer(copy=False, help="Size rounded up to whole GB, 1 GB minimum.")
    monthly_price = fields.Float(copy=False, help="Frozen when the snapshot was taken.")
    paid_until = fields.Date(copy=False, help="Prepaid up to this date.")
    pending_invoice_id = fields.Many2one('account.move', copy=False, ondelete='set null')
    pending_period_end = fields.Date(copy=False)
    billing_warned = fields.Boolean(default=False, copy=False)
    billing_state = fields.Selection([
        ('free', 'Not billed'), ('paid', 'Paid'), ('pending', 'Payment pending'),
        ('overdue', 'Overdue'),
    ], compute='_compute_billing_state')

    @api.depends('source', 'monthly_price', 'paid_until', 'pending_invoice_id',
                 'pending_invoice_id.payment_state', 'pending_invoice_id.state')
    def _compute_billing_state(self):
        today = fields.Date.today()
        for rec in self:
            if rec.source != 'snapshot' or not rec.monthly_price:
                rec.billing_state = 'free'
            elif rec._pending_invoice_unpaid():
                rec.billing_state = 'overdue' if (rec.paid_until and rec.paid_until < today) else 'pending'
            else:
                rec.billing_state = 'paid'

    def _pending_invoice_unpaid(self):
        inv = self.pending_invoice_id
        return bool(inv) and inv.state == 'posted' and inv.payment_state not in ('paid', 'in_payment')

    # ------------------------------------------------------------ pricing
    @api.model
    def _snapshot_price(self, size_bytes):
        """(billable GB, monthly price) for a snapshot of ``size_bytes``."""
        per_gb = self.env['saas.pricing.engine'].snapshot_price_per_gb()
        gb = max(1, math.ceil((size_bytes or 0) / (1024 ** 3)))
        return gb, round(gb * per_gb, 2) if per_gb > 0 else 0.0

    @api.model
    def _snapshot_first_charge(self, partner, monthly):
        """Prorated amount due now and the end of the current cycle for a
        snapshot taken today. The first snapshot opens the customer's cycle."""
        today = fields.Date.today()
        commercial = partner.commercial_partner_id
        cycle_end = commercial.snapshot_billing_date
        if not cycle_end or cycle_end <= today:
            cycle_end = today + relativedelta(months=1)
            return monthly, cycle_end
        # Days left in the current cycle over the cycle's length.
        cycle_start = cycle_end - relativedelta(months=1)
        total = max(1, (cycle_end - cycle_start).days)
        left = max(0, (cycle_end - today).days)
        return round(monthly * left / total, 2), cycle_end

    @api.model
    def _snapshot_grace_days(self):
        raw = self.env['ir.config_parameter'].sudo().get_param('saas_master.snapshot_grace_days', '7')
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 7

    # ------------------------------------------------------------ hooks
    def _on_snapshot_ready(self):
        """The snapshot finished (size known): freeze its price and charge
        the first (prorated) period."""
        self.ensure_one()
        if self.source != 'snapshot' or self.monthly_price:
            return super()._on_snapshot_ready()
        gb, monthly = self._snapshot_price((self.size_mb or 0) * 1024 * 1024)
        self.write({'billable_gb': gb, 'monthly_price': monthly})
        if monthly <= 0:
            self.paid_until = fields.Date.today() + relativedelta(years=100)
            return True
        partner = (self.partner_id or self.instance_id.partner_id).commercial_partner_id
        due_now, period_end = self._snapshot_first_charge(partner, monthly)
        if not partner.snapshot_billing_date or partner.snapshot_billing_date <= fields.Date.today():
            partner.snapshot_billing_date = period_end
        self._issue_snapshot_invoice(due_now, period_end, first=True)
        return True

    def _on_snapshot_deleted(self):
        """A deleted snapshot never pays again: void its unpaid invoice."""
        for rec in self:
            inv = rec.pending_invoice_id
            if inv and rec._pending_invoice_unpaid():
                try:
                    inv.button_cancel()
                except Exception:
                    _logger.exception("Could not cancel snapshot invoice %s", inv.id)
        return super()._on_snapshot_deleted()

    # ------------------------------------------------------------ invoicing
    def _issue_snapshot_invoice(self, amount, period_end, first=False):
        """One invoice for THIS snapshot covering up to ``period_end``.
        Wallet (all or nothing) → saved card → left open for the customer."""
        self.ensure_one()
        partner = (self.partner_id or self.instance_id.partner_id)
        Instance = self.env['saas.instance'].sudo()
        anchor = self.instance_id or Instance.search([('partner_id', '=', partner.id)], limit=1)
        product = Instance._get_billing_product()
        label = _('Snapshot "%s" — %d GB (%s%s)') % (
            self.name, self.billable_gb or 1,
            _('prorated to ') if first else _('month to '),
            fields.Date.to_string(period_end))
        order_lines = [(0, 0, {'product_id': product.id, 'name': label,
                               'product_uom_qty': 1, 'price_unit': round(amount, 2)})]
        wallet = self.env['saas.wallet'].for_partner(partner, create=False)
        wallet_amount = 0.0
        if amount > 0 and wallet and wallet.balance >= amount:
            wallet._lock()
            if wallet.balance >= amount:
                wallet_amount = round(amount, 2)
                order_lines.append((0, 0, {
                    'product_id': product.id,
                    'name': _('Wallet credit applied — snapshot "%s"') % self.name,
                    'product_uom_qty': 1, 'price_unit': -wallet_amount}))
        order_vals = {'partner_id': partner.id,
                      'origin': ORIGIN_SNAPSHOT % ('%s#%s' % (self.name, self.id)),
                      'order_line': order_lines}
        pricelist = partner.property_product_pricelist
        if pricelist:
            order_vals['pricelist_id'] = pricelist.id
        order = self.env['sale.order'].sudo().create(order_vals)
        order.action_confirm()
        invoice = order._create_invoices()
        invoice.action_post()
        if wallet_amount:
            wallet._consume(wallet_amount, origin='invoice_consumption',
                            reason=_('Applied to invoice %s') % (invoice.name or ''),
                            move=invoice, instance=self.instance_id or None)
        self.write({'pending_invoice_id': invoice.id, 'pending_period_end': period_end,
                    'billing_warned': False})
        if invoice.amount_total <= 0:
            self._apply_snapshot_payment()
            return invoice
        if anchor and anchor._auto_renew_method():
            try:
                if anchor._try_auto_charge_invoice(invoice, kind='snapshot'):
                    return invoice
            except Exception:
                _logger.exception("Snapshot %s: auto-charge failed", self.id)
        self._notify('saas_billing.mail_template_saas_snapshot_invoice')
        return invoice

    def _apply_snapshot_payment(self):
        for rec in self:
            if rec.pending_period_end:
                rec.write({'paid_until': rec.pending_period_end, 'pending_period_end': False,
                           'pending_invoice_id': False, 'billing_warned': False})

    def _notify(self, template_xmlid):
        self.ensure_one()
        template = self.env.ref(template_xmlid, raise_if_not_found=False)
        if template:
            try:
                template.send_mail(self.id, force_send=False)
            except Exception:
                _logger.exception("Could not send %s for snapshot %s", template_xmlid, self.id)

    # ------------------------------------------------------------ cron
    @api.model
    def _cron_bill_snapshots(self):
        """Daily: invoice the next month in advance, chase unpaid ones,
        delete after the grace period."""
        today = fields.Date.today()
        live = self.sudo().search([('source', '=', 'snapshot'), ('state', '=', 'done'),
                                   ('monthly_price', '>', 0)])
        grace = datetime.timedelta(days=self._snapshot_grace_days())
        for snap in live:
            try:
                if snap._pending_invoice_unpaid():
                    due = snap.paid_until or today
                    if today > due and not snap.billing_warned:
                        snap.billing_warned = True
                        snap._notify('saas_billing.mail_template_saas_snapshot_overdue')
                    elif today > due + grace:
                        snap._notify('saas_billing.mail_template_saas_snapshot_deleted')
                        snap.action_delete_snapshot()
                    continue
                if snap.pending_invoice_id and not snap._pending_invoice_unpaid():
                    # Paid (or cancelled) outside the hook: settle it.
                    if snap.pending_invoice_id.payment_state in ('paid', 'in_payment'):
                        snap._apply_snapshot_payment()
                    else:
                        snap.write({'pending_invoice_id': False, 'pending_period_end': False})
                if not snap.paid_until:
                    continue
                if snap.paid_until - datetime.timedelta(days=SNAPSHOT_BILLING_LEAD_DAYS) <= today:
                    period_end = snap.paid_until + relativedelta(months=1)
                    snap._issue_snapshot_invoice(snap.monthly_price, period_end)
                    partner = (snap.partner_id or snap.instance_id.partner_id).commercial_partner_id
                    if not partner.snapshot_billing_date or partner.snapshot_billing_date < period_end:
                        partner.snapshot_billing_date = period_end
            except Exception:
                _logger.exception("Snapshot billing failed for %s", snap.id)
            if not getattr(threading.current_thread(), 'testing', False):
                self.env.cr.commit()
