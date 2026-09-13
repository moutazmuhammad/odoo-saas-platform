from dateutil.relativedelta import relativedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

# ----------------------------------------------------------------------
# Customer wallet — v47 LOT-BASED, two-class ledger.
#
# A wallet holds platform credit in discrete LOTS. Each lot has a class:
#
#   * customer_funded  — the customer's own prepaid money (unused
#       subscription value converted on upgrade, refunds of paid value).
#       NEVER expires. Shown in the UI as "Your balance".
#   * system_issued    — credit the platform GAVE the customer (goodwill /
#       service credits). MAY expire. Shown as "Bonus credit (expires …)".
#
# Consumption always spends EXPIRING (system_issued, soonest-expiry-first)
# credit BEFORE the customer's own money, so a customer never loses value
# they paid for.
#
# Financial safety (Stripe-grade):
#   * Every movement is recorded in an append-only audit ledger
#     (saas.wallet.transaction) with a balance snapshot.
#   * Balance is derived ONLY from live lots' ``amount_remaining`` —
#     never edited directly.
#   * All movements take a row lock on the wallet (serialised) and are
#     IDEMPOTENT per source document: consuming or refunding the same
#     invoice twice is a no-op, backed by a DB unique constraint on
#     (move_id, lot_id, kind). No double spend, no duplicate refund, ever.
#   * Refunds restore credit to the ORIGINAL lot, preserving its class and
#     expiry — a consumed bonus that's refunded stays bonus, so refunds
#     can never inflate non-expiring "real money".
# ----------------------------------------------------------------------

CLASS_CUSTOMER = 'customer_funded'
CLASS_SYSTEM = 'system_issued'


