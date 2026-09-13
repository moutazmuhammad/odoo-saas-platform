from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class SaasInstanceFolder(models.Model):
    _name = 'saas.instance.folder'
    _description = 'Instance Folder'
    _order = 'full_path asc'
    _parent_name = 'parent_id'
    _parent_store = True

    name = fields.Char(required=True, string='Folder Name')
    partner_id = fields.Many2one(
        'res.partner', string='Owner', required=True,
        ondelete='cascade', index=True,
    )
    parent_id = fields.Many2one(
        'saas.instance.folder', string='Parent Folder',
        ondelete='cascade', index=True,
    )
    child_ids = fields.One2many(
        'saas.instance.folder', 'parent_id', string='Subfolders',
    )
    parent_path = fields.Char(index=True, unaccent=False)
    full_path = fields.Char(
        string='Full Path', compute='_compute_full_path', store=True,
        recursive=True,
    )
    depth = fields.Integer(
        compute='_compute_full_path', store=True, recursive=True,
    )
    instance_ids = fields.One2many(
        'saas.instance', 'folder_id', string='Instances',
    )
    instance_count = fields.Integer(
        compute='_compute_instance_count', string='Direct Instances',
    )
    total_instance_count = fields.Integer(
        compute='_compute_instance_count', string='Total Instances',
    )

    @api.depends('name', 'parent_id.full_path')
    def _compute_full_path(self):
        for folder in self:
            if folder.parent_id:
                folder.full_path = '%s / %s' % (folder.parent_id.full_path, folder.name)
                folder.depth = folder.parent_id.depth + 1
            else:
                folder.full_path = folder.name
                folder.depth = 0

    @api.depends('instance_ids')
    def _compute_instance_count(self):
        for folder in self:
            folder.instance_count = len(folder.instance_ids)
            # Total = direct + all descendants
            descendants = self.search([
                ('parent_path', 'like', '%s%%' % (folder.parent_path or '')),
                ('id', '!=', folder.id),
            ]) if folder.parent_path else self.browse()
            all_folders = folder | descendants
            folder.total_instance_count = self.env['saas.instance'].sudo().search_count([
                ('folder_id', 'in', all_folders.ids),
            ])

    @api.constrains('parent_id')
    def _check_parent_not_self(self):
        for folder in self:
            if folder.parent_id == folder:
                raise ValidationError(_("A folder cannot be its own parent."))

    def _get_all_descendant_ids(self):
        """Return IDs of this folder and all its descendants."""
        self.ensure_one()
        if not self.parent_path:
            return [self.id]
        descendants = self.search([
            ('parent_path', 'like', '%s%%' % self.parent_path),
        ])
        return descendants.ids
