from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class SaasInstancePackage(models.Model):
    _name = 'saas.instance.package'
    _description = 'Instance Python Package'
    _order = 'name'
    _sql_constraints = [
        ('unique_package_per_instance',
         'UNIQUE(instance_id, name)',
         'This package is already added to this instance.'),
    ]

    instance_id = fields.Many2one(
        'saas.instance',
        string='Instance',
        required=True,
        ondelete='cascade',
    )
    name = fields.Char(
        string='Package Name',
        required=True,
        help='Python package name (e.g. phonenumbers, openpyxl==3.1.0).',
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name'):
                vals['name'] = vals['name'].strip()
        records = super().create(vals_list)
        records.mapped('instance_id')._sync_text_from_packages()
        return records

    def write(self, vals):
        if vals.get('name'):
            vals['name'] = vals['name'].strip()
        result = super().write(vals)
        self.mapped('instance_id')._sync_text_from_packages()
        return result

    def unlink(self):
        instances = self.mapped('instance_id')
        result = super().unlink()
        instances._sync_text_from_packages()
        return result

    @api.constrains('name')
    def _check_name(self):
        for rec in self:
            if not rec.name or not rec.name.strip():
                raise ValidationError(_("Package name cannot be empty."))
