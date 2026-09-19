from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class SaasComputeTier(models.Model):
    """Commercial half of saas.compute.tier, split out of
    saas_core/models/saas_compute_tier.py — its resource shape
    (name/code/replicas/is_default/description) stays in saas_core since
    provisioning reads it directly; this addon adds the commercial shape
    (monthly_price/cost_price/profit/margin_pct) via _inherit, same
    pattern as saas.plan.
    """
    _inherit = 'saas.compute.tier'

    monthly_price = fields.Float(
        string='Monthly Price', default=0.0,
        help='Flat monthly fee for this tier. Added after infrastructure '
             'pricing; not scaled by region — same convention as '
             'saas.support.plan.',
    )
    cost_price = fields.Float(
        string='Cost / month', default=0.0,
        help='Optional: what this tier actually costs to run (extra pod '
             'replicas\' compute/RAM). Purely informational — shown next '
             'to the sale price for margin visibility, NOT enforced as a '
             'floor the way the main pricing engine\'s worker/storage '
             'floors are. 0 = not tracked.',
    )
    profit = fields.Float(
        string='Gross Profit / month', compute='_compute_profitability',
        help='Monthly Price − Cost. Display-only.',
    )
    margin_pct = fields.Float(
        string='Margin %', compute='_compute_profitability',
        help='(Monthly Price − Cost) / Monthly Price. Display-only.',
    )
    is_profitable = fields.Boolean(
        string='Profitable', compute='_compute_profitability',
        help='True when gross profit >= 0, or when cost is not tracked (0).',
    )
    cost_tracked = fields.Boolean(
        string='Cost Tracked', compute='_compute_profitability',
        help='False when Cost / month is 0 (not entered) — Profit/Margin '
             'are not meaningful until a cost is set.',
    )
    minimum_profitable_price = fields.Float(
        string='Minimum Profitable Price', compute='_compute_profitability',
        help='The price needed to hit the platform\'s target margin '
             '(Settings > Margin Protection) on this tier\'s cost. 0 when '
             'cost is not tracked.',
    )

    @api.depends('monthly_price', 'cost_price')
    def _compute_profitability(self):
        engine = self.env['saas.pricing.engine']
        icp = self.env['ir.config_parameter'].sudo()
        try:
            target_margin = float(
                icp.get_param('saas_master.target_margin_pct', '30') or 30)
        except (TypeError, ValueError):
            target_margin = 30.0
        for rec in self:
            result = engine.profitability(rec.monthly_price, rec.cost_price)
            rec.profit = result['profit']
            rec.margin_pct = result['margin_pct']
            rec.is_profitable = result['is_profitable']
            rec.cost_tracked = rec.cost_price > 0
            rec.minimum_profitable_price = (
                engine.minimum_profitable_price(rec.cost_price, target_margin)
                or 0.0) if rec.cost_tracked else 0.0

    @api.constrains('monthly_price')
    def _check_price(self):
        for rec in self:
            if rec.monthly_price < 0:
                raise ValidationError(_(
                    "Compute tier '%s': monthly price can't be negative."
                ) % rec.name)
