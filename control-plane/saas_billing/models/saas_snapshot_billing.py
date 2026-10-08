"""On-demand snapshot billing (DigitalOcean-style, prepaid, per GB-month).

* A snapshot's price is frozen when it is taken: its size rounded UP to
  whole GB (1 GB minimum) × ``saas_master.snapshot_price_per_gb`` per month.
* The customer has ONE snapshot billing day (the first snapshot sets it,
  one month from that day). A new snapshot owes the days left until that
  day (prorated); every live snapshot then owes a month in advance,
  invoiced a few days before the billing day.
* The customer never pays per snapshot: at any time there is at most ONE
  open snapshot invoice per customer. Whenever the set of billable lines
  changes (new snapshot, deleted snapshot, renewal due) the open unpaid
  invoice is cancelled and reissued with the exact current lines. Paid
  invoices are never touched, and the prepaid period is never refunded.
* Payment: wallet (all or nothing), then the saved card, otherwise the
  invoice waits for the customer. Unpaid at its due date → warning email;
  after ``saas_master.snapshot_grace_days`` (default 7) the snapshots on
  it are deleted and the customer is told.
* Snapshots are a customer service: they outlive the project and keep
  billing until deleted.
"""
import datetime
import json
import logging
import math
import threading

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)

ORIGIN_SNAPSHOT = 'SAAS:SNAPSHOT:%s'
SNAPSHOT_BILLING_LEAD_DAYS = 3


