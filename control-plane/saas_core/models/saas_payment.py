import logging

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Payment-provider abstraction layer (A1).
#
# PCI scope: the platform NEVER stores card numbers, CVV, or expiry. All
# sensitive data lives only at the payment provider (Stripe / Adyen / …).
# We persist ONLY the three safe references the requirement allows:
#   * payment provider identifier  -> saas.payment.method.provider_id
#   * external customer identifier -> saas.payment.method.external_customer_ref
#   * external token / reference   -> saas.payment.method.external_token_ref
# These wrap Odoo's ``payment.token`` (itself SAQ-A compliant: it keeps
# only a provider-side reference plus a masked label like "•••• 4242").
#
# ``saas.payment.provider.config`` maps COUNTRIES to providers so the
# platform can route customers to a different gateway per country. The
# ``saas.payment.gateway`` abstract service resolves the provider, saves
# the safe references after a successful payment, and charges a saved
# method on renewal — so the rest of the codebase never talks to a
# specific provider directly.
# ----------------------------------------------------------------------


class SaasPaymentProviderConfig(models.Model):
    """Country -> payment-provider routing. Lets the platform use, e.g.,
    Stripe in the EU and a local gateway elsewhere. The first active row
    whose countries include the customer's country wins (lowest sequence);
    a row flagged default is the fallback when no country matches."""
    _name = 'saas.payment.provider.config'
    _description = 'SaaS Payment Provider Routing'
    _order = 'sequence, id'

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    provider_id = fields.Many2one(
        'payment.provider', string='Payment Provider', required=True,
        ondelete='cascade',
        help='The Odoo payment provider (Stripe, Adyen, …) used for '
             'customers in the selected countries.',
    )
    country_ids = fields.Many2many(
        'res.country', string='Countries',
        help='Customers whose country is in this list are routed to this '
             'provider. Leave empty and tick "Default" for a catch-all.',
    )
    is_default = fields.Boolean(
        string='Default Routing',
        help='Used when no country-specific routing matches the customer. '
             'Keep at most one default.',
    )

    @api.constrains('is_default')
    def _check_single_default(self):
        if self.search_count([('is_default', '=', True), ('active', '=', True)]) > 1:
            raise ValidationError(_(
                "Only one active payment-provider routing may be the default."))


class SaasPaymentMethod(models.Model):
    """A customer's saved payment method — only safe references, never PCI
    data. Wraps an Odoo ``payment.token``."""
    _name = 'saas.payment.method'
    _description = 'SaaS Saved Payment Method'
    _order = 'is_default desc, id desc'

    partner_id = fields.Many2one(
        'res.partner', string='Customer', required=True, ondelete='cascade',
        index=True,
    )
    provider_id = fields.Many2one(
        'payment.provider', string='Payment Provider', required=True,
        ondelete='cascade',
    )
    # payment.provider.code is a Selection, so the related field must be a
    # Selection too (matching type) — not Char.
    provider_code = fields.Selection(
        related='provider_id.code', store=True, string='Provider Code',
    )
    token_id = fields.Many2one(
        'payment.token', string='Provider Token', ondelete='cascade',
        help='Odoo tokenized reference to the method held at the provider. '
             'No card number / CVV / expiry is stored here or anywhere.',
    )
    external_customer_ref = fields.Char(
        string='External Customer Ref',
        help='The provider-side customer identifier (e.g. Stripe cus_…).',
    )
    external_token_ref = fields.Char(
        string='External Token Ref',
        help='The provider-side payment-method token / reference.',
    )
    display_label = fields.Char(
        string='Method', help='Masked label, e.g. "Visa •••• 4242".',
    )
    country_id = fields.Many2one('res.country', string='Country')
    active = fields.Boolean(default=True)
    is_default = fields.Boolean(string='Default Method')

    @api.model
    def _for_partner(self, partner):
        """Active saved methods for the partner's commercial entity."""
        if not partner:
            return self.browse()
        commercial = partner.commercial_partner_id or partner
        return self.sudo().search([
            ('partner_id', '=', commercial.id),
            ('active', '=', True),
            ('token_id.active', '=', True),
        ])

    @api.model
    def _default_for_partner(self, partner):
        methods = self._for_partner(partner)
        return methods.filtered('is_default')[:1] or methods[:1]

    def _make_default(self):
        self.ensure_one()
        others = self._for_partner(self.partner_id) - self
        others.write({'is_default': False})
        self.is_default = True

    def action_remove(self):
        """Customer-initiated removal: archive the method and its token so
        auto-renew stops using it."""
        for rec in self:
            if rec.token_id:
                rec.token_id.active = False
            rec.active = False
        return True


class SaasPaymentAttempt(models.Model):
    """One automatic charge attempt against an invoice — the audit trail
    behind the retry schedule (renewal date, +1, +3, +5 days)."""
    _name = 'saas.payment.attempt'
    _description = 'SaaS Automatic Payment Attempt'
    _order = 'create_date desc, id desc'

    move_id = fields.Many2one(
        'account.move', string='Invoice', required=True, ondelete='cascade',
        index=True,
    )
    instance_id = fields.Many2one(
        'saas.instance', string='Instance', ondelete='cascade', index=True,
    )
    attempt_no = fields.Integer(string='Attempt #', default=1)
    attempted_on = fields.Date(string='Attempted On', index=True)
    state = fields.Selection([
        ('done', 'Charged'),
        ('pending', 'Pending (async)'),
        ('failed', 'Failed'),
    ], string='Result', default='failed')
    message = fields.Char()


