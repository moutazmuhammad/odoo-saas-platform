import base64
import logging

from odoo import tools

_logger = logging.getLogger(__name__)

# Committed raster of the VELTNEX brand mark (blue rounded square + white
# "V"). Odoo's `website.favicon` is a Binary that runs `image_process(...,
# output_format='ICO')` on write — Pillow can't open SVG, so we ship a PNG.
_FAVICON_PATH = 'saas_website/static/src/img/veltnex-icon-256.png'


def _set_website_favicon(env):
    """Point every website's favicon at the VELTNEX mark.

    Browsers auto-request ``/favicon.ico`` (e.g. for the view-source tab)
    even when the page declares an ``<link rel="icon">``; Odoo's website
    route serves that from ``website.favicon``, which otherwise defaults to
    the stock Odoo favicon and leaks the brand. Setting the field makes
    ``/favicon.ico`` → ``/web/image/website/<id>/favicon`` return VELTNEX.
    """
    try:
        with tools.file_open(_FAVICON_PATH, 'rb') as f:
            png_b64 = base64.b64encode(f.read())
    except OSError:
        _logger.warning("VELTNEX favicon asset missing at %s; skipping", _FAVICON_PATH)
        return
    websites = env['website'].sudo().search([])
    if websites:
        # write() triggers _handle_favicon -> converts the PNG to ICO.
        websites.write({'favicon': png_b64})
        _logger.info("Set VELTNEX favicon on %s website(s)", len(websites))


def post_init_hook(env):
    _set_website_favicon(env)
    _enable_website_languages(env)


def _enable_website_languages(env):
    arabic = env['res.lang']._activate_lang('ar_001')
    english = env['res.lang']._activate_lang('en_US')
    for website in env['website'].sudo().search([]):
        website.write({'language_ids': [(4, arabic.id), (4, english.id)]})
    installed = env['ir.module.module'].search([('state', '=', 'installed')]).mapped('name')
    env['ir.module.module']._load_module_terms(installed, ['ar_001'], overwrite=False)