class ResPartnerSnapshotBilling(models.Model):
    _inherit = 'res.partner'

    snapshot_billing_date = fields.Date(
        copy=False,
        help="The customer's snapshot billing day: every live snapshot is "
             "prepaid up to this date, then renewed a month at a time.")
    snapshot_pending_invoice_id = fields.Many2one(
        'account.move', copy=False, ondelete='set null',
        help="The customer's single open snapshot invoice, if any.")
    snapshot_invoice_signature = fields.Char(copy=False)
    snapshot_billing_warned = fields.Boolean(default=False, copy=False)


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
    # First (prorated) period not paid yet: becomes a line on the customer's
    # open snapshot invoice.
    unbilled_amount = fields.Float(copy=False)
    unbilled_period_end = fields.Date(copy=False)
    # The invoice this snapshot is currently waiting on (shared with the
    # customer's other snapshots) and what paying it prepays up to.
    pending_invoice_id = fields.Many2one('account.move', copy=False, ondelete='set null')
    pending_period_end = fields.Date(copy=False)
    billing_state = fields.Selection([
        ('free', 'Not billed'), ('paid', 'Paid'), ('pending', 'Payment pending'),
        ('overdue', 'Overdue'),
    ], compute='_compute_billing_state')

    @api.depends('source', 'monthly_price', 'paid_until', 'pending_invoice_id',
                 'pending_invoice_id.payment_state', 'pending_invoice_id.state',
                 'pending_invoice_id.invoice_date_due', 'unbilled_amount')
    def _compute_billing_state(self):
        today = fields.Date.today()
        for rec in self:
            if rec.source != 'snapshot' or not rec.monthly_price:
                rec.billing_state = 'free'
            elif rec._pending_invoice_unpaid():
                due = rec.pending_invoice_id.invoice_date_due or today
                rec.billing_state = 'overdue' if due < today else 'pending'
            elif rec.unbilled_amount:
                rec.billing_state = 'pending'
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
        """Prorated amount due for a snapshot taken today and the end of
        the customer's current cycle (the first snapshot opens the cycle)."""
        today = fields.Date.today()
        cycle_end = partner.commercial_partner_id.snapshot_billing_date
        if not cycle_end or cycle_end <= today:
            return monthly, today + relativedelta(months=1)
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

    def _billing_partner(self):
        self.ensure_one()
        return (self.partner_id or self.instance_id.partner_id).commercial_partner_id

    # ------------------------------------------------------------ hooks
    def _on_snapshot_ready(self):
        """The snapshot finished (size known): freeze its price, record the
        prorated first period and refresh the customer's open invoice."""
        self.ensure_one()
        if self.source != 'snapshot' or self.monthly_price:
            return super()._on_snapshot_ready()
        gb, monthly = self._snapshot_price((self.size_mb or 0) * 1024 * 1024)
        self.write({'billable_gb': gb, 'monthly_price': monthly})
        if monthly <= 0:
            self.paid_until = fields.Date.today() + relativedelta(years=100)
            return True
        partner = self._billing_partner()
        due_now, period_end = self._snapshot_first_charge(partner, monthly)
        if not partner.snapshot_billing_date or partner.snapshot_billing_date <= fields.Date.today():
            partner.snapshot_billing_date = period_end
        self.write({'unbilled_amount': due_now, 'unbilled_period_end': period_end})
        self._rebuild_partner_invoice(partner)
        return True

    def _on_snapshot_deleted(self):
        """A deleted snapshot drops off the customer's open invoice."""
        partners = self.env['res.partner']
        for rec in self:
            partners |= rec._billing_partner()
            rec.write({'unbilled_amount': 0.0, 'unbilled_period_end': False,
                       'pending_invoice_id': False, 'pending_period_end': False})
        res = super()._on_snapshot_deleted()
        for partner in partners:
            self._rebuild_partner_invoice(partner, exclude=self)
        return res

    # ------------------------------------------------------------ the one invoice
    @api.model
    def _partner_live_snapshots(self, partner):
        return self.sudo().search([
            ('source', '=', 'snapshot'), ('state', '=', 'done'), ('monthly_price', '>', 0),
            ('partner_id', 'child_of', partner.commercial_partner_id.id)])

    @api.model
    def _partner_billable_lines(self, partner, exclude=None):
        """What the customer owes right now: prorated first periods plus a
        month ahead for every snapshot whose prepaid period ends within
        the lead window. Returns [(snapshot, amount, label, period_end)]."""
        today = fields.Date.today()
        lead = datetime.timedelta(days=SNAPSHOT_BILLING_LEAD_DAYS)
        lines = []
        for snap in self._partner_live_snapshots(partner):
            if exclude and snap in exclude:
                continue
            if snap.unbilled_amount and snap.unbilled_period_end:
                lines.append((snap, snap.unbilled_amount,
                              _('Snapshot "%s" — %d GB, prorated to %s') % (
                                  snap.name, snap.billable_gb or 1,
                                  fields.Date.to_string(snap.unbilled_period_end)),
                              snap.unbilled_period_end))
            elif snap.paid_until and snap.paid_until - lead <= today:
                period_end = snap.paid_until + relativedelta(months=1)
                lines.append((snap, snap.monthly_price,
                              _('Snapshot "%s" — %d GB, %s to %s') % (
                                  snap.name, snap.billable_gb or 1,
                                  fields.Date.to_string(snap.paid_until),
                                  fields.Date.to_string(period_end)),
                              period_end))
        return lines

    @api.model
    def _rebuild_partner_invoice(self, partner, exclude=None):
        """Keep exactly one open snapshot invoice per customer matching the
        current billable lines. Reissues (cancel + new) when they changed."""
        partner = partner.commercial_partner_id
        lines = self._partner_billable_lines(partner, exclude=exclude)
        signature = json.dumps([[s.id, round(a, 2), fields.Date.to_string(e)] for s, a, _l, e in lines])
        open_inv = partner.snapshot_pending_invoice_id
        open_unpaid = bool(open_inv) and open_inv.state == 'posted' \
            and open_inv.payment_state not in ('paid', 'in_payment')
        if open_inv and not open_unpaid:
            # Paid or cancelled elsewhere: settle it and move on.
            if open_inv.payment_state in ('paid', 'in_payment'):
                self.search([('pending_invoice_id', '=', open_inv.id)])._apply_snapshot_payment()
            partner.write({'snapshot_pending_invoice_id': False, 'snapshot_invoice_signature': False})
            open_inv = None
        if open_inv and partner.snapshot_invoice_signature == signature:
            return open_inv
        if open_inv:
            try:
                open_inv.button_cancel()
            except Exception:
                _logger.exception("Could not cancel snapshot invoice %s", open_inv.id)
            self.search([('pending_invoice_id', '=', open_inv.id)]).write(
                {'pending_invoice_id': False, 'pending_period_end': False})
            partner.write({'snapshot_pending_invoice_id': False, 'snapshot_invoice_signature': False,
                           'snapshot_billing_warned': False})
        if not lines:
            return self.env['account.move']
        return self._issue_partner_invoice(partner, lines, signature)

    @api.model
    def _issue_partner_invoice(self, partner, lines, signature):
        """One invoice with one line per snapshot period. Wallet (all or
        nothing) → saved card → left open and the customer is emailed."""
        Instance = self.env['saas.instance'].sudo()
        product = Instance._get_billing_product()
        total = round(sum(a for _s, a, _l, _e in lines), 2)
        order_lines = [(0, 0, {'product_id': product.id, 'name': label,
                               'product_uom_qty': 1, 'price_unit': round(amount, 2)})
                       for _snap, amount, label, _end in lines]
        wallet = self.env['saas.wallet'].for_partner(partner, create=False)
        wallet_amount = 0.0
        if total > 0 and wallet and wallet.balance >= total:
            wallet._lock()
            if wallet.balance >= total:
                wallet_amount = total
                order_lines.append((0, 0, {
                    'product_id': product.id, 'name': _('Wallet credit applied — snapshots'),
                    'product_uom_qty': 1, 'price_unit': -wallet_amount}))
        order_vals = {'partner_id': partner.id,
                      'origin': ORIGIN_SNAPSHOT % ('%s:%s' % (partner.id, fields.Date.today())),
                      'order_line': order_lines}
        pricelist = partner.property_product_pricelist
        if pricelist:
            order_vals['pricelist_id'] = pricelist.id
        order = self.env['sale.order'].sudo().create(order_vals)
        order.action_confirm()
        invoice = order._create_invoices()
        # Due when the first prepaid period runs out (today for a new snapshot).
        due = min(((s.paid_until or fields.Date.today()) if not s.unbilled_amount else fields.Date.today())
                  for s, _a, _l, _e in lines)
        invoice.invoice_date_due = max(due, fields.Date.today())
        invoice.action_post()
        if wallet_amount:
            wallet._consume(wallet_amount, origin='invoice_consumption',
                            reason=_('Applied to invoice %s') % (invoice.name or ''), move=invoice)
        for snap, _amount, _label, period_end in lines:
            snap.write({'pending_invoice_id': invoice.id, 'pending_period_end': period_end})
        partner.write({'snapshot_pending_invoice_id': invoice.id,
                       'snapshot_invoice_signature': signature,
                       'snapshot_billing_warned': False})
        if invoice.amount_total <= 0:
            self.search([('pending_invoice_id', '=', invoice.id)])._apply_snapshot_payment()
            return invoice
        anchor = Instance.search([('partner_id', 'child_of', partner.id),
                                  ('state', 'in', ('running', 'stopped', 'suspended'))], limit=1)
        if anchor and anchor._auto_renew_method():
            try:
                if anchor._try_auto_charge_invoice(invoice, kind='snapshot'):
                    return invoice
            except Exception:
                _logger.exception("Snapshot invoice %s: auto-charge failed", invoice.id)
        lines[0][0]._notify('saas_billing.mail_template_saas_snapshot_invoice')
        return invoice

    def _apply_snapshot_payment(self):
        """The customer's snapshot invoice was paid: prepay every snapshot
        on it up to its period end and advance the billing day."""
        partners = self.env['res.partner']
        for rec in self:
            end = rec.pending_period_end or rec.unbilled_period_end
            vals = {'pending_invoice_id': False, 'pending_period_end': False,
                    'unbilled_amount': 0.0, 'unbilled_period_end': False}
            if end:
                vals['paid_until'] = end
            rec.write(vals)
            partners |= rec._billing_partner()
        for partner in partners:
            paid_until = max((s.paid_until for s in self._partner_live_snapshots(partner) if s.paid_until),
                             default=False)
            partner.write({'snapshot_pending_invoice_id': False, 'snapshot_invoice_signature': False,
                           'snapshot_billing_warned': False,
                           **({'snapshot_billing_date': paid_until} if paid_until else {})})

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
        """Daily: refresh each customer's single snapshot invoice (renewals
        a month in advance), chase unpaid ones, delete after the grace."""
        today = fields.Date.today()
        grace = datetime.timedelta(days=self._snapshot_grace_days())
        partners = self.sudo().search([('source', '=', 'snapshot'), ('state', '=', 'done'),
                                       ('monthly_price', '>', 0)]).mapped('partner_id').mapped('commercial_partner_id')
        for partner in partners:
            try:
                invoice = self._rebuild_partner_invoice(partner)
                if invoice and invoice.state == 'posted' and invoice.payment_state not in ('paid', 'in_payment'):
                    due = invoice.invoice_date_due or today
                    on_it = self.search([('pending_invoice_id', '=', invoice.id)])
                    if today > due + grace:
                        for snap in on_it:
                            snap._notify('saas_billing.mail_template_saas_snapshot_deleted')
                        on_it.action_delete_snapshot()
                    elif today > due and not partner.snapshot_billing_warned:
                        partner.snapshot_billing_warned = True
                        if on_it:
                            on_it[0]._notify('saas_billing.mail_template_saas_snapshot_overdue')
            except Exception:
                _logger.exception("Snapshot billing failed for partner %s", partner.id)
            if not getattr(threading.current_thread(), 'testing', False):
                self.env.cr.commit()
