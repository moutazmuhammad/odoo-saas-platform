from odoo import fields, models


class SaasBasedDomain(models.Model):
    _name = 'saas.based.domain'
    _description = 'Base Domain'
    _inherit = ['mail.thread']

    name = fields.Char(
        string='Domain Name',
        required=True,
        tracking=True,
        help='The parent domain under which instance subdomains are created '
             '(e.g. "saas.example.com"). Instances will be reachable at '
             '<subdomain>.<domain>.',
    )
    proxy_server_id = fields.Many2one(
        'saas.server',
        string='Proxy Server',
        tracking=True,
        domain="[('is_proxy_server', '=', True)]",
        help='Reverse proxy server that handles SSL termination and routes '
             'traffic for this domain. The wildcard DNS record (*.domain) '
             'should point to this server. When set, Nginx configs are '
             'deployed here instead of on each Docker server.',
    )
    region_id = fields.Many2one(
        'saas.region',
        string='Region',
        related='proxy_server_id.region_id',
        store=True,
        readonly=True,
        help="Region this domain serves, derived from its proxy server. "
             "The instance configurator only offers a domain whose region "
             "matches the customer's chosen region (co-location). A domain "
             "with no proxy (or a proxy with no region) is region-neutral.",
    )
