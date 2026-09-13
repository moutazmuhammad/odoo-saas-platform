from odoo import fields, models


class SaasOdooVersion(models.Model):
    _name = 'saas.odoo.version'
    _description = 'Odoo Version'
    _order = 'name'

    name = fields.Char(
        string='Version',
        required=True,
        help='Odoo version identifier (e.g. "18.0", "17.0").',
    )
    docker_image = fields.Char(
        string='Docker Image',
        help='Docker image repository for this Odoo version (e.g. "odoo", "myregistry/odoo").',
    )
    docker_image_tag = fields.Char(
        string='Image Tag',
        help='Docker image tag (e.g. "18.0", "17.0-latest"). '
             'Combined with the image name to form the full image reference.',
    )
    is_hosting_version = fields.Boolean(
        string='Hosting Version',
        default=False,
        help='If checked, this version is available for self-managed hosting. '
             'Hosting versions use dedicated Docker images separate from service versions.',
    )
    nginx_template = fields.Selection(
        selection=[
            ('new', 'New (16+) — /websocket'),
            ('old', 'Old (≤15) — /longpolling'),
        ],
        string='Nginx Template',
        default='new',
        required=True,
        help='Nginx reverse proxy template to use for instances of this version. '
             'Odoo 16+ uses /websocket, older versions use /longpolling.',
    )

    def _get_docker_image(self):
        """Return the full docker image:tag string."""
        self.ensure_one()
        return '%s:%s' % (self.docker_image, self.docker_image_tag)
