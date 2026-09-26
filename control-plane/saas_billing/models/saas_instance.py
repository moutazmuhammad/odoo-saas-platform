import logging
import math

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Untranslated technical tokens used as sale.order.origin so the dunning
# and renewal lookups work regardless of UI language. These must never be
# wrapped in _() — they are matched verbatim by _get_all_invoices().
ORIGIN_INITIAL = 'SAAS:INITIAL:%s'
ORIGIN_RENEWAL = 'SAAS:RENEWAL:%s'
ORIGIN_SUBSCRIPTION = 'SAAS:SUBSCRIPTION:%s'
ORIGIN_PLAN_UPGRADE = 'SAAS:UPGRADE:%s'
ORIGIN_DATA_RESTORATION = 'SAAS:RESTORATION:%s'
ORIGIN_BACKUP_ADDON = 'SAAS:BACKUP-ADDON:%s'
ORIGIN_COMPUTE_TIER = 'SAAS:COMPUTE-TIER:%s'
ORIGIN_STORAGE_BLOCK = 'SAAS:STORAGE-BLOCK:%s'
# Adding a Staging/Development environment server: the prorated activation
# invoice that gates provisioning of a child env (mirrors STORAGE-BLOCK).
ORIGIN_ENVIRONMENT = 'SAAS:ENVIRONMENT:%s'
# Origins considered "optional" for dunning purposes (won't trigger suspension).
# Daily-backup add-on is opt-in — a missed payment for it shouldn't take down
# the whole instance the customer still uses every day.
OPTIONAL_INVOICE_ORIGIN_PREFIXES = (
    'SAAS:SUBSCRIPTION:', 'SAAS:UPGRADE:', 'SAAS:BACKUP-ADDON:',
    # Buying storage blocks is opt-in — an unpaid block invoice must not
    # suspend the workspace (the customer simply doesn't get the extra room).
    'SAAS:STORAGE-BLOCK:',
    # Adding an env server is opt-in — an unpaid env invoice must not suspend
    # the project; the child simply stays unprovisioned until paid.
    'SAAS:ENVIRONMENT:',
    # A compute-tier upgrade is opt-in — an unpaid upgrade invoice must not
    # suspend the instance; it simply stays on its current tier until paid.
    'SAAS:COMPUTE-TIER:',
)
# Days past a daily-backup add-on invoice's due date before snapshots are
# paused. Snapshots resume automatically once the invoice is paid.
DAILY_BACKUP_SUSPEND_GRACE_DAYS = 3

# v47: ALL promotions and discounts are removed. The only pricing
# variation is monthly vs annual (computed in saas.pricing.engine). There
# is no trial promo, no promo cycles, no stacking.

# Auto-renew retry schedule (A1): days AFTER the invoice DUE date on which the
# saved payment method is re-charged if still unpaid. The renewal-date
# attempt is day 0; these are the follow-ups. All fall inside the default
# 7-day grace period, so suspension only happens after the last retry.
PAYMENT_RETRY_OFFSET_DAYS = (1, 3, 5)

# Auto-renew reminders (A1): days BEFORE the renewal date to notify.
RENEWAL_REMINDER_OFFSET_DAYS = (7, 1)