class SaasWallet(models.Model):
    _name = 'saas.wallet'
    _description = 'SaaS Customer Wallet (account credit)'
    _rec_name = 'partner_id'

    partner_id = fields.Many2one(
        'res.partner', string='Customer', required=True, ondelete='cascade',
        index=True,
    )
    currency_id = fields.Many2one(
        'res.currency', string='Currency',
        default=lambda self: self.env.company.currency_id, required=True,
    )
    lot_ids = fields.One2many('saas.wallet.lot', 'wallet_id', string='Lots')
    transaction_ids = fields.One2many(
        'saas.wallet.transaction', 'wallet_id', string='Ledger',
    )
    balance = fields.Monetary(
        string='Total Balance', currency_field='currency_id',
        compute='_compute_balances', store=True, readonly=True,
    )
    balance_funded = fields.Monetary(
        string='Your Balance', currency_field='currency_id',
        compute='_compute_balances', store=True, readonly=True,
        help='The customer\'s own money. Never expires.',
    )
    balance_bonus = fields.Monetary(
        string='Bonus Credit', currency_field='currency_id',
        compute='_compute_balances', store=True, readonly=True,
        help='Promotional / goodwill credit. May expire.',
    )
    transaction_count = fields.Integer(compute='_compute_transaction_count')

    _sql_constraints = [
        ('partner_uniq', 'unique(partner_id)',
         'A customer can only have one wallet.'),
    ]

    @api.depends('lot_ids.amount_remaining', 'lot_ids.state',
                 'lot_ids.expiry_date', 'lot_ids.credit_class')
    def _compute_balances(self):
        today = fields.Date.today()
        for wallet in self:
            funded = bonus = 0.0
            for lot in wallet.lot_ids:
                if not lot._is_live(today):
                    continue
                if lot.credit_class == CLASS_CUSTOMER:
                    funded += lot.amount_remaining
                else:
                    bonus += lot.amount_remaining
            wallet.balance_funded = round(funded, 2)
            wallet.balance_bonus = round(bonus, 2)
            wallet.balance = round(funded + bonus, 2)

    @api.depends('transaction_ids')
    def _compute_transaction_count(self):
        for wallet in self:
            wallet.transaction_count = len(wallet.transaction_ids)

    # ------------------------------------------------------------------
    # Lookup / creation / locking
    # ------------------------------------------------------------------
    @api.model
    def _for_partner(self, partner, create=True):
        """Wallet for ``partner``'s commercial entity (shared across its
        contacts), created on demand when ``create``."""
        if not partner:
            return self.browse()
        commercial = partner.commercial_partner_id or partner
        wallet = self.sudo().search(
            [('partner_id', '=', commercial.id)], limit=1)
        if not wallet and create:
            wallet = self.sudo().create({
                'partner_id': commercial.id,
                'currency_id': (
                    commercial.currency_id or self.env.company.currency_id).id,
            })
        return wallet

    def _lock(self):
        """Serialise all movements on this wallet (row lock)."""
        self.ensure_one()
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM saas_wallet WHERE id = %s FOR UPDATE", (self.id,))
        self.invalidate_recordset(
            ['balance', 'balance_funded', 'balance_bonus'])

    def _live_balance(self):
        """Current spendable balance from live lots (post-lock truth)."""
        self.ensure_one()
        today = fields.Date.today()
        return round(sum(
            lot.amount_remaining for lot in self.lot_ids
            if lot._is_live(today)), 2)

    def _live_lots_priority(self):
        """Live lots ordered for consumption: expiring (system_issued,
        soonest expiry first) BEFORE the customer's own non-expiring money."""
        today = fields.Date.today()
        live = [lot for lot in self.lot_ids if lot._is_live(today)
                and lot.amount_remaining > 0]
        return sorted(live, key=lambda l: (
            0 if l.credit_class == CLASS_SYSTEM else 1,
            l.expiry_date or fields.Date.to_date('9999-12-31'),
            l.id,
        ))

    @staticmethod
    def _default_expiry(env):
        try:
            months = int(env['ir.config_parameter'].sudo().get_param(
                'saas_master.system_credit_expiry_months', '12') or 12)
        except (TypeError, ValueError):
            months = 12
        return fields.Date.today() + relativedelta(months=max(1, months))

    # ------------------------------------------------------------------
    # Movements — the ONLY way the balance changes
    # ------------------------------------------------------------------
    def _credit(self, amount, origin, reason='', move=None, instance=None,
                credit_class=CLASS_CUSTOMER, expiry_date=None):
        """Add a new credit LOT (amount > 0). ``customer_funded`` lots never
        expire; ``system_issued`` lots expire on ``expiry_date`` (defaulted
        from settings when not given). Returns the lot."""
        self.ensure_one()
        amount = round(amount or 0.0, 2)
        if amount <= 0:
            return self.env['saas.wallet.lot']
        self._lock()
        if credit_class == CLASS_SYSTEM and not expiry_date:
            expiry_date = self._default_expiry(self.env)
        if credit_class == CLASS_CUSTOMER:
            expiry_date = False  # customer money never expires
        lot = self.env['saas.wallet.lot'].sudo().create({
            'wallet_id': self.id,
            'credit_class': credit_class,
            'currency_id': self.currency_id.id,
            'amount_initial': amount,
            'amount_remaining': amount,
            'expiry_date': expiry_date or False,
            'source_origin': origin,
            'source_move_id': move.id if move else False,
            'instance_id': instance.id if instance else False,
        })
        self._log(lot, 'credit', amount, origin, reason, move, instance)
        return lot

    def _consume(self, amount, origin='invoice_consumption', reason='',
                 move=None, instance=None):
        """Spend up to ``amount`` across live lots (expiring first). Returns
        the amount actually consumed. IDEMPOTENT per ``move``: if this move
        was already consumed, returns the prior total without re-charging."""
        self.ensure_one()
        amount = round(amount or 0.0, 2)
        if amount <= 0:
            return 0.0
        self._lock()
        Txn = self.env['saas.wallet.transaction'].sudo()
        if move:
            prior = Txn.search([
                ('wallet_id', '=', self.id), ('move_id', '=', move.id),
                ('kind', '=', 'consume')])
            if prior:
                return round(-sum(prior.mapped('amount')), 2)
        remaining = min(self._live_balance(), amount)
        spent_total = 0.0
        for lot in self._live_lots_priority():
            if remaining <= 0:
                break
            take = min(lot.amount_remaining, remaining)
            if take <= 0:
                continue
            lot.sudo().write({
                'amount_remaining': round(lot.amount_remaining - take, 2)})
            self._log(lot, 'consume', -take, origin, reason, move, instance)
            spent_total += take
            remaining -= take
        return round(spent_total, 2)

    def _refund_move(self, move, reason='', max_amount=None):
        """Return credit consumed by ``move`` to its ORIGINAL lots (class +
        expiry preserved). IDEMPOTENT: a move is refunded at most once.
        ``max_amount`` caps the refund (e.g. to the still-unpaid residual
        for a partially-paid invoice)."""
        self.ensure_one()
        if not move:
            return 0.0
        self._lock()
        Txn = self.env['saas.wallet.transaction'].sudo()
        if Txn.search_count([
                ('wallet_id', '=', self.id), ('move_id', '=', move.id),
                ('kind', '=', 'refund')]):
            return 0.0  # already refunded
        consume_txns = Txn.search([
            ('wallet_id', '=', self.id), ('move_id', '=', move.id),
            ('kind', '=', 'consume')])
        budget = (round(max_amount, 2) if max_amount is not None
                  else round(-sum(consume_txns.mapped('amount')), 2))
        refunded = 0.0
        for txn in consume_txns:
            if budget <= 0:
                break
            lot = txn.lot_id
            give = min(-txn.amount, budget)
            # Don't restore beyond the lot's original size.
            give = min(give, round(lot.amount_initial - lot.amount_remaining, 2))
            if give <= 0:
                continue
            lot.sudo().write({
                'amount_remaining': round(lot.amount_remaining + give, 2),
                'state': 'active',
            })
            self._log(lot, 'refund', give, 'refund',
                      reason or _('Refund — invoice %s cancelled') % (move.name or ''),
                      move, txn.instance_id)
            refunded += give
            budget -= give
        return round(refunded, 2)

    @api.model
    def _cron_expire_credits(self):
        """Expire spent-down system_issued lots past their expiry date. Posts
        an audit ``expiry`` entry and zeroes the lot. Customer-funded lots
        are never touched."""
        today = fields.Date.today()
        lots = self.env['saas.wallet.lot'].sudo().search([
            ('credit_class', '=', CLASS_SYSTEM),
            ('state', '=', 'active'),
            ('expiry_date', '<', today),
            ('amount_remaining', '>', 0),
        ])
        for lot in lots:
            wallet = lot.wallet_id
            wallet._lock()
            amount = lot.amount_remaining
            lot.write({'amount_remaining': 0.0, 'state': 'expired'})
            wallet._log(lot, 'expiry', -amount, 'expiry',
                        _('Bonus credit expired'), None, lot.instance_id)
            self.env.cr.commit()

    def _log(self, lot, kind, amount, origin, reason, move, instance):
        """Append an immutable audit row + balance snapshot."""
        self.ensure_one()
        self.env['saas.wallet.transaction'].sudo().create({
            'wallet_id': self.id,
            'lot_id': lot.id if lot else False,
            'credit_class': lot.credit_class if lot else False,
            'kind': kind,
            'amount': round(amount, 2),
            'balance_after': self._live_balance(),
            'origin': origin,
            'reason': reason or '',
            'move_id': move.id if move else False,
            'instance_id': instance.id if instance else False,
        })

    def action_view_transactions(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Wallet Ledger'),
            'res_model': 'saas.wallet.transaction',
            'view_mode': 'list,form',
            'domain': [('wallet_id', '=', self.id)],
            'context': {'default_wallet_id': self.id},
        }


