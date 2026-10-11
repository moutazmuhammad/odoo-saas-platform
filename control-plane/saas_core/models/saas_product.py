from odoo import fields, models


class SaasProduct(models.Model):
    _name = 'saas.product'
    _description = 'SaaS Product'
    _order = 'sequence, name'

    name = fields.Char(
        string='Product Name',
        required=True,
        help='Display name shown to customers (e.g. "Odoo Hosting").',
    )
    sequence = fields.Integer(default=10)
    is_published = fields.Boolean(
        string='Published',
        default=True,
        help='Only published products can be ordered or reactivated.',
    )
    is_hosting = fields.Boolean(
        string='Self-Managed Hosting',
        default=True,
        help='This product is self-managed hosting: customers provide their '
             'own Git repository and Odoo version.',
    )
    instance_count = fields.Integer(
        string='Instances',
        compute='_compute_instance_count',
    )

    def action_view_instances(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Instances',
            'res_model': 'saas.instance',
            'view_mode': 'list,form',
            'domain': [('saas_product_id', '=', self.id)],
            'context': {'default_saas_product_id': self.id},
        }

    def _compute_instance_count(self):
        data = self.env['saas.instance']._read_group(
            [('saas_product_id', 'in', self.ids)],
            ['saas_product_id'],
            ['__count'],
        )
        counts = {prod.id: count for prod, count in data}
        for rec in self:
            rec.instance_count = counts.get(rec.id, 0)
