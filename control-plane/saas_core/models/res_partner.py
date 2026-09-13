from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

from odoo.addons.phone_validation.tools.phone_validation import phone_format


class ResPartner(models.Model):
    _inherit = 'res.partner'

    saas_trial_used = fields.Boolean(
        string='Service Trial Used',
        default=False,
        help='Whether this client has already used their free service trial.',
    )
    saas_hosting_trial_used = fields.Boolean(
        string='Hosting Trial Used',
        default=False,
        help='Whether this client has already used their free hosting trial.',
    )
    saas_trial_end_date = fields.Date(
        string='Trial Ends',
        help='Date when the free trial period expires. '
             'All trial instances are suspended after this date.',
    )
    saas_instance_count = fields.Integer(
        string='Instances',
        compute='_compute_saas_instance_count',
    )
    saas_wallet_id = fields.Many2one(
        'saas.wallet', string='SaaS Wallet',
        compute='_compute_saas_wallet', search='_search_saas_wallet',
    )
    saas_wallet_balance = fields.Monetary(
        string='Wallet Balance', compute='_compute_saas_wallet',
        currency_field='currency_id',
    )

    def _compute_saas_wallet(self):
        Wallet = self.env['saas.wallet'].sudo()
        for rec in self:
            wallet = Wallet.search(
                [('partner_id', '=', rec.commercial_partner_id.id)], limit=1)
            rec.saas_wallet_id = wallet.id or False
            rec.saas_wallet_balance = wallet.balance if wallet else 0.0

    def _search_saas_wallet(self, operator, value):
        wallets = self.env['saas.wallet'].sudo().search(
            [('id', operator, value)])
        return [('id', 'in', wallets.mapped('partner_id').ids)]

    def action_view_saas_wallet(self):
        self.ensure_one()
        wallet = self.env['saas.wallet']._for_partner(self, create=True)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Wallet'),
            'res_model': 'saas.wallet',
            'res_id': wallet.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _compute_saas_instance_count(self):
        data = self.env['saas.instance']._read_group(
            [('partner_id', 'in', self.ids)],
            ['partner_id'],
            ['__count'],
        )
        counts = {partner.id: count for partner, count in data}
        for rec in self:
            rec.saas_instance_count = counts.get(rec.id, 0)

    def _saas_uniqueness_applies(self, partner):
        """Whether the saas_core uniqueness checks apply to *partner*.

        Restrict to commercial customers (those with sales activity or
        explicitly flagged as customers) so non-SaaS uses of res.partner
        — multiple shipping addresses, accounting contacts, etc. — are
        not blocked. Subclass to broaden/narrow as needed.
        """
        return bool(partner.customer_rank)

    @api.constrains('email')
    def _check_unique_email(self):
        candidates = self.filtered(
            lambda p: p.email and self._saas_uniqueness_applies(p)
        )
        if not candidates:
            return
        emails = list({(p.email or '').lower() for p in candidates})
        # Single batched search for any partner outside *self* sharing
        # any of the candidate emails.
        duplicates = self.sudo().search([
            ('email', 'in', emails),
            ('id', 'not in', candidates.ids),
        ])
        # Map by lowercased email for O(1) per-record lookup.
        dup_emails = {(p.email or '').lower() for p in duplicates}
        # Also detect intra-batch duplicates.
        seen = {}
        for p in candidates:
            key = (p.email or '').lower()
            if key in dup_emails or key in seen:
                raise ValidationError(_(
                    "The email address '%s' is already used by another contact."
                ) % p.email)
            seen[key] = p.id

    @api.constrains('phone', 'country_id')
    def _check_phone_country(self):
        candidates = self.filtered(
            lambda p: p.phone and self._saas_uniqueness_applies(p)
        )
        if not candidates:
            return
        # Per-record format validation (cheap, in-memory).
        for partner in candidates:
            if partner.country_id:
                try:
                    phone_format(
                        partner.phone,
                        partner.country_id.code,
                        partner.country_id.phone_code,
                        force_format='E164',
                        raise_exception=True,
                    )
                except Exception as exc:
                    raise ValidationError(_(
                        "The phone number '%s' is not valid for %s: %s"
                    ) % (partner.phone, partner.country_id.name, exc))
        # Single batched uniqueness lookup.
        phones = list({p.phone for p in candidates})
        duplicates = self.sudo().search([
            ('phone', 'in', phones),
            ('id', 'not in', candidates.ids),
        ])
        dup_phones = {p.phone for p in duplicates}
        seen = {}
        for p in candidates:
            if p.phone in dup_phones or p.phone in seen:
                raise ValidationError(_(
                    "The phone number '%s' is already used by another contact."
                ) % p.phone)
            seen[p.phone] = p.id

    def _saas_commercial(self):
        """The commercial (top-level company) partner that OWNS trial + paid
        state. Trial eligibility is per commercial entity, NOT per contact —
        otherwise a company could farm one free trial per child contact (and
        keep getting trials after paying). Mirrors how the wallet is shared
        across a commercial entity. Falls back to self for a stand-alone
        individual.
        """
        self.ensure_one()
        return self.commercial_partner_id or self

    def _saas_trial_used(self, hosting=False):
        """True if this commercial entity has already consumed its free trial
        of the given type (checked across all its contacts)."""
        self.ensure_one()
        commercial = self._saas_commercial()
        return bool(commercial.saas_hosting_trial_used if hosting
                    else commercial.saas_trial_used)

    def _saas_mark_trial_used(self, hosting=False, end_date=None):
        """Record the free trial as consumed on the commercial entity."""
        self.ensure_one()
        commercial = self._saas_commercial()
        flag = 'saas_hosting_trial_used' if hosting else 'saas_trial_used'
        if not commercial[flag]:
            vals = {flag: True}
            if end_date:
                vals['saas_trial_end_date'] = end_date
            commercial.write(vals)

    def _saas_has_paid_instance(self, hosting=False):
        """True if this commercial entity already owns a paid (non-trial,
        non-cancelled) instance of the given type — across ALL its contacts.
        Once they pay for a server, the free trial no longer applies.

        ``draft`` and ``pending_payment`` are INCOMPLETE orders — nothing has
        been paid or provisioned yet (see the awaiting-payment handling at
        saas_instance state transitions), so they must NOT count as a paid
        instance. Otherwise a buyer who opens a paid checkout and abandons it
        would be permanently barred from the free trial they never used.
        """
        self.ensure_one()
        return bool(self.env['saas.instance'].sudo().search_count([
            ('partner_id', 'child_of', self._saas_commercial().id),
            ('is_trial', '=', False),
            ('is_hosting', '=', bool(hosting)),
            ('state', 'not in', ('cancelled', 'cancelled_by_client',
                                 'draft', 'pending_payment')),
        ], limit=1))

    def action_view_saas_instances(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'SaaS Instances',
            'res_model': 'saas.instance',
            'view_mode': 'list,form',
            'domain': [('partner_id', '=', self.id)],
            'context': {'default_partner_id': self.id},
        }
