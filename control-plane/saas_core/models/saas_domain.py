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
        string='Cluster',
        tracking=True,
        help='The cluster whose load balancer this domain\'s wildcard DNS '
             'points at. Instances on this domain are placed on that '
             'cluster. Empty = any cluster in the region (the DNS must then '
             'reach all of them).',
    )
    region_id = fields.Many2one(
        'saas.region',
        string='Region',
        related='proxy_server_id.region_id',
        store=True,
        readonly=True,
        help="Region this domain serves, derived from its cluster. "
             "The instance configurator only offers a domain whose region "
             "matches the customer's chosen region (co-location). A domain "
             "with no cluster (or a cluster with no region) is region-neutral.",
    )
