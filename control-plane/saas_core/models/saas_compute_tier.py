from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class SaasComputeTier(models.Model):
    """A selectable Kubernetes compute tier (pod replica count), priced.

    Kubernetes-only — see saas.instance.compute_tier_id and
    KubernetesDriver.scale(). Deliberately NOT the same thing as "High
    Availability": HA is just the commercial NAME given to the 2-replica
    tier today; the architecture supports any replica count as its own
    named, priced tier (e.g. a future "Scale+" at 8 replicas) without
    code changes — add a record, not a field.

    Mirrors saas.support.plan's shape exactly (a separate, data-seeded
    model for named priced tiers, extensible by adding records rather
    than editing a Selection).
    """
    _name = 'saas.compute.tier'
    _description = 'SaaS Compute Tier'
    _order = 'sequence, replicas, id'

    sequence = fields.Integer(default=10)
    name = fields.Char(required=True, help='e.g. "Standard", "HA", "Scale".')
    code = fields.Char(
        required=True,
        help='Stable technical code (e.g. "standard", "ha", "scale"). '
             'Do not change once in use.',
    )
    active = fields.Boolean(default=True)
    is_default = fields.Boolean(
        string='Default Tier',
        help='The tier assigned to a new instance when it is not chosen '
             'explicitly. Keep exactly one — normally the 1-replica tier.',
    )
    replicas = fields.Integer(
        string='Pod Replicas', required=True, default=1,
        help='Value written to the Kubernetes CR spec.replicas when an '
             'instance is on this tier — see KubernetesDriver.scale(). '
             'Values above 1 require the region\'s cluster to have an '
             'RWX-capable StorageClass (see KubernetesDriver._build_odoo_instance).',
    )
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
    margin_pct = fields.Float(
        string='Margin %', compute='_compute_margin_pct',
        help='(Monthly Price − Cost) / Monthly Price. Display-only.',
    )
    description = fields.Text(
        help='Customer-facing explanation shown on the portal tier picker.',
    )
    currency_id = fields.Many2one(
        'res.currency',
        default=lambda self: self.env.company.currency_id,
    )

    _sql_constraints = [
        ('code_uniq', 'unique(code)', 'Compute tier code must be unique.'),
    ]

    @api.depends('monthly_price', 'cost_price')
    def _compute_margin_pct(self):
        for rec in self:
            rec.margin_pct = (
                100.0 * (rec.monthly_price - rec.cost_price) / rec.monthly_price
            ) if rec.monthly_price else 0.0

    @api.constrains('is_default')
    def _check_single_default(self):
        if self.search_count([('is_default', '=', True)]) > 1:
            raise ValidationError(_("Only one compute tier may be the default."))

    @api.constrains('monthly_price')
    def _check_price(self):
        for rec in self:
            if rec.monthly_price < 0:
                raise ValidationError(_(
                    "Compute tier '%s': monthly price can't be negative."
                ) % rec.name)

    @api.constrains('replicas')
    def _check_replicas(self):
        for rec in self:
            if rec.replicas < 1:
                raise ValidationError(_(
                    "Compute tier '%s': replicas must be at least 1."
                ) % rec.name)

    @api.model
    def _get_default(self):
        """The compute tier assigned when an instance doesn't pick one."""
        return self.sudo().search(
            [('active', '=', True), ('is_default', '=', True)], limit=1,
        )
