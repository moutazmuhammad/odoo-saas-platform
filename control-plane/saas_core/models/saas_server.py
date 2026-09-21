import logging

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class SaasServer(models.Model):
    """A Kubernetes cluster registration — an allocation/capacity unit
    representing one region's cluster (one region maps to one cluster).
    This record is a placeholder for the whole cluster, not a single
    machine: the cluster's actual connection details (kubeconfig,
    ingress) live on ``saas.region``, not here.
    """
    _name = 'saas.server'
    _description = 'Compute Target (Kubernetes Cluster)'
    _inherit = ['mail.thread']
    _order = 'sequence, name'

    sequence = fields.Integer(
        string='Sequence',
        default=10,
    )
    name = fields.Char(
        string='Name',
        required=True,
        tracking=True,
        help='Human-readable label for this server (e.g. "EU Production 1").',
    )
    region_id = fields.Many2one(
        'saas.region',
        string='Region',
        index=True,
        tracking=True,
        help='Region this server belongs to. Used for region-based '
             'allocation and pricing.',
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        default=lambda self: self.env.company,
        index=True,
        help='Company that owns this server. Used by the multi-company '
             'record rule — set to empty for shared infrastructure.',
    )

    # ========== Network ==========
    ip_v4 = fields.Char(
        string='Public IPv4',
        help='Public IPv4 address of this server, reachable from the internet.',
    )
    private_ip_v4 = fields.Char(
        string='Private IPv4',
        help='Private / internal IPv4 address used for communication '
             'between servers on the same network.',
    )

    # ========== Capacity ==========
    max_instances = fields.Integer(
        string='Max Instances',
        default=0,
        help='Maximum number of running instances on this cluster. '
             '0 = unlimited.',
    )
    max_cpu_cores = fields.Float(
        string='Max CPU Cores',
        default=0,
        help='Total CPU cores available for allocation on this cluster. '
             '0 = unlimited.',
    )
    max_ram_gb = fields.Float(
        string='Max RAM (GB)',
        default=0,
        help='Total RAM in GB available for allocation on this cluster. '
             '0 = unlimited.',
    )
    allow_overcommit = fields.Boolean(
        string='Allow Overcommit',
        default=False,
        help='When enabled, this server can accept instances beyond its '
             'capacity limits as a fallback when no ideal server is available.',
    )
    instance_count = fields.Integer(
        string='Running Instances',
        compute='_compute_capacity_usage',
    )
    allocated_cpu = fields.Float(
        string='Allocated CPU',
        compute='_compute_capacity_usage',
    )
    allocated_ram_gb = fields.Float(
        string='Allocated RAM (GB)',
        compute='_compute_capacity_usage',
    )

    # ===== Phase 4: per-tenant cost model (rate card) =====
    # Infra cost rates for tenants on this server, in the company currency per
    # month. Set them so (rate × provisioned resources) over the server's tenants
    # recovers the box's real monthly cost at target density. Per-server because
    # a small cluster and a beefy one have different $/resource.
    cost_per_cpu_month = fields.Float(
        string='Cost / CPU-core / month', default=0.0,
        help='Phase 4 cost model: monthly infra cost attributed per provisioned '
             'CPU core. Drives per-tenant cost → margin.')
    cost_per_gb_ram_month = fields.Float(
        string='Cost / GB-RAM / month', default=0.0,
        help='Monthly infra cost attributed per provisioned GB of RAM.')
    cost_per_gb_storage_month = fields.Float(
        string='Cost / GB-storage / month', default=0.0,
        help='Monthly infra cost attributed per GB of storage used.')
    monthly_cost = fields.Float(
        string='Server Monthly Cost', default=0.0,
        help='Reference: the real all-in monthly cost of this cluster '
             '(nodes + volumes + bandwidth). For sanity-checking the rate '
             'card against the sum of tenant costs.')
    compute_driver = fields.Selection(
        [('kubernetes', 'Kubernetes (Cluster)')],
        string='Deployment Type', default='kubernetes', required=True,
        help='Which backend this entry represents. This record is an '
             'allocation/capacity placeholder for a whole cluster, NOT a '
             'single server; the cluster\'s actual connection (kubeconfig, '
             'ingress) is configured on this entry\'s Region, since one '
             'region maps to one cluster.')
    registry_host = fields.Char(
        string='Container Registry Host',
        help="Phase 2.2: registry endpoint for immutable tenant images "
             "(e.g. 127.0.0.1:5000 self-hosted, or registry.digitalocean.com/<repo> "
             "in prod). When set, builds produce <registry>/tenant-<sub>:<sha> and "
             "deploy pulls by digest. Empty = legacy source-clone + build-on-host.")
    object_filestore_mount = fields.Char(
        string='Object-Storage Filestore Mount',
        help="Phase 2: host path of the object-storage-backed POSIX mount "
             "(JuiceFS over MinIO/Spaces, e.g. /mnt/jfs). When set, instances "
             "provisioned on this server place their Odoo filestore on "
             "<mount>/<partner>/<sub>/filestore (bind-mounted into the "
             "container) instead of local disk — making compute disposable. "
             "Leave empty to keep filestores on local disk.",
    )

    # ========== Health / Reachability ==========
    # A server whose cluster API can't be reached is dead capacity:
    # allocating a tenant to it strands the deploy and the customer's
    # project sits in "pending provision" until the 24h escalation. We probe
    # reachability and exclude unreachable hosts from allocation + capacity so
    # a customer is NEVER placed on a cluster we already know is down.
    health_state = fields.Selection(
        [('unknown', 'Unknown'),
         ('ok', 'Reachable'),
         ('unreachable', 'Unreachable')],
        string='Health', default='unknown', required=True, tracking=True,
        help='Reachability of this server\'s cluster API, refreshed by the '
             'health cron and at allocation time. Unreachable hosts are '
             'skipped when placing new instances.',
    )
    last_health_check = fields.Datetime(string='Last Health Check', readonly=True)
    last_health_error = fields.Char(string='Last Health Error', readonly=True)

    # Timeout for the reachability probe (seconds). Short on purpose: it
    # runs synchronously during allocation.
    _HEALTH_PROBE_TIMEOUT = 4

    def _probe_reachable(self, timeout=None):
        """Fast reachability probe, run inline at allocation.

        Returns ``(ok: bool, error: str)``.
        """
        self.ensure_one()
        return self._probe_kubernetes_reachable(timeout=timeout)

    def _probe_kubernetes_reachable(self, timeout=None):
        """Cheap reachability probe: load the region's kubeconfig and make
        one short-timeout API call. Reuses KubernetesDriver's own
        client-loading rather than duplicating the kubeconfig-parsing/auth
        logic here."""
        self.ensure_one()
        from ..drivers.kubernetes_driver import KubernetesDriver
        try:
            driver = KubernetesDriver(self)
            driver._core_api().list_namespace(
                limit=1, _request_timeout=timeout or self._HEALTH_PROBE_TIMEOUT)
            return True, ''
        except Exception as e:
            return False, str(e)

    @api.model_create_multi
    def create(self, vals_list):
        servers = super().create(vals_list)
        # New compute capacity -> let queued deploys retry immediately (PROV-004).
        if servers:
            self.env['saas.instance']._saas_flag_pending_for_retry()
        return servers

    def write(self, vals):
        # Detect newly-enabled overcommit to retry queued deploys without
        # waiting out their back-off (PROV-004).
        adds_capacity = (
            vals.get('allow_overcommit') and not all(self.mapped('allow_overcommit'))
        )
        res = super().write(vals)
        if adds_capacity:
            self.env['saas.instance']._saas_flag_pending_for_retry()
        return res

    def _update_health(self, ok, error=''):
        """Persist a probe result, logging on any state transition."""
        self.ensure_one()
        new_state = 'ok' if ok else 'unreachable'
        if self.health_state != new_state:
            _logger.warning(
                "Server %s health %s -> %s%s",
                self.name, self.health_state, new_state,
                (' (%s)' % error) if error else '',
            )
            # Page operators when a host goes down (SEC-009). Recovery (-> ok)
            # is a warning so the alert thread closes itself.
            self.env['saas.alert']._notify(
                'server_health',
                'Server %s health %s -> %s' % (self.name, self.health_state, new_state),
                level='error' if not ok else 'warning',
                detail=error or None,
            )
            # A host coming back healthy is new capacity — retry queued
            # deploys immediately instead of waiting out their back-off (PROV-004).
            if ok:
                self.env['saas.instance']._saas_flag_pending_for_retry()
        self.write({
            'health_state': new_state,
            'last_health_check': fields.Datetime.now(),
            'last_health_error': (error or '')[:500] if not ok else False,
        })

    @api.model
    def _cron_health_check(self):
        """Refresh reachability for every registered cluster.

        Keeps ``health_state`` current so allocation and region capacity can
        cheaply exclude dead clusters without probing on the hot path."""
        for server in self.search([]):
            ok, err = server._probe_reachable()
            server._update_health(ok, err)
            # Commit per server so one slow probe can't lose the rest.
            self.env.cr.commit()

    # ========== Capacity ==========

    def _compute_capacity_usage(self):
        """Compute current allocation from running/provisioning instances.

        Single batched query grouped by (docker_server_id, plan_id) so we
        never per-record `search()` regardless of how many servers are
        loaded. Plans are pre-fetched once.
        """
        Instance = self.env['saas.instance']
        active_states = ('provisioning', 'running')
        # Count of active instances per server
        count_data = Instance._read_group(
            [
                ('docker_server_id', 'in', self.ids),
                ('state', 'in', active_states),
            ],
            ['docker_server_id'],
            ['__count'],
        )
        count_map = {srv.id: count for srv, count in count_data}

        # Resource allocation per server, grouped by plan to amortise
        # plan-attribute lookups across all instances of the same plan.
        alloc_data = Instance._read_group(
            [
                ('docker_server_id', 'in', self.ids),
                ('state', 'in', active_states),
                ('plan_id', '!=', False),
            ],
            ['docker_server_id', 'plan_id'],
            ['__count'],
        )
        cpu_map = {sid: 0.0 for sid in self.ids}
        ram_map = {sid: 0.0 for sid in self.ids}
        for server, plan, count in alloc_data:
            cpu_map[server.id] = cpu_map.get(server.id, 0.0) + plan.cpu_limit * count
            ram_map[server.id] = ram_map.get(server.id, 0.0) + self._parse_ram_to_gb(
                plan.ram_limit or '0'
            ) * count

        for rec in self:
            rec.instance_count = count_map.get(rec.id, 0)
            rec.allocated_cpu = cpu_map.get(rec.id, 0.0)
            rec.allocated_ram_gb = ram_map.get(rec.id, 0.0)

    @staticmethod
    def _parse_ram_to_gb(ram_str):
        """Parse a RAM string like '512m', '1g', '2g' to GB float."""
        ram_str = (ram_str or '0').strip().lower()
        if ram_str.endswith('g'):
            return float(ram_str[:-1])
        if ram_str.endswith('m'):
            return float(ram_str[:-1]) / 1024.0
        try:
            return float(ram_str) / (1024.0 ** 3)
        except (ValueError, TypeError):
            return 0.0

    def _has_capacity_for(self, plan, ignore_limits=False):
        """Check if this server can accommodate one more instance.

        Args:
            plan: saas.plan record (or falsy) defining resource needs.
            ignore_limits: when True, skip capacity checks (overcommit mode).
        """
        self.ensure_one()
        if ignore_limits:
            return True
        if self.max_instances and self.instance_count >= self.max_instances:
            return False
        if plan and self.max_cpu_cores:
            if self.allocated_cpu + plan.cpu_limit > self.max_cpu_cores:
                return False
        if plan and self.max_ram_gb:
            ram_needed = self._parse_ram_to_gb(plan.ram_limit or '0')
            if self.allocated_ram_gb + ram_needed > self.max_ram_gb:
                return False
        return True

    @api.model
    def _region_match_domain(self, region):
        """Domain fragment matching servers in ``region``.

        Co-location + behaviour-neutral migration: a server with no
        ``region_id`` is treated as belonging to the DEFAULT region, so a
        fleet that has not yet been assigned regions keeps allocating
        exactly as before. A non-default region matches only servers
        explicitly assigned to it. ``region`` falsy/unknown -> no
        constraint (today's behaviour)."""
        if not region:
            return []
        if isinstance(region, int):
            region = self.env['saas.region'].sudo().browse(region)
        if not (region and region.exists()):
            return []
        if region.is_default:
            return ['|', ('region_id', '=', region.id), ('region_id', '=', False)]
        return [('region_id', '=', region.id)]

    @api.constrains('ip_v4', 'private_ip_v4', 'region_id')
    def _check_no_duplicate_machine(self):
        """One cluster = one server record.

        Public IPs are globally unique; private IPs only collide within the
        same region (different regions/VPCs may reuse private subnets).
        """
        default = self.env['saas.region']._get_default()

        def eff(server):
            return server.region_id or default

        for srv in self:
            if not srv.ip_v4 and not srv.private_ip_v4:
                continue
            for other in self.search([('id', '!=', srv.id)]):
                same_public = srv.ip_v4 and srv.ip_v4 == other.ip_v4
                same_private = (
                    srv.private_ip_v4
                    and srv.private_ip_v4 == other.private_ip_v4
                    and eff(srv) == eff(other)
                )
                if same_public or same_private:
                    raise ValidationError(_(
                        "Server '%(srv)s' has the same %(kind)s IP as "
                        "server '%(other)s'. One cluster must be a single "
                        "Server record — duplicating it as separate "
                        "records makes allocation treat it as two "
                        "independent clusters. Merge the records, or fix "
                        "the IP address.",
                        srv=srv.name, other=other.name,
                        kind=_('public') if same_public else _('private'),
                    ))

    @api.model
    def _allocate_docker_server(self, plan=None, raise_on_failure=False,
                               region=None):
        """Level 1 — Ideal allocation: least-loaded cluster with capacity.

        Returns a saas.server record, or None if no cluster qualifies.
        When *raise_on_failure* is True, raises ValidationError instead of
        returning None (used by strict provisioning mode). When *region*
        is set, only clusters in that region are considered (co-location).
        """
        # Exclude hosts already known to be unreachable (last health cron) so
        # we never even consider a dead cluster for a new customer.
        domain = [('health_state', '!=', 'unreachable')]
        candidates = self.search(domain + self._region_match_domain(region))
        if not candidates:
            if raise_on_failure:
                raise ValidationError(
                    _("No reachable compute clusters are available.")
                )
            return None

        eligible = candidates.filtered(lambda s: s._has_capacity_for(plan))
        if not eligible:
            if raise_on_failure:
                raise ValidationError(
                    _("All compute clusters are at full capacity. "
                      "No server can accommodate a new instance%s.")
                    % (' with plan "%s"' % plan.name if plan else '')
                )
            return None

        # Least-loaded first, then LIVE-probe so a customer is never placed on
        # a cluster that has gone down since the last health cron. A candidate
        # that fails the probe is marked unreachable and skipped — the deploy
        # falls over to the next healthy cluster instead of stranding the order.
        for server in eligible.sorted(key=lambda s: s.instance_count):
            ok, err = server._probe_reachable()
            server._update_health(ok, err)
            if ok:
                return server
        if raise_on_failure:
            raise ValidationError(
                _("No reachable compute cluster could be allocated — every "
                  "candidate failed a connectivity check.")
            )
        return None

    @api.model
    def _allocate_overcommit_server(self, plan=None, region=None):
        """Level 2 — Overcommit fallback: least-loaded cluster that allows
        overcommit.

        Ignores capacity limits, but only considers servers that have
        ``allow_overcommit`` enabled. When *region* is set, stays within
        that region (co-location).

        Returns a saas.server record, or None.
        """
        domain = [
            ('allow_overcommit', '=', True),
            ('health_state', '!=', 'unreachable'),
        ]
        candidates = self.search(domain + self._region_match_domain(region))
        if not candidates:
            return None
        # Live-probe (least-loaded first) so overcommit can't strand a deploy
        # on a dead cluster either.
        for server in candidates.sorted(key=lambda s: s.instance_count):
            ok, err = server._probe_reachable()
            server._update_health(ok, err)
            if ok:
                return server
        return None