class SaasWalletLot(models.Model):
    _name = 'saas.wallet.lot'
    _description = 'SaaS Wallet Credit Lot'
    _order = 'expiry_date asc, id asc'

    wallet_id = fields.Many2one(
        'saas.wallet', required=True, ondelete='cascade', index=True)
    currency_id = fields.Many2one(related='wallet_id.currency_id', store=True)
    credit_class = fields.Selection([
        (CLASS_CUSTOMER, 'Your balance (never expires)'),
        (CLASS_SYSTEM, 'Bonus credit (may expire)'),
    ], required=True, default=CLASS_CUSTOMER, index=True)
    amount_initial = fields.Monetary(currency_field='currency_id', required=True)
    amount_remaining = fields.Monetary(currency_field='currency_id', required=True)
    expiry_date = fields.Date(
        help='Customer-funded lots never expire (empty). System-issued lots '
             'expire on this date.')
    state = fields.Selection([
        ('active', 'Active'), ('depleted', 'Depleted'), ('expired', 'Expired'),
    ], default='active', compute='_compute_state', store=True)
    source_origin = fields.Char()
    source_move_id = fields.Many2one('account.move', ondelete='set null')
    instance_id = fields.Many2one('saas.instance', ondelete='set null')
    partner_id = fields.Many2one(
        related='wallet_id.partner_id', store=True, string='Customer')

    @api.depends('amount_remaining', 'expiry_date')
    def _compute_state(self):
        today = fields.Date.today()
        for lot in self:
            if lot.expiry_date and lot.expiry_date < today \
                    and lot.credit_class == CLASS_SYSTEM:
                lot.state = 'expired'
            elif lot.amount_remaining <= 0:
                lot.state = 'depleted'
            else:
                lot.state = 'active'

    def _is_live(self, today=None):
        """Spendable now: has balance, not expired."""
        self.ensure_one()
        today = today or fields.Date.today()
        if self.amount_remaining <= 0:
            return False
        if self.credit_class == CLASS_SYSTEM and self.expiry_date \
                and self.expiry_date < today:
            return False
        return True

    @api.constrains('amount_remaining', 'amount_initial')
    def _check_amounts(self):
        for lot in self:
            if lot.amount_remaining < -0.001:
                raise ValidationError(_("A wallet lot cannot go negative."))
            if lot.amount_remaining > lot.amount_initial + 0.001:
                raise ValidationError(_(
                    "A wallet lot's remaining cannot exceed its initial credit."))


