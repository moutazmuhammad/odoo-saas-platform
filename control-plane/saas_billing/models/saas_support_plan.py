from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class SaasSupportPlan(models.Model):
    """A paid support tier (recurring add-on).

    Support is a value-based offering: customers with mission-critical
    workloads pay more for faster response SLAs. The selected plan is
    stored on the instance and billed every cycle alongside the plan.

    Pricing note: support is a FLAT monthly fee added AFTER infrastructure
    pricing and is NOT scaled by the region multiplier (see
    ``saas.pricing.engine``). Exactly one plan should be marked default
    (the free / best-effort tier, price 0) — it's assigned when the
    customer doesn't pick one.
    """
    _name = 'saas.support.plan'
    _description = 'SaaS Support Plan'
    _order = 'sequence, monthly_price, id'

    sequence = fields.Integer(default=10)
    name = fields.Char(required=True, help='e.g. "Standard", "Pro", "Enterprise".')
    code = fields.Char(
        required=True,
        help='Stable technical code used by the pricing engine / checkout '
             '(e.g. "standard"). Do not change once in use.',
    )
    active = fields.Boolean(default=True)
    is_default = fields.Boolean(
        string='Default Plan',
        help='The plan assigned when the customer does not choose one. '
             'Keep exactly one — typically the free best-effort tier.',
    )
    monthly_price = fields.Float(
        string='Monthly Price', default=0.0,
        help='Flat monthly fee. Added after infrastructure pricing; NOT '
             'scaled by region.',
    )
    response_time = fields.Char(
        string='Response Time',
        help='Display-only SLA shown to the customer, e.g. "24h", "4h", '
             '"1h", "Best effort".',
    )
    description = fields.Text()
    currency_id = fields.Many2one(
        'res.currency',
        default=lambda self: self.env.company.currency_id,
    )
    cost_price = fields.Float(
        string='Cost / month', default=0.0,
        help='Optional: what this support tier actually costs to run '
             '(staff time, on-call, tooling). Purely informational — '
             'compared against Monthly Price for margin visibility, NOT '
             'enforced as a floor. 0 = not tracked.',
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
             '(Settings > Margin Protection) on this plan\'s cost. 0 when '
             'cost is not tracked.',
    )

    _sql_constraints = [
        ('code_uniq', 'unique(code)', 'Support plan code must be unique.'),
    ]

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

    @api.constrains('is_default')
    def _check_single_default(self):
        if self.search_count([('is_default', '=', True)]) > 1:
            raise ValidationError(_("Only one support plan may be the default."))

    @api.constrains('monthly_price')
    def _check_price(self):
        for rec in self:
            if rec.monthly_price < 0:
                raise ValidationError(_(
                    "Support plan '%s': monthly price can't be negative."
                ) % rec.name)

    @api.model
    def _get_default(self):
        """The support plan assigned when the customer doesn't pick one."""
        return self.sudo().search(
            [('active', '=', True), ('is_default', '=', True)], limit=1,
        )

    @api.model
    def _price_for_code(self, code):
        """Flat monthly price for an active support plan code (0 if none /
        default / unknown). Used by the pricing engine."""
        if not code:
            return 0.0
        plan = self.sudo().search(
            [('code', '=', code), ('active', '=', True)], limit=1,
        )
        return plan.monthly_price or 0.0 if plan else 0.0
