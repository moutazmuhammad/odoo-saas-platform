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
        help='Legacy ssh_docker concept (an external reverse-proxy host for '
             'SSL termination) — not used for Kubernetes deploys, which '
             'terminate TLS natively via the cluster\'s own Ingress + '
             'cert-manager (see saas.region.native_ingress_tls). Left in '
             'place for now; not required to be set.',
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
