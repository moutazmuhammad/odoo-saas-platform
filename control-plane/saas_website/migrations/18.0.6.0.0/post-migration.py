from odoo import SUPERUSER_ID, api
from odoo.addons.saas_website.hooks import _enable_website_languages


def migrate(cr, version):
    _enable_website_languages(api.Environment(cr, SUPERUSER_ID, {}))
