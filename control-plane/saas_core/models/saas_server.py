import logging
import re
import socket

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

from ..utils import SSHConnection

_logger = logging.getLogger(__name__)

_DB_NAME_RE = re.compile(r'^[a-z0-9-]+$')


class SaasServer(models.Model):
    _name = 'saas.server'
    _description = 'Server'
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

    # ========== Roles ==========
    is_docker_host = fields.Boolean(
        string='Docker Host',
        default=False,
        tracking=True,
        help='This server runs Docker containers for SaaS instances.',
    )
    is_db_server = fields.Boolean(
        string='Database Server',
        default=False,
        tracking=True,
        help='This server runs PostgreSQL for SaaS instance databases.',
    )
    is_proxy_server = fields.Boolean(
        string='Reverse Proxy',
        default=False,
        tracking=True,
        help='This server acts as a reverse proxy (Nginx) for routing '
             'traffic from wildcard domains to Docker servers.',
    )
    region_id = fields.Many2one(
        'saas.region',
        string='Region',
        index=True,
        tracking=True,
        help='Region this server belongs to. All servers serving one '
             'instance (proxy, docker, db) must share the same region '
             '(co-location). Used for region-based allocation and pricing.',
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        default=lambda self: self.env.company,
        index=True,
        help='Company that owns this server. Used by the multi-company '
             'record rule — set to empty for shared infrastructure.',
    )
    expected_host_key_fingerprint = fields.Char(
        string='Expected SSH Host Key (SHA256)',
        groups='saas_core.group_saas_manager',
        help='Pinned SHA-256 fingerprint of the SSH host key. When set, '
             'connections that present a different fingerprint are refused '
             '(MITM protection). Format: "SHA256:base64string" — copy from '
             '`ssh-keyscan host | ssh-keygen -lf -`. Leave empty to allow '
             'any host key (TOFU-on-first-use, INSECURE).',
    )

    # ========== Topology ==========
    db_server_id = fields.Many2one(
        'saas.server',
        string='Database Server',
        tracking=True,
        domain="[('is_db_server', '=', True)]",
        help='The PostgreSQL server that instances on this Docker host '
             'should use. Set to this server itself for an all-in-one setup, '
             'or to a dedicated DB server for a distributed topology.',
    )

    # ========== Capacity ==========
    max_instances = fields.Integer(
        string='Max Instances',
        default=0,
        help='Maximum number of running instances on this Docker host. '
             '0 = unlimited.',
    )
    max_cpu_cores = fields.Float(
        string='Max CPU Cores',
        default=0,
        help='Total CPU cores available for allocation on this Docker host. '
             '0 = unlimited.',
    )
    max_ram_gb = fields.Float(
        string='Max RAM (GB)',
        default=0,
        help='Total RAM in GB available for allocation on this Docker host. '
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

    # ========== SSH Configuration ==========
    ssh_key_pair_id = fields.Many2one(
        'saas.ssh.key.pair',
        string='SSH Key Pair',
        help='SSH key used to authenticate when connecting to this server.',
    )
    ssh_user = fields.Char(
        string='SSH User',
        default='root',
        help='Operating system user for the SSH connection (e.g. root, ubuntu).',
    )
    ssh_port = fields.Integer(
        string='SSH Port',
        default=22,
        help='TCP port on which the SSH daemon listens.',
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
    ssh_connect_using = fields.Selection(
        selection=[
            ('public_ip', 'Public IP'),
            ('private_ip', 'Private IP'),
        ],
        string='Connect via',
        default='public_ip',
        required=True,
        help='Which IP address the SaaS manager should use when opening SSH sessions.',
    )

    # ========== Docker Host Fields ==========
    docker_base_path = fields.Char(
        string='Docker Base Path',
        default='/home/odoo',
        help='Root directory on the server where instance folders are created '
             '(e.g. /home/odoo). Each instance gets a sub-folder here.',
    )
    docker_container_ids = fields.One2many(
        'saas.docker.container',
        'server_id',
        string='Docker Containers',
        help='Containers currently running on this server (populated via Refresh).',
    )
    # ===== Phase 4: per-tenant cost model (rate card) =====
    # Infra cost rates for tenants on this server, in the company currency per
    # month. Set them so (rate × provisioned resources) over the server's tenants
    # recovers the box's real monthly cost at target density. Per-server because
    # a small droplet and a beefy host have different $/resource.
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
        help='Reference: the real all-in monthly cost of this box (droplet + '
             'volumes + bandwidth). For sanity-checking the rate card against '
             'the sum of tenant costs.')
    compute_driver = fields.Selection(
        [('ssh_docker', 'Docker over SSH'), ('kubernetes', 'Kubernetes')],
        string='Compute Driver', default='ssh_docker', required=True,
        help='Phase 6: which ComputeDriver backend runs tenants on this server. '
             'The Control Plane is identical for both — selecting "Kubernetes" '
             'routes the same business logic through KubernetesDriver instead of '
             'SshDockerDriver (proves the Phase-1 seam: a new backend is a new '
             'file, not a rewrite).')
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

    # ========== Database Server Fields ==========
    psql_port = fields.Integer(
        string='PostgreSQL Port',
        default=5432,
        help='TCP port on which the PostgreSQL service listens.',
    )

    # ========== Health / Reachability ==========
    # A server whose SSH port can't be reached is a dead host: allocating a
    # tenant to it strands the deploy (SSH times out) and the customer's
    # project sits in "pending provision" until the 24h escalation. We probe
    # reachability and exclude unreachable hosts from allocation + capacity so
    # a customer is NEVER placed on a host we already know is down.
    health_state = fields.Selection(
        [('unknown', 'Unknown'),
         ('ok', 'Reachable'),
         ('unreachable', 'Unreachable')],
        string='Health', default='unknown', required=True, tracking=True,
        help='Reachability of this server\'s SSH port, refreshed by the health '
             'cron and at allocation time. Unreachable hosts are skipped when '
             'placing new instances.',
    )
    last_health_check = fields.Datetime(string='Last Health Check', readonly=True)
    last_health_error = fields.Char(string='Last Health Error', readonly=True)

    # TCP-connect timeout for the reachability probe (seconds). Short on
    # purpose: it runs synchronously during allocation.
    _HEALTH_PROBE_TIMEOUT = 4

    def _probe_reachable(self, timeout=None):
        """Fast TCP-connect probe to the server's SSH endpoint.

        Returns ``(ok: bool, error: str)``. A plain TCP connect (no SSH
        handshake / auth) is enough to catch the failure that strands a
        customer deploy: a host that is down, a wrong / placeholder IP
        (e.g. an RFC-5737 ``203.0.113.x`` test address), a closed port, or
        a dropped/filtered route. Cheap enough to run inline at allocation.
        """
        self.ensure_one()
        try:
            ip = self._get_ssh_ip()
        except Exception as e:  # missing/invalid IP config == unreachable
            return False, str(e)
        port = self.ssh_port or 22
        try:
            with socket.create_connection(
                (ip, port), timeout=timeout or self._HEALTH_PROBE_TIMEOUT):
                return True, ''
        except OSError as e:
            return False, '%s:%s — %s' % (ip, port, e)

    @api.model_create_multi
    def create(self, vals_list):
        servers = super().create(vals_list)
        # New compute capacity -> let queued deploys retry immediately (PROV-004).
        if any(s.is_docker_host for s in servers):
            self.env['saas.instance']._saas_flag_pending_for_retry()
        return servers

    def write(self, vals):
        # Detect newly-enabled capacity (docker host / overcommit) to retry
        # queued deploys without waiting out their back-off (PROV-004).
        adds_capacity = (
            (vals.get('is_docker_host') and not all(self.mapped('is_docker_host')))
            or (vals.get('allow_overcommit') and not all(self.mapped('allow_overcommit')))
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
            if ok and self.is_docker_host:
                self.env['saas.instance']._saas_flag_pending_for_retry()
        self.write({
            'health_state': new_state,
            'last_health_check': fields.Datetime.now(),
            'last_health_error': (error or '')[:500] if not ok else False,
        })

    @api.model
    def _cron_health_check(self):
        """Refresh reachability for every infra server (docker / db / proxy).

        Keeps ``health_state`` current so allocation and region capacity can
        cheaply exclude dead hosts without probing on the hot path."""
        servers = self.search([
            '|', '|',
            ('is_docker_host', '=', True),
            ('is_db_server', '=', True),
            ('is_proxy_server', '=', True),
        ])
        for server in servers:
            ok, err = server._probe_reachable()
            server._update_health(ok, err)
            # Commit per server so one slow probe can't lose the rest.
            self.env.cr.commit()

    # ========== Capacity ==========

    @api.depends('is_docker_host')
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
            if rec.is_docker_host:
                rec.allocated_cpu = cpu_map.get(rec.id, 0.0)
                rec.allocated_ram_gb = ram_map.get(rec.id, 0.0)
            else:
                rec.allocated_cpu = 0.0
                rec.allocated_ram_gb = 0.0

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
        """Check if this Docker host can accommodate one more instance.

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

    @api.constrains('region_id', 'db_server_id')
    def _check_db_server_same_region(self):
        """Co-location: a Docker host and the DB server it points at must be
        in the same region. A server with no region counts as the default
        region, so an un-regioned fleet is unaffected (behaviour-neutral)."""
        default = self.env['saas.region']._get_default()

        def eff(server):
            # Effective region: explicit, else the default region.
            return server.region_id or default

        for srv in self:
            if not srv.db_server_id or srv.db_server_id == srv:
                continue
            host_region = eff(srv)
            db_region = eff(srv.db_server_id)
            if host_region and db_region and host_region != db_region:
                raise ValidationError(_(
                    "Co-location: Docker host '%(host)s' is in region "
                    "'%(hr)s' but its database server '%(db)s' is in region "
                    "'%(dr)s'. The Docker, DB and proxy servers serving an "
                    "instance must all be in the same region.",
                    host=srv.name, hr=host_region.name,
                    db=srv.db_server_id.name, dr=db_region.name,
                ))

    @api.constrains('ip_v4', 'private_ip_v4', 'region_id')
    def _check_no_duplicate_machine(self):
        """One physical machine = one server record (with role flags).

        Same-machine detection everywhere (db_host, nginx upstream, port
        bindings) compares server *records*, so modeling one machine as
        two records silently flips deployments onto the remote code
        path: TCP to the database instead of the local socket, ports
        bound beyond loopback, proxy traffic over the network. Public
        IPs are globally unique; private IPs only collide within the
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
                        "server '%(other)s'. One physical machine must be "
                        "a single Server record with multiple roles "
                        "(Docker Host / Database Server / Reverse Proxy) "
                        "— duplicating it as separate records makes "
                        "deployments treat it as two machines and route "
                        "same-server traffic over the network. Merge the "
                        "records, or fix the IP address.",
                        srv=srv.name, other=other.name,
                        kind=_('public') if same_public else _('private'),
                    ))

    @api.model
    def _allocate_docker_server(self, plan=None, raise_on_failure=False,
                               region=None):
        """Level 1 — Ideal allocation: least-loaded host with capacity.

        Returns a saas.server record, or None if no host qualifies.
        When *raise_on_failure* is True, raises ValidationError instead of
        returning None (used by strict provisioning mode). When *region*
        is set, only hosts in that region are considered (co-location)."""
        # Exclude hosts already known to be unreachable (last health cron) so
        # we never even consider a dead box for a new customer.
        candidates = self.search(
            [('is_docker_host', '=', True),
             ('health_state', '!=', 'unreachable')]
            + self._region_match_domain(region)
        )
        if not candidates:
            if raise_on_failure:
                raise ValidationError(
                    _("No reachable Docker host servers are available.")
                )
            return None

        eligible = candidates.filtered(lambda s: s._has_capacity_for(plan))
        if not eligible:
            if raise_on_failure:
                raise ValidationError(
                    _("All Docker host servers are at full capacity. "
                      "No server can accommodate a new instance%s.")
                    % (' with plan "%s"' % plan.name if plan else '')
                )
            return None

        # Least-loaded first, then LIVE-probe so a customer is never placed on
        # a host that has gone down since the last health cron. A candidate
        # that fails the probe is marked unreachable and skipped — the deploy
        # falls over to the next healthy host instead of stranding the order.
        for server in eligible.sorted(key=lambda s: s.instance_count):
            ok, err = server._probe_reachable()
            server._update_health(ok, err)
            if ok:
                return server
        if raise_on_failure:
            raise ValidationError(
                _("No reachable Docker host could be allocated — every "
                  "candidate failed a connectivity check.")
            )
        return None

    @api.model
    def _allocate_overcommit_server(self, plan=None, region=None):
        """Level 2 — Overcommit fallback: least-loaded host that allows overcommit.

        Ignores capacity limits, but only considers servers that have
        ``allow_overcommit`` enabled. When *region* is set, stays within
        that region (co-location).

        Returns a saas.server record, or None.
        """
        candidates = self.search([
            ('is_docker_host', '=', True),
            ('allow_overcommit', '=', True),
            ('health_state', '!=', 'unreachable'),
        ] + self._region_match_domain(region))
        if not candidates:
            return None
        # Live-probe (least-loaded first) so overcommit can't strand a deploy
        # on a dead host either.
        for server in candidates.sorted(key=lambda s: s.instance_count):
            ok, err = server._probe_reachable()
            server._update_health(ok, err)
            if ok:
                return server
        return None

    # ========== SSH Methods ==========

    def _get_ssh_ip(self):
        """Return the IP to use for SSH based on ssh_connect_using."""
        self.ensure_one()
        if self.ssh_connect_using == 'private_ip':
            if not self.private_ip_v4:
                raise ValidationError(
                    _("Private IP address is required on server '%s' "
                      "when SSH is set to use Private IP.")
                    % self.name
                )
            return self.private_ip_v4
        if not self.ip_v4:
            raise ValidationError(
                _("Public IP address is required on server '%s'.") % self.name
            )
        return self.ip_v4

    def _fetch_database_sizes(self, subdomains):
        """Return ``{subdomain: total_size_bytes}`` summing EVERY database
        owned by each subdomain.

        A hosting customer can create many databases; they're all named
        either exactly ``<subdomain>`` or with the ``<subdomain>_*``
        prefix. All of them must count against the plan's storage
        allowance — not just a single base DB. One SSH + one psql call
        lists every database's size, then we aggregate by owning subdomain
        in Python. The ``_`` separator makes the prefix match unambiguous
        (subdomains can't contain ``_`` — see ``_DB_NAME_RE``); we still
        match the longest subdomain first as a safety net for nested names.

        Single SSH + single psql call instead of N. Used by the usage-
        and storage-check crons to avoid opening a fresh paramiko
        handshake per instance.
        """
        self.ensure_one()
        # Re-validate names (defence in depth — caller should have done it).
        subs = [s for s in subdomains if s and _DB_NAME_RE.match(s)]
        if not subs:
            return {}
        cmd = (
            "sudo -u postgres psql -At -F '|' -c "
            "\"SELECT datname, pg_database_size(datname) FROM pg_database "
            "WHERE datistemplate = false AND datname <> 'postgres';\""
        )
        with self._get_ssh_connection() as ssh:
            exit_code, stdout, _stderr = ssh.execute(cmd)
        if exit_code != 0:
            return {}
        totals = {s: 0 for s in subs}
        ordered = sorted(subs, key=len, reverse=True)  # longest prefix wins
        for line in stdout.splitlines():
            parts = line.strip().split('|', 1)
            if len(parts) != 2:
                continue
            datname = parts[0].strip()
            try:
                size = int(parts[1])
            except (ValueError, TypeError):
                continue
            for s in ordered:
                if datname == s or datname.startswith(s + '_'):
                    totals[s] += size
                    break
        return totals

    def _get_ssh_connection(self):
        """Return an SSHConnection context manager for this server."""
        self.ensure_one()
        # Read the private key with sudo() so callers without manager
        # privileges (e.g. cron-triggered controllers in the future)
        # don't trigger an AccessError on the field-level group on
        # `private_key_file` / `expected_host_key_fingerprint`. The
        # cleartext key never leaves this module.
        keypair = self.sudo().ssh_key_pair_id
        key_b64 = keypair._private_key_b64() if keypair else False
        if not keypair or not key_b64:
            raise ValidationError(
                _("SSH key pair with a private key file is required on server '%s'.")
                % self.name
            )
        ssh_ip = self._get_ssh_ip()
        return SSHConnection(
            host=ssh_ip,
            port=self.ssh_port or 22,
            user=self.ssh_user or 'root',
            private_key_b64=key_b64,
            key_type=keypair.type or 'rsa',
            expected_host_key=self.sudo().expected_host_key_fingerprint or None,
        )

    def action_test_connection(self):
        """Test SSH connection to the server."""
        self.ensure_one()
        try:
            ssh_ip = self._get_ssh_ip()
            with self._get_ssh_connection() as ssh:
                exit_code, stdout, stderr = ssh.execute(
                    'echo "Connection OK" && hostname'
                )
            if exit_code == 0:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _("Connection Successful"),
                        'message': _(
                            "SSH connection to %s succeeded. Hostname: %s"
                        ) % (ssh_ip, stdout.strip()),
                        'type': 'success',
                        'sticky': False,
                    },
                }
            else:
                raise UserError(
                    _("Connection test command failed:\n%s") % stderr
                )
        except (UserError, ValidationError):
            raise
        except Exception as e:
            raise UserError(
                _("SSH connection failed:\n%s") % str(e)
            )

    def action_open_terminal(self):
        """Open a web-based SSH terminal to this server."""
        self.ensure_one()
        self._get_ssh_ip()
        if not self.ssh_key_pair_id or not self.ssh_key_pair_id._private_key_b64():
            raise ValidationError(
                _("SSH key pair with a private key file is required on server '%s'.")
                % self.name
            )
        return {
            'type': 'ir.actions.client',
            'tag': 'ssh_terminal',
            'name': _("Terminal: %s") % self.name,
            'context': {
                'server_model': self._name,
                'server_id': self.id,
                'server_name': self.name,
            },
        }

    # ========== Docker Host Actions ==========

    def action_refresh_containers(self):
        """Fetch all Docker containers from the server via SSH and update the list."""
        self.ensure_one()
        separator = '|||'
        fmt = separator.join([
            '{{.ID}}', '{{.Image}}', '{{.Command}}',
            '{{.CreatedAt}}', '{{.Status}}', '{{.Ports}}', '{{.Names}}',
        ])
        cmd = "docker ps -a --format '%s' --no-trunc" % fmt

        try:
            with self._get_ssh_connection() as ssh:
                exit_code, stdout, stderr = ssh.execute(cmd)
                if exit_code != 0:
                    raise UserError(
                        _("Failed to list containers:\n%s") % stderr
                    )
        except (UserError, ValidationError):
            raise
        except Exception as e:
            raise UserError(
                _("SSH connection failed:\n%s") % str(e)
            )

        container_model = self.env['saas.docker.container']
        existing = {c.container_id: c for c in self.docker_container_ids}
        seen_ids = set()

        for line in stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(separator)
            if len(parts) < 7:
                continue
            cid = parts[0][:12]
            seen_ids.add(cid)
            vals = {
                'image': parts[1],
                'command': parts[2],
                'created': parts[3],
                'status': parts[4],
                'ports': parts[5],
                'name': parts[6],
            }
            if cid in existing:
                existing[cid].write(vals)
            else:
                vals.update({
                    'server_id': self.id,
                    'container_id': cid,
                })
                container_model.create(vals)

        # Remove containers that no longer exist on the server
        stale = container_model.browse([
            c.id for cid, c in existing.items() if cid not in seen_ids
        ])
        if stale:
            stale.unlink()
