from odoo import api, fields, models


class SaasInstance(models.Model):
    _inherit = 'saas.instance'

    folder_id = fields.Many2one(
        'saas.instance.folder', string='Folder',
        ondelete='set null', index=True,
    )

    # access_token and access_url are provided by portal.mixin
    # (inherited in saas_core). _compute_access_url is overridden
    # here to point to the portal instance detail page.

    def _compute_access_url(self):
        for rec in self:
            rec.access_url = '/my/instances/%s' % rec.id
