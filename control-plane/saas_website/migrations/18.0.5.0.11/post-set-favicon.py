from odoo.api import Environment, SUPERUSER_ID

from odoo.addons.saas_website.hooks import _set_website_favicon


def migrate(cr, version):
    """Replace the stock Odoo favicon on existing installs with VELTNEX."""
    env = Environment(cr, SUPERUSER_ID, {})
    _set_website_favicon(env)