class SaasInstance(models.Model):
    """The commercial half of saas.instance (same table as saas_core's).

    Sale order / invoice links, billing period, pending and scheduled plan
    changes, renewal and dunning, auto-renew with a saved payment method,
    add-on purchases (daily backup, compute tier, storage blocks,
    environment slots), the wallet plumbing and the per-tenant margin.
    saas_core keeps identity, provisioning and lifecycle and reaches this
    code only through the no-op hooks it declares ("Billing hooks" in
    saas_core/models/saas_instance.py), overridden at the end of this
    class.
    """
    _inherit = 'saas.instance'

    support_plan_id = fields.Many2one(
        'saas.support.plan',
        string='Support Plan',
        ondelete='restrict',
        default=lambda self: self.env['saas.support.plan']._get_default(),
        help='Paid support tier for this instance (P3). A flat monthly fee '
             'billed alongside the plan; not scaled by region. Defaults to '
             'the free best-effort tier; the customer can pick a higher one '
             'at create / upgrade.',
    )

    # Saved payment method chosen for auto-renew (A1). Provider-agnostic
    # wrapper that stores ONLY safe references (provider id + external
    # customer / token refs); the legacy ``payment_token_id`` is kept in
    # sync for the existing charge path.
    saas_payment_method_id = fields.Many2one(
        'saas.payment.method', string='Auto-renew Payment Method',
        copy=False, ondelete='set null',
    )

    # ========== Sales & Invoicing ==========
    sale_order_id = fields.Many2one(
        'sale.order',
        string='Sale Order',
        tracking=True,
        ondelete='set null',
        index=True,
        help='Sale order linked to this instance.',
    )

    restoration_invoice_id = fields.Many2one(
        'account.move',
        string='Restoration Invoice',
        readonly=True,
        ondelete='set null',
        help='Unpaid restoration fee invoice. Instance is suspended until paid.',
    )
    restoration_backup_id = fields.Many2one(
        'saas.instance.backup',
        string='Snapshot to Restore',
        readonly=True,
        ondelete='set null',
        help='Retained snapshot restored onto this instance once the '
             'restoration invoice is paid.',
    )

    sale_order_count = fields.Integer(
        string='Sale Orders',
        compute='_compute_sale_order_count',
    )

    invoice_count = fields.Integer(
        string='Invoices',
        compute='_compute_invoice_count',
    )

    daily_backup_pending_invoice_id = fields.Many2one(
        'account.move',
        string='Daily Backup Pending Invoice',
        tracking=True,
        copy=False,
        ondelete='set null',
        help='Unpaid invoice gating the activation of daily backups. '
             'Set when the customer clicks "Enable Daily Backups" on the '
             'portal; cleared (and daily_backup_enabled flipped to True) '
             'as soon as the invoice transitions to paid / in_payment.',
    )

    # Backup billing follows the main subscription's billing_period and is
    # aligned to the plan renewal date, so a monthly plan bills the add-on
    # monthly and a yearly plan bills it yearly (12x up-front), merged into
    # the plan renewal invoice.
    daily_backup_next_invoice_date = fields.Date(
        string='Daily Backup Next Invoice',
        copy=False,
        help='When the next daily-backup charge is due. The add-on follows '
             'the subscription period and is aligned to the plan renewal '
             'date, so this normally equals ``next_invoice_date`` and the '
             'charge is merged into the plan renewal invoice.',
    )

    daily_backup_last_invoice_date = fields.Date(
        string='Daily Backup Last Invoice',
        copy=False,
        help='Most recent month the daily-backup add-on was billed for.',
    )

    pending_compute_tier_id = fields.Many2one(
        'saas.compute.tier',
        string='Pending Compute Tier',
        copy=False,
        ondelete='set null',
        help='Target tier of an in-progress UPGRADE, gated by '
             'compute_tier_pending_invoice_id. Set when the customer '
             'requests an upgrade; cleared (and compute_tier_id updated) '
             'once the invoice is paid and the scale succeeds.',
    )

    compute_tier_pending_invoice_id = fields.Many2one(
        'account.move',
        string='Compute Tier Pending Invoice',
        tracking=True,
        copy=False,
        ondelete='set null',
        help='Unpaid invoice gating a compute-tier UPGRADE (downgrades are '
             'immediate and free — see action_change_compute_tier). '
             'Cleared once the invoice transitions to paid / in_payment.',
    )

    # ---------- Saved card + auto-renewal ----------
    # ``payment_token_id`` holds the saved card that renewal crons
    # charge automatically. It's captured the first time the customer
    # pays an activation invoice with "Save my card" ticked. The
    # customer can clear it at any time from the portal billing
    # settings; clearing it disables both auto-renew toggles.
    payment_token_id = fields.Many2one(
        'payment.token',
        string='Saved Card',
        copy=False,
        ondelete='set null',
        help='Card used for auto-renewal. Captured on the first '
             'tokenized activation payment; cleared when the customer '
             'removes it from billing settings.',
    )

    auto_renew_subscription = fields.Boolean(
        string='Auto-renew Subscription',
        copy=False,
        default=True,
        help='When enabled and a saved card is on file, the monthly / '
             'yearly subscription invoice is charged automatically on '
             'renewal. When disabled the invoice is still issued, but '
             'the customer pays it manually.',
    )

    auto_renew_daily_backup = fields.Boolean(
        string='Auto-renew Daily Backups',
        copy=False,
        default=True,
        help='Legacy toggle. The daily-backup charge now rides on the '
             'subscription renewal invoice, so it is auto-charged together '
             'with the plan whenever Auto-renew Subscription is on and a '
             'card is on file; there is no separate backup invoice to '
             'auto-charge.',
    )

    # ========== Billing Period (per-instance) ==========
    billing_period = fields.Selection(
        [('monthly', 'Monthly'), ('yearly', 'Yearly')],
        string='Billing Period',
        default='monthly',
        help='Billing cycle chosen by the client for this instance.',
    )

    # ========== Pending Upgrade (awaiting payment) ==========
    pending_plan_id = fields.Many2one(
        'saas.plan',
        string='Pending Upgrade Plan',
        ondelete='set null',
        help='Plan the client has chosen but not yet paid for. '
             'Applied automatically once payment is confirmed.',
    )

    pending_billing_period = fields.Selection(
        [('monthly', 'Monthly'), ('yearly', 'Yearly')],
        string='Pending Billing Period',
    )

    pending_change_invoice_id = fields.Many2one(
        'account.move',
        string='Pending Change Invoice',
        ondelete='set null',
        copy=False,
        help='Invoice the client must pay for the pending plan change. '
             'Explicit link so the payment hook and the safety-net cron '
             'can match the payment exactly, even if sale_order_id is '
             'later overwritten by another flow.',
    )

    # ========== Scheduled Downgrade ==========
    scheduled_plan_id = fields.Many2one(
        'saas.plan',
        string='Scheduled Downgrade Plan',
        ondelete='set null',
        help='Lower plan to switch to at the end of the current billing cycle.',
    )

    scheduled_billing_period = fields.Selection(
        [('monthly', 'Monthly'), ('yearly', 'Yearly')],
        string='Scheduled Billing Period',
    )

    # ========== Recurring Billing ==========
    next_invoice_date = fields.Date(
        string='Next Invoice Date',
        tracking=True,
        index=True,
        help='Date on which the next recurring invoice will be generated. '
             'Set automatically after the first payment.',
    )

    last_invoice_date = fields.Date(
        string='Last Invoice Date',
        readonly=True,
    )

    suspension_warning_sent = fields.Boolean(
        default=False,
        help='Whether a suspension warning email has been sent for the '
             'current overdue period.',
    )

    # Auto-renew reminder flags (A1) — reset every cycle in
    # ``_set_next_invoice_date`` / on renewal so the 7-day and 1-day
    # notices fire once per period.
    renewal_reminder_7d_sent = fields.Boolean(default=False, copy=False)

    renewal_reminder_1d_sent = fields.Boolean(default=False, copy=False)

    # Wallet (A4): surplus prepaid value to move into the customer's
    # wallet when a pending plan change is actually applied (on payment),
    # so unused subscription value is never forfeited on an upgrade.
    pending_wallet_credit = fields.Float(
        string='Pending Wallet Credit', copy=False, default=0.0,
    )

    pending_storage_blocks = fields.Integer(
        string='Pending Storage Blocks', copy=False, default=0,
        help='Blocks from a storage purchase awaiting payment; added to '
             'extra_storage_blocks once the activation invoice is paid.',
    )

    storage_block_pending_invoice_id = fields.Many2one(
        'account.move', string='Storage Block Invoice', copy=False,
        ondelete='set null',
    )

    # --- Mid-cycle slot RESERVATION (buy capacity without a server) -------
    # The customer reserves N Staging/Development slots (paid, no repo needed,
    # no server created). Within the reserved count he then creates/deletes
    # servers freely — deleting frees a slot for reuse, it never refunds or
    # lowers the count. These hold a reservation purchase until its invoice
    # is paid, at which point the slots are granted (mirrors storage blocks).
    env_pending_invoice_id = fields.Many2one(
        'account.move', string='Environment Activation Invoice', copy=False,
        ondelete='set null',
        help='On a Staging/Development child: the prorated activation invoice '
             'that gates its provisioning. Cleared once paid.')
    reserved_staging_pending = fields.Integer(
        string='Pending Reserved Staging Slots', copy=False, default=0,
        help='Staging slots to grant once the reservation invoice is paid.')

    reserved_dev_pending = fields.Integer(
        string='Pending Reserved Development Slots', copy=False, default=0,
        help='Development slots to grant once the reservation invoice is paid.')

    slot_reservation_pending_invoice_id = fields.Many2one(
        'account.move', string='Slot Reservation Invoice', copy=False,
        ondelete='set null',
        help='The prorated invoice for a mid-cycle slot reservation. The slots '
             'are granted once it is paid; cleared then.')

    # ===== Phase 4: per-tenant margin (revenue − infra cost) =====
    # store=True (billing/pricing architecture redesign, Profitability
    # dashboard): the dashboard's list/pivot/graph views sort and group by
    # these fields, and Odoo's ORM refuses to generate SQL ORDER BY / most
    # read_group aggregation for a non-stored field ("Cannot convert ...
    # to SQL because it is not stored") — this crashed the dashboard the
    # moment it was opened. Lives here (not saas_core) because revenue
    # reads billing-owned fields (plan price, billing_period, support plan).
    margin_currency_id = fields.Many2one(
        'res.currency', compute='_compute_margin', string='Margin Currency',
        store=True)

    monthly_cost = fields.Monetary(
        string='Infra Cost / month', compute='_compute_margin',
        currency_field='margin_currency_id', store=True,
        help='Phase 4: provisioned CPU/RAM + used storage × this server\'s rate '
             'card. For a Production env, includes its child (staging/dev) costs.')

    monthly_revenue = fields.Monetary(
        string='Revenue / month', compute='_compute_margin',
        currency_field='margin_currency_id', store=True,
        help='Monthly-equivalent recurring revenue (plan + support, period-normalized). '
             'Child environments bill via the parent, so their own revenue is 0.')

    monthly_margin = fields.Monetary(
        string='Margin / month', compute='_compute_margin',
        currency_field='margin_currency_id', store=True,
        help='Revenue − infra cost. Negative = this tenant loses money.')

    margin_pct = fields.Float(
        string='Margin %', compute='_compute_margin', store=True,
        help='Margin as a percentage of revenue.')

    is_profitable = fields.Boolean(
        string='Profitable', compute='_compute_margin', store=True,
        search='_search_profitable',
        help='True when monthly margin ≥ 0.')

    def _instance_infra_cost(self):
        """Own monthly infra cost from the server rate card (excludes children)."""
        self.ensure_one()
        srv = self.docker_server_id
        if not srv:
            return 0.0
        plan = self.plan_id
        cpu = plan.cpu_limit or 0.0
        ram_gb = (self._parse_ram_string(plan.ram_limit) / (1024 ** 3)) if plan and plan.ram_limit else 0.0
        storage_gb = self.storage_used_gb or 0.0
        return (cpu * (srv.cost_per_cpu_month or 0.0)
                + ram_gb * (srv.cost_per_gb_ram_month or 0.0)
                + storage_gb * (srv.cost_per_gb_storage_month or 0.0))

    def _instance_monthly_revenue(self):
        """Monthly-equivalent recurring revenue. Children bill via the parent, so
        they contribute 0 (their cost rolls up to the parent's margin)."""
        self.ensure_one()
        if self.parent_id:
            return 0.0
        plan = self.plan_id
        if not plan:
            return 0.0
        base = (plan.yearly_price or plan.price * 12) / 12.0 \
            if self.billing_period == 'yearly' else plan.price
        # Support is a flat monthly price (per the pricing rules: support/backup
        # are flat ×12 for yearly), so it's the same per month regardless of period.
        support = self.support_plan_id.monthly_price if self.support_plan_id else 0.0
        return (base or 0.0) + (support or 0.0)

    @api.depends('plan_id', 'billing_period', 'storage_used_gb',
                 'support_plan_id', 'support_plan_id.monthly_price',
                 'docker_server_id.cost_per_cpu_month',
                 'docker_server_id.cost_per_gb_ram_month',
                 'docker_server_id.cost_per_gb_storage_month',
                 'child_env_ids.storage_used_gb', 'child_env_ids.plan_id')
    def _compute_margin(self):
        company_cur = self.env.company.currency_id
        engine = self.env['saas.pricing.engine']
        for rec in self:
            rec.margin_currency_id = (rec.plan_id.currency_id or company_cur)
            # Production rolls up its children's infra cost; children show their own.
            cost = rec._instance_infra_cost()
            if not rec.parent_id:
                cost += sum(c._instance_infra_cost() for c in rec.child_env_ids)
            revenue = rec._instance_monthly_revenue()
            result = engine.profitability(revenue, cost)
            rec.monthly_cost = cost
            rec.monthly_revenue = revenue
            rec.monthly_margin = result['profit']
            rec.margin_pct = result['margin_pct']
            rec.is_profitable = result['is_profitable']

    @api.model
    def _cron_flag_unprofitable_tenants(self):
        """Phase 4.3.4 alert: log + chatter-notify Production tenants whose monthly
        margin is negative, so ops can act (reprice, resize, or cut). Cheap: runs
        over billable parentless instances and posts only on a state change."""
        recs = self.search([
            ('parent_id', '=', False), ('plan_id', '!=', False),
            ('state', 'in', ('running', 'suspended'))])
        flagged = recs.filtered(lambda r: not r.is_profitable)
        for r in flagged:
            _logger.warning(
                "[margin] tenant %s is UNPROFITABLE: revenue=%.2f cost=%.2f margin=%.2f",
                r.subdomain, r.monthly_revenue, r.monthly_cost, r.monthly_margin)
            try:
                r.message_post(body=_(
                    "⚠️ Running at a loss: revenue %(rev).2f − infra cost %(cost).2f "
                    "= %(margin).2f / month. Review pricing or resources.") % {
                    'rev': r.monthly_revenue, 'cost': r.monthly_cost,
                    'margin': r.monthly_margin})
            except Exception:
                pass
        return len(flagged)

    def _search_profitable(self, operator, value):
        # Lightweight search: compute on the candidate set (paid, parentless).
        recs = self.search([('parent_id', '=', False), ('plan_id', '!=', False)])
        ids = [r.id for r in recs if r.is_profitable]
        want = value if operator == '=' else not value
        return [('id', 'in' if want else 'not in', ids)]

    # ========== Sales & Invoicing Actions ==========

    @api.depends('sale_order_id')
    def _compute_sale_order_count(self):
        for rec in self:
            rec.sale_order_count = 1 if rec.sale_order_id else 0

    @api.depends('sale_order_id', 'sale_order_id.invoice_ids', 'partner_id', 'name', 'subdomain')
    def _compute_invoice_count(self):
        for rec in self:
            rec.invoice_count = len(rec._get_all_invoices())

    def _instance_origin_tokens(self):
        """Return the list of untranslated origin tokens this instance uses."""
        self.ensure_one()
        ref = self.name or self.subdomain
        if not ref:
            return []
        return [
            ORIGIN_INITIAL % ref,
            ORIGIN_RENEWAL % ref,
            ORIGIN_SUBSCRIPTION % ref,
            ORIGIN_PLAN_UPGRADE % ref,
            ORIGIN_DATA_RESTORATION % ref,
            ORIGIN_BACKUP_ADDON % ref,
        ]

    def _get_all_invoices(self):
        """Return all invoices related to this instance across all sale orders.

        Matches sale orders by exact untranslated origin tokens. Also
        accepts legacy translated origins ("Renewal: …", "Subscription: …"
        in any language) for backwards compatibility with records created
        before token-based origins were introduced.
        """
        self.ensure_one()
        instance_ref = self.name or self.subdomain
        if not instance_ref:
            return self.env['account.move']
        expected_origins = self._instance_origin_tokens() + [instance_ref]
        domain = [
            ('partner_id', '=', self.partner_id.id),
            '|',
            ('origin', 'in', expected_origins),
            ('id', '=', self.sale_order_id.id if self.sale_order_id else 0),
        ]
        sale_orders = self.env['sale.order'].search(domain)
        # Defensive: also pick up legacy SOs where origin contains the
        # instance ref preceded by a known label in any locale.
        if not sale_orders or self.sale_order_id not in sale_orders:
            legacy = self.env['sale.order'].search([
                ('partner_id', '=', self.partner_id.id),
                ('origin', 'ilike', instance_ref),
            ])
            sale_orders |= legacy.filtered(
                lambda s: s.origin and instance_ref in s.origin
            )
        if not sale_orders:
            return self.env['account.move']
        return sale_orders.mapped('invoice_ids')

    # Invoice origin prefixes the client may NOT cancel — these are
    # mandatory (the dunning system enforces payment). Everything else
    # (plan upgrades, the daily-backup add-on, …) is optional and the
    # client can decline it from the portal.
    _NON_CANCELLABLE_INVOICE_PREFIXES = (
        'SAAS:INITIAL:', 'SAAS:RENEWAL:', 'SAAS:RESTORATION:',
        # Legacy translated prefixes (pre token-based origins).
        'Renewal:', 'Data restoration:',
    )

    def _invoice_is_client_cancellable(self, invoice):
        """True if the client may cancel this unpaid invoice (it's optional,
        not an initial subscription / renewal / restoration)."""
        self.ensure_one()
        if not invoice or invoice.state != 'posted':
            return False
        if invoice.payment_state in ('paid', 'in_payment'):
            return False
        # A never-deployed order still awaiting its first payment can always be
        # abandoned by the client ("don't complete the purchase"): nothing was
        # provisioned, so cancelling just frees the subdomain. The
        # non-cancellable rule below only guards LIVE instances (renewals /
        # restorations the dunning system must enforce).
        if self.state in ('pending_payment', 'draft'):
            return True
        origins = invoice.line_ids.sale_line_ids.order_id.mapped('origin')
        return not any(
            o and any(o.startswith(p) for p in self._NON_CANCELLABLE_INVOICE_PREFIXES)
            for o in origins
        )

    def _get_cancellable_unpaid_invoice(self):
        """The single unpaid, client-cancellable invoice for this instance
        (or empty recordset). Used to offer a "Decline / Cancel" action
        instead of nagging the customer to pay forever."""
        self.ensure_one()
        for inv in self._get_all_invoices().filtered(
            lambda i: i.state == 'posted'
            and i.payment_state not in ('paid', 'in_payment')
            and i.amount_residual > 0
        ).sorted('create_date', reverse=True):
            if self._invoice_is_client_cancellable(inv):
                return inv
        return self.env['account.move']

    def action_client_cancel_invoice(self, invoice):
        """Client declines an optional unpaid invoice: cancel it, undo any
        pending plan change, and — if the instance was never deployed
        (draft / pending_payment) — cancel the instance so the subdomain is
        freed. Returns a short status string: 'cancelled' | 'instance_cancelled'.
        Raises UserError if the invoice isn't client-cancellable."""
        self.ensure_one()
        if not self._invoice_is_client_cancellable(invoice):
            raise UserError(_(
                "This invoice is required and can't be cancelled. Please "
                "complete the payment or contact support."
            ))
        from markupsafe import Markup
        invoice.button_cancel()

        if self.pending_plan_id:
            self._append_log("Pending upgrade cancelled by client.")
            self.message_post(body=Markup(
                "<b>Client cancelled plan upgrade payment</b><br/>"
                "Was upgrading to: <b>%s</b><br/>Invoice: %s"
            ) % (self.pending_plan_id.name, invoice.name))
            try:
                self._send_notification(
                    'saas_billing.mail_template_saas_payment_cancelled')
            except Exception:
                _logger.exception("payment-cancelled notice failed for %s", self.id)
            self.write({
                'pending_plan_id': False,
                'pending_billing_period': False,
                'pending_change_invoice_id': False,
            })

        if self.state in ('pending_payment', 'draft'):
            subdomain = self.name or self.subdomain
            self.write({
                'state': 'cancelled_by_client',
                'cancellation_reason': (
                    "Client declined the initial order before payment.\n"
                    "Invoice: %s\nSubdomain: %s" % (invoice.name, subdomain)
                ),
            })
            self._append_log("Order declined by client. Subdomain released.")
            try:
                self._send_notification(
                    'saas_billing.mail_template_saas_payment_cancelled')
            except Exception:
                _logger.exception("order-cancelled notice failed for %s", self.id)
            return 'instance_cancelled'
        return 'cancelled'

    def _get_daily_backup_product(self):
        """Return the singleton product.product for the daily-backup add-on.

        Created on first use. Used both in the one-time purchase
        invoice (when the customer enables the feature) and as a
        recurring line on subsequent renewal invoices.
        """
        product = self.env['product.product'].sudo().search(
            [('default_code', '=', 'SAAS-BACKUP-ADDON')], limit=1,
        )
        if not product:
            product = self.env['product.product'].sudo().create({
                'name': 'Daily Backups Add-on',
                'default_code': 'SAAS-BACKUP-ADDON',
                'type': 'service',
                'list_price': 0.0,
                'sale_ok': True,
                'purchase_ok': False,
                'taxes_id': [(5, 0, 0)],
            })
        return product

    def _get_daily_backup_price(self):
        """Monthly price of the daily-backup add-on for THIS instance.

        Usage-based: the storage actually consumed is rounded UP to the
        next whole GB and charged at the configured per-GB rate
        (``saas_master.snapshot_price_per_gb``, default $0.40/GB), 1 GB
        minimum. "Consumed" = the deduplicated snapshot repo footprint
        (what the snapshots really occupy in the bucket); before the
        first snapshot exists (activation invoice) we fall back to the
        instance's measured used storage, since that is what the first
        snapshot will capture. Re-evaluated on every monthly renewal, so
        the charge follows the customer's data over time.
        """
        self.ensure_one()
        # Delegate to the pricing engine so the checkout quote, the portal
        # and the recurring invoice all charge the SAME number.
        used_bytes = self._snapshot_total_bytes() or self.total_storage_bytes or 0
        return self.env['saas.pricing.engine'].daily_backup_price(
            used_bytes=used_bytes,
        )

    def _get_compute_tier_product(self):
        """Return the singleton product.product for a compute-tier upgrade
        invoice. Created on first use, same as the daily-backup product —
        one generic product, the actual tier name/price go on the order
        line (see action_change_compute_tier)."""
        product = self.env['product.product'].sudo().search(
            [('default_code', '=', 'SAAS-COMPUTE-TIER')], limit=1,
        )
        if not product:
            product = self.env['product.product'].sudo().create({
                'name': 'Compute Tier Upgrade',
                'default_code': 'SAAS-COMPUTE-TIER',
                'type': 'service',
                'list_price': 0.0,
                'sale_ok': True,
                'purchase_ok': False,
                'taxes_id': [(5, 0, 0)],
            })
        return product

    def _get_retained_snapshot_fee(self):
        """One-off charge for restoring the snapshot retained after the
        instance was deleted.

        Computed, not configured: the number of months the snapshot sat
        in cloud storage after cancellation × its size rounded UP to the
        next whole GB × the per-GB monthly rate
        (``saas_master.snapshot_price_per_gb``). A started month counts
        as a whole month (minimum 1). No retained snapshot → 0.
        """
        self.ensure_one()
        retained = self.retained_snapshot()
        if not retained:
            return 0.0
        per_gb = self.env['saas.pricing.engine'].snapshot_price_per_gb()
        if per_gb <= 0:
            return 0.0
        gb = max(1, math.ceil((retained.size_mb or 0.0) / 1024.0))
        months = 1
        if retained and retained.create_date:
            # A5: whole CALENDAR months retained (no days/30 approximation).
            months = max(1, self.env['saas.pricing.engine'].months_between(
                retained.create_date.date(), fields.Date.today()))
        # This is a FIXED one-off fee — never touched by the yearly
        # subscription discount (A5 discount scope).
        return round(months * gb * per_gb, 2)

    def action_purchase_daily_backup(self):
        """Create an unpaid invoice for the backup add-on.

        Sequence:
        1. Sale order with a single ``Daily Backups Add-on`` line at
           the monthly add-on price from settings.
        2. Confirm SO → create + post the invoice.
        3. Store it on ``daily_backup_pending_invoice_id``. Once it
           transitions to ``paid``/``in_payment``, the
           ``account.move.write`` override below flips
           ``daily_backup_enabled`` to True and clears the pointer.

        Caller (portal route) then redirects to our custom checkout
        page where the customer pays.
        """
        self.ensure_one()
        # Snapshots are available to BOTH hosting and managed-services
        # instances (the one app-level add-on a service gets); only trials
        # are excluded.
        if self.is_trial:
            raise UserError(_(
                "Daily backups can't be purchased on a trial plan."
            ))
        if self.daily_backup_enabled:
            raise UserError(_(
                "Daily backups are already enabled on this instance."
            ))
        if self.daily_backup_pending_invoice_id:
            existing = self.daily_backup_pending_invoice_id
            if existing.state == 'posted' and existing.payment_state not in (
                'paid', 'in_payment', 'reversed', 'invoicing_legacy',
            ):
                # An unpaid invoice already exists — return it instead
                # of creating a second one. Portal redirects there.
                return existing
            # Old invoice is paid (shouldn't happen — hook would have
            # cleared this), or cancelled. Clear and re-issue.
            self.daily_backup_pending_invoice_id = False

        monthly_price = self._get_daily_backup_price()
        if monthly_price <= 0:
            raise UserError(_(
                "Daily backup pricing isn't configured. Ask the "
                "platform operator to set the monthly add-on price "
                "in SaaS settings."
            ))

        # The add-on now follows the SUBSCRIPTION's billing period so it can
        # ride on the plan's renewal invoice from here on. Enabling mid-cycle
        # is a one-off, PRORATED catch-up charge for the time left until the
        # plan renews (monthly plan → rest of this month; yearly plan → rest
        # of this year), after which the account.move payment hook aligns the
        # add-on's next-invoice date to the plan's renewal date and every
        # future charge is merged into the renewal (one bill, same period).
        today = fields.Date.today()
        period = self.billing_period or 'monthly'
        period_label = 'Yearly' if period == 'yearly' else 'Monthly'
        months = self.env['saas.pricing.engine'].period_months(period)
        full = months * monthly_price
        charge = full
        if self.next_invoice_date and self.last_invoice_date:
            total_days = (self.next_invoice_date - self.last_invoice_date).days
            left = (self.next_invoice_date - today).days
            if total_days > 0 and 0 < left < total_days:
                charge = round(full * left / total_days, 2)

        product = self._get_daily_backup_product()
        pricelist = self.partner_id.property_product_pricelist
        line_name = _(
            'Daily Backups Add-on (%s) — %s (prorated to your renewal date)'
        ) % (period_label, self.name or self.subdomain)
        order_lines = [(0, 0, {
            'product_id': product.id,
            'name': line_name,
            'product_uom_qty': 1,
            'price_unit': charge,
        })]

        order_vals = {
            'partner_id': self.partner_id.id,
            'origin': ORIGIN_BACKUP_ADDON % (self.name or self.subdomain),
            'order_line': order_lines,
        }
        if pricelist:
            order_vals['pricelist_id'] = pricelist.id
        order = self.env['sale.order'].sudo().create(order_vals)
        order.action_confirm()
        invoice = order._create_invoices()
        invoice.action_post()
        self.write({'daily_backup_pending_invoice_id': invoice.id})
        self._append_log(
            "Daily-backup add-on activation invoice %s created — %s period, "
            "prorated catch-up %.2f (full %.2f)." % (
                invoice.name, period_label, charge, full)
        )
        return invoice

    def action_change_compute_tier(self, tier_id):
        """Change this instance's compute tier (Standard/HA/Scale/...).

        A tier with MORE replicas than the current one is an upgrade: it
        costs more, so it's gated behind a prorated activation invoice —
        same shape as ``action_purchase_daily_backup`` — and the actual
        scale only happens once that invoice is paid (see the
        ``account.move`` payment hook). A tier with FEWER replicas (or an
        equally/less expensive one, including any free tier) applies
        immediately with no charge and no refund for the current period —
        there is nothing to gate payment behind.
        """
        self.ensure_one()
        if self.docker_server_id.compute_driver != 'kubernetes':
            raise UserError(_(
                "Compute tiers require the Kubernetes backend."
            ))
        tier = self.env['saas.compute.tier'].sudo().browse(tier_id)
        if not tier.exists() or not tier.active:
            raise UserError(_("That compute tier is not available."))
        current = self.compute_tier_id
        if current == tier:
            raise UserError(_(
                "'%s' is already on the %s tier."
            ) % (self.subdomain, tier.name))

        current_replicas = current.replicas if current else 1
        if tier.monthly_price <= 0 or tier.replicas <= current_replicas:
            # Free tier, downgrade, or lateral move — nothing to charge.
            self.env['saas.job'].enqueue(
                self, '_do_scale_compute_tier', args=(tier.id,),
                channel='deploy', lock_key='instance:%s' % self.id,
                max_attempts=1, idempotent=False,
                on_error='_on_compute_tier_scale_error')
            self._append_log(
                "Compute tier change to '%s' (%d replica(s)) queued — no "
                "charge." % (tier.name, tier.replicas))
            return True

        # Upgrade to a priced tier: pay first, same proration math as the
        # daily-backup/HA activation flow.
        if self.is_trial:
            raise UserError(_(
                "Compute tier upgrades can't be purchased on a trial plan."
            ))
        if self.compute_tier_pending_invoice_id:
            existing = self.compute_tier_pending_invoice_id
            if existing.state == 'posted' and existing.payment_state not in (
                'paid', 'in_payment', 'reversed', 'invoicing_legacy',
            ):
                return existing
            self.compute_tier_pending_invoice_id = False

        today = fields.Date.today()
        period = self.billing_period or 'monthly'
        period_label = 'Yearly' if period == 'yearly' else 'Monthly'
        months = self.env['saas.pricing.engine'].period_months(period)
        full = months * tier.monthly_price
        charge = full
        if self.next_invoice_date and self.last_invoice_date:
            total_days = (self.next_invoice_date - self.last_invoice_date).days
            left = (self.next_invoice_date - today).days
            if total_days > 0 and 0 < left < total_days:
                charge = round(full * left / total_days, 2)

        product = self._get_compute_tier_product()
        pricelist = self.partner_id.property_product_pricelist
        line_name = _(
            'Compute Tier Upgrade: %s (%s) — %s (prorated to your renewal date)'
        ) % (tier.name, period_label, self.name or self.subdomain)
        order_lines = [(0, 0, {
            'product_id': product.id,
            'name': line_name,
            'product_uom_qty': 1,
            'price_unit': charge,
        })]

        order_vals = {
            'partner_id': self.partner_id.id,
            'origin': ORIGIN_COMPUTE_TIER % (self.name or self.subdomain),
            'order_line': order_lines,
        }
        if pricelist:
            order_vals['pricelist_id'] = pricelist.id
        order = self.env['sale.order'].sudo().create(order_vals)
        order.action_confirm()
        invoice = order._create_invoices()
        invoice.action_post()
        self.write({
            'compute_tier_pending_invoice_id': invoice.id,
            'pending_compute_tier_id': tier.id,
        })
        self._append_log(
            "Compute tier upgrade to '%s' activation invoice %s created — "
            "%s period, prorated catch-up %.2f (full %.2f)." % (
                tier.name, invoice.name, period_label, charge, full)
        )
        return invoice

    def _get_billing_product(self):
        """Return the default product.product used on SaaS sale order lines.

        Creates it on first use if it doesn't exist yet.
        """
        product = self.env['product.product'].sudo().search(
            [('default_code', '=', 'SAAS-SUB')], limit=1,
        )
        if not product:
            product = self.env['product.product'].sudo().create({
                'name': 'SaaS Subscription',
                'default_code': 'SAAS-SUB',
                'type': 'service',
                'list_price': 0.0,
                'sale_ok': True,
                'purchase_ok': False,
                'taxes_id': [(5, 0, 0)],
            })
        return product

    def _support_order_line(self, period, period_label):
        """Sale-order line tuple for the instance's support plan, or None.

        Support is a flat MONTHLY fee billed on the same cycle as the plan;
        on a yearly plan it's charged x12 (qty=12) so the support term
        matches the plan term. The free/default plan (price 0) adds nothing,
        so this is behaviour-neutral until support is priced and picked."""
        self.ensure_one()
        support = self.support_plan_id
        if not support or support.monthly_price <= 0:
            return None
        months = 12 if period == 'yearly' else 1
        return (0, 0, {
            'product_id': self._get_billing_product().id,
            'name': _('Support: %s (%s) — %s') % (
                support.name, period_label, self.name or self.subdomain,
            ),
            'product_uom_qty': months,
            'price_unit': support.monthly_price,
        })

    def _snapshot_order_line(self, period=None):
        """Sale-order line tuple for the daily-backup add-on over ONE billing
        period, or None. The add-on now follows the SUBSCRIPTION's period
        (like the support plan): a monthly plan bills one month (qty 1); a
        yearly plan bills the whole year up-front (qty 12) at the same per-
        month rate — so it can be folded into the plan's renewal invoice and
        the customer gets a single, period-aligned bill. The per-GB rate is
        a flat monthly fee and is NEVER discounted on yearly billing.

        ``period`` defaults to the instance's own billing period. Price is
        storage-aware + lock-aware via ``_get_daily_backup_price``.

        Snapshots apply to BOTH hosting and services subscribers (the only
        app-level feature a managed services instance gets); the gate is the
        subscription flag, not the product type."""
        self.ensure_one()
        if not self.daily_backup_enabled:
            return None
        price = self._get_daily_backup_price()
        if price <= 0:
            return None
        period = period or self.billing_period or 'monthly'
        months = self.env['saas.pricing.engine'].period_months(period)
        period_label = 'Yearly' if period == 'yearly' else 'Monthly'
        return (0, 0, {
            'product_id': self._get_daily_backup_product().id,
            'name': _('Daily Backups Add-on (%s) — %s') % (
                period_label, self.name or self.subdomain,
            ),
            'product_uom_qty': months,
            'price_unit': price,
        })

    def _compute_tier_order_line(self, period, period_label):
        """Sale-order line tuple for the instance's compute tier, or None.

        Same shape as ``_support_order_line`` — once a priced tier is
        selected it's simply billed every renewal at its flat monthly
        price, same cycle as the plan (x12 on a yearly plan). No separate
        next-invoice-date tracking needed: unlike the daily-backup add-on
        (which can be turned on mid-cycle independently), a tier change
        is always synced to now via action_change_compute_tier's own
        proration, so it's already aligned to the plan's cycle by the
        time the first renewal rolls around. The default (free) tier
        adds nothing, so this is behaviour-neutral until a priced tier is
        picked."""
        self.ensure_one()
        tier = self.compute_tier_id
        if not tier or tier.monthly_price <= 0:
            return None
        months = 12 if period == 'yearly' else 1
        return (0, 0, {
            'product_id': self._get_compute_tier_product().id,
            'name': _('Compute Tier: %s (%s) — %s') % (
                tier.name, period_label, self.name or self.subdomain,
            ),
            'product_uom_qty': months,
            'price_unit': tier.monthly_price,
        })

    # ==================================================================
    #  Storage blocks (v47) — a PURCHASED recurring add-on that expands
    #  capacity. Never an automatic usage charge.
    # ==================================================================
    def _get_storage_block_product(self):
        product = self.env['product.product'].sudo().search(
            [('default_code', '=', 'SAAS-STORAGE-BLOCK')], limit=1)
        if not product:
            product = self.env['product.product'].sudo().create({
                'name': 'Extra Storage Block',
                'default_code': 'SAAS-STORAGE-BLOCK',
                'type': 'service', 'list_price': 0.0,
                'sale_ok': True, 'purchase_ok': False, 'taxes_id': [(5, 0, 0)],
            })
        return product

    def _storage_block_order_line(self, period, blocks=None):
        """Recurring SO line for PURCHASED storage blocks on the given period
        (qty = blocks × months), or None. Block price is a flat monthly fee —
        billed ×months on yearly, never discounted (only resources are)."""
        self.ensure_one()
        blocks = self.extra_storage_blocks if blocks is None else blocks
        if blocks <= 0:
            return None
        block_gb, block_price = self.env['saas.pricing.engine'].storage_block_config()
        if block_gb <= 0 or block_price <= 0:
            return None
        months = self.env['saas.pricing.engine'].period_months(period)
        return (0, 0, {
            'product_id': self._get_storage_block_product().id,
            'name': _('Storage: %d × %d GB block(s) (%s) — %s') % (
                blocks, block_gb,
                'Yearly' if period == 'yearly' else 'Monthly',
                self.name or self.subdomain),
            'product_uom_qty': blocks * months,
            'price_unit': block_price,
        })

    def action_purchase_storage_block(self, qty=1):
        """Buy ``qty`` storage blocks: issue a prorated activation invoice for
        the remainder of the current cycle; on payment, increment
        ``extra_storage_blocks`` (raising effective capacity). Recurs on every
        renewal thereafter via ``_storage_block_order_line``."""
        self.ensure_one()
        qty = max(1, int(qty or 1))
        block_gb, block_price = self.env['saas.pricing.engine'].storage_block_config()
        if block_gb <= 0 or block_price <= 0:
            raise UserError(_(
                "Storage blocks aren't available yet. Please contact support "
                "or upgrade your plan to expand capacity."))
        # Prorate the first charge over the days left in the current cycle so
        # the customer only pays for what's left until it joins the renewal.
        months = self.env['saas.pricing.engine'].period_months(
            self.billing_period or 'monthly')
        full = qty * block_price * months
        charge = full
        if self.next_invoice_date and self.last_invoice_date:
            total_days = (self.next_invoice_date - self.last_invoice_date).days
            left = (self.next_invoice_date - fields.Date.today()).days
            if total_days > 0 and 0 < left < total_days:
                charge = round(full * left / total_days, 2)
        pricelist = self.partner_id.property_product_pricelist
        order_lines = [(0, 0, {
            'product_id': self._get_storage_block_product().id,
            'name': _('Add %d × %d GB storage block(s) — %s (prorated)') % (
                qty, block_gb, self.name or self.subdomain),
            'product_uom_qty': 1,
            'price_unit': charge,
        })]
        wallet_line, wallet_amount = self._wallet_credit_line(order_lines)
        if wallet_line:
            order_lines.append(wallet_line)
        order_vals = {
            'partner_id': self.partner_id.id,
            'origin': ORIGIN_STORAGE_BLOCK % (self.name or self.subdomain),
            'order_line': order_lines,
        }
        if pricelist:
            order_vals['pricelist_id'] = pricelist.id
        order = self.env['sale.order'].sudo().create(order_vals)
        order.action_confirm()
        invoice = order._create_invoices()
        invoice.action_post()
        self._wallet_settle_consumption(invoice, wallet_amount)
        # Remember how many blocks this purchase activates on payment.
        self.write({
            'pending_storage_blocks': (self.pending_storage_blocks or 0) + qty,
            'storage_block_pending_invoice_id': invoice.id,
        })
        self._append_log(
            "Storage block purchase: %d × %d GB, activation invoice %s (%.2f)."
            % (qty, block_gb, invoice.name, charge))
        if invoice.amount_total <= 0:
            self._activate_pending_storage_blocks()
        return invoice

    def _activate_pending_storage_blocks(self):
        """Apply purchased blocks after payment: raise capacity + clear the
        capacity warning immediately (instant 'Fix Now' recovery)."""
        self.ensure_one()
        pending = self.pending_storage_blocks or 0
        if pending <= 0:
            return
        self.write({
            'extra_storage_blocks': (self.extra_storage_blocks or 0) + pending,
            'pending_storage_blocks': 0,
            'storage_block_pending_invoice_id': False,
        })
        self._append_log("Storage capacity expanded by %d block(s)." % pending)
        self.message_post(body=_(
            "Storage expanded — your workspace now has more room. Thanks for "
            "scaling with us."))
        # Re-evaluate so a paused/at-capacity workspace recovers at once.
        try:
            if self.state == 'suspended':
                self.action_reactivate() if hasattr(self, 'action_reactivate') else None
            self._evaluate_capacity()
        except Exception:
            _logger.exception("Capacity re-eval after block activation failed for %s",
                              self.subdomain)

    def action_release_storage_block(self, qty=1):
        """Release ``qty`` storage blocks at the next cycle (lowers capacity +
        recurring charge). Blocked if releasing would drop capacity below
        current usage (same 75% headroom guard as a plan downgrade)."""
        self.ensure_one()
        qty = max(1, int(qty or 1))
        if qty > (self.extra_storage_blocks or 0):
            raise UserError(_("You don't have that many storage blocks."))
        block_gb, _bp = self.env['saas.pricing.engine'].storage_block_config()
        new_cap = self.effective_storage_limit_gb - qty * block_gb
        used = (self.total_storage_bytes or 0.0) / (1024 ** 3)
        if new_cap > 0 and used >= self.DOWNGRADE_THRESHOLD * new_cap:
            raise UserError(_(
                "You're using too much storage to release that capacity. "
                "Free up space first, then you can release storage."))
        self.write({'extra_storage_blocks': self.extra_storage_blocks - qty})
        self._append_log("Released %d storage block(s)." % qty)
        return True

    def _env_server_price(self, period=None):
        """Per-server price of a Staging/Development server for THIS project
        (lowest spec, region-scaled, × env_price_factor) for ``period``. Single
        source of truth via the pricing engine — used by the configurator, the
        one-click create flow, and the recurring renewal line."""
        self.ensure_one()
        anchor = self._env_anchor()
        period = period or anchor.billing_period or 'monthly'
        return self.env['saas.pricing.engine'].env_server_price(
            billing=period,
            region=anchor.region_id.id if anchor.region_id else None)

    def _environment_order_lines(self, period):
        """Recurring SO lines for the project's purchased Staging/Development
        SLOTS — one line per slot at the lowest-spec env price. Billing
        follows the entitlement (what the customer paid for), not how many
        children they've actually spun up: creating within the slots is
        free, so renewal must bill slots, not live servers. Only meaningful
        on the Production anchor; children never self-bill."""
        self.ensure_one()
        if self.environment != 'production':
            return []
        price = self._env_server_price(period)
        if price <= 0:
            return []
        labels = dict(self._fields['environment'].selection)
        period_label = 'Yearly' if period == 'yearly' else 'Monthly'
        lines = []
        for env_type, count in (('staging', self.staging_slots or 0),
                                ('development', self.dev_slots or 0)):
            for i in range(max(0, count)):
                lines.append((0, 0, {
                    'product_id': self._get_billing_product().id,
                    'name': _('%s slot #%d (%s)') % (
                        labels.get(env_type, env_type), i + 1, period_label),
                    'product_uom_qty': 1,
                    'price_unit': price,
                }))
        return lines

    def _initial_environment_order_lines(self, period, period_label):
        """Initial-invoice lines for the env servers chosen at checkout
        (counts in pending_staging_count/pending_dev_count). One line per
        server at the lowest-spec env price for ``period``."""
        self.ensure_one()
        if self.environment != 'production':
            return []
        price = self._env_server_price(period)
        if price <= 0:
            return []
        labels = dict(self._fields['environment'].selection)
        lines = []
        for env_type, count in (
                ('staging', self.pending_staging_count or 0),
                ('development', self.pending_dev_count or 0)):
            for i in range(max(0, count)):
                lines.append((0, 0, {
                    'product_id': self._get_billing_product().id,
                    'name': _('%s server #%d (%s) — %s') % (
                        labels.get(env_type, env_type), i + 1, period_label,
                        self.name or self.subdomain),
                    'product_uom_qty': 1,
                    'price_unit': price,
                }))
        return lines

    def action_reserve_environment_slots(self, env_type, qty=1):
        """Reserve (buy) ``qty`` Staging/Development slots on the project — paid
        capacity, NO Git repo required and NO server created. Prorated for the
        remainder of the cycle; on payment the slots are granted and the
        customer can create/delete servers within them freely. Mirrors
        ``action_purchase_storage_block``."""
        self.ensure_one()
        if self.environment != 'production':
            raise UserError(_(
                "Reserve environment slots from the Production server."))
        if self.is_trial:
            raise UserError(_(
                "Upgrade to a paid plan before reserving environment slots."))
        if env_type not in ('staging', 'development'):
            raise UserError(_("Unknown environment type."))
        qty = max(1, int(qty or 1))
        period = self.billing_period or 'monthly'
        full = self._env_server_price(period) * qty
        charge = full
        if self.next_invoice_date and self.last_invoice_date:
            total_days = (self.next_invoice_date - self.last_invoice_date).days
            left = (self.next_invoice_date - fields.Date.today()).days
            if total_days > 0 and 0 < left < total_days:
                charge = round(full * left / total_days, 2)
        label = dict(self._fields['environment'].selection).get(
            env_type, env_type)
        pricelist = self.partner_id.property_product_pricelist
        order_lines = [(0, 0, {
            'product_id': self._get_billing_product().id,
            'name': _('Reserve %d %s slot(s) — %s (prorated)') % (
                qty, label, self.name or self.subdomain),
            'product_uom_qty': 1,
            'price_unit': charge,
        })]
        wallet_line, wallet_amount = self._wallet_credit_line(order_lines)
        if wallet_line:
            order_lines.append(wallet_line)
        order_vals = {
            'partner_id': self.partner_id.id,
            'origin': ORIGIN_ENVIRONMENT % ('%d %s slot(s)' % (qty, label)),
            'order_line': order_lines,
        }
        if pricelist:
            order_vals['pricelist_id'] = pricelist.id
        order = self.env['sale.order'].sudo().create(order_vals)
        order.action_confirm()
        invoice = order._create_invoices()
        invoice.action_post()
        self._wallet_settle_consumption(invoice, wallet_amount)
        field = ('reserved_staging_pending' if env_type == 'staging'
                 else 'reserved_dev_pending')
        self.write({
            field: (self[field] or 0) + qty,
            'slot_reservation_pending_invoice_id': invoice.id,
        })
        self._append_log(
            "Reserve %d %s slot(s) — invoice %s (%.2f)."
            % (qty, label, invoice.name, charge))
        if invoice.amount_total <= 0:
            self._activate_reserved_slots()
            return {'auto_provisioned': True, 'reserved': qty}
        if self._auto_renew_method() and self._try_auto_charge_invoice(
                invoice, kind='subscription'):
            return {'auto_provisioned': True, 'reserved': qty}
        return {
            'auto_provisioned': False,
            'invoice_id': invoice.id,
            'checkout_url': '/my/instances/%s/checkout' % self.id,
        }

    def _activate_reserved_slots(self):
        """Grant the pending reserved slots after their invoice is paid."""
        self.ensure_one()
        staging = max(0, self.reserved_staging_pending or 0)
        dev = max(0, self.reserved_dev_pending or 0)
        if not (staging or dev):
            return
        self.write({
            'staging_slots': (self.staging_slots or 0) + staging,
            'dev_slots': (self.dev_slots or 0) + dev,
            'reserved_staging_pending': 0,
            'reserved_dev_pending': 0,
            'slot_reservation_pending_invoice_id': False,
        })
        self._append_log(
            "Reserved slots granted: %d staging, %d development." % (staging, dev))
        self.message_post(body=_(
            "Environment slots reserved — create servers within them anytime."))

    def action_release_environment_slots(self, env_type, qty=1):
        """Release (give up) ``qty`` reserved Staging/Development slots, lowering
        the recurring charge. Only FREE slots (reserved minus in-use) can be
        released — delete a server first to free its slot. The unused portion of
        the current cycle is credited back to the wallet."""
        self.ensure_one()
        if self.environment != 'production':
            raise UserError(_(
                "Release environment slots from the Production server."))
        if env_type not in ('staging', 'development'):
            raise UserError(_("Unknown environment type."))
        qty = max(1, int(qty or 1))
        slots = self._env_slots_for(env_type)
        used = self._env_used_for(env_type)
        free = slots - used
        if qty > free:
            label = dict(self._fields['environment'].selection).get(
                env_type, env_type)
            raise UserError(_(
                "Only %d free %s slot(s) can be released (the rest are in use). "
                "Delete a server first to free its slot.") % (free, label))
        field = ('staging_slots' if env_type == 'staging' else 'dev_slots')
        self.write({field: max(0, (self[field] or 0) - qty)})
        # Credit back the unused portion of the cycle for each released slot.
        remaining, _d, _t = self._proration_credit(
            self._env_server_price() * qty)
        if remaining > 0:
            self._grant_wallet_credit(
                remaining, origin='environment_slot_release',
                reason=_('Released %d %s slot(s)') % (qty, env_type))
        self._append_log(
            "Released %d %s slot(s) (now %d reserved)."
            % (qty, env_type, self[field]))
        return {'released': qty, 'slots': self[field]}

    # ==================================================================
    #  Wallet credit (A4) helpers
    #  ─ the single, reusable plumbing every invoice-creating flow uses
    #    so prepaid value is never lost and the promo / wallet are shown
    #    explicitly on the invoice as their own lines.
    # ==================================================================
    def _wallet(self, create=True):
        """This instance's customer wallet (commercial partner)."""
        self.ensure_one()
        return self.env['saas.wallet'].for_partner(
            self.partner_id, create=create)

    @staticmethod
    def _order_lines_subtotal(order_lines):
        """Sum of (qty × unit) over a list of (0,0,vals) SO-line commands."""
        total = 0.0
        for cmd in order_lines:
            vals = cmd[2] if len(cmd) > 2 and isinstance(cmd[2], dict) else {}
            total += (vals.get('price_unit') or 0.0) * (
                vals.get('product_uom_qty') or 0.0)
        return round(total, 2)

    def _wallet_credit_line(self, order_lines):
        """Negative SO line that applies available wallet credit to these
        lines, capped at their positive subtotal. Returns ``(line, amount)``
        or ``(None, 0.0)``. Locks the wallet so the amount shown on the
        invoice is exactly what gets debited (no race with another flow).
        The caller MUST call ``_wallet_settle_consumption`` after the
        invoice is posted."""
        self.ensure_one()
        wallet = self._wallet(create=False)
        if not wallet or wallet.balance <= 0:
            return None, 0.0
        wallet._lock()
        subtotal = self._order_lines_subtotal(order_lines)
        if subtotal <= 0:
            return None, 0.0
        amount = round(min(wallet.balance, subtotal), 2)
        if amount <= 0:
            return None, 0.0
        line = (0, 0, {
            'product_id': self._get_billing_product().id,
            'name': _('Wallet credit applied — %s') % (
                self.name or self.subdomain),
            'product_uom_qty': 1,
            'price_unit': -amount,
        })
        return line, amount

    def _wallet_settle_consumption(self, invoice, amount):
        """Debit the wallet for credit applied to ``invoice`` and link the
        ledger entry to the move (so it can be refunded if cancelled)."""
        self.ensure_one()
        if amount <= 0 or not invoice:
            return
        self._wallet(create=True)._consume(
            amount, origin='invoice_consumption',
            reason=_('Applied to invoice %s') % (invoice.name or ''),
            move=invoice, instance=self)

    def _grant_wallet_credit(self, amount, origin, reason=''):
        """Move unused/forfeited subscription value into the wallet so it is
        never lost (A4). No-op for non-positive amounts."""
        self.ensure_one()
        if (amount or 0.0) <= 0:
            return
        wallet = self._wallet(create=True)
        wallet._credit(round(amount, 2), origin=origin, reason=reason,
                       instance=self)
        self._append_log(
            "Wallet credited %.2f (%s). New balance: %.2f."
            % (amount, origin, wallet.balance))
        self.message_post(body=_(
            "%.2f added to your wallet — %s. Wallet balance: %.2f."
        ) % (amount, reason or origin, wallet.balance))

    def _void_unpaid_invoices_refund_credit(self):
        """Cancel every still-unpaid posted invoice for this instance,
        returning any wallet credit that was RESERVED on them.

        Wallet credit is reserved (consumed) when an invoice is posted so the
        figure shown on the invoice is exactly what gets debited (no race with
        a concurrent flow). That reservation is correct while the invoice can
        still be paid, but when the instance is cancelled those invoices are
        abandoned — without this, the reserved credit would be stranded
        forever. ``account.move.button_cancel`` already refunds the consumed
        credit idempotently (``_saas_refund_wallet_credit``), so cancelling
        the abandoned invoices makes the customer whole."""
        self.ensure_one()
        unpaid = self._get_all_invoices().filtered(
            lambda m: m.move_type == 'out_invoice'
            and m.state == 'posted'
            and m.payment_state not in ('paid', 'in_payment')
            and m.amount_residual > 0
        )
        for inv in unpaid:
            try:
                inv.button_cancel()
            except Exception:
                _logger.exception(
                    "Failed to void unpaid invoice %s while cancelling %s — "
                    "wallet credit may need a manual refund.",
                    inv.name, self.subdomain,
                )

    def action_confirm_and_bill(self):
        """Validate instance, create sale order, confirm it, and generate invoice.

        This is the single entry point for the billing flow:
        draft → pending_payment (or paid if zero-amount).
        """
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_("Instance must be in Draft state to confirm and bill."))
        if self.sale_order_id:
            raise UserError(_("A sale order already exists for this instance."))
        if not self.partner_id:
            raise UserError(_("Please set a customer before confirming."))
        if not self.plan_id:
            raise UserError(_("Please set a plan before confirming."))

        plan = self.plan_id
        period = self.billing_period or 'monthly'
        price = plan.get_price_for_period(period)
        period_label = 'Monthly' if period == 'monthly' else 'Yearly'
        # -- Build order lines (respect partner pricelist when available) --
        pricelist = self.partner_id.property_product_pricelist
        order_lines = [(0, 0, {
            'product_id': self._get_billing_product().id,
            'name': _('%s (%s) — %s') % (plan.name, period_label, self.name or self.subdomain),
            'product_uom_qty': 1,
            'price_unit': price,
        })]

        # Support plan (P5): bill it on the initial invoice too.
        support_line = self._support_order_line(period, period_label)
        if support_line:
            order_lines.append(support_line)

        # Daily-backup add-on chosen at checkout (daily_backup_enabled set at
        # instance creation). It's a flat MONTHLY fee, so charge one full
        # month upfront here and anchor its monthly cycle when the instance
        # is marked paid (see _set_next_invoice_date). Without this the
        # feature was switched on but never billed.
        if self.daily_backup_enabled:
            snapshot_line = self._snapshot_order_line()
            if snapshot_line:
                order_lines.append(snapshot_line)

        # Odoo.sh-style environments: Staging/Development servers chosen at
        # checkout (counts in pending_staging_count/pending_dev_count). Billed
        # one full period upfront here; the servers are spawned on payment and
        # then ride the renewal (see _environment_order_lines).
        order_lines += self._initial_environment_order_lines(period, period_label)

        # Wallet (A4): consume any available account credit on this invoice.
        wallet_line, wallet_amount = self._wallet_credit_line(order_lines)
        if wallet_line:
            order_lines.append(wallet_line)

        # -- Create & confirm sale order --
        order_vals = {
            'partner_id': self.partner_id.id,
            'origin': ORIGIN_INITIAL % (self.name or self.subdomain),
            'order_line': order_lines,
        }
        if pricelist:
            order_vals['pricelist_id'] = pricelist.id
        order = self.env['sale.order'].create(order_vals)
        order.action_confirm()
        self.sale_order_id = order

        # -- Create & post invoice --
        invoice = order._create_invoices()
        invoice.action_post()
        self._wallet_settle_consumption(invoice, wallet_amount)

        # -- Transition state & auto-deploy --
        if invoice.amount_total <= 0:
            self.state = 'paid'
            self._set_next_invoice_date()
            self._append_log(
                "Sale order %s confirmed. Zero-amount invoice — deploying automatically."
                % order.name
            )
            self.message_post(body=_(
                "Sale order %s confirmed. No payment required — deploying now."
            ) % order.name)
            self.action_deploy()
            return True
        else:
            self.state = 'pending_payment'
            self._append_log(
                "Sale order %s confirmed. Invoice %s awaiting payment. "
                "Instance will deploy automatically once paid."
                % (order.name, invoice.name)
            )
            self.message_post(body=_(
                "Sale order %s confirmed. Invoice %s created and awaiting payment. "
                "Instance will deploy automatically once paid."
            ) % (order.name, invoice.name))

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': invoice.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_mark_as_paid(self):
        """Manually mark as paid and auto-deploy (for wire transfers, trials, etc.)."""
        self.ensure_one()
        if self.state != 'pending_payment':
            raise UserError(_("Instance must be in 'Pending Payment' state."))
        self.state = 'paid'
        # Initialise the recurring billing schedule. Without this the
        # renewal cron never picks up the instance and the customer gets
        # an indefinite free subscription.
        self._set_next_invoice_date()
        self._append_log("Manually marked as paid. Deploying automatically.")
        self.message_post(body=_("Manually marked as paid — deploying now."))
        self.action_deploy()

    def action_view_sale_order(self):
        """Open the linked sale order."""
        self.ensure_one()
        if not self.sale_order_id:
            return False
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'sale.order',
            'res_id': self.sale_order_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_view_invoices(self):
        """Open all invoices related to this instance."""
        self.ensure_one()
        invoices = self._get_all_invoices()
        if not invoices:
            return False
        if len(invoices) == 1:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'account.move',
                'res_id': invoices.id,
                'view_mode': 'form',
                'target': 'current',
            }
        return {
            'type': 'ir.actions.act_window',
            'name': _('Invoices'),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('id', 'in', invoices.ids)],
            'target': 'current',
        }

    # ========== Recurring Billing ==========

    def _set_next_invoice_date(self):
        """Compute and write the next invoice date based on the instance billing period."""
        self.ensure_one()
        if not self.plan_id or self.is_trial:
            return
        today = fields.Date.today()
        period = self.billing_period or 'monthly'
        if period == 'yearly':
            interval = relativedelta(years=1)
        else:
            interval = relativedelta(months=1)
        self.next_invoice_date = today + interval
        self.last_invoice_date = today
        self.suspension_warning_sent = False
        # New cycle anchor: re-arm the auto-renew reminders (A1).
        self.renewal_reminder_7d_sent = False
        self.renewal_reminder_1d_sent = False

        # Anchor the daily-backup add-on's billing cycle the first time the
        # instance is activated with backups already enabled (e.g. the
        # customer ticked "daily backups" at checkout). The add-on now follows
        # the PLAN's period: the activation invoice already charged the whole
        # period (1 month / 12 months — see ``action_confirm_and_bill`` →
        # ``_snapshot_order_line``), so the next backup charge falls on the
        # plan's renewal date and is merged into that renewal. Only set it
        # once, and only when a price is actually configured — otherwise the
        # renewal cron would log "price not configured" every day.
        if (self.daily_backup_enabled
                and not self.daily_backup_next_invoice_date
                and self._get_daily_backup_price() > 0):
            self.daily_backup_last_invoice_date = today
            self.daily_backup_next_invoice_date = self.next_invoice_date

        # The compute tier needs no equivalent anchoring — see
        # _compute_tier_order_line's docstring: it's always billed on the
        # SAME cycle as the plan, no independent next-invoice-date to
        # align.

    # ============================================================
    # Saved card + auto-renewal
    # ============================================================
    def _capture_payment_token_from_invoice(self, invoice):
        """Persist the SAFE references of the tokenized method used to pay
        ``invoice`` so auto-renew can charge it later (A1).

        PCI scope: NOTHING sensitive is stored. We persist only the
        provider id + external customer ref + external token ref via
        ``saas.payment.method`` (which wraps Odoo's ``payment.token`` —
        itself SAQ-A: a provider-side reference plus a masked label). The
        token only exists when the customer opted to save the method
        (Odoo's ``tokenize`` flag), so retention stays customer-controlled.

        Accepts ``done`` and ``pending`` transactions: a token row is
        created before a 3DS-pending tx terminates, and it's gated by
        ``active`` so a tx that ultimately fails is never used."""
        self.ensure_one()
        if not invoice or self.saas_payment_method_id:
            # Don't overwrite an existing saved method — switching methods
            # is an explicit portal action.
            return
        tx = invoice.transaction_ids.filtered(
            lambda t: t.state in ('done', 'pending') and t.token_id
            and t.token_id.active
        )[:1]
        if not tx:
            return
        method = self.env['saas.payment.gateway'].save_method_from_transaction(
            self.partner_id, tx)
        if not method:
            return
        # Keep the legacy pointer in sync for the existing charge path and
        # make this the instance's auto-renew method.
        self.write({
            'saas_payment_method_id': method.id,
            'payment_token_id': method.token_id.id,
        })
        self._append_log(
            "Payment method saved for auto-renewal: %s (provider %s). You "
            "can remove or replace it any time from Billing settings."
            % (method.display_label or 'card', method.provider_code or '—')
        )

    def _try_auto_charge_invoice(self, invoice, kind):
        """Attempt to auto-charge ``invoice`` using the saved card.

        ``kind`` is 'subscription' or 'snapshot' — used only for log
        prefixes so the operator can tell renewal flows apart in
        the journal.

        Returns ``True`` only when the transaction reaches the
        terminal ``done`` state. ``pending`` (e.g. 3DS in flight) is
        treated as "wait and re-check" — the caller suppresses the
        payment-due email in that case so the customer isn't pinged
        for an invoice that may still settle. A genuine failure
        returns ``False`` and leaves the invoice unpaid for dunning.

        Pre-flight checks:
        - token must be active and its provider must be enabled, or
          we surface a "Please add a new card" message instead of
          letting Odoo throw deep in ``_send_payment_request``.
        - invoice currency must match the token's provider — a
          mismatch would charge the wrong amount or fail; we log
          and skip rather than try.
        """
        self.ensure_one()
        if not invoice or invoice.payment_state in ('paid', 'in_payment'):
            return False
        method = self._auto_renew_method()
        if not method:
            self._record_payment_attempt(
                invoice, 'failed',
                _("No saved payment method on file."))
            self._append_log(
                "Auto-renew skipped for invoice %s — no saved payment "
                "method. Please add one from Billing settings." % invoice.name)
            return False
        # Delegate the actual charge to the provider-agnostic gateway.
        state, message = self.env['saas.payment.gateway'].charge(method, invoice)
        self._record_payment_attempt(invoice, state, message)
        if state == 'done':
            self._append_log(
                "Auto-renew charged %s for invoice %s (%.2f %s)." % (
                    method.display_label or 'saved method', invoice.name,
                    invoice.amount_total, invoice.currency_id.name))
            return True
        if state == 'pending':
            # In-flight (3DS / async gateway). Suppress the payment-due
            # email; the dunning + retry crons revisit a stuck pending.
            _logger.info(
                "[AUTO-RENEW:%s] pending for %s invoice %s.",
                kind, self.subdomain, invoice.name)
            self._append_log(
                "Auto-renew for invoice %s is awaiting bank confirmation."
                % invoice.name)
            return True
        # Failure — leave the invoice for the retry schedule + dunning.
        _logger.info(
            "[AUTO-RENEW:%s] charge failed for %s invoice %s: %s",
            kind, self.subdomain, invoice.name, message)
        self._append_log(
            "Auto-renew charge for invoice %s did not go through (%s)."
            % (invoice.name, message))
        return False

    def _auto_renew_method(self):
        """The saved payment method auto-renew should use: the instance's
        chosen method, else the customer's default. Empty if none/disabled."""
        self.ensure_one()
        method = self.saas_payment_method_id
        if method and method.active and method.token_id and method.token_id.active:
            return method
        return self.env['saas.payment.method'].default_for_partner(
            self.partner_id)

    def _record_payment_attempt(self, invoice, state, message=''):
        """Append a ``saas.payment.attempt`` audit row for the retry trail."""
        self.ensure_one()
        if not invoice:
            return
        prior = self.env['saas.payment.attempt'].sudo().search_count(
            [('move_id', '=', invoice.id)])
        self.env['saas.payment.attempt'].sudo().create({
            'move_id': invoice.id,
            'instance_id': self.id,
            'attempt_no': prior + 1,
            'attempted_on': fields.Date.today(),
            'state': state,
            'message': (message or '')[:500],
        })

    # ========== Auto-renew reminders + retry schedule (A1) ==========
    @api.model
    def _cron_send_renewal_reminders(self):
        """Notify customers 7 days and 1 day before their renewal date so
        they (and their saved card) are ready for the auto-charge."""
        today = fields.Date.today()
        for offset, flag in (
            (7, 'renewal_reminder_7d_sent'),
            (1, 'renewal_reminder_1d_sent'),
        ):
            target = today + relativedelta(days=offset)
            instances = self.search([
                ('state', '=', 'running'),
                ('is_trial', '=', False),
                ('plan_id', '!=', False),
                # Children (staging/dev) never self-bill — the project's
                # Production anchor carries the whole subscription.
                ('parent_id', '=', False),
                ('next_invoice_date', '=', target),
                (flag, '=', False),
            ])
            for instance in instances:
                try:
                    instance._send_notification(
                        'saas_billing.mail_template_saas_renewal_reminder')
                    instance.write({flag: True})
                    self.env.cr.commit()
                except Exception:
                    self.env.cr.rollback()
                    _logger.exception(
                        "Renewal reminder (%dd) failed for %s",
                        offset, instance.subdomain)

    @api.model
    def _cron_retry_failed_payments(self):
        """Re-attempt the auto-charge of still-unpaid mandatory invoices on
        the retry schedule (1, 3 and 5 days after the invoice date). Runs
        within the grace period, so suspension only follows the LAST retry
        (handled by the dunning cron)."""
        today = fields.Date.today()
        instances = self.search([
            ('state', 'in', ('running', 'stopped')),
            ('is_trial', '=', False),
            ('parent_id', '=', False),
            ('auto_renew_subscription', '=', True),
        ])
        for instance in instances:
            try:
                instance._retry_failed_payments(today)
                self.env.cr.commit()
            except Exception:
                self.env.cr.rollback()
                _logger.exception(
                    "Payment retry failed for %s", instance.subdomain)

    @staticmethod
    def _payment_due_date(move):
        """THE single payment timing anchor (v47/F4): every retry, reminder,
        dunning and suspension decision derives from this — the invoice due
        date, falling back to the invoice date. No other date math is used
        for payment timing anywhere, so the schedules can never diverge."""
        return move.invoice_date_due or move.invoice_date

    def _retry_failed_payments(self, today):
        """Retry every still-unpaid MANDATORY invoice whose age (measured
        from its DUE date — F4) matches the retry schedule and that hasn't
        already been attempted today."""
        self.ensure_one()
        if not self._auto_renew_method():
            return
        Attempt = self.env['saas.payment.attempt'].sudo()
        invoices = self._get_all_invoices().filtered(
            lambda m: m.move_type == 'out_invoice'
            and m.state == 'posted'
            and m.payment_state not in ('paid', 'in_payment')
            and self._payment_due_date(m)
            and not self._is_optional_invoice(m)
        )
        for inv in invoices:
            age = (today - self._payment_due_date(inv)).days
            if age not in PAYMENT_RETRY_OFFSET_DAYS:
                continue
            if Attempt.search_count([
                    ('move_id', '=', inv.id), ('attempted_on', '=', today)]):
                continue  # already tried today
            self._append_log(
                "Auto-renew retry (due+%d) for invoice %s." % (age, inv.name))
            self._try_auto_charge_invoice(inv, kind='retry')

    @api.model
    def _cron_generate_recurring_invoices(self):
        """Generate renewal invoices for running instances whose billing
        cycle has elapsed.  Skips trials and instances without a plan."""
        today = fields.Date.today()
        instances = self.search([
            ('state', '=', 'running'),
            ('is_trial', '=', False),
            ('plan_id', '!=', False),
            ('parent_id', '=', False),
            ('next_invoice_date', '<=', today),
        ])
        for instance in instances:
            try:
                instance._generate_renewal_invoice()
                self.env.cr.commit()
            except Exception:
                self.env.cr.rollback()
                _logger.exception(
                    "Failed to generate renewal invoice for %s",
                    instance.subdomain,
                )

    def _paid_pending_change_invoice(self):
        """Paid invoice for the current pending plan change, or empty.

        Prefers the explicit ``pending_change_invoice_id`` link. Falls
        back (for changes requested before that field existed) to the
        newest posted invoice of ``sale_order_id`` — but only when that
        order's origin is an upgrade/subscription token, so an older
        paid invoice can never re-apply a NEW unpaid pending change.
        """
        self.ensure_one()
        paid = ('paid', 'in_payment')
        empty = self.env['account.move']
        inv = self.pending_change_invoice_id
        if inv:
            if inv.state == 'posted' and inv.payment_state in paid:
                return inv
            return empty
        # Legacy fallback: _request_upgrade / action_subscribe_from_trial
        # always pointed sale_order_id at the change's own order.
        ref = self.name or self.subdomain
        order = self.sale_order_id
        if not (ref and order) or order.origin not in (
            ORIGIN_PLAN_UPGRADE % ref, ORIGIN_SUBSCRIPTION % ref,
        ):
            return empty
        latest = order.invoice_ids.filtered(
            lambda m: m.state == 'posted'
        ).sorted('create_date', reverse=True)[:1]
        if latest and latest.payment_state in paid:
            return latest
        return empty

    @api.model
    def _cron_apply_paid_pending_changes(self):
        """Safety net for the payment hook: apply pending plan changes
        whose invoice is already paid.

        The normal path is ``_saas_check_instance_payment()`` firing when
        the invoice's payment_state flips, then applying the change in a
        background thread. If that is ever missed (thread killed mid-way,
        payment post-processed while the module was upgrading, hook error
        rolled back with the payment retried independently), the customer
        has paid but stays on the old plan forever — there was no retry.
        This cron converges those stragglers.
        """
        instances = self.search([('pending_plan_id', '!=', False)])
        for instance in instances:
            try:
                invoice = instance._paid_pending_change_invoice()
                if not invoice:
                    continue
                method = ('_apply_pending_upgrade' if instance.is_trial
                          else '_apply_pending_plan_change')
                _logger.info(
                    "SaaS instance %s: invoice %s for pending change to %s "
                    "is paid but the change was never applied — applying "
                    "now (safety-net cron, %s).",
                    instance.subdomain, invoice.name,
                    instance.pending_plan_id.name, method,
                )
                instance._append_log(
                    "Pending plan change invoice %s is paid but the change "
                    "was never applied — applying now (safety-net cron)."
                    % invoice.name
                )
                getattr(instance, method)()
                self.env.cr.commit()
            except Exception:
                self.env.cr.rollback()
                _logger.exception(
                    "Safety-net cron failed to apply paid pending plan "
                    "change for %s", instance.subdomain,
                )

    @api.model
    def _daily_backup_unpaid_invoices(self):
        """Posted, still-unpaid invoices that cover this instance's daily
        backups, newest first. Two sources:

        1. Standalone backup add-on invoices (origin SAAS:BACKUP-ADDON) —
           the separate monthly cycle.
        2. **Merged renewals (M4):** when the snapshot month is folded into
           the main renewal (M3), the snapshot charge lives on a
           SAAS:RENEWAL invoice as a daily-backup product line. An unpaid
           such renewal means the backup month is unpaid, so it must pause
           snapshots exactly like an unpaid standalone backup invoice.
        """
        self.ensure_one()
        sub_ref = self.name or self.subdomain

        def _unpaid(moves):
            return moves.filtered(
                lambda m: m.state == 'posted'
                and m.payment_state not in (
                    'paid', 'in_payment', 'reversed', 'invoicing_legacy',
                )
                and m.amount_residual > 0
            )

        SO = self.env['sale.order'].sudo()
        # 1) standalone backup add-on invoices
        backup_orders = SO.search([
            ('origin', '=', ORIGIN_BACKUP_ADDON % sub_ref)])
        invs = _unpaid(backup_orders.invoice_ids)

        # 2) renewal invoices carrying a merged daily-backup line
        backup_product = self.env['product.product'].sudo().search(
            [('default_code', '=', 'SAAS-BACKUP-ADDON')], limit=1)
        if backup_product:
            renewal_orders = SO.search([
                ('origin', '=', ORIGIN_RENEWAL % sub_ref)])
            merged = _unpaid(renewal_orders.invoice_ids).filtered(
                lambda m: any(
                    line.product_id == backup_product
                    for line in m.invoice_line_ids
                )
            )
            invs |= merged

        return invs.sorted('invoice_date_due')

    def _sync_daily_backup_suspension(self):
        """Pause snapshots when the monthly add-on invoice is overdue;
        resume them once it's paid. Idempotent — safe to call from the
        renewal cron and from the payment hook."""
        self.ensure_one()
        # Applies to any instance with the add-on (hosting or services).
        if not self.daily_backup_enabled:
            return
        cutoff = fields.Date.today() - relativedelta(
            days=DAILY_BACKUP_SUSPEND_GRACE_DAYS,
        )
        overdue = self._daily_backup_unpaid_invoices().filtered(
            lambda m: m.invoice_date_due and m.invoice_date_due < cutoff
        )
        should_suspend = bool(overdue)
        if should_suspend and not self.daily_backup_suspended:
            self.daily_backup_suspended = True
            self._sync_scheduled_backup()
            self._append_log(
                "Daily snapshots PAUSED — the monthly backup add-on "
                "invoice is overdue. They resume automatically once it's "
                "paid."
            )
            self.message_post(body=_(
                "Daily snapshots paused: the monthly backup add-on "
                "invoice is overdue. Snapshots resume automatically as "
                "soon as the invoice is paid."
            ))
        elif not should_suspend and self.daily_backup_suspended:
            self.daily_backup_suspended = False
            self._sync_scheduled_backup()
            self._append_log(
                "Daily snapshots RESUMED — backup add-on is paid up."
            )
            self.message_post(body=_(
                "Daily snapshots resumed — your backup add-on is paid up."
            ))

    def _cron_renew_daily_backup_addons(self):
        """Maintain the daily-backup add-on: pause/resume snapshots based on
        whether the add-on is paid up.

        The add-on no longer has a billing cycle of its own: it follows the
        SUBSCRIPTION's period and its charge is merged into the plan's renewal
        invoice (see ``_generate_renewal_invoice``), so there is no standalone
        backup invoice to issue here. This cron only enforces the pause/resume
        of snapshots when a merged renewal carrying the backup line goes
        overdue. Skips trials and instances whose backup flag is off.
        """
        instances = self.search([
            ('state', '=', 'running'),
            ('is_trial', '=', False),
            ('daily_backup_enabled', '=', True),
        ])
        for instance in instances:
            try:
                instance._sync_daily_backup_suspension()
                self.env.cr.commit()
            except Exception:
                self.env.cr.rollback()
                _logger.exception(
                    "Daily-backup add-on maintenance failed for %s",
                    instance.subdomain,
                )

    def _generate_renewal_invoice(self):
        """Create a new sale order + invoice for the next billing period.

        If a downgrade is scheduled, apply it first so the renewal
        invoice uses the new (lower) plan price.
        """
        self.ensure_one()
        if self.is_trial:
            return

        # Idempotency / concurrency guard (BIZ-009): lock this instance row so
        # two overlapping cron runs can't both bill the same cycle. After the
        # lock is granted we re-read next_invoice_date from the row — under
        # READ COMMITTED the loser sees the value the winner already advanced
        # past today and returns, so each cycle is invoiced exactly once.
        today = fields.Date.today()
        self.env.cr.execute(
            "SELECT next_invoice_date FROM saas_instance WHERE id = %s FOR UPDATE",
            (self.id,))
        locked = self.env.cr.fetchone()
        db_next = locked[0] if locked else None
        if db_next and db_next > today:
            _logger.info(
                "Renewal for %s already billed this cycle (next_invoice_date=%s) "
                "— skipping duplicate.", self.subdomain, db_next)
            return
        # Ensure ORM reads below see the freshly-locked DB state.
        self.invalidate_recordset(['next_invoice_date'])

        # Apply scheduled downgrade at cycle boundary
        if self.scheduled_plan_id:
            old_plan = self.plan_id
            new_plan = self.scheduled_plan_id
            new_period = self.scheduled_billing_period or self.billing_period or 'monthly'
            self.write({
                'plan_id': new_plan.id,
                'billing_period': new_period,
                'scheduled_plan_id': False,
                'scheduled_billing_period': False,
            })
            self._append_log(
                "Scheduled downgrade applied at cycle end: %s → %s"
                % (old_plan.name if old_plan else 'None', new_plan.name)
            )
            self.message_post(body=_(
                "Downgrade applied: switched from %s to %s."
            ) % (old_plan.name if old_plan else '—', new_plan.name))
            self.env['saas.audit.log'].saas_audit(
                'instance_scale', model='saas.instance', res_id=self.id,
                res_name=self.subdomain,
                detail='Scheduled downgrade applied: %s -> %s' % (
                    old_plan.name if old_plan else 'None', new_plan.name))

            # Update container resources for the lower plan
            if self.state in ('running', 'stopped', 'suspended'):
                try:
                    self._update_container_resources()
                except Exception as e:
                    _logger.exception(
                        "Failed to update resources after downgrade for %s",
                        self.subdomain,
                    )
                    self._append_log(
                        "WARNING: Plan downgraded but resource update failed: %s" % e)

            # Remove excess backups that exceed the new plan's lower limit
            try:
                self.env['saas.instance.backup'].cleanup_excess_for_instance(self)
            except Exception:
                _logger.exception(
                    "Failed to cleanup excess backups after downgrade for %s",
                    self.subdomain,
                )

        plan = self.plan_id
        if not plan:
            return

        period = self.billing_period or 'monthly'
        price = plan.get_price_for_period(period)
        period_label = 'Monthly' if period == 'monthly' else 'Yearly'

        pricelist = self.partner_id.property_product_pricelist
        order_lines = [(0, 0, {
            'product_id': self._get_billing_product().id,
            'name': _('%s (%s) — %s renewal') % (
                plan.name, period_label, self.name or self.subdomain,
            ),
            'product_uom_qty': 1,
            'price_unit': price,
        })]

        # Support plan (P5): a flat monthly fee billed on the SAME cycle as
        # the plan (see _support_order_line). The free/default plan and
        # unpriced plans add nothing (behaviour-neutral until configured).
        support_line = self._support_order_line(period, period_label)
        if support_line:
            order_lines.append(support_line)

        # Daily-backup add-on: the snapshot follows the PLAN's billing period
        # and is aligned to the plan's renewal date at activation, so it is
        # folded into THIS renewal for the SAME period (monthly plan → 1 month,
        # yearly plan → 12 months) whenever its due date has reached the
        # renewal. One bill, one cadence — no separate monthly backup invoice.
        # ``merge_snapshot`` is advanced together with next_invoice_date below
        # (pre-post, atomic) so the two cycles stay locked in step.
        merge_snapshot = (
            self.daily_backup_enabled
            and self.daily_backup_next_invoice_date
            and self.daily_backup_next_invoice_date <= self.next_invoice_date
        )
        if merge_snapshot:
            snapshot_line = self._snapshot_order_line(period)
            if snapshot_line:
                order_lines.append(snapshot_line)
            else:
                # price 0 / backups off between the check and here — don't
                # advance the backup date for a line we didn't add.
                merge_snapshot = False

        # Compute tier: same simple always-included shape as the support
        # plan above — no merge/alignment tracking needed (see
        # _compute_tier_order_line's docstring).
        tier_line = self._compute_tier_order_line(period, period_label)
        if tier_line:
            order_lines.append(tier_line)

        # v47: storage is billed ONLY for blocks the customer deliberately
        # PURCHASED (extra_storage_blocks) — never an automatic usage-based
        # overage. The recurring block line tracks the same period as the
        # plan (qty = blocks × months) so it matches monthly/yearly cadence.
        block_line = self._storage_block_order_line(period)
        if block_line:
            order_lines.append(block_line)

        # Odoo.sh-style environments: one recurring line per active Staging/
        # Development server in the project (lowest-spec env price). Children
        # never self-bill — their cost rides this Production renewal.
        order_lines += self._environment_order_lines(period)

        # Wallet (A4): consume available account credit on this renewal.
        wallet_line, wallet_amount = self._wallet_credit_line(order_lines)
        if wallet_line:
            order_lines.append(wallet_line)

        order_vals = {
            'partner_id': self.partner_id.id,
            'origin': ORIGIN_RENEWAL % (self.name or self.subdomain),
            'order_line': order_lines,
        }
        if pricelist:
            order_vals['pricelist_id'] = pricelist.id
        order = self.env['sale.order'].create(order_vals)
        order.action_confirm()
        # Keep sale_order_id pointing to the original SO for payment detection;
        # renewal invoices are tracked via partner + origin for dunning.
        invoice = order._create_invoices()

        # Advance the billing cycle *before* posting the invoice. account.move
        # post commits the move at the accounting layer; if we advanced the
        # date afterwards and any later step failed (mail, write), the cron
        # would re-post a duplicate renewal tomorrow.
        if period == 'yearly':
            interval = relativedelta(years=1)
        else:
            interval = relativedelta(months=1)
        renewal_vals = {
            'next_invoice_date': self.next_invoice_date + interval,
            'last_invoice_date': fields.Date.today(),
            'suspension_warning_sent': False,
            # New cycle: re-arm the 7-day / 1-day auto-renew reminders.
            'renewal_reminder_7d_sent': False,
            'renewal_reminder_1d_sent': False,
        }
        # If this invoice merged the snapshot, advance the backup's date by the
        # SAME interval as the plan so the two cycles stay aligned (next backup
        # charge rides the next renewal). Done in the same pre-post write as
        # next_invoice_date for atomicity + idempotency.
        if merge_snapshot:
            renewal_vals['daily_backup_last_invoice_date'] = fields.Date.today()
            renewal_vals['daily_backup_next_invoice_date'] = (
                self.next_invoice_date + interval
            )
        self.write(renewal_vals)
        invoice.action_post()
        # Settle wallet consumption now that the invoice exists (so the
        # ledger entry links to the move).
        self._wallet_settle_consumption(invoice, wallet_amount)
        self._append_log(
            "Renewal invoice %s created for %s period."
            % (invoice.name, period_label)
        )
        self.message_post(body=_(
            "Renewal invoice %s created (%s). Payment due.",
        ) % (invoice.name, period_label))

        # Auto-charge the saved card if subscription auto-renew is on
        # and a card is on file. Skip the payment-due notification if
        # the charge succeeds so customers aren't pinged for a bill
        # that's already settled.
        auto_paid = False
        if self.auto_renew_subscription and self._auto_renew_method():
            auto_paid = self._try_auto_charge_invoice(invoice, kind='subscription')

        # Send payment-due notification (best-effort: never roll back the
        # renewal if mail delivery fails). Skip when auto-charge already
        # paid the invoice.
        if not auto_paid:
            try:
                self._send_notification('saas_billing.mail_template_saas_payment_due')
            except Exception:
                _logger.exception(
                    "Failed to send payment-due notification for renewal of %s",
                    self.subdomain,
                )

    # ========== Dunning / Grace Period ==========

    @api.model
    def _grace_period_days(self):
        """Platform-wide grace period (days) before an overdue instance is
        suspended. Configured once in Settings → 'Grace Period (Days)' and
        applied to every plan (no longer a per-plan field)."""
        try:
            return int(self.env['ir.config_parameter'].sudo().get_param(
                'saas_master.grace_period_days', '7') or 0)
        except (TypeError, ValueError):
            return 7

    @api.model
    def _cron_check_overdue_invoices(self):
        """Suspend instances whose invoices are overdue past the grace period.

        Checks both running AND stopped instances so that a customer
        cannot dodge suspension by stopping their instance before the
        cron runs.

        Also sends a warning email when the invoice first becomes overdue.
        """
        today = fields.Date.today()
        instances = self.search([
            ('state', 'in', ('running', 'stopped')),
            ('is_trial', '=', False),
            ('parent_id', '=', False),
            ('sale_order_id', '!=', False),
        ])
        for instance in instances:
            try:
                instance._check_dunning(today)
                self.env.cr.commit()
            except Exception:
                # Per-instance failure must NOT abort the whole pass.
                # Roll back the row and log loudly so ops can see
                # which instance got stuck — the next cron run will
                # retry. ``_check_dunning`` itself already handles
                # the common race (state changed mid-cron) silently,
                # so anything reaching here is genuinely unexpected.
                self.env.cr.rollback()
                _logger.exception(
                    "Dunning check crashed for %s — will retry next cron",
                    instance.subdomain,
                )

    def _is_optional_invoice(self, invoice):
        """Return True if the invoice is for an optional upgrade that
        the client can back out of without losing their current service."""
        so_origins = invoice.line_ids.sale_line_ids.order_id.mapped('origin')
        return any(
            origin and any(
                origin.startswith(prefix)
                for prefix in OPTIONAL_INVOICE_ORIGIN_PREFIXES
            )
            for origin in so_origins
        )

    def _check_dunning(self, today):
        """Check if any linked invoices are overdue and act accordingly.

        Searches ALL invoices related to this instance (across all sale
        orders, including renewals) so that unpaid renewal invoices are
        caught even though sale_order_id points to the original order.

        Skips optional upgrade/subscription invoices — the client may
        have requested an upgrade but decided not to pay. These should
        not cause suspension of an otherwise active subscription.

        Handles both running and stopped instances:
        - Running: calls action_suspend() to stop the container via SSH.
        - Stopped: container is already stopped, so we just mark the
          state as 'suspended' directly (no SSH needed).
        """
        self.ensure_one()
        if not self.partner_id:
            return

        # Find all invoices related to this instance (initial + renewals)
        all_invoices = self._get_all_invoices()
        if not all_invoices:
            return

        overdue_invoices = all_invoices.filtered(
            lambda m: m.move_type == 'out_invoice'
            and m.payment_state not in ('paid', 'in_payment')
            and m.state == 'posted'
            and m.invoice_date_due
            and m.invoice_date_due < today
        )
        if not overdue_invoices:
            return

        # Exclude optional upgrade invoices — these are charges the
        # client initiated but can choose not to pay (they keep their
        # current plan).  Only mandatory invoices (initial subscription,
        # renewals, data restoration) should trigger suspension.
        mandatory_overdue = overdue_invoices.filtered(
            lambda m: not self._is_optional_invoice(m)
        )
        if not mandatory_overdue:
            return
        overdue_invoices = mandatory_overdue

        grace_days = self._grace_period_days()
        oldest = min(overdue_invoices, key=lambda m: m.invoice_date_due)
        oldest_due = oldest.invoice_date_due
        days_overdue = (today - oldest_due).days

        if days_overdue > grace_days:
            # Grace period exceeded — suspend.
            #
            # Re-read state at the last possible moment: the cron's
            # initial ``search`` returned this instance as 'running'
            # or 'stopped' but other workers / actions may have moved
            # it since (provisioning, suspended, cancelled, …). Acting
            # on a stale state raised UserError inside the cron loop
            # in the past, which was silently swallowed → instance
            # stayed running with an overdue invoice (revenue leak).
            self.invalidate_recordset(['state'])
            current_state = self.state
            if current_state == 'running':
                # ``action_suspend`` queues the docker stop in a
                # background thread; we wrap it so a transient SSH
                # failure leaves the instance in 'suspended' state
                # rather than re-raising into the cron loop and
                # rolling back the whole dunning pass.
                try:
                    self.action_suspend()
                except UserError:
                    # Lost the race — state changed between recheck
                    # and the call. Next cron pass will revisit.
                    _logger.info(
                        "Dunning skipped %s: state changed mid-cron "
                        "(now %s); will retry on next run.",
                        self.subdomain, self.state,
                    )
                    return
                except Exception:
                    # SSH / docker failure — mark suspended in DB so
                    # access is denied; ops can investigate the host.
                    _logger.exception(
                        "Dunning action_suspend failed for %s; "
                        "forcing state=suspended in DB only.",
                        self.subdomain,
                    )
                    self.state = 'suspended'
                    self.pending_operation = False
            elif current_state == 'stopped':
                # Container is already stopped — just mark as suspended
                # so the customer cannot restart without paying.
                self.state = 'suspended'
            else:
                # Provisioning, already suspended, cancelled, failed,
                # etc. — nothing safe to do this pass.
                _logger.info(
                    "Dunning skipped %s: state is %s (not actionable).",
                    self.subdomain, current_state,
                )
                return
            self._append_log(
                "AUTO-SUSPENDED: Invoice %s overdue by %d days (grace: %d)."
                % (oldest.name, days_overdue, grace_days)
            )
            self._send_notification('saas_core.mail_template_saas_suspended')
        elif not self.suspension_warning_sent:
            # Within grace period — send warning
            self.suspension_warning_sent = True
            self._send_notification('saas_billing.mail_template_saas_payment_due')
            self._append_log(
                "Payment overdue warning sent. Invoice %s due %s. "
                "Grace period: %d days."
                % (oldest.name, oldest_due, grace_days)
            )

    # ========== Plan Upgrade/Downgrade ==========

    def action_subscribe_from_trial(self, new_plan_id, billing_period='monthly'):
        """Start a trial-to-paid upgrade: create invoice and wait for payment.

        The actual plan switch happens in _apply_pending_upgrade() which is
        called automatically when payment is confirmed.

        Args:
            new_plan_id: ID of the target plan
            billing_period: 'monthly' or 'yearly'

        Returns the created invoice (or True if zero-amount).
        """
        self.ensure_one()
        if not self.is_trial:
            raise UserError(_("This instance is not on a trial plan."))
        if self.state not in ('running', 'suspended'):
            raise UserError(
                _("Instance must be running or suspended to subscribe.")
            )

        new_plan = self.env['saas.plan'].browse(int(new_plan_id))
        if not new_plan.exists():
            raise UserError(_("Invalid plan."))
        if new_plan.is_trial_plan:
            raise UserError(_("Cannot subscribe to another trial plan."))
        if (self.saas_product_id and new_plan.saas_product_ids
                and self.saas_product_id not in new_plan.saas_product_ids):
            raise UserError(_("Selected plan does not belong to this service."))

        if billing_period not in ('monthly', 'yearly'):
            billing_period = 'monthly'
        if billing_period == 'yearly' and not new_plan.yearly_price:
            billing_period = 'monthly'

        price = new_plan.get_price_for_period(billing_period)
        period_label = 'Monthly' if billing_period == 'monthly' else 'Yearly'

        # Store the chosen plan + period — applied on payment. v47: no promo.
        self.write({
            'pending_plan_id': new_plan.id,
            'pending_billing_period': billing_period,
        })

        order_lines = [(0, 0, {
            'product_id': self._get_billing_product().id,
            'name': _('%s (%s) — %s') % (
                new_plan.name, period_label, self.name or self.subdomain,
            ),
            'product_uom_qty': 1,
            'price_unit': price,
        })]
        # Consume any existing wallet balance.
        wallet_line, wallet_amount = self._wallet_credit_line(order_lines)
        if wallet_line:
            order_lines.append(wallet_line)

        # Create sale order and invoice
        pricelist = self.partner_id.property_product_pricelist
        order_vals = {
            'partner_id': self.partner_id.id,
            'origin': ORIGIN_SUBSCRIPTION % (self.name or self.subdomain),
            'order_line': order_lines,
        }
        if pricelist:
            order_vals['pricelist_id'] = pricelist.id
        order = self.env['sale.order'].create(order_vals)
        order.action_confirm()
        self.sale_order_id = order

        invoice = order._create_invoices()
        invoice.action_post()
        self._wallet_settle_consumption(invoice, wallet_amount)
        self.pending_change_invoice_id = invoice[:1]

        self._append_log(
            "Upgrade to %s (%s) requested. Invoice %s created — awaiting "
            "payment." % (new_plan.name, period_label, invoice.name)
        )
        self.message_post(body=_(
            "Upgrade to %s (%s) requested. Awaiting payment."
        ) % (new_plan.name, period_label))

        # Zero-amount plan: apply immediately
        if invoice.amount_total <= 0:
            self._apply_pending_upgrade()
            return True

        return invoice

    def _apply_pending_upgrade(self):
        """Apply the pending plan upgrade after payment is confirmed."""
        self.ensure_one()
        new_plan = self.pending_plan_id
        if not new_plan:
            return

        old_plan = self.plan_id
        was_suspended = self.state == 'suspended'
        was_trial = self.is_trial
        billing_period = self.pending_billing_period or 'monthly'

        # Reactivate FIRST if suspended. If the restart fails we don't
        # want to have already flipped is_trial=False — that would leave
        # the customer charged but with a permanently unreachable instance
        # and no way to retry.
        if was_suspended:
            self._append_log("Reactivating instance after paid subscription.")
            try:
                # Use _do_restart synchronously here (we're already inside
                # the background payment thread). action_restart would
                # spawn another bg thread and decouple error reporting.
                self._do_restart()
            except Exception as e:
                # Leave plan/trial unchanged so the customer can retry.
                _logger.exception(
                    "Restart failed during paid-upgrade for %s — aborting "
                    "plan switch so the customer is not charged for an "
                    "unreachable instance.", self.subdomain,
                )
                self._append_log(
                    "ERROR: restart failed during paid-upgrade — plan "
                    "switch deferred. %s" % e
                )
                raise

        self.write({
            'plan_id': new_plan.id,
            'is_trial': False,
            'billing_period': billing_period,
            'pending_plan_id': False,
            'pending_billing_period': False,
            'pending_change_invoice_id': False,
        })

        self._set_next_invoice_date()

        self._append_log(
            "Payment received. Plan upgraded: %s → %s"
            % (old_plan.name if old_plan else 'Trial', new_plan.name)
        )
        self.message_post(body=_(
            "Payment confirmed. Upgraded from %s to paid plan: %s."
        ) % ('trial' if was_trial else (old_plan.name if old_plan else 'plan'),
             new_plan.name))

        # Update container resources / regenerate configs (best effort —
        # the customer is already paid and reactivated, don't roll back
        # the upgrade if these fail; they can be retried by Redeploy).
        if self.state in ('running', 'stopped', 'suspended'):
            try:
                self._update_container_resources()
            except Exception as e:
                _logger.exception(
                    "Failed to update container resources on subscription for %s",
                    self.subdomain,
                )
                self._append_log(
                    "WARNING: Plan updated but container resource update failed: %s" % e
                )

    @staticmethod
    def _to_monthly_equivalent(price, period):
        """Normalize a period price to its monthly equivalent for comparison."""
        if period == 'yearly' and price > 0:
            return price / 12.0
        return price

    def action_request_plan_change(self, new_plan_id, billing_period=None):
        """Request a plan change from the portal.

        UPGRADE (new effective monthly cost > old effective monthly cost):
          - Calculate remaining value of current plan
          - Charge: new_plan_price - remaining_value (min 0)
          - Applied immediately after payment
          - Billing cycle resets from today

        DOWNGRADE (new effective monthly cost <= old effective monthly cost):
          - NO refund, NO credit, NO proration
          - Blocked if current DB size >= 75% of target plan's db_size_limit
          - Scheduled for end of current billing cycle
          - Client keeps current (higher) plan until then

        Upgrade vs downgrade is determined by comparing effective monthly
        costs so that switching periods (monthly ↔ yearly) is classified
        correctly.

        Returns:
          - Invoice record (upgrade, needs payment)
          - True (upgrade, zero charge)
          - 'scheduled' (downgrade scheduled)
        """
        self.ensure_one()
        # Lock the instance row to prevent concurrent plan changes
        # (two browser tabs submitting at the same time).
        self.env.cr.execute(
            "SELECT id FROM saas_instance WHERE id = %s FOR UPDATE NOWAIT",
            (self.id,),
        )
        # Re-read fields after lock to get latest state
        self.invalidate_recordset()

        if self.state not in ('running', 'stopped', 'suspended'):
            raise UserError(
                _("Can only change plan on running, stopped, or suspended instances.")
            )

        # Auto-cancel existing pending upgrade if client changes their mind.
        # This allows switching to a different plan without manual cancellation.
        if self.pending_plan_id:
            self._cancel_pending_upgrade()
        if self.scheduled_plan_id:
            raise UserError(_(
                "A downgrade is already scheduled. "
                "Please cancel the scheduled downgrade before requesting another plan change."
            ))

        new_plan = self.env['saas.plan'].browse(int(new_plan_id))
        if not new_plan.exists():
            raise UserError(_("Invalid plan."))
        if new_plan.is_trial_plan:
            raise UserError(_("Cannot switch to a trial plan."))
        billing_period = billing_period or self.billing_period or 'monthly'

        if new_plan.id == self.plan_id.id and billing_period == (self.billing_period or 'monthly'):
            raise UserError(_("Already on this plan and billing period."))

        # Block yearly → monthly on the same plan.
        # Annual subscribers must wait until their subscription period ends.
        if (new_plan.id == self.plan_id.id
                and (self.billing_period or 'monthly') == 'yearly'
                and billing_period == 'monthly'):
            raise UserError(_(
                "You cannot switch from yearly to monthly billing before your "
                "current annual subscription ends%s. "
                "Your yearly plan will remain active until then."
            ) % (
                ' (%s)' % self.next_invoice_date.strftime('%B %d, %Y')
                if self.next_invoice_date else ''
            ))
        new_price = new_plan.get_price_for_period(billing_period)
        old_period = self.billing_period or 'monthly'
        old_price = self.plan_id.get_price_for_period(old_period) if self.plan_id else 0

        # Monthly → Yearly on the same plan is always an immediate upgrade
        # (customer commits to paying more upfront, with remaining days credited).
        if (new_plan.id == self.plan_id.id
                and old_period == 'monthly'
                and billing_period == 'yearly'):
            return self._request_upgrade(new_plan, billing_period, new_price, old_price)

        # Compare effective monthly costs to correctly classify
        # cross-period changes (e.g. $10/month vs $100/year = $8.33/month)
        new_monthly = self._to_monthly_equivalent(new_price, billing_period)
        old_monthly = self._to_monthly_equivalent(old_price, old_period)

        if new_monthly > old_monthly:
            return self._request_upgrade(new_plan, billing_period, new_price, old_price)
        else:
            return self._request_downgrade(new_plan, billing_period)

    # ---------- AUTO-CANCEL PENDING ----------

    def _cancel_pending_upgrade(self):
        """Cancel the current pending upgrade and ALL of its unpaid invoices.

        Called automatically when the client selects a different plan
        while an upgrade is still awaiting payment. Cancels every unpaid
        invoice tied to either an upgrade SO or a subscription SO for
        this instance — using only `sale_order_id` would miss invoices
        from earlier upgrade rounds whose SO has already been replaced.
        """
        self.ensure_one()
        old_plan_name = (
            self.pending_plan_id.name if self.pending_plan_id else 'Unknown'
        )

        # Find any unpaid posted invoice originating from an upgrade
        # or subscription SO (both are "optional" — the client may back
        # out of paying without losing their current plan).
        invoices = self._get_all_invoices().filtered(
            lambda inv: (
                inv.state == 'posted'
                and inv.payment_state not in ('paid', 'in_payment')
                and inv.amount_residual > 0
                and self._is_optional_invoice(inv)
            )
        )
        for inv in invoices:
            try:
                inv.button_cancel()
            except Exception:
                _logger.exception(
                    "Failed to cancel optional invoice %s for instance %s",
                    inv.name, self.subdomain,
                )

        self._append_log(
            "Auto-cancelled pending upgrade to %s and %d unpaid invoice(s) "
            "(client selected a different plan)."
            % (old_plan_name, len(invoices))
        )
        self.write({
            'pending_plan_id': False,
            'pending_billing_period': False,
            'pending_change_invoice_id': False,
        })

    # ---------- UPGRADE ----------

    def _proration_credit(self, old_price):
        """Unused value of the CURRENT subscription period for ``old_price``,
        prorated over the ACTUAL elapsed cycle (A3).

        No artificial day deductions: the customer is credited for the full
        unused time (``remaining_days / total_days × old_price``). Uses the
        real cycle length (last_invoice_date → next_invoice_date), never a
        days/30 approximation. Returns ``(remaining_value, remaining_days,
        total_days)``."""
        self.ensure_one()
        today = fields.Date.today()
        if not (self.next_invoice_date and self.last_invoice_date):
            return 0.0, 0, 0
        total_days = (self.next_invoice_date - self.last_invoice_date).days
        remaining_days = (self.next_invoice_date - today).days
        if total_days <= 0 or remaining_days <= 0:
            return 0.0, max(0, remaining_days), max(0, total_days)
        remaining_value = round((old_price / total_days) * remaining_days, 2)
        return remaining_value, remaining_days, total_days

    def _request_upgrade(self, new_plan, billing_period, new_price, old_price):
        """Create the proration invoice for an upgrade. Applied on payment.

        Two cases, both customer-fair and free of the old churn arbitrage:

        * SAME billing period (monthly→monthly / yearly→yearly to a bigger
          plan): the current billing cycle is KEPT (never reset) and the
          customer is charged only the PRORATED DIFFERENCE between the new
          and old plan for the days left in the cycle. Because the cycle is
          not reset, a customer can't oscillate plan changes to refresh their
          remaining time and mint wallet credit. The invoice line is exactly
          the incremental cost — no hidden off-ledger discount.

        * PERIOD change (monthly→yearly) or no active cycle: a fresh period
          starts at the new price, the full unused value of the old period is
          credited as an EXPLICIT, visible negative invoice line (not baked
          into the price), and any surplus beyond the new price is carried to
          the wallet when the change is applied (A4 — prepaid value is never
          lost). The cycle is reset because the period genuinely restarts.

        Any existing wallet balance is also consumed on the invoice."""
        self.ensure_one()
        period_label = 'Yearly' if billing_period == 'yearly' else 'Monthly'
        old_period = self.billing_period or 'monthly'

        remaining_value, remaining_days, total_days = self._proration_credit(
            old_price)

        # Same-period upgrade keeps the cycle and charges only the difference.
        same_period = (old_period == billing_period) and total_days > 0
        surplus = 0.0
        order_lines = []

        if same_period:
            new_remaining = round(new_price * remaining_days / total_days, 2)
            charge = round(max(0.0, new_remaining - remaining_value), 2)
            line_name = _(
                '%s (%s) — %s — Plan upgrade (prorated difference for %d '
                'remaining day(s))'
            ) % (new_plan.name, period_label, self.name or self.subdomain,
                 remaining_days)
            order_lines.append((0, 0, {
                'product_id': self._get_billing_product().id,
                'name': line_name,
                'product_uom_qty': 1,
                'price_unit': charge,
            }))
            self._append_log(
                "Upgrade (same period): new_price=%.2f, prorated_diff=%.2f "
                "for %d/%d day(s) — billing cycle kept."
                % (new_price, charge, remaining_days, total_days))
        else:
            # Fresh period at the new price; full unused value credited as an
            # explicit line; surplus carried to the wallet on apply.
            applied_credit = round(min(remaining_value, new_price), 2)
            surplus = round(max(0.0, remaining_value - new_price), 2)
            order_lines.append((0, 0, {
                'product_id': self._get_billing_product().id,
                'name': _('%s (%s) — %s — Plan upgrade') % (
                    new_plan.name, period_label, self.name or self.subdomain),
                'product_uom_qty': 1,
                'price_unit': new_price,
            }))
            if applied_credit > 0:
                order_lines.append((0, 0, {
                    'product_id': self._get_billing_product().id,
                    'name': _('Credit — %d unused day(s) on %s') % (
                        remaining_days, self.plan_id.name),
                    'product_uom_qty': 1,
                    'price_unit': -applied_credit,
                }))
            self._append_log(
                "Upgrade (new period): new_price=%.2f, credit=%.2f, "
                "wallet_surplus=%.2f — billing cycle resets."
                % (new_price, applied_credit, surplus))

        # Store pending upgrade + the surplus to grant on apply.
        self.write({
            'pending_plan_id': new_plan.id,
            'pending_billing_period': billing_period,
            'pending_wallet_credit': surplus,
        })

        # Consume existing wallet balance on the upgrade invoice too.
        wallet_line, wallet_amount = self._wallet_credit_line(order_lines)
        if wallet_line:
            order_lines.append(wallet_line)

        # Create invoice
        pricelist = self.partner_id.property_product_pricelist
        order_vals = {
            'partner_id': self.partner_id.id,
            'origin': ORIGIN_PLAN_UPGRADE % (self.name or self.subdomain),
            'order_line': order_lines,
        }
        if pricelist:
            order_vals['pricelist_id'] = pricelist.id
        order = self.env['sale.order'].create(order_vals)
        order.action_confirm()
        self.sale_order_id = order

        invoice = order._create_invoices()
        invoice.action_post()
        self._wallet_settle_consumption(invoice, wallet_amount)
        self.pending_change_invoice_id = invoice[:1]

        self.message_post(body=_(
            "Upgrade to %s (%s) requested. Invoice %s (%.2f) — awaiting payment."
        ) % (new_plan.name, period_label, invoice.name, invoice.amount_total))

        # Zero charge: apply immediately
        if invoice.amount_total <= 0:
            self._apply_pending_plan_change()
            return True

        return invoice

    def _apply_pending_plan_change(self):
        """Apply a pending upgrade after payment is confirmed."""
        self.ensure_one()
        new_plan = self.pending_plan_id
        if not new_plan:
            return

        billing_period = self.pending_billing_period or self.billing_period or 'monthly'
        old_plan = self.plan_id
        old_period = self.billing_period or 'monthly'
        surplus = self.pending_wallet_credit or 0.0
        # Keep the existing billing cycle for a same-period upgrade — the
        # customer only paid the prorated DIFFERENCE, so the next renewal is
        # still due at the original date (and full new-plan price). Reset the
        # cycle only when the period actually changed (e.g. monthly→yearly) or
        # there is no active cycle to keep. This is what removes the old
        # "reset every upgrade" arbitrage.
        reset_cycle = (old_period != billing_period) or not (
            self.next_invoice_date and self.last_invoice_date)

        self.write({
            'plan_id': new_plan.id,
            'billing_period': billing_period,
            'pending_plan_id': False,
            'pending_billing_period': False,
            'pending_change_invoice_id': False,
            'pending_wallet_credit': 0.0,
        })

        # A4: any unused-value surplus beyond the upgrade price is moved into
        # the wallet now that the change is paid/applied — never forfeited.
        if surplus > 0:
            self._grant_wallet_credit(
                surplus, origin='upgrade_surplus',
                reason=_('Unused value from %s carried to wallet on upgrade')
                % (old_plan.name if old_plan else _('previous plan')))

        if reset_cycle:
            self._set_next_invoice_date()

        cycle_msg = 'reset' if reset_cycle else 'kept (prorated difference billed)'
        self._append_log(
            "Payment received. Plan upgraded: %s → %s. Billing cycle %s."
            % (old_plan.name if old_plan else 'None', new_plan.name, cycle_msg)
        )
        self.message_post(body=_(
            "Payment confirmed. Upgraded from %s to %s."
        ) % (old_plan.name if old_plan else '—', new_plan.name))
        self.env['saas.audit.log'].saas_audit(
            'instance_scale', model='saas.instance', res_id=self.id,
            res_name=self.subdomain,
            detail='Plan %s -> %s (%s)' % (
                old_plan.name if old_plan else 'None', new_plan.name, cycle_msg))

        if self.state in ('running', 'stopped', 'suspended'):
            try:
                self._update_container_resources()
            except Exception as e:
                _logger.exception(
                    "Failed to update container resources for %s", self.subdomain,
                )
                self._append_log(
                    "WARNING: Plan updated but resource update failed: %s" % e
                )

    # ---------- DOWNGRADE ----------

    DOWNGRADE_THRESHOLD = 0.75  # 75% of target plan's db_size_limit

    def _request_downgrade(self, new_plan, billing_period):
        """Validate and schedule a downgrade for end of billing cycle.

        Blocked if current total storage >= 75% of the target plan's storage_limit.
        No refund, no credit, no proration.
        """
        self.ensure_one()

        # Refresh storage usage before checking threshold. Use the
        # strict variant — if we can't measure usage we MUST refuse the
        # downgrade rather than greenlight it on a stale/zero value
        # (the customer could otherwise be charged for a plan that can
        # never serve their data).
        try:
            self._strict_refresh_usage()
        except Exception as exc:
            raise UserError(_(
                "Cannot verify current storage usage — refusing the "
                "downgrade until usage can be measured. Please try again "
                "in a few minutes.\n\nDetails: %s"
            ) % exc)

        # --- Storage threshold check (75% of target plan limit) ---
        if new_plan.storage_limit > 0:
            current_usage_gb = (self.total_storage_bytes or 0) / (1024 ** 3)
            threshold_gb = self.DOWNGRADE_THRESHOLD * new_plan.storage_limit

            self._append_log(
                "Downgrade check: current_usage=%.2f GB, "
                "target_storage_limit=%.2f GB, threshold(75%%)=%.2f GB"
                % (current_usage_gb, new_plan.storage_limit, threshold_gb)
            )

            if current_usage_gb >= threshold_gb:
                raise UserError(_(
                    "Your current storage usage is too high for the selected plan.\n\n"
                    "Current usage: %.2f GB\n"
                    "Target plan limit: %.2f GB\n"
                    "Minimum required headroom: 25%% free (threshold: %.2f GB)\n\n"
                    "Please reduce your data before downgrading, "
                    "or choose a plan with a higher limit."
                ) % (current_usage_gb, new_plan.storage_limit, threshold_gb))

        # --- Schedule the downgrade ---
        self.write({
            'scheduled_plan_id': new_plan.id,
            'scheduled_billing_period': billing_period,
        })

        end_date = self.next_invoice_date or _('end of billing cycle')
        self._append_log(
            "Downgrade to %s scheduled for %s. No refund, no credit."
            % (new_plan.name, end_date)
        )
        self.message_post(body=_(
            "Downgrade to %s scheduled for %s. "
            "Your current plan remains active until then."
        ) % (new_plan.name, end_date))

        return 'scheduled'

    def action_cancel_scheduled_downgrade(self):
        """Cancel a pending scheduled downgrade."""
        self.ensure_one()
        if self.scheduled_plan_id:
            plan_name = self.scheduled_plan_id.name
            self.write({
                'scheduled_plan_id': False,
                'scheduled_billing_period': False,
            })
            self._append_log("Scheduled downgrade to %s cancelled." % plan_name)
            self.message_post(body=_(
                "Scheduled downgrade to %s has been cancelled."
            ) % plan_name)

    def _has_overdue_invoices_past_grace(self):
        """Return True if this instance has invoices overdue past the grace period."""
        self.ensure_one()
        if self.is_trial or not self.partner_id:
            return False
        all_invoices = self._get_all_invoices()
        if not all_invoices:
            return False
        today = fields.Date.today()
        overdue = all_invoices.filtered(
            lambda m: m.move_type == 'out_invoice'
            and m.payment_state not in ('paid', 'in_payment', 'reversed')
            and m.state == 'posted'
            and m.invoice_date_due
            and m.invoice_date_due < today
        )
        if not overdue:
            return False
        grace_days = self._grace_period_days()
        oldest_due = min(overdue, key=lambda m: m.invoice_date_due).invoice_date_due
        return (today - oldest_due).days > grace_days

    def action_reactivate(self, new_plan_id, billing_period='monthly'):
        """Reactivate a cancelled instance with a new plan.

        Reuses the same record — resets state to draft, assigns the new
        plan, clears old infrastructure fields, then runs the billing /
        deploy flow.  The retained snapshot is preserved so the admin
        can still restore data if the client requests it.
        """
        self.ensure_one()
        if self.state not in ('cancelled', 'cancelled_by_client'):
            raise UserError(_("Only cancelled instances can be reactivated."))

        # Retry any cleanup that failed during the original
        # cancellation BEFORE we clear the old FKs (we lose the
        # ability to reach the old resources once the FKs go).
        self._retry_pending_cleanup()

        new_plan = self.env['saas.plan'].browse(int(new_plan_id))
        if not new_plan.exists() or new_plan.is_trial_plan:
            raise UserError(_("Please select a valid paid plan."))

        if billing_period not in ('monthly', 'yearly'):
            billing_period = 'monthly'
        if billing_period == 'yearly' and not new_plan.yearly_price:
            billing_period = 'monthly'

        # Reset to draft with new plan — clear old infra but keep
        # history. ``cancellation_reason`` and the retained snapshot
        # rows are intentionally NOT cleared so the customer can
        # restore their data after re-enabling daily backups.
        # ``daily_backup_enabled`` IS cleared on purpose: a cancelled
        # subscription means the snapshot add-on is gone too, so the
        # customer must opt in (and pay) again before they can use
        # snapshots — including restoring from the one we retained.
        self.write({
            'state': 'draft',
            'plan_id': new_plan.id,
            'billing_period': billing_period,
            'is_trial': False,
            # Clear stale infrastructure (will be re-allocated on deploy)
            'docker_server_id': False,
            'db_server_id': False,
            'xmlrpc_port': False,
            'longpolling_port': False,
            'deploy_retry_count': 0,
            'is_overcommitted': False,
            # Clear stale billing refs (new SO will be created)
            'sale_order_id': False,
            'pending_plan_id': False,
            'pending_billing_period': False,
            'pending_change_invoice_id': False,
            'scheduled_plan_id': False,
            'scheduled_billing_period': False,
            'suspension_warning_sent': False,
            # Daily-backup subscription is reset — the customer must
            # re-enable it (and pay a fresh activation invoice) before
            # nightly snapshots resume OR the retained snapshot can
            # be restored. See ``action_restore_full_instance``'s gate.
            'daily_backup_enabled': False,
            'daily_backup_pending_invoice_id': False,
            'daily_backup_next_invoice_date': False,
            'daily_backup_last_invoice_date': False,
            # Compute tier is reset the same way — a fresh commitment
            # re-provisions at the default (free) tier; the customer
            # re-selects (and pays for) a higher tier again if they still
            # want one.
            'compute_tier_id': self.env['saas.compute.tier'].get_default().id,
            'pending_compute_tier_id': False,
            'compute_tier_pending_invoice_id': False,
            # Saved card + auto-renew are tied to the previous
            # subscription. Reactivation is a fresh commitment; force
            # the customer to opt in again so the new subscription
            # never charges an old card without explicit consent
            # (also avoids PCI surprises if the customer changed banks
            # during the cancellation window).
            'payment_token_id': False,
            'auto_renew_subscription': True,
            'auto_renew_daily_backup': True,
            # the retained snapshot (a backup row) is intentionally kept
            # Reset restore banner so client sees the option again
            'restore_banner_dismissed': False,
            'restoration_invoice_id': False,
        })

        self._append_log(
            "Instance reactivated by client. New plan: %s (%s)."
            % (new_plan.name, billing_period)
        )
        self.message_post(body=_(
            "Instance reactivated. New plan: %s (%s)."
        ) % (new_plan.name, billing_period))

        # Run billing flow (creates SO + invoice)
        self.action_confirm_and_bill()
        return True

    # ========== saas_core hook implementations ==========

    def _do_scale_compute_tier(self, tier_id):
        res = super()._do_scale_compute_tier(tier_id)
        # A paid upgrade stays pending until the scale really succeeded
        # (super raises otherwise), then the pending marker is cleared.
        self.write({'pending_compute_tier_id': False})
        return res

    def _teardown_billing_vals(self):
        vals = super()._teardown_billing_vals()
        vals.update({
            'daily_backup_pending_invoice_id': False,
            'daily_backup_next_invoice_date': False,
            'daily_backup_last_invoice_date': False,
            'pending_compute_tier_id': False,
            'compute_tier_pending_invoice_id': False,
        })
        return vals

    def _activate_pending_environment(self):
        self.env_pending_invoice_id = False
        return super()._activate_pending_environment()

    def _env_child_extra_vals(self):
        vals = super()._env_child_extra_vals()
        vals['billing_period'] = self.billing_period or 'monthly'
        return vals

    def _on_trial_expired(self):
        super()._on_trial_expired()
        # Clear any pending upgrade that was never paid (capture the plan
        # name BEFORE clearing — otherwise the log entry would dereference
        # a False record).
        if self.pending_plan_id:
            pending_name = self.pending_plan_id.name
            self.write({
                'pending_plan_id': False,
                'pending_billing_period': False,
                'pending_change_invoice_id': False,
            })
            self._append_log(
                "Pending upgrade to %s cleared (trial expired)." % pending_name)

    def _status_dict_billing_extra(self):
        extra = super()._status_dict_billing_extra()
        extra.update({
            'pending_plan_id': self.pending_plan_id.id if self.pending_plan_id else False,
            'restoration_pending': bool(self.restoration_invoice_id),
        })
        return extra