class SaasPaymentGateway(models.AbstractModel):
    """Provider-agnostic billing service: resolve the provider for a
    customer, save the safe references after a payment, and charge a saved
    method. The rest of the codebase calls these — never a provider SDK."""
    _name = 'saas.payment.gateway'
    _description = 'SaaS Payment Gateway (provider abstraction)'

    @api.model
    def _provider_for_partner(self, partner):
        """Resolve the payment provider for a customer, honouring the
        country routing table, then Odoo's per-provider country
        availability, then any enabled provider. Empty recordset if the
        platform has no usable provider configured."""
        Provider = self.env['payment.provider'].sudo()
        Config = self.env['saas.payment.provider.config'].sudo()
        country = partner.country_id if partner else False

        def _usable(prov):
            return prov and prov.state in ('enabled', 'test')

        # 1) explicit country routing
        if country:
            cfg = Config.search([
                ('active', '=', True),
                ('country_ids', 'in', country.id),
            ], order='sequence', limit=1)
            if cfg and _usable(cfg.provider_id):
                return cfg.provider_id
        # 2) default routing row
        cfg = Config.search([
            ('active', '=', True), ('is_default', '=', True),
        ], limit=1)
        if cfg and _usable(cfg.provider_id):
            return cfg.provider_id
        # 3) Odoo's own per-provider country availability
        domain = [('state', 'in', ('enabled', 'test'))]
        providers = Provider.search(domain)
        if country:
            matched = providers.filtered(
                lambda p: not p.available_country_ids
                or country in p.available_country_ids)
            if matched:
                return matched[:1]
        return providers[:1]

    @api.model
    def _save_method_from_transaction(self, partner, tx):
        """Persist the safe references for a tokenized transaction as a
        ``saas.payment.method`` (creating none if there's no token). Stores
        ONLY provider id + external customer ref + external token ref +
        masked label. Returns the method (or empty)."""
        if not partner or not tx:
            return self.env['saas.payment.method']
        token = tx.token_id
        if not token or not token.active:
            return self.env['saas.payment.method']
        commercial = partner.commercial_partner_id or partner
        Method = self.env['saas.payment.method'].sudo()
        existing = Method.search([('token_id', '=', token.id)], limit=1)
        if existing:
            return existing
        # The provider-side customer reference, if the provider stored one
        # on the token (field name varies by module; read defensively).
        customer_ref = (
            getattr(token, 'provider_ref', False)
            or getattr(token, 'transaction_ids', False)
            and token.transaction_ids[:1].provider_reference or '')
        method = Method.create({
            'partner_id': commercial.id,
            'provider_id': token.provider_id.id,
            'token_id': token.id,
            'external_customer_ref': customer_ref or '',
            'external_token_ref': token.provider_ref or '',
            'display_label': token.payment_details or token.display_name or '',
            'country_id': commercial.country_id.id or False,
        })
        # First saved method becomes the default.
        if len(Method._for_partner(commercial)) == 1:
            method.is_default = True
        return method

    @api.model
    def _charge(self, method, invoice):
        """Charge a saved ``saas.payment.method`` for an invoice's residual.

        Returns a tuple ``(state, message)`` where state is 'done',
        'pending' or 'failed'. Pure transport: no logging policy, no
        retry scheduling — callers own that."""
        if not method or not method.token_id or not method.token_id.active:
            return 'failed', _("Saved payment method is no longer available.")
        if not invoice or invoice.payment_state in ('paid', 'in_payment'):
            return 'done', _("Already paid.")
        provider = method.provider_id
        if not provider or provider.state not in ('enabled', 'test'):
            return 'failed', _("Payment provider is currently unavailable.")
        journal_cur = provider.journal_id.currency_id if provider.journal_id else None
        if journal_cur and journal_cur != invoice.currency_id:
            return 'failed', _(
                "Currency mismatch between the invoice and the provider.")
        try:
            tx = self.env['payment.transaction'].sudo().create({
                'amount': invoice.amount_residual,
                'currency_id': invoice.currency_id.id,
                'partner_id': invoice.partner_id.id,
                'provider_id': provider.id,
                'payment_method_id': method.token_id.payment_method_id.id,
                'token_id': method.token_id.id,
                'operation': 'offline',
                'invoice_ids': [(6, 0, [invoice.id])],
            })
            tx._send_payment_request()
        except Exception as exc:  # noqa: BLE001 - provider errors are opaque
            _logger.exception("Auto-charge crashed for invoice %s", invoice.name)
            return 'failed', _("Charge could not be initiated: %s") % exc
        if tx.state == 'done':
            return 'done', _("Charged %s.") % invoice.name
        if tx.state == 'pending':
            return 'pending', _("Awaiting bank confirmation.")
        return 'failed', _("The charge did not go through.")
