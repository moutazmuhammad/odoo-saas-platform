from odoo import fields, models, _


class ResPartner(models.Model):
    """Wallet-facing fields, split out of saas_core/models/res_partner.py
    — their comodel (saas.wallet) lives here. Trial-eligibility fields
    and the email/phone uniqueness constraints stay in saas_core."""
    _inherit = 'res.partner'

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
        wallet = self.env['saas.wallet'].for_partner(self, create=True)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Wallet'),
            'res_model': 'saas.wallet',
            'res_id': wallet.id,
            'view_mode': 'form',
            'target': 'current',
        }