class SaasWalletTransaction(models.Model):
    _name = 'saas.wallet.transaction'
    _description = 'SaaS Wallet Ledger Entry'
    _order = 'id desc'

    wallet_id = fields.Many2one(
        'saas.wallet', required=True, ondelete='cascade', index=True)
    lot_id = fields.Many2one('saas.wallet.lot', ondelete='set null', index=True)
    partner_id = fields.Many2one(
        related='wallet_id.partner_id', store=True, string='Customer')
    currency_id = fields.Many2one(related='wallet_id.currency_id', store=True)
    credit_class = fields.Selection([
        (CLASS_CUSTOMER, 'Your balance'), (CLASS_SYSTEM, 'Bonus credit'),
    ])
    kind = fields.Selection([
        ('credit', 'Credit added'), ('consume', 'Applied to invoice'),
        ('refund', 'Refunded'), ('expiry', 'Bonus expired'),
    ], required=True, index=True)
    amount = fields.Monetary(
        currency_field='currency_id', required=True,
        help='Signed: + added/refunded, − consumed/expired.')
    balance_after = fields.Monetary(currency_field='currency_id', readonly=True)
    origin = fields.Char()
    reason = fields.Char(string='Description')
    move_id = fields.Many2one('account.move', ondelete='set null', index=True)
    instance_id = fields.Many2one('saas.instance', ondelete='set null')

    _sql_constraints = [
        # Hard backstop against double spend / duplicate refund: a given
        # invoice can touch a given lot at most once per kind. (NULL move_id
        # rows — credits/expiry — are distinct in Postgres, so unaffected.)
        ('move_lot_kind_uniq', 'unique(move_id, lot_id, kind)',
         'This wallet movement was already recorded for that invoice.'),
    ]

    def write(self, vals):
        protected = set(vals) - {'move_id', 'instance_id', 'lot_id'}
        if protected and any(t.id for t in self):
            raise UserError(_(
                "Wallet ledger entries are immutable. Post a compensating "
                "entry instead."))
        return super().write(vals)

    @api.constrains('amount')
    def _check_amount(self):
        for rec in self:
            if rec.amount == 0:
                raise ValidationError(_("A ledger entry cannot be zero."))
