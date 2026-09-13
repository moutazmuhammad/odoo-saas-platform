from odoo import fields, models


class SaasConfigViewer(models.TransientModel):
    _name = 'saas.config.viewer'
    _description = 'Config File Viewer'

    content = fields.Text(string='Configuration', readonly=True)
