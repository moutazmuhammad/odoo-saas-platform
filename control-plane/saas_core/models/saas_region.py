from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

from ..fields import EncryptedChar


class SaasRegion(models.Model):
    """A hosting region — one Kubernetes cluster. Server cost varies by
    region, so each region carries a ``price_multiplier`` applied to the
    compute+storage portion of a quote (see ``saas.pricing.engine``).
    """
    _name = 'saas.region'
    _description = 'SaaS Region'
    _order = 'sequence, name'

    sequence = fields.Integer(default=10)
    name = fields.Char(required=True)
    code = fields.Char(
        required=True,
        help='Stable technical code, e.g. "eu", "us-east". Do not change once in use.',
    )
    active = fields.Boolean(default=True)
    is_default = fields.Boolean(
        string='Default Region',
        help='Region used when a customer does not pick one. Keep exactly one.',
    )
    is_recommended = fields.Boolean(
        string='Recommended Region',
        help='The region pre-selected during checkout and shown as '
             '"Recommended". Keep exactly one. The cheapest available region '
             'is separately labelled "Budget".',
    )
    price_multiplier = fields.Float(
        string='Price Multiplier', default=1.0,
        help='Multiplies the compute+storage portion of the price for '
             'instances in this region (server cost varies by region). '
             '1.0 = no change. Add-on prices are NOT affected.',
    )
    currency_id = fields.Many2one('res.currency')
    server_ids = fields.One2many('saas.server', 'region_id', string='Servers')
    kubeconfig_id = fields.Many2one(
        'saas.kubeconfig',
        string='Kubeconfig',
        groups='saas_core.group_saas_manager',
        help="Credential for the Kubernetes cluster hosting this region's "
             "compute-layer tenants (one cluster per region) — analogous "
             "to how a Compute entry holds an SSH Key Pair for the Docker "
             "Compose path. A dedicated, encrypted-at-rest record (same "
             "upload-only pattern as SSH Key Pairs), not a field pasted "
             "directly here.",
    )
    ingress_host = fields.Char(
        string='Ingress Host',
        groups='saas_core.group_saas_manager',
        help="Reachable host/IP of this region's cluster ingress front door "
             "(e.g. the Traefik/Gateway API LoadBalancer address) — where an "
             "external reverse proxy sends traffic for a Kubernetes-hosted "
             "tenant. Which controller/Service actually backs this isn't "
             "safely auto-discoverable (the operator supports both plain "
             "Ingress and Gateway API, configured at the operator level — "
             "see compute/operator/internal/controller/network.go), so this "
             "is a manually-configured, ops-owned value, the same way "
             "``kubeconfig`` is.",
    )
    native_ingress_tls = fields.Boolean(
        string='Native Kubernetes Ingress TLS',
        help='When enabled, a Kubernetes-backed instance in this region '
             'skips the external Nginx/Certbot-over-SSH step entirely — '
             "TLS is terminated by the cluster's own Ingress controller, "
             "with cert-manager (via tls_cluster_issuer) issuing the "
             "certificate. ingress_host/ingress_port are not needed for "
             "this path (no external reverse proxy is involved).",
    )
    tls_cluster_issuer = fields.Char(
        string='TLS ClusterIssuer',
        help='Name of the cert-manager ClusterIssuer this region\'s '
             'cluster should use when native_ingress_tls is enabled '
             '(e.g. "letsencrypt-test-client").',
    )
    ingress_port = fields.Integer(
        string='Ingress Port',
        default=80,
        help='Port on ingress_host to connect to. Tenants keep TLS '
             'termination at the existing nginx/Certbot layer, so this is '
             'plain HTTP (80) by default — see KubernetesDriver.endpoint().',
    )

    # ---------- Tenant image builds (customer Git repos) ----------
    # Any OCI registry works (self-hosted registry, Harbor, GHCR, ECR, ...):
    # builds push <registry_host>/<registry_prefix>/tenant-<sub>:<tag> and
    # the cluster pulls it with the same credentials.
    registry_host = fields.Char(
        string='Registry Host',
        groups='saas_core.group_saas_manager',
        help='Registry the cluster pulls tenant images from, e.g. '
             '"ghcr.io", "registry.example.com" or "localhost:32000". '
             'Empty = customer Git repositories cannot be deployed in this '
             'region.')
    registry_push_host = fields.Char(
        string='Registry Push Host',
        groups='saas_core.group_saas_manager',
        help='Where the in-cluster build pushes to, when that differs from '
             'the pull host (e.g. an in-cluster registry pulled by nodes as '
             '"localhost:32000" but pushed to as '
             '"registry.container-registry.svc.cluster.local:5000"). '
             'Empty = same as Registry Host.')
    registry_prefix = fields.Char(
        string='Registry Path Prefix',
        groups='saas_core.group_saas_manager',
        help='Optional repository path prefix, e.g. "my-org/odoo-tenants".')
    registry_username = fields.Char(
        string='Registry Username', groups='saas_core.group_saas_manager')
    registry_password = EncryptedChar(
        string='Registry Password / Token',
        groups='saas_core.group_saas_manager', copy=False)
    registry_insecure = fields.Boolean(
        string='Plain-HTTP Registry',
        groups='saas_core.group_saas_manager',
        help='Push over plain HTTP (only for an in-cluster test registry).')
    builder_image = fields.Char(
        string='Builder Image', default='moby/buildkit:v0.16.0-rootless',
        groups='saas_core.group_saas_manager',
        help='Rootless BuildKit image the build Job runs.')
    git_image = fields.Char(
        string='Git Image', default='alpine/git:v2.45.2',
        groups='saas_core.group_saas_manager',
        help='Image the build Job clones repositories with.')

    # ---------- Monitoring (Prometheus) ----------
    # Tenant CPU/RAM usage and the customer metrics dashboard read this
    # cluster's Prometheus through the Kubernetes API service proxy, using
    # the region kubeconfig — Prometheus is never exposed outside the
    # cluster. Install: compute/charts/monitoring/prometheus-values.yaml.
    prometheus_namespace = fields.Char(
        string='Prometheus Namespace', default='monitoring',
        groups='saas_core.group_saas_manager',
        help='Namespace of the Prometheus server Service. Empty = no '
             'Prometheus in this region (CPU/RAM usage and history are '
             'unavailable; storage is still measured).')
    prometheus_service = fields.Char(
        string='Prometheus Service', default='prometheus-server:80',
        groups='saas_core.group_saas_manager',
        help='Prometheus server Service as "<name>:<port>".')

    _sql_constraints = [
        ('code_uniq', 'unique(code)', 'Region code must be unique.'),
    ]

    @api.constrains('is_default')
    def _check_single_default(self):
        if self.search_count([('is_default', '=', True)]) > 1:
            raise ValidationError(_("Only one region may be marked as default."))

    @api.constrains('is_recommended')
    def _check_single_recommended(self):
        if self.search_count([('is_recommended', '=', True)]) > 1:
            raise ValidationError(_(
                "Only one region may be marked as recommended."))

    @api.constrains('price_multiplier')
    def _check_multiplier(self):
        for rec in self:
            if rec.price_multiplier <= 0:
                raise ValidationError(_(
                    "Region '%s': price multiplier must be greater than 0."
                ) % rec.name)

    @api.model
    def _get_default(self):
        """The INFRASTRUCTURE default region — the home of un-regioned
        servers (see ``saas.server._region_match_domain``). This is the
        ``is_default`` flag, NOT the customer-facing checkout default
        (which is recommended-first — see ``_recommended_available``)."""
        return self.sudo().search(
            [('active', '=', True), ('is_default', '=', True)], limit=1,
        ) or self.sudo().search(
            [('active', '=', True)], order='sequence, id', limit=1,
        )

    @api.model
    def _recommended_available(self):
        """The recommended region that can actually host (proxy+docker+db).
        Falls back to the explicit default, then the cheapest available, then
        the first available — so checkout always has a region to pre-select."""
        regions = self._available_regions()
        if not regions:
            return self.browse()
        rec = regions.filtered('is_recommended')[:1]
        if rec:
            return rec
        dflt = regions.filtered('is_default')[:1]
        if dflt:
            return dflt
        return self._cheapest_available() or regions[:1]

    def has_capacity(self):
        """True when this region can actually host an instance: it needs a
        kubeconfig configured (so its servers' cluster is actually
        reachable-in-principle) AND at least one registered, not-known-
        unreachable Kubernetes cluster (``saas.server``) in-region. A
        region with neither is empty and must not be offered to customers.

        Servers with no region count as the default region (see
        ``saas.server._region_match_domain``), so the default region is
        served by an un-regioned fleet too. Uses the cached ``health_state``
        (refreshed by the health cron and at allocation time) rather than a
        live probe, so this stays cheap to call on every checkout page
        render — if the cluster has gone down since the last check, the
        region reports no capacity and the order is refused at checkout
        with a clear message, far better than creating a project that
        strands in "pending provision"."""
        self.ensure_one()
        if not self.kubeconfig_id:
            return False
        Server = self.env['saas.server'].sudo()
        dom = Server._region_match_domain(self)
        return bool(Server.search_count(
            [('health_state', '!=', 'unreachable')] + dom))

    @api.model
    def _available_regions(self):
        """Active regions that can actually host an instance (a kubeconfig
        and at least one reachable cluster). Empty regions are excluded —
        they must not be shown to or selectable by customers."""
        return self.sudo().search(
            [('active', '=', True)], order='sequence, id',
        ).filtered(lambda r: r.has_capacity())

    @api.model
    def _cheapest_available(self):
        """The available region with the LOWEST price multiplier — the
        platform's customer-facing default, so the advertised entry price is
        always the cheapest. Ties break on sequence then id (stable). Returns
        an empty recordset when no region can host (caller falls back to the
        un-regioned fleet, i.e. x1.0)."""
        regions = self._available_regions()
        if not regions:
            return self.browse()
        return regions.sorted(
            key=lambda r: (r.price_multiplier or 1.0, r.sequence, r.id),
        )[0]
