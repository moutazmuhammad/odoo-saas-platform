import base64
import datetime
import json
import logging
import os
import re
import secrets
import shlex
import string
import threading
import time
from jinja2 import Environment, FileSystemLoader

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError

from ..utils import run_in_background
from ..fields import EncryptedChar
from .saas_instance_backup import DEFAULT_MAX_BACKUPS

_logger = logging.getLogger(__name__)

# Per-instance locks serialising the one-time template-DB build so two
# concurrent first-creates can't both run the init (or one drop the
# other's in-flight build). Keyed by instance id; only the build's
# critical section is held, and only same-instance builds contend.
_HOSTING_TEMPLATE_BUILD_LOCKS = {}
_HOSTING_TEMPLATE_BUILD_LOCKS_GUARD = threading.Lock()


def _hosting_template_build_lock(instance_id):
    with _HOSTING_TEMPLATE_BUILD_LOCKS_GUARD:
        lock = _HOSTING_TEMPLATE_BUILD_LOCKS.get(instance_id)
        if lock is None:
            lock = threading.Lock()
            _HOSTING_TEMPLATE_BUILD_LOCKS[instance_id] = lock
        return lock


TEMPLATES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'templates',
)

_JINJA_ENV = Environment(
    loader=FileSystemLoader(TEMPLATES_PATH),
    keep_trailing_newline=True,
)

SUBDOMAIN_RE = re.compile(r'^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$')
DB_USER_RE = re.compile(r'^[a-z_][a-z0-9_]*$')


# ----------------------------------------------------------------------
# Usage metrics come from each region's Prometheus (CPU/RAM) and an
# in-pod measurement (storage). The dashboard's live poll is cached per
# instance so many viewers cost one query; history is queried from
# Prometheus directly (its retention covers METRIC_RETENTION_DAYS).
# ----------------------------------------------------------------------
LIVE_METRICS_CACHE_TTL = 5           # secs a live CPU/RAM reading is reused
METRIC_RETENTION_DAYS = 14           # longest history window offered
_USAGE_REFRESH_BATCH = 100           # tenants per usage-refresh cron run
_USAGE_MAX_STORAGE_FAILURES = 3      # consecutive failures before skipping a server
_LIVE_METRICS_CACHE = {}             # instance_id -> (monotonic ts, payload)
_LIVE_METRICS_GUARD = threading.Lock()

# Host-side flock file used to serialise nginx vhost mutations on a proxy.
# Every write/reload/remove on a given proxy host grabs this lock so
# concurrent provisions sharing one proxy can't race `nginx -t`/reload and
# corrupt the routing table for co-located tenants (SCALE-001).
_NGINX_LOCK_FILE = '/run/lock/saas-nginx.lock'


class SaasInstance(models.Model):
    _name = 'saas.instance'
    _description = 'SaaS Instance'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'portal.mixin']
    _order = 'create_date desc'

    # ========== Identity ==========
    subdomain = fields.Char(
        string='Subdomain',
        required=True,
        tracking=True,
        help='Unique subdomain prefix for this instance (e.g. "acme"). '
             'Combined with the base domain to form the full URL.',
    )
    domain_id = fields.Many2one(
        'saas.based.domain',
        string='Base Domain',
        required=True,
        ondelete='restrict',
        index=True,
        default=lambda self: self.env['saas.based.domain'].search([], limit=1),
        help='The parent domain under which this instance is hosted '
             '(e.g. "odoo.example.com").',
    )
    name = fields.Char(
        string='Instance Name',
        compute='_compute_name',
        store=True,
        help='Full hostname of the instance, computed from subdomain and base domain.',
    )
    partner_id = fields.Many2one(
        'res.partner',
        string='Customer',
        tracking=True,
        ondelete='restrict',
        index=True,
        help='The customer who owns this Odoo instance.',
    )
    url = fields.Char(
        string='URL',
        compute='_compute_url',
        store=True,
        help='Public HTTPS URL to access this instance.',
    )

    # ========== Service & Plan ==========
    saas_product_id = fields.Many2one(
        'saas.product',
        string='Service',
        tracking=True,
        ondelete='restrict',
        index=True,
        help='The service/product this instance provides '
             '(e.g. "Pharmacy Management", "POS").',
    )
    plan_id = fields.Many2one(
        'saas.plan',
        string='Plan',
        tracking=True,
        ondelete='restrict',
        index=True,
        help='Resource plan defining CPU, RAM, and storage limits for this instance.',
    )

    # ========== Infrastructure ==========
    odoo_version_id = fields.Many2one(
        'saas.odoo.version',
        string='Odoo Version',
        tracking=True,
        ondelete='restrict',
        help='Odoo version and Docker image used by this instance.',
    )

    @api.onchange('saas_product_id')
    def _onchange_saas_product_id(self):
        if self.saas_product_id:
            self.odoo_version_id = self.saas_product_id.odoo_version_id
            if self.plan_id and self.plan_id.saas_product_ids and self.saas_product_id not in self.plan_id.saas_product_ids:
                self.plan_id = False
    docker_server_id = fields.Many2one(
        'saas.server',
        string='Compute Target',
        tracking=True,
        ondelete='restrict',
        index=True,
        help='Where this instance actually runs: a Kubernetes cluster '
             'registration (a saas.server row represents a node/entry in '
             'that cluster; its connection details live on the server\'s '
             'Region, not here). Leave empty for automatic allocation '
             'based on capacity.',
    )
    compute_driver = fields.Selection(
        related='docker_server_id.compute_driver', string='Deployment',
        store=True, readonly=True,
        help='Which backend this instance actually runs on. Kubernetes is '
             'the only backend today (the earlier Docker Compose-over-SSH '
             'backend was removed). A convenience passthrough of '
             'docker_server_id.compute_driver so it\'s glanceable on the '
             'instance list/form and usable for grouping/filtering '
             '(billing/pricing architecture redesign, Part 4/5) without '
             'opening the server record. Stored so it can be grouped by '
             'in the Profitability dashboard.',
    )
    region_id = fields.Many2one(
        'saas.region',
        string='Region',
        index=True,
        ondelete='restrict',
        default=lambda self: self.env['saas.region']._recommended_available()
        or self.env['saas.region']._get_default(),
        help='Region the instance is hosted in. Chosen at creation and '
             'fixed thereafter — drives region pricing and constrains '
             'server allocation (proxy/docker/db all in this region). '
             'Empty on legacy instances (treated as the default region, '
             'multiplier 1.0).',
    )
    # support_plan_id lives in saas_billing/models/saas_instance.py now —
    # its comodel (saas.support.plan) moved there, and a Many2one's
    # comodel must belong to an already-loaded module at _auto_init time,
    # so the field declaration has to move with it (see the plan doc's
    # "Critical finding"). Read freely from here via self.support_plan_id
    # — Odoo merges _inherit contributions into one class regardless of
    # which addon's file declares the field.
    db_server_id = fields.Many2one(
        'saas.server',
        string='Database Server',
        tracking=True,
        ondelete='restrict',
        index=True,
        help='PostgreSQL server that hosts the database for this instance. '
             'Not used for Kubernetes deploys today (the managed database '
             'runs inside the tenant\'s own cluster/namespace) — kept for a '
             'possible future external-DB topology.',
    )
    provisioning_mode = fields.Selection(
        selection=[
            ('strict', 'Strict'),
            ('flexible', 'Flexible'),
            ('manual', 'Manual'),
        ],
        string='Provisioning Mode',
        default='flexible',
        required=True,
        help='Controls how servers are allocated for this instance:\n'
             '- Strict: fail if no server has capacity.\n'
             '- Flexible: fallback to overcommit or pending state.\n'
             '- Manual: skip auto-allocation, expect manual assignment.',
    )
    is_overcommitted = fields.Boolean(
        string='Overcommitted',
        default=False,
        readonly=True,
        help='Set automatically when the instance was placed on a server '
             'that exceeded its capacity limits (overcommit fallback).',
    )
    deploy_retry_count = fields.Integer(
        string='Deploy Retries',
        default=0,
        readonly=True,
        help='Number of times deployment has been automatically retried.',
    )
    max_deploy_retries = fields.Integer(
        string='Max Retries',
        default=3,
        help='Maximum automatic deploy retries before marking as permanently failed. '
             '0 = no auto-retry.',
    )
    pending_provision_since = fields.Datetime(
        string='Pending Since',
        readonly=True,
        help='Timestamp when the instance first entered pending_provision. '
             'Used by the retry cron to back off + give up after a max '
             'wait window so we never infinite-loop on capacity-blocked '
             'deploys.',
    )
    pending_provision_attempts = fields.Integer(
        string='Pending Retry Count',
        default=0,
        readonly=True,
        help='Number of times the retry cron has attempted to deploy '
             'this pending instance. Drives exponential back-off.',
    )
    pending_retry_now = fields.Boolean(
        string='Retry Now',
        default=False,
        copy=False,
        help='Set when server capacity/health changes so the retry cron skips '
             'the exponential back-off and re-attempts this pending instance '
             'on its next tick (PROV-004), instead of waiting hours.',
    )
    pre_provisioning_state = fields.Char(
        string='Pre-Provisioning State',
        readonly=True,
        help='State before entering provisioning. Used to recover instances '
             'stuck in provisioning after a server restart.',
    )
    pending_operation = fields.Selection(
        selection=[
            ('deploy', 'Deploy'),
            ('redeploy', 'Redeploy'),
            ('start', 'Start'),
            ('stop', 'Stop'),
            ('restart', 'Restart'),
            ('suspend', 'Suspend'),
            ('restore', 'Restore Backup'),
            ('cancel', 'Cancel'),
            ('delete', 'Delete'),
        ],
        readonly=True,
        help='Operation in progress while state=provisioning. Used by the '
             'recovery cron to decide whether stuck instances can be safely '
             'auto-recovered or whether manual intervention is required '
             '(destructive operations like delete/cancel must NOT be '
             'auto-reverted to a "live" state).',
    )
    xmlrpc_port = fields.Integer(
        string='HTTP Port',
        readonly=True,
        help='Host port mapped to the Odoo XML-RPC / HTTP interface inside the container.',
    )
    longpolling_port = fields.Integer(
        string='Longpolling Port',
        readonly=True,
        help='Host port mapped to the Odoo longpolling / websocket interface inside the container.',
    )

    # ========== Credentials ==========
    admin_password = EncryptedChar(
        string='Admin Master Password',
        readonly=True,
        groups='saas_core.group_saas_manager',
        help='Odoo master password (admin_passwd in odoo.conf). '
             'Used for database management operations.',
    )
    db_user = fields.Char(
        string='Database User',
        readonly=True,
        groups='saas_core.group_saas_manager',
        help='PostgreSQL role name created for this instance.',
    )
    db_password = EncryptedChar(
        string='Database Password',
        readonly=True,
        groups='saas_core.group_saas_manager',
        help='Password for the PostgreSQL role used by this instance.',
    )

    restore_banner_dismissed = fields.Boolean(
        string='Restore Banner Dismissed',
        default=False,
        help='Client dismissed the data restore suggestion banner.',
    )
    # ========== Hosting ==========
    is_hosting = fields.Boolean(
        string='Hosting Instance',
        compute='_compute_is_hosting',
        store=True,
        help='True if this instance is a self-managed hosting instance.',
    )
    pip_packages = fields.Text(
        string='PyPI Packages',
        help='Python packages to install via pip on container startup. '
             'One package per line (e.g. phonenumbers, openpyxl).',
    )
    pip_install_error = fields.Text(
        string='Last Package Install Error',
        copy=False,
        help='Output of the last failed pip install, surfaced to the '
             'customer. Empty when the most recent install succeeded.',
    )
    package_ids = fields.One2many(
        'saas.instance.package',
        'instance_id',
        string='Python Packages',
        help='Python packages to install via pip on container startup.',
    )

    @api.depends('saas_product_id.is_hosting')
    def _compute_is_hosting(self):
        for rec in self:
            rec.is_hosting = rec.saas_product_id.is_hosting if rec.saas_product_id else False

    # ========== Hosting add-ons ==========
    daily_backup_enabled = fields.Boolean(
        string='Daily Backups',
        default=False,
        tracking=True,
        help='Hosting-only: when enabled, the daily backup cron creates '
             'an incremental, deduplicated snapshot of the entire instance '
             '(databases + filestore + addons + config + docker-compose '
             '+ pip requirements) using restic. Last 7 days are retained. '
             'Billed as an add-on on the SAME period as the subscription '
             '(merged into the plan renewal) at the rate configured in SaaS '
             'settings.',
    )
    daily_backup_suspended = fields.Boolean(
        string='Daily Backups Paused',
        default=False,
        copy=False,
        tracking=True,
        help='Set when the monthly daily-backup add-on invoice is '
             'overdue: the nightly snapshot is skipped until the '
             'invoice is paid, at which point snapshots resume '
             'automatically. The add-on stays subscribed (we do not '
             'lose the next-invoice anchor) — it is paused, not '
             'cancelled.',
    )
    restic_password = EncryptedChar(
        string='Restic Repository Password',
        readonly=True,
        groups='saas_core.group_saas_manager',
        help='AES-256 password for this instance\'s restic backup '
             'repository. Generated on the first daily backup. NEVER '
             'shared with the customer. Loss of this value renders '
             'all daily snapshots unrecoverable — back up the saas '
             'master database appropriately.',
    )
    # ---------- Compute tier (/ROADMAP.md §5 Phase 2) ----------
    # Selectable Kubernetes pod-replica tier (Standard=1 / HA=2 / Scale=4,
    # extensible — see saas.compute.tier). Deliberately NOT a boolean HA
    # flag: replica count and "High Availability" are related but not the
    # same thing (HA is just the commercial name for the 2-replica tier;
    # a customer might pick Scale for capacity without caring about the
    # HA framing). Does NOT change which backend the instance runs on
    # (that's saas_master.default_compute_driver, an independent,
    # platform-level choice) — only ever meaningful on an instance already
    # on the Kubernetes backend, see action_change_compute_tier.
    compute_tier_id = fields.Many2one(
        'saas.compute.tier',
        string='Compute Tier',
        ondelete='restrict',
        default=lambda self: self.env['saas.compute.tier'].get_default(),
        tracking=True,
        help='Kubernetes pod-replica tier for this instance (Standard/HA/'
             'Scale — see saas.compute.tier). Meaningless on a Docker-'
             'Compose-backed instance. Changing it patches the running '
             'instance in place (KubernetesDriver.scale) — an upgrade '
             '(more replicas) is billed immediately (prorated); a '
             'downgrade (fewer replicas) applies immediately with no '
             'refund for the current period.',
    )
    # ---------- Cancellation cleanup retry flags ----------
    # Set by ``_do_delete_instance`` when the corresponding cleanup
    # step (PG drop / nginx remove) raised. ``action_reactivate``
    # checks these and retries before clearing the infrastructure
    # FKs, so a stale role / vhost can't block the new deploy.
    pg_cleanup_pending = fields.Boolean(
        string='PG Cleanup Pending',
        copy=False,
        default=False,
        help='True if the PostgreSQL drop failed during the last '
             'cancellation. Cleared once the retry succeeds.',
    )
    nginx_cleanup_pending = fields.Boolean(
        string='Nginx Cleanup Pending',
        copy=False,
        default=False,
        help='True if the Nginx vhost removal failed during the last '
             'cancellation. Cleared once the retry succeeds.',
    )

    # ========== Free Trial ==========
    is_trial = fields.Boolean(
        string='Free Trial',
        default=False,
        tracking=True,
        index=True,
        help='Whether this instance was created during the client free trial.',
    )


    # The commercial half of saas.instance — sale order / invoice links,
    # billing period, pending/scheduled plan changes, renewal dates,
    # auto-renew + saved payment method, add-on activation invoices — lives
    # in saas_billing/models/saas_instance.py (same table, _inherit). Core
    # only reaches billing through the no-op hooks in "Billing hooks" below.

    # ===== Storage capacity (v47) — "capacity upgrade experience" =====
    # Storage is handled ONLY via plan upgrade or purchased storage blocks.
    # There is NO usage-based / per-GB billing. These fields drive a
    # positive, never-punitive capacity flow (warn → full → grace →
    # paused-until-upgrade), with messaging framed as scaling, not penalty.
    extra_storage_blocks = fields.Integer(
        string='Purchased Storage Blocks', copy=False, default=0,
        help='Storage blocks the customer bought to expand capacity. Each '
             'block adds saas_master.storage_block_gb GB and is billed at '
             'the block price every cycle (a deliberate purchase, never an '
             'automatic overage charge).',
    )
    effective_storage_limit_gb = fields.Float(
        string='Total Capacity (GB)', compute='_compute_effective_storage_limit',
        store=True,
        help='Included plan storage plus purchased storage blocks.',
    )
    storage_state = fields.Selection([
        ('ok', 'OK'),
        ('warn80', 'Reaching capacity (80%)'),
        ('full', 'At capacity (100%)'),
        ('grace', 'At capacity — grace period'),
        ('restricted', 'Paused — awaiting upgrade'),
    ], string='Capacity State', default='ok', copy=False, index=True,
        help='Internal capacity state. Customer-facing copy is always '
             'positive (see _capacity_summary).')
    storage_over_since = fields.Date(
        string='At Capacity Since', copy=False,
        help='Date usage first reached 100% of total capacity — anchors the '
             'configurable grace period before the workspace is paused.',
    )
    storage_last_notice = fields.Selection([
        ('warn80', '80%'), ('full', '100%'),
        ('grace', 'grace'), ('restricted', 'paused'),
    ], string='Last Capacity Notice', copy=False,
        help='Highest capacity notice already sent, so each stage notifies '
             'once. Reset when usage falls back to healthy.')

    # ===== Odoo.sh-style environments (hosting only) =====
    # A hosting subscription is a "project": exactly one Production environment
    # (the billing anchor that owns the subscription) plus any number of
    # Staging / Development child servers. Children never self-bill — their
    # cost is added as recurring lines on the Production renewal, and they are
    # excluded from the renewal/dunning crons via ('parent_id', '=', False).
    environment = fields.Selection([
        ('production', 'Production'),
        ('staging', 'Staging'),
        ('development', 'Development'),
    ], string='Environment', default='production', required=True, index=True,
        copy=False,
        help='Environment type (hosting). Production is the mandatory billing '
             'anchor bound to the main branch; Staging/Development are pinned '
             'to the lowest spec and billed per server.')
    parent_id = fields.Many2one(
        'saas.instance', string='Project (Production)', copy=False,
        ondelete='cascade', index=True,
        domain="[('environment', '=', 'production')]",
        help='The Production environment this Staging/Development server '
             'belongs to. Empty on Production itself.')
    child_env_ids = fields.One2many(
        'saas.instance', 'parent_id', string='Environments',
        help='Staging/Development servers under this Production project.')
    main_branch = fields.Char(
        string='Main Branch', default='main', copy=False,
        help='The customer\'s primary Git branch. Production always tracks it; '
             'Development branches are created from it.')
    pending_staging_count = fields.Integer(
        string='Pending Staging Servers', copy=False, default=0,
        help='Staging servers to spawn once the initial subscription invoice '
             'is paid (mirrors pending_storage_blocks).')
    pending_dev_count = fields.Integer(
        string='Pending Development Servers', copy=False, default=0,
        help='Development servers to spawn once the initial subscription '
             'invoice is paid.')
    # --- Purchased environment entitlement (slots) ---------------------
    # On the Production anchor: how many Staging/Development servers the
    # customer has PAID for. Children up to this count are free to create
    # (the slot is already billed, upfront + on renewal); pressing "+"
    # beyond it charges a prorated amount and adds a slot.
    staging_slots = fields.Integer(
        string='Staging Slots', copy=False, default=0,
        help='Purchased Staging capacity on the Production project. Create up '
             'to this many Staging servers for free; more requires payment.')
    dev_slots = fields.Integer(
        string='Development Slots', copy=False, default=0,
        help='Purchased Development capacity on the Production project. Create '
             'up to this many Development servers for free; more requires '
             'payment.')
    env_is_extra_slot = fields.Boolean(
        string='Extra (paid) environment', copy=False, default=False,
        help='On a Staging/Development child bought beyond the purchased '
             'slots: once its activation invoice settles it adds a recurring '
             'slot to the project. Cleared on activation.')
    project_name = fields.Char(
        string='Project Name', copy=False,
        help='Customer-facing project name chosen at checkout. Falls back to '
             'the subdomain when empty.')

    @api.depends('plan_id.storage_limit', 'extra_storage_blocks')
    def _compute_effective_storage_limit(self):
        block_gb, _bp = self.env['saas.pricing.engine'].storage_block_config()
        for rec in self:
            base = rec.plan_id.storage_limit or 0.0
            rec.effective_storage_limit_gb = base + (
                (rec.extra_storage_blocks or 0) * (block_gb or 0))

    # ========== Backups ==========
    backup_ids = fields.One2many(
        'saas.instance.backup', 'instance_id',
        string='Backups',
    )

    # ========== Resource Usage ==========
    cpu_usage = fields.Char(
        string='CPU Usage',
        readonly=True,
        help='CPU usage as percentage of the plan CPU limit.',
    )
    cpu_usage_pct = fields.Float(
        string='CPU Usage %',
        readonly=True,
        help='CPU usage as a float percentage of the plan CPU limit.',
    )
    ram_usage = fields.Char(
        string='RAM Usage',
        readonly=True,
        help='RAM usage (used / plan limit).',
    )
    ram_percent = fields.Char(
        string='RAM %',
        readonly=True,
        help='RAM usage as percentage of the plan RAM limit.',
    )
    ram_usage_pct = fields.Float(
        string='RAM Usage %',
        readonly=True,
        help='RAM usage as a float percentage of the plan RAM limit.',
    )
    storage_usage_pct = fields.Float(
        string='Storage Usage %',
        readonly=True,
        help='Total storage usage as percentage of the plan storage limit.',
    )
    disk_usage = fields.Char(
        string='Container Disk',
        readonly=True,
        help='Disk space used by the instance folder on the Docker server.',
    )
    db_size = fields.Char(
        string='Database Size',
        readonly=True,
        help='Size of the PostgreSQL database on the database server.',
    )
    total_storage = fields.Char(
        string='Total Storage Size',
        readonly=True,
        help='Total storage: container files + PostgreSQL database.',
    )
    total_storage_bytes = fields.Float(
        string='Total Storage (bytes)',
        readonly=True,
    )
    # A2: storage display (included / used) — cheap non-stored computes used
    # by the portal, the storage-usage email and the overage summary. NOTE:
    # ``storage_usage_pct`` already exists above (kept by the usage refresh
    # cron); these two only add the absolute GB figures.
    storage_used_gb = fields.Float(
        string='Storage Used (GB)', compute='_compute_storage_gb',
    )
    storage_limit_gb = fields.Float(
        string='Storage Included (GB)', compute='_compute_storage_gb',
    )

    @api.depends('total_storage_bytes', 'plan_id.storage_limit')
    def _compute_storage_gb(self):
        for rec in self:
            rec.storage_used_gb = round(
                (rec.total_storage_bytes or 0.0) / (1024 ** 3), 2)
            rec.storage_limit_gb = rec.plan_id.storage_limit or 0.0

    # ===== Phase 5.1: multi-container model SEAM (no scale-out impl yet) =====
    container_ids = fields.One2many(
        'saas.instance.container', 'instance_id', string='Workloads',
        help='Scale-out seam: explicit role-tagged workloads (app/cron/longpoll). '
             'Empty for normal single-container tenants — the lifecycle then uses '
             'the implicit single app container. Populated only by a scale-out '
             'instance, so multi-host scale-out is additive (no rewrite).')

    def _workloads(self):
        """Return [(role, container_name)] for this instance. v1 (the only path
        exercised today) yields the single implicit app container; a scale-out
        instance yields its explicit ``container_ids``. This is the ONE place the
        rest of the platform should ask "what containers make up this tenant?",
        so adding workers later doesn't touch lifecycle/reconcile call sites."""
        self.ensure_one()
        if self.container_ids:
            return [(c.role, c.name) for c in self.container_ids]
        return [('app', self._get_container_name())]

    def _compute_handles(self):
        """ComputeHandle per workload (parallel to _compute_handle for the single
        container). Future multi-container lifecycle/reconcile iterates these."""
        self.ensure_one()
        base = self._compute_handle()
        if not self.container_ids:
            return [base]
        from ..drivers.base import ComputeHandle
        return [ComputeHandle(
            server_id=base.server_id, container_name=name,
            instance_path=base.instance_path, host=base.host,
            http_port=base.http_port) for _role, name in self._workloads()]

    # ===== Phase 3: reconciliation engine (desired → actual) =====
    desired_state = fields.Selection(
        [('running', 'Running'), ('stopped', 'Stopped'), ('ignore', 'Not reconciled')],
        string='Desired State', compute='_compute_desired_state',
        help='What the container SHOULD be doing, derived from the lifecycle '
             'state. The reconciler drives the real container toward this.')
    actual_state = fields.Char(
        string='Actual State', readonly=True, copy=False,
        help='Last container status observed by the reconciler (running / '
             'exited / not_found / restarting …).')
    last_reconcile = fields.Datetime(string='Last Reconciled', readonly=True, copy=False)

    # Lifecycle states whose container should be RUNNING vs STOPPED; others
    # (draft/provisioning/cancelled) are not container-reconciled.
    _RECONCILE_RUNNING = ('running',)
    _RECONCILE_STOPPED = ('stopped', 'suspended')

    @api.depends('state')
    def _compute_desired_state(self):
        for rec in self:
            if rec.state in rec._RECONCILE_RUNNING:
                rec.desired_state = 'running'
            elif rec.state in rec._RECONCILE_STOPPED:
                rec.desired_state = 'stopped'
            else:
                rec.desired_state = 'ignore'

    usage_last_updated = fields.Datetime(
        string='Usage Last Updated',
        readonly=True,
        help='Last time resource usage statistics were refreshed.',
    )

    # ========== Operations ==========
    provisioning_log = fields.Text(
        string='Provisioning Log',
        readonly=True,
        help='Timestamped log of all provisioning and deployment steps.',
    )
    extra_config = fields.Text(
        string='Extra Configuration',
        help='Additional odoo.conf directives, one key = value pair per line. '
             'Lines starting with # are ignored. '
             'Values here override auto-calculated settings (e.g. '
             'limit_memory_soft, limit_memory_hard, limit_time_real).',
    )
    override_docker_cpu = fields.Char(
        string='Docker CPU Override',
        groups='saas_core.group_saas_manager',
        help='Override cpus in docker-compose.yml (e.g. "2.0"). '
             'Leave empty to use the plan default.',
    )
    override_docker_mem = fields.Char(
        string='Docker Memory Override',
        groups='saas_core.group_saas_manager',
        help='Override mem_limit in docker-compose.yml (e.g. "2g", "2500m"). '
             'Leave empty to use the plan-based default (plan RAM × 1.3).',
    )
    override_docker_swap = fields.Char(
        string='Docker Swap Override',
        groups='saas_core.group_saas_manager',
        help='Override memswap_limit in docker-compose.yml (e.g. "3g"). '
             'Leave empty to use the same value as mem_limit. '
             'Set to "-1" for unlimited swap.',
    )
    deploy_image = fields.Char(
        string='Deployed Image (immutable)',
        groups='saas_core.group_saas_manager',
        help='The tenant image (with this instance\'s Git repos baked in) '
             'currently deployed, set by the build pipeline once a build is '
             'live. Empty = the plain Odoo version image (no repos).',
    )

    # ========== Custom Repos ==========
    repo_ids = fields.One2many(
        'saas.instance.repo',
        'instance_id',
        string='Custom Repositories',
    )

    # ========== State ==========
    state = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('pending_payment', 'Pending Payment'),
            ('paid', 'Paid'),
            ('pending_provision', 'Pending Provision'),
            ('provisioning', 'Provisioning'),
            ('running', 'Running'),
            ('stopped', 'Stopped'),
            ('failed', 'Failed'),
            ('suspended', 'Suspended'),
            ('cancelled', 'Cancelled'),
            ('cancelled_by_client', 'Cancelled by Client'),
        ],
        string='Status',
        default='draft',
        tracking=True,
        required=True,
        index=True,
        help='Current lifecycle state of the instance.',
    )
    last_error = fields.Text(
        string='Last Error',
        readonly=True,
        help='Reason the last background operation failed.',
    )
    last_error_date = fields.Datetime(
        string='Error Date',
        readonly=True,
        help='When the last error occurred.',
    )
    cancellation_reason = fields.Text(
        string='Cancellation Reason',
        readonly=True,
        help='Details about why and when the instance was cancelled.',
    )
    retained_backup_path = fields.Char(
        string='Retained Backup',
        readonly=True,
        groups='saas_core.group_saas_manager',
        help='Cloud storage path of the most recent backup kept after '
             'instance deletion. Can be used to restore client data if '
             'they return. Not visible to the client.',
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        default=lambda self: self.env.company,
        ondelete='restrict',
        index=True,
        help='Company that manages this SaaS instance.',
    )

    # ========== Constraints ==========
    def init(self):
        """Create unique indexes for subdomain (full) and ports (partial).

        Subdomain uniqueness is **unconditional** — including cancelled
        instances. Once a subdomain is bound to an instance record, it
        stays reserved forever (or until that record is hard-deleted
        from the backend), so nobody can claim it for a fresh order
        and the original owner is steered toward Reactivate Instance.

        Ports remain a partial unique index excluding cancelled rows
        because that lets us recycle ports for new instances on the
        same docker host without bumping into the audit-trail records
        of long-cancelled ones.
        """
        cr = self.env.cr
        # ---------- Subdomain: full unique index ----------
        # Detect whether the current index already covers cancelled
        # rows. If it still has the legacy ``WHERE … NOT IN`` clause,
        # tear it down and create the strict one in its place.
        cr.execute("""
            SELECT indexdef FROM pg_indexes
            WHERE indexname = 'saas_instance_unique_subdomain_per_domain'
        """)
        row = cr.fetchone()
        needs_recreate = (not row) or ('cancelled' in (row[0] or '').lower())
        if needs_recreate:
            # Renaming clashing subdomains BEFORE creating the strict
            # index — without this, the CREATE would fail if there
            # are pre-existing duplicate (subdomain, domain_id) rows
            # left over from before the rule changed (e.g. two
            # cancelled instances at the same subdomain). We append a
            # ``-cancelled-<id>`` suffix to the duplicates so each
            # row becomes unique while staying obviously cancelled.
            cr.execute("""
                WITH ranked AS (
                    SELECT id, subdomain, domain_id,
                           row_number() OVER (
                               PARTITION BY subdomain, domain_id
                               ORDER BY
                                   CASE WHEN state NOT IN
                                       ('cancelled', 'cancelled_by_client')
                                       THEN 0 ELSE 1 END,
                                   id
                           ) AS rn
                    FROM saas_instance
                )
                UPDATE saas_instance s
                    SET subdomain = s.subdomain || '-cancelled-' || s.id
                  FROM ranked r
                 WHERE s.id = r.id
                   AND r.rn > 1
            """)
            renamed = cr.rowcount
            if renamed:
                _logger.warning(
                    "saas.instance: renamed %d duplicate subdomain(s) "
                    "with a '-cancelled-<id>' suffix to allow the new "
                    "strict unique-subdomain index to be created.",
                    renamed,
                )
            cr.execute("""
                ALTER TABLE saas_instance
                    DROP CONSTRAINT IF EXISTS saas_instance_unique_subdomain_per_domain;
                DROP INDEX IF EXISTS saas_instance_unique_subdomain_per_domain;
                CREATE UNIQUE INDEX saas_instance_unique_subdomain_per_domain
                    ON saas_instance (subdomain, domain_id);
            """)
        # ---------- Ports: keep the partial index (recycle on cancel) ----------
        for col in ('xmlrpc_port', 'longpolling_port'):
            idx = 'saas_instance_unique_%s_per_server' % col
            cr.execute("""
                SELECT 1 FROM pg_indexes
                WHERE indexname = %s AND indexdef ILIKE '%%cancelled%%'
            """, (idx,))
            if cr.fetchone():
                continue
            cr.execute("""
                ALTER TABLE saas_instance
                    DROP CONSTRAINT IF EXISTS %s;
                DROP INDEX IF EXISTS %s;
                CREATE UNIQUE INDEX %s
                    ON saas_instance (docker_server_id, %s)
                    WHERE state NOT IN ('cancelled', 'cancelled_by_client')
                      AND %s IS NOT NULL AND %s > 0;
            """ % (idx, idx, idx, col, col, col))

        # ---------- Secondary indexes for hot filter columns (PERF-007) ----
        # The cron sweeps and portal listings filter heavily on these; without
        # indexes Postgres seq-scans, and latency grows with the table. Plain
        # btree, created idempotently so re-running init() is a no-op.
        for name, cols in (
            ('saas_instance_state_idx', 'state'),
            ('saas_instance_docker_state_idx', 'docker_server_id, state'),
            ('saas_instance_partner_idx', 'partner_id'),
            ('saas_instance_plan_state_idx', 'plan_id, state'),
        ):
            cr.execute(
                "CREATE INDEX IF NOT EXISTS %s ON saas_instance (%s)"
                % (name, cols)
            )

    _sql_constraints = []

    @api.model
    def _saas_reencrypt_secrets(self):
        """Re-store every plaintext secret so it gets encrypted with the
        currently-configured ``saas_secret_key`` (SEC-002).

        Run ONCE after setting the key (e.g. from the shell:
        ``env['saas.instance']._saas_reencrypt_secrets()``). Idempotent and
        safe to re-run. Reading a field yields plaintext (legacy or decrypted)
        and writing it back triggers column-level encryption.
        """
        from .. import crypto
        if not crypto.is_enabled():
            raise UserError(_(
                "No valid saas_secret_key is configured. Add one to odoo.conf "
                "(or the SAAS_SECRET_KEY env var) before re-encrypting."))
        counts = {}
        instances = self.sudo().search([])
        n = 0
        for rec in instances:
            vals = {f: rec[f] for f in
                    ('admin_password', 'db_password', 'restic_password')
                    if rec[f]}
            if vals:
                rec.write(vals)
                n += 1
        counts['instances'] = n
        for model in ('saas.instance.repo', 'saas.product'):
            Model = self.env[model].sudo()
            mn = 0
            for rec in Model.search([('github_token', '!=', False)]):
                if rec.github_token:
                    rec.github_token = rec.github_token
                    mn += 1
            counts[model] = mn
        self.env.cr.commit()
        _logger.info("SEC-002 re-encrypted secrets: %s", counts)
        return counts

    @api.constrains('is_trial', 'partner_id')
    def _check_one_trial_per_client(self):
        for rec in self:
            if rec.is_trial and rec.partner_id:
                # One trial per COMMERCIAL entity (across all its contacts),
                # not per contact — see res.partner._saas_commercial.
                commercial = rec.partner_id._saas_commercial()
                # A 'failed' trial must NOT block a retry: deploy runs async
                # (the worker marks the instance 'failed' after this commit),
                # and the design deliberately sets the trial-used FLAG only on
                # a SUCCESSFUL deploy so a transient infra failure never locks
                # the customer out. Counting a failed trial here would re-impose
                # exactly that lock-out — so exclude it (alongside cancelled).
                existing = self.search([
                    ('partner_id', 'child_of', commercial.id),
                    ('is_trial', '=', True),
                    ('id', '!=', rec.id),
                    ('state', 'not in', ('cancelled', 'cancelled_by_client',
                                         'failed')),
                ], limit=1)
                if existing:
                    raise ValidationError(
                        _("Client '%s' already has a free trial instance (%s). "
                          "Only one trial is allowed per client.")
                        % (commercial.name, existing.subdomain)
                    )

    @api.constrains('subdomain')
    def _check_subdomain_format(self):
        for rec in self:
            if rec.subdomain and not SUBDOMAIN_RE.match(rec.subdomain):
                raise ValidationError(
                    _("Subdomain '%s' is invalid. Use only lowercase letters, "
                      "digits, and hyphens (max 63 chars, must start/end with alphanumeric).")
                    % rec.subdomain
                )

    @api.constrains('environment', 'parent_id', 'region_id', 'partner_id')
    def _check_environment_hierarchy(self):
        """Production is the root (no parent); Staging/Dev must hang off a
        Production project and co-locate with it (same region + partner)."""
        for rec in self:
            if rec.environment == 'production':
                if rec.parent_id:
                    raise ValidationError(_(
                        "A Production environment cannot belong to another "
                        "project. Remove its parent."))
            else:
                if not rec.parent_id:
                    raise ValidationError(_(
                        "A %s environment must belong to a Production project.")
                        % rec.environment)
                if rec.parent_id.environment != 'production':
                    raise ValidationError(_(
                        "The parent of a %s environment must be a Production "
                        "environment.") % rec.environment)
                if rec.region_id and rec.parent_id.region_id \
                        and rec.region_id != rec.parent_id.region_id:
                    raise ValidationError(_(
                        "Staging/Development servers must run in the same "
                        "region as their Production project."))
                if rec.partner_id and rec.parent_id.partner_id \
                        and rec.partner_id != rec.parent_id.partner_id:
                    raise ValidationError(_(
                        "A child environment must belong to the same customer "
                        "as its Production project."))

    # ========== CRUD Overrides ==========

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            # Block trial if the client has already used their free trial.
            # Separate trials for services vs hosting.
            # Use SELECT ... FOR UPDATE to prevent race conditions.
            if vals.get('is_trial') and vals.get('partner_id'):
                # Determine if this is a hosting trial
                is_hosting_trial = False
                if vals.get('saas_product_id'):
                    product = self.env['saas.product'].browse(vals['saas_product_id'])
                    is_hosting_trial = product.is_hosting

                trial_field = 'saas_hosting_trial_used' if is_hosting_trial else 'saas_trial_used'
                # Lock + read the trial flag on the COMMERCIAL entity (company),
                # not the ordering contact — so child contacts can't each get a
                # fresh trial. (trial_field is a fixed column name, not input.)
                partner = self.env['res.partner'].browse(vals['partner_id'])
                commercial = partner.commercial_partner_id or partner
                self.env.cr.execute(
                    "SELECT %s FROM res_partner "
                    "WHERE id = %%s FOR UPDATE" % trial_field,
                    (commercial.id,),
                )
                row = self.env.cr.fetchone()
                trial_type = 'hosting' if is_hosting_trial else 'service'
                if row and row[0]:
                    raise ValidationError(
                        _("Client '%s' has already used their free %s trial. "
                          "Only one trial per type is allowed.")
                        % (partner.name, trial_type)
                    )
                # Once the partner has paid for a server, the trial no
                # longer applies — the server is paid.
                if partner._saas_has_paid_instance(hosting=is_hosting_trial):
                    raise ValidationError(
                        _("Client '%s' already owns a paid %s instance. "
                          "The free trial is no longer available — the "
                          "server is paid.")
                        % (partner.name, trial_type)
                    )
            subdomain = vals.get('subdomain', '')
            if subdomain and not vals.get('db_user'):
                safe_subdomain = subdomain.replace('-', '_').replace('.', '_')
                vals['db_user'] = 'saas_%s' % safe_subdomain
            if not vals.get('db_password'):
                vals['db_password'] = SaasInstance._generate_random_password()
            if not vals.get('admin_password'):
                vals['admin_password'] = SaasInstance._generate_random_password()
        records = super().create(vals_list)
        for rec in records:
            if rec.pip_packages:
                rec._sync_packages_from_text()
        return records

    def write(self, vals):
        result = super().write(vals)
        if 'pip_packages' in vals and not self.env.context.get('_skip_pip_sync'):
            self._sync_packages_from_text()
        return result

    # Strict PEP 508 / requirement-spec regex. Refuses anything that pip
    # would interpret as an option flag (e.g. `--index-url=...`) or a
    # path/URL — those would let a tenant redirect pip to attacker-
    # controlled packages whose setup.py runs arbitrary code.
    _PIP_PACKAGE_RE = re.compile(
        r'^[A-Za-z0-9][A-Za-z0-9._-]*'           # name
        r'(\[[A-Za-z0-9._,-]+\])?'                # optional [extras]
        r'(\s*(==|!=|<=|>=|<|>|~=)\s*[A-Za-z0-9._*+-]+'  # spec op + version
        r'(\s*,\s*(==|!=|<=|>=|<|>|~=)\s*[A-Za-z0-9._*+-]+)*)?$'
    )

    def _validate_pip_line(self, line):
        if not self._PIP_PACKAGE_RE.match(line):
            raise UserError(_(
                "Invalid pip package spec: %r\n\n"
                "Use the form 'name[==version]' (PEP 508). "
                "Options like '--index-url' or paths/URLs are not allowed."
            ) % line)

    def _sync_packages_from_text(self):
        """Sync ``pip_packages`` text field to ``package_ids`` One2many.

        Parses the text field, deduplicates by package name, and
        creates/updates/removes ``package_ids`` records to match.
        """
        Package = self.env['saas.instance.package']
        for rec in self:
            existing = {}
            for p in rec.package_ids:
                key = p.name.lower().split('=')[0].split('<')[0].split('>')[0].split('!')[0].split('[')[0].strip()
                existing[key] = p

            new_names = []
            if rec.pip_packages:
                for line in rec.pip_packages.splitlines():
                    line = line.strip()
                    if line and not line.startswith('#'):
                        rec._validate_pip_line(line)
                        new_names.append(line)

            new_keys = set()
            to_create = []
            for name in new_names:
                key = name.lower().split('=')[0].split('<')[0].split('>')[0].split('!')[0].split('[')[0].strip()
                if key in new_keys:
                    continue
                new_keys.add(key)
                if key not in existing:
                    to_create.append({'instance_id': rec.id, 'name': name})
                elif existing[key].name != name:
                    existing[key].name = name

            to_remove = rec.package_ids.filtered(
                lambda p: p.name.lower().split('=')[0].split('<')[0].split('>')[0].split('!')[0].split('[')[0].strip() not in new_keys
            )
            to_remove.unlink()
            if to_create:
                Package.create(to_create)

    def _sync_text_from_packages(self):
        """Sync ``package_ids`` One2many back to ``pip_packages`` text field."""
        for rec in self:
            names = rec.package_ids.mapped('name')
            rec.with_context(_skip_pip_sync=True).pip_packages = '\n'.join(names) if names else False

    def unlink(self):
        """Block deletion of instances that have live infrastructure.

        Only draft and cancelled instances (no running infra) can be deleted
        from the database.  Everything else must go through the
        action_delete_instance() teardown workflow first.
        """
        safe_states = ('draft', 'cancelled', 'cancelled_by_client')
        unsafe = self.filtered(lambda r: r.state not in safe_states)
        if unsafe:
            raise UserError(
                _("Cannot delete instances that have been deployed. "
                  "Use the 'Delete' action to tear down infrastructure first.\n"
                  "Affected: %s")
                % ', '.join(unsafe.mapped('subdomain'))
            )
        return super().unlink()

    def _sync_partner_trial(self):
        """Mark the trial as used on the COMMERCIAL entity (one per type:
        service / hosting) so it can't be farmed across child contacts."""
        self.ensure_one()
        trial_days = int(self.env['ir.config_parameter'].sudo().get_param(
            'saas_master.trial_days', '14',
        ))
        end_date = fields.Date.today() + datetime.timedelta(days=trial_days)
        self.partner_id._saas_mark_trial_used(
            hosting=self.is_hosting, end_date=end_date)

    # ========== Computed ==========
    @api.depends('subdomain', 'domain_id.name')
    def _compute_name(self):
        for rec in self:
            if rec.subdomain and rec.domain_id:
                rec.name = '%s.%s' % (rec.subdomain, rec.domain_id.name)
            else:
                rec.name = rec.subdomain or ''

    @api.depends('subdomain', 'domain_id.name')
    def _compute_url(self):
        for rec in self:
            if rec.subdomain and rec.domain_id:
                rec.url = 'https://%s.%s' % (rec.subdomain, rec.domain_id.name)
            else:
                rec.url = ''

    # ========== Compute tier scaling ==========
    # Sales/invoicing actions (including the paid entry point
    # action_change_compute_tier) live in saas_billing.

    def _do_scale_compute_tier(self, tier_id):
        """saas.job entrypoint: patch this instance's Kubernetes CR to
        ``tier``'s replica count and verify it actually comes back healthy
        BEFORE committing ``compute_tier_id`` — for a paid upgrade, the
        customer has already paid by the time this runs (enqueued from the
        account_move payment hook), but the portal should never claim a
        tier is active if the scale didn't really work; for a free/
        downgrade change (enqueued directly from
        action_change_compute_tier) there's no payment involved at all.

        On failure: patch back to the replica count it had before, leave
        ``compute_tier_id`` (and billing's ``pending_compute_tier_id``) untouched, and
        alert ops — a paid invoice is not refunded here; that's a billing
        decision for a human, not this job.
        """
        self.ensure_one()
        if self.docker_server_id.compute_driver != 'kubernetes':
            raise UserError(_(
                "Cannot scale '%s': not running on the Kubernetes backend."
            ) % self.subdomain)
        tier = self.env['saas.compute.tier'].sudo().browse(tier_id)
        previous_tier = self.compute_tier_id
        previous_replicas = previous_tier.replicas if previous_tier else 1
        driver = self._compute_driver()
        handle = self._compute_handle()
        self._append_log(
            "Scaling to '%s' tier (%d replica(s))..." % (tier.name, tier.replicas))
        driver.scale(handle, tier.replicas)
        # Settle delay before the first health check — live-verified this is
        # necessary: patching spec.replicas doesn't synchronously update
        # status.phase, so a health() call in the same instant as scale()
        # can still read the PRE-patch "Ready" phase (the operator's
        # reconciler hasn't run yet) and _wait_until_healthy would exit
        # successfully on that first, stale reading — even when the new
        # replica count is actually invalid (e.g. RWO storage rejecting
        # replicas > 1) and the CR is about to go Degraded. Reproduced live
        # against a real cluster: without this delay, a genuinely broken
        # scale was reported as healthy.
        time.sleep(5)
        try:
            self._data_service()._wait_until_healthy(driver, handle, timeout=300)
        except Exception as e:
            self._append_log(
                "Scale to '%s' (%d replica(s)) failed its health check (%s) "
                "— rolling back to %d replica(s)." % (
                    tier.name, tier.replicas, e, previous_replicas))
            driver.scale(handle, previous_replicas)
            self.env['saas.alert']._notify(
                'compute_tier_scale_failed',
                'Compute tier scale failed for %s' % self.subdomain,
                level='error', detail=str(e))
            raise UserError(_(
                "Could not switch '%s' to the %s tier: the instance did "
                "not come back healthy at the new replica count. Rolled "
                "back to %d replica(s). Support has been notified."
            ) % (self.subdomain, tier.name, previous_replicas)) from e
        self.write({'compute_tier_id': tier.id})
        self._append_log(
            "Now on the '%s' tier (%d replica(s))." % (tier.name, tier.replicas))
        self.message_post(body=_(
            "Compute tier changed to %s — this instance now runs across "
            "%d replica(s)."
        ) % (tier.name, tier.replicas))

    def _on_compute_tier_scale_error(self, exception):
        """on_error handler for the _do_scale_compute_tier job — the
        method itself already rolls back and alerts on a HEALTH-CHECK
        failure; this only fires for something it couldn't (an exception
        raised before/after that try/except, e.g. the initial ``scale()``
        call itself failing). Mirrors _on_background_error's alerting
        without touching ``state`` (tier scaling never changes it)."""
        self.ensure_one()
        error_msg = str(exception)
        self._append_log("COMPUTE TIER SCALE FAILED: %s" % error_msg)
        self.env['saas.alert']._notify(
            'compute_tier_scale_failed',
            'Compute tier scale failed for %s' % self.subdomain,
            level='error', detail=error_msg)

    # ==================================================================
    #  Odoo.sh-style environments (hosting) — a Production project plus
    #  per-server-billed Staging / Development children. Modeled on the
    #  storage-block add-on flow (prorated activation invoice → on-payment
    #  provisioning → recurring renewal line).
    # ==================================================================
    ENV_PLAN_NAME = 'Environment (lowest spec)'

    def _get_env_plan(self):
        """Find-or-create the hidden lowest-spec hosting plan used to SIZE
        Staging/Development containers (cpu/ram/workers/storage_limit). Children
        never self-bill (their recurring cost rides the Production renewal via
        ``_env_server_price``), so this plan's price is cosmetic — but it must
        still satisfy the platform's cost-floor constraint
        (``saas.plan._check_price_floor``), so we stamp the lowest-spec hosting
        monthly price (which is floor-aware) and link the hosting product so the
        floor is evaluated against the hosting rate set."""
        Plan = self.env['saas.plan'].sudo()
        plan = Plan.search(
            [('is_custom', '=', True), ('name', '=', self.ENV_PLAN_NAME)],
            limit=1)
        if plan:
            return plan
        engine = self.env['saas.pricing.engine']
        cfg = engine._rate_config('hosting')
        res = Plan._recommended_resources('hosting', cfg['min_workers'])
        # Floor-aware monthly figure for the lowest spec. yearly_price stays 0
        # so the (12×floor) yearly check is skipped — neither value is ever
        # billed for an environment server.
        floor_ok_price = engine.monthly_price(
            'hosting', cfg['min_workers'], cfg['min_storage'])
        vals = {
            'name': self.ENV_PLAN_NAME,
            'is_custom': True,
            'is_public_tier': False,
            'is_trial_plan': False,
            'manual_price': True,
            'price': floor_ok_price,
            'yearly_price': 0.0,
            'workers': cfg['min_workers'],
            'storage_limit': float(cfg['min_storage']),
            'cpu_limit': res.get('cpu_limit') or 1.0,
            'ram_limit': res.get('ram_limit') or '1g',
        }
        # Link the hosting product so the cost-floor constraint uses the
        # hosting rate set (matching the price we stamped above).
        if self.saas_product_id:
            vals['saas_product_ids'] = [(6, 0, [self.saas_product_id.id])]
        return Plan.create(vals)

    def _env_anchor(self):
        """The Production instance that owns the project this record is in."""
        self.ensure_one()
        return self if self.environment == 'production' else (
            self.parent_id or self)

    def _env_branch(self):
        """The Git branch this environment is bound to: Production → main
        branch; Development → its own name; Staging → its chosen/own branch."""
        self.ensure_one()
        if self.environment == 'production':
            return self.main_branch or 'main'
        repo = self.repo_ids[:1]
        if repo and repo.branch:
            return repo.branch
        if self.environment == 'development':
            return self.subdomain
        return (self.parent_id.main_branch if self.parent_id else 'main')

    def _unique_env_subdomain(self, base):
        """A valid, unique subdomain for a child env, derived from the project
        subdomain + ``base`` (e.g. acme-feature-x)."""
        self.ensure_one()
        slug = re.sub(r'[^a-z0-9-]+', '-',
                      ('%s-%s' % (self.subdomain, base)).lower()).strip('-')
        slug = slug[:55] or 'env'
        Inst = self.env['saas.instance'].sudo()
        candidate, n = slug, 1
        while Inst.search_count([('subdomain', '=', candidate),
                                 ('domain_id', '=', self.domain_id.id)]):
            n += 1
            candidate = '%s-%d' % (slug[:50], n)
        return candidate

    def _create_env_child(self, env_type, name=None, branch=None):
        """Create a Staging/Development child record (+ its repo pinned to the
        right branch, and the branch itself on the provider). Does NOT bill or
        deploy — callers handle proration/payment then call
        ``_activate_pending_environment``."""
        self.ensure_one()
        if self.environment != 'production':
            raise UserError(_(
                "Environments are created from the Production server."))
        if not self.is_hosting:
            raise UserError(_(
                "Environments are only available for hosting subscriptions."))
        # Creating (spinning up) a server needs a Git repository — it runs on a
        # branch. Reserving/paying for slots does NOT (that's a separate action
        # that only grows the entitlement). So this gate guards CREATION only.
        if not self.repo_ids:
            raise UserError(_(
                "Connect a Git repository to your Production server before "
                "creating a Staging or Development server. You can still "
                "reserve slots without one."))
        if env_type not in ('staging', 'development'):
            raise UserError(_("Unknown environment type '%s'.") % env_type)
        name = (name or '').strip()
        if env_type == 'development':
            if not name:
                raise UserError(_(
                    "Please provide a name for the development server."))
            branch = name  # a dev server's branch is always its own name
        else:  # staging
            name = name or 'staging'
            branch = (branch or '').strip() or name
        vals = {
            'subdomain': self._unique_env_subdomain(name),
            'domain_id': self.domain_id.id,
            'partner_id': self.partner_id.id,
            'saas_product_id': self.saas_product_id.id,
            'plan_id': self._get_env_plan().id,
            'odoo_version_id': self.odoo_version_id.id,
            'region_id': self.region_id.id if self.region_id else False,
            'environment': env_type,
            'parent_id': self.id,
            'state': 'draft',
            # High Availability defaults to off (the field's own default) —
            # a Staging/Development environment doesn't need the extra
            # replica/cost Production might have.
        }
        vals.update(self._env_child_extra_vals())
        child = self.env['saas.instance'].sudo().create(vals)
        # Copy the project's repo onto the child, pinned to its branch, and
        # create the linked branch from the project's main branch (Odoo.sh).
        prod_repo = self.repo_ids[:1]
        if prod_repo:
            child_repo = self.env['saas.instance.repo'].sudo().create({
                'instance_id': child.id,
                'repo_url': prod_repo.repo_url,
                'branch': branch,
                'github_token': prod_repo.sudo().github_token or False,
                'webhook_enabled': prod_repo.webhook_enabled,
            })
            try:
                child_repo._create_branch_on_provider(
                    branch, self.main_branch or 'main')
            except UserError:
                # The branch is mandatory for development; for staging an
                # existing/chosen branch is fine, so a creation hiccup there is
                # non-fatal (the clone targets the chosen branch directly).
                if env_type == 'development':
                    child.unlink()
                    raise
        return child

    def _activate_pending_environment(self):
        """Provision a paid Staging/Development child and deploy (saas_billing
        clears the activation invoice). Children never get their own billing cycle (the
        recurring cost rides the Production renewal)."""
        self.ensure_one()
        if self.environment == 'production':
            return
        # A paid "extra" environment (bought beyond the purchased slots) adds
        # one recurring slot to the project once its activation invoice
        # settles — so renewal bills it and the free-create check counts it.
        if self.env_is_extra_slot and self.parent_id:
            parent = self.parent_id.sudo()
            field = ('staging_slots' if self.environment == 'staging'
                     else 'dev_slots')
            parent.write({field: (parent[field] or 0) + 1})
            self.env_is_extra_slot = False
        if self.state in ('draft', 'pending_payment', 'paid'):
            self.state = 'paid'
        self._append_log("Environment payment received — deploying.")
        self.message_post(body=_(
            "Environment server payment received. Deploying automatically."))
        run_in_background(
            self, '_do_deploy_after_payment',
            error_method='_on_background_error', error_args=('failed',),
            thread_name='saas_env_deploy_%s' % self.subdomain)

    def _env_slots_for(self, env_type):
        """Purchased slot count for an env type on this Production anchor."""
        self.ensure_one()
        return (self.staging_slots if env_type == 'staging'
                else self.dev_slots) or 0

    def _env_used_for(self, env_type):
        """Live (non-cancelled) children of ``env_type`` under this anchor."""
        self.ensure_one()
        return len(self.child_env_ids.filtered(
            lambda c: c.environment == env_type
            and c.state not in ('cancelled', 'cancelled_by_client')))

    def _spawn_pending_environments(self):
        """After the initial invoice is paid, GRANT the purchased Staging/
        Development slots (entitlement). We no longer auto-create the child
        servers at checkout — the customer creates them on demand later,
        free up to the purchased slot count (the prod server is what gets
        provisioned after payment). Idempotent: clears the pending
        counters."""
        self.ensure_one()
        if self.environment != 'production':
            return
        staging = max(0, self.pending_staging_count or 0)
        dev = max(0, self.pending_dev_count or 0)
        if not (staging or dev):
            return
        self.write({
            'staging_slots': (self.staging_slots or 0) + staging,
            'dev_slots': (self.dev_slots or 0) + dev,
            'pending_staging_count': 0,
            'pending_dev_count': 0,
        })
        self._append_log(
            "Environment slots granted: %d staging, %d development "
            "(create them anytime from the workspace)." % (staging, dev))

    def action_create_environment(self, env_type, name=None, branch=None):
        """One-click add of a Staging/Development server mid-cycle. Issues a
        prorated activation invoice for the remainder of the project cycle; if
        the wallet covers it or a saved card charges successfully, the server
        provisions immediately, otherwise the caller routes to checkout.
        Modeled on ``action_purchase_storage_block``."""
        self.ensure_one()
        if self.environment != 'production':
            raise UserError(_(
                "Add environments from the Production server of the project."))
        if self.is_trial:
            raise UserError(_(
                "Upgrade to a paid plan before adding environments."))
        if env_type not in ('staging', 'development'):
            raise UserError(_("Unknown environment type."))

        # Creating is FREE and capped at the reserved count: the customer
        # already paid for the slots when he reserved them, and within the
        # cycle he may delete + recreate servers up to that count at no extra
        # charge. To go beyond it he must reserve (buy) more slots first.
        used = self._env_used_for(env_type)
        slots = self._env_slots_for(env_type)
        if used >= slots:
            label = dict(self._fields['environment'].selection).get(
                env_type, env_type)
            raise UserError(_(
                "You've used all %d reserved %s server(s). Reserve another "
                "slot to create more.") % (slots, label))
        # ``_create_env_child`` enforces the Git-repo requirement (a server
        # runs on a branch).
        child = self._create_env_child(env_type, name=name, branch=branch)
        child._activate_pending_environment()
        self._append_log(
            "Environment '%s' created within reserved %s slots (%d/%d used)."
            % (child.subdomain, env_type, used + 1, slots))
        return {'child_id': child.id, 'auto_provisioned': True, 'free': True}

    def action_delete_environment(self, delete_branch=False):
        """Remove a Staging/Development server, FREEING its reserved slot for
        reuse. Deleting is free and does NOT refund or lower the reserved
        count: the customer paid for the slot capacity for the cycle and may
        recreate a server in it at no extra charge (to stop paying he releases
        the slot via ``action_release_environment_slots``). Production can't be
        removed this way (cancel the subscription instead)."""
        self.ensure_one()
        if self.environment == 'production':
            raise UserError(_(
                "The Production environment can't be removed individually — "
                "cancel the subscription instead."))
        branch = self._env_branch()
        repo = self.repo_ids[:1]
        if delete_branch and repo and branch:
            repo._delete_branch_on_provider(branch)
        # Just tear down the server. The slot stays reserved (still billed,
        # still usable) — ``_env_used_for`` drops by one once cancelled, so the
        # customer can immediately create another server within the same slot.
        self.action_cancel()
        return True

    def action_merge_environment(self, source_id):
        """Odoo.sh-style drag-to-merge: merge the SOURCE environment's branch
        into THIS environment's branch on the Git provider, then redeploy this
        environment so it runs the merged code. ``self`` is the merge TARGET."""
        self.ensure_one()
        source = self.env['saas.instance'].sudo().browse(int(source_id))
        if not source.exists():
            raise UserError(_("Source environment not found."))
        # Both must be in the same project and owned by the same customer.
        if source.partner_id != self.partner_id:
            raise UserError(_("These environments belong to different customers."))
        if self._env_anchor() != source._env_anchor():
            raise UserError(_("You can only merge between servers of the same "
                              "project."))
        if source == self:
            raise UserError(_("Pick two different servers to merge."))
        repo = self.repo_ids[:1]
        if not repo:
            raise UserError(_("This environment has no connected repository."))
        target_branch = self._env_branch()
        source_branch = source._env_branch()
        status = repo._merge_branch_on_provider(target_branch, source_branch)
        self._append_log(
            "Merged branch '%s' into '%s' (%s)."
            % (source_branch, target_branch, status))
        self.message_post(body=_(
            "Merged <b>%s</b> into <b>%s</b> (%s).") % (
            source_branch, target_branch, status))
        # Pull the merged code and restart — only when there was something to
        # merge and the server is in a redeployable state.
        redeployed = False
        if status == 'merged' and self.state in ('running', 'stopped'):
            try:
                self.action_redeploy()
                redeployed = True
            except Exception:
                _logger.exception(
                    "Redeploy after merge failed for %s", self.subdomain)
        return {
            'status': status,
            'source_branch': source_branch,
            'target_branch': target_branch,
            'redeployed': redeployed,
        }

    # ========== Private Helpers ==========

    @staticmethod
    def _generate_random_password(length=24):
        """Generate a cryptographically secure random password."""
        alphabet = string.ascii_letters + string.digits + '-_.~+='
        return ''.join(secrets.choice(alphabet) for _ in range(length))

    def _generate_db_user(self):
        """Generate a db username based on subdomain."""
        self.ensure_one()
        safe_subdomain = self.subdomain.replace('-', '_').replace('.', '_')
        db_user = 'saas_%s' % safe_subdomain
        if not DB_USER_RE.match(db_user):
            raise ValidationError(
                _("Cannot generate a safe database username from subdomain '%s'.")
                % self.subdomain
            )
        return db_user

    @staticmethod
    def _sanitize_path_component(value):
        """Strip a string to characters safe inside a remote filesystem path.

        Allows lowercase ASCII alphanumerics, hyphen and underscore.
        Returns '' if no character survives. Used as defence-in-depth
        against path traversal via free-text fields like `partner.ref`
        — never trust caller-controlled values inside `mkdir -p`,
        `rm -rf`, `chown`, `chmod` etc.
        """
        if not value:
            return ''
        cleaned = ''.join(
            c for c in value.strip().lower().replace(' ', '_')
            if c.isascii() and (c.isalnum() or c in '_-')
        )
        # Refuse purely-dot or empty strings (would escape into the parent).
        if not cleaned or cleaned.strip('._-') == '':
            return ''
        return cleaned[:64]

    def _get_partner_code(self):
        """Return a filesystem-safe partner identifier: code_name."""
        self.ensure_one()
        code = self._sanitize_path_component(self.partner_id.ref or '')
        if not code:
            code = str(self.partner_id.id)
        safe_name = self._sanitize_path_component(self.partner_id.name or '')
        return '%s_%s' % (code, safe_name) if safe_name else code

    def _get_instance_path(self):
        """Return the full remote path for this instance."""
        self.ensure_one()
        server = self.docker_server_id
        base = server.docker_base_path.rstrip('/')
        partner = self._get_partner_code()
        # subdomain is regex-validated (SUBDOMAIN_RE) so it can't contain '/'
        # or '..' but re-check defensively before composing the path.
        sub = self.subdomain or ''
        if not SUBDOMAIN_RE.match(sub):
            raise UserError(
                _("Refusing to build instance path: subdomain '%s' is invalid.")
                % sub
            )
        path = '%s/%s/%s' % (base, partner, sub)
        # Final containment check: the realpath must remain under base.
        norm = os.path.normpath(path)
        if not norm.startswith(os.path.normpath(base) + '/'):
            raise UserError(
                _("Refusing to build instance path: '%s' escapes base '%s'.")
                % (norm, base)
            )
        return path

    def _get_container_name(self):
        """Return the Docker container name for this instance."""
        self.ensure_one()
        return 'odoo_%s' % self.subdomain

    def _get_filestore_mount(self):
        """Phase 2: host path of this instance's object-storage-backed filestore,
        or '' when the server keeps filestores on local disk.

        When the docker host has ``object_filestore_mount`` set (a JuiceFS mount
        like /mnt/jfs), the instance's filestore lives at
        ``<mount>/<partner>/<sub>/filestore`` and is bind-mounted into the
        container at /var/lib/odoo/filestore — so destroying the container loses
        no attachments. Same path-containment guarantees as _get_instance_path."""
        self.ensure_one()
        base = (self.docker_server_id.object_filestore_mount or '').strip()
        if not base:
            return ''
        base = base.rstrip('/')
        sub = self.subdomain or ''
        if not SUBDOMAIN_RE.match(sub):
            raise UserError(
                _("Refusing to build filestore path: subdomain '%s' is invalid.")
                % sub)
        path = '%s/%s/%s/filestore' % (base, self._get_partner_code(), sub)
        norm = os.path.normpath(path)
        if not norm.startswith(os.path.normpath(base) + '/'):
            raise UserError(
                _("Refusing to build filestore path: '%s' escapes mount '%s'.")
                % (norm, base))
        return path

    # ---- Phase 1: ComputeDriver seam (additive; see docs/architecture) ----
    def _compute_handle(self):
        """Backend-agnostic handle describing this instance's compute workload.

        ``instance_path`` is left empty: KubernetesDriver treats it as an
        optional namespace override and falls back to deriving the
        namespace itself from ``container_name`` (``_namespace_for``) when
        it's falsy — there is no host filesystem path to report now that
        ssh_docker (whose ``docker_base_path``-rooted path this used to be)
        is gone.
        """
        self.ensure_one()
        from ..drivers.base import ComputeHandle
        server = self.docker_server_id
        return ComputeHandle(
            server_id=server.id,
            container_name=self._get_container_name(),
            instance_path='',
            host=server.ip_v4 or '',
            http_port=self.xmlrpc_port or 0,
        )

    def _compute_driver(self, connection=None):
        """Return the ComputeDriver for this instance's backend.

        Kubernetes is the only compute backend now (ssh_docker was
        removed). Business logic NEVER changes — it only ever calls
        ``self._compute_driver().X()``; a future second backend would
        only need a new driver file + a switch here.

        Lazy import matches the existing pattern (see drivers/__init__.py's
        docstring): this package is deliberately not imported from
        models/__init__.py so unit tests can import it without a database.
        ``connection`` is accepted for call-site compatibility but unused
        by KubernetesDriver."""
        self.ensure_one()
        server = self.docker_server_id
        from ..drivers.kubernetes_driver import KubernetesDriver
        return KubernetesDriver(server, connection=connection)

    def action_open_terminal(self):
        """Open a web-based terminal, exec'd straight into this instance's
        own pod — the Kubernetes-native replacement for the old raw-SSH
        host-shell (there is no platform "host" any more, only tenant
        pods; see ``saas_core/controllers/ssh_terminal.py``).

        SEC-005: gated by the narrower ``group_saas_pod_shell`` (not plain
        SaaS Manager) since this reaches into a live tenant's container —
        enforced here too (defense in depth alongside the view button's
        own ``groups=`` and the controller's own check, the one that
        actually matters: this method only returns a client-action tag,
        it never opens the exec channel itself)."""
        self.ensure_one()
        if not self.env.user.has_group('saas_core.group_saas_pod_shell'):
            raise AccessError(_(
                "Pod Shell privileges are required to open a terminal on "
                "an instance (SaaS Manager alone is not enough)."))
        if not self.docker_server_id:
            raise UserError(_(
                "Instance '%s' isn't fully set up yet — no cluster "
                "assigned.") % self.subdomain)
        return {
            'type': 'ir.actions.client',
            'tag': 'ssh_terminal',
            'name': _("Terminal: %s") % self.subdomain,
            'context': {
                'server_model': self._name,
                'server_id': self.id,
                'server_name': self.subdomain,
            },
        }

    def _data_service(self):
        """Return the DataService (``_wait_until_healthy`` — deploy/scale
        readiness polling; the ssh_docker-era snapshot/materialize/
        migrate-to-kubernetes primitives were removed with ssh_docker)."""
        from ..dataservice.service import DataService
        return DataService(self.env)

    def _get_db_host(self):
        """Return the hostname/IP for odoo.conf (used inside the container).

        Same-server: ``host.docker.internal`` (resolved by Docker).
        Different server: DB server's private or public IP.
        """
        self.ensure_one()
        psql_server = self.db_server_id
        if psql_server == self.docker_server_id:
            return 'host.docker.internal'
        return psql_server.private_ip_v4 or psql_server.ip_v4

    def _get_db_host_for_ssh(self):
        """Return the DB hostname/IP for commands run on the host via SSH.

        Same-server: ``localhost`` (psql runs on the same machine).
        Different server: DB server's private or public IP.
        """
        self.ensure_one()
        psql_server = self.db_server_id
        if psql_server == self.docker_server_id:
            return 'localhost'
        return psql_server.private_ip_v4 or psql_server.ip_v4

    def _get_proxy_backend_ip(self):
        """IP a *remote* proxy server uses to reach this instance's
        published ports.

        Prefers the private network so proxy→Odoo traffic (plain HTTP
        after SSL termination) never crosses the public internet. Must
        stay in sync with the interface the ports are bound to in
        ``_render_and_write_configs`` (private IP, else all interfaces).
        """
        self.ensure_one()
        return self.docker_server_id.private_ip_v4 or self.docker_server_id.ip_v4

    def _get_container_uid(self, ssh):
        """Return the UID of the default user inside the Docker image.

        Uses ``--entrypoint`` to bypass the custom entrypoint (which
        requires mounted volumes) and runs a plain ``id -u``.  Falls
        back to 101 (the default in the official Odoo images) if
        detection fails.
        """
        self.ensure_one()
        odoo_image = self.odoo_version_id._get_docker_image()
        exit_code, uid_out, _ = ssh.execute(
            'docker run --rm --entrypoint id %s -u 2>/dev/null'
            % shlex.quote(odoo_image)
        )
        uid = uid_out.strip()
        if exit_code != 0 or not uid.isdigit():
            return '101'
        return uid

    # ------------------------------------------------------------------
    # Phase 5: hosting self-service database operations + PG-level
    # helpers, ported from the ssh_docker era onto KubernetesDriver.exec()
    # (see /home/moutaz/.claude/plans/virtual-humming-salamander.md).
    #
    # Architecture note: each instance's PostgreSQL is a dedicated,
    # single-tenant server (compute/operator/internal/resources/
    # database.go's ``DatabaseStatefulSet``, or an equivalent CNPG
    # ``Cluster``) reachable over TCP from the Odoo web pod — NOT a
    # shared multi-tenant server the way ssh_docker's ``db_server_id``
    # host used to be. The role in ``/etc/odoo/odoo.conf``'s
    # ``db_user``/``db_password`` is the actual Postgres superuser for
    # that dedicated server (the official ``postgres`` image grants
    # ``POSTGRES_USER`` superuser), so every PG-admin operation below
    # (CREATE/DROP DATABASE, template flag, WITH TEMPLATE clone) can run
    # as that role with no separate "sudo -u postgres" step and no
    # public-schema-ownership dance — both were needed only because the
    # old shared server pre-existed independently of any one tenant.
    # Connection details are read from ``/etc/odoo/odoo.conf`` INSIDE the
    # pod (rendered by the operator's own init container from its
    # Secret) rather than from ``saas.instance.db_user``/``db_password``
    # — those model fields are generated at instance-create time but are
    # NOT what the operator actually provisions the tenant's Postgres
    # with (it mints its own random credentials into a Secret), so they
    # would silently be wrong for Kubernetes-backed instances.
    # ------------------------------------------------------------------

    # PostgreSQL identifier rules: starts with a letter, [a-z0-9_-],
    # max 63 bytes. Reject the catalog DBs explicitly.
    _DB_NAME_RE = re.compile(r'^[a-z][a-z0-9_-]{0,62}$')
    # Reserved DB names — PostgreSQL system catalogs and Odoo defaults.
    _DB_RESERVED = frozenset(['postgres', 'template0', 'template1', 'odoo'])
    # Hard floor for customer-typed suffixes. Anything shorter is
    # almost certainly a slip; reject before we waste a CLI init.
    _DB_NAME_MIN_LENGTH = 3
    # Slightly more permissive form used for internal identifiers (the
    # per-instance template DB starts with an underscore).
    _DB_IDENT_RE = re.compile(r'^[_a-z][a-z0-9_-]{0,62}$')

    def _hosting_db_prefix(self):
        """Prefix every customer-created DB with the instance subdomain.

        Two reasons:
        * Tenant safety — two customers can both pick "prod"; only
          ``acme_prod`` and ``zen_prod`` ever exist on the cluster.
        * Listing — the portal can show only DBs that match the
          prefix, so the cron / drop / duplicate paths never see a
          stranger's data.
        """
        sub = (self.subdomain or '').strip().lower()
        return '%s_' % sub if sub else ''

    def _validate_db_name(self, name):
        """Validate a raw customer-typed DB name (without the instance
        prefix). Returns the normalized name, or raises ``UserError``."""
        raw = (name or '').strip()
        if not raw:
            raise UserError(_("Database name is required."))
        if raw != raw.lower():
            raise UserError(_(
                "Database name must be lowercase. '%s' contains uppercase letters."
            ) % raw)
        name = raw
        if len(name) < self._DB_NAME_MIN_LENGTH:
            raise UserError(_(
                "Database name must be at least %d characters long."
            ) % self._DB_NAME_MIN_LENGTH)
        if len(name) > 63:
            raise UserError(_(
                "Database name is too long: %d characters (max 63)."
            ) % len(name))
        if not name[0].isalpha():
            raise UserError(_(
                "Database name must start with a letter (got '%s')."
            ) % name[0])
        bad_chars = [c for c in name if not (c.isalnum() or c in '_-')]
        if bad_chars:
            raise UserError(_(
                "Database name contains characters that aren't allowed: %s. "
                "Use only letters, digits, underscores, and hyphens."
            ) % ', '.join("'%s'" % c for c in sorted(set(bad_chars))))
        if name.endswith('-') or name.endswith('_'):
            raise UserError(_(
                "Database name can't end with a hyphen or underscore."
            ))
        if '--' in name or '__' in name:
            raise UserError(_(
                "Database name can't contain consecutive underscores or hyphens."
            ))
        if not self._DB_NAME_RE.match(name):
            raise UserError(_(
                "Database name '%s' is not a valid PostgreSQL identifier."
            ) % name)
        if name in self._DB_RESERVED:
            raise UserError(_(
                "'%s' is reserved and can't be used as a database name."
            ) % name)
        return name

    def _hosting_db_full_name(self, name):
        """Combine the instance prefix and the customer-typed suffix."""
        self.ensure_one()
        prefix = self._hosting_db_prefix()
        raw = (name or '').strip().lower()
        if prefix and raw.startswith(prefix):
            raw = raw[len(prefix):]
        suffix = self._validate_db_name(raw)
        full = '%s%s' % (prefix, suffix)
        if len(full) > 63:
            raise UserError(_(
                "Database name '%s' is too long (max 63 characters, "
                "including the '%s' prefix)."
            ) % (full, prefix))
        return full

    def _ensure_hosting_for_db_ops(self):
        self.ensure_one()
        if not self.is_hosting:
            raise UserError(_(
                "Database management is only available for hosting instances."
            ))
        allowed = self.state == 'running' or (
            self.state == 'provisioning'
            and self.pending_operation == 'restore'
        )
        if not allowed:
            raise UserError(_(
                "Your instance needs to be running before you can manage "
                "databases. Current status: %s."
            ) % self.state)
        if not self.docker_server_id:
            raise UserError(_(
                "This instance isn't fully set up yet. Please contact "
                "support."
            ))

    # -- PG-level admin helpers (run inside the pod via _docker_exec_sql) --

    def _pg_db_exists(self, db_name):
        """Return True if ``db_name`` exists on this instance's Postgres."""
        self.ensure_one()
        if not db_name or not self._DB_IDENT_RE.match(db_name):
            raise UserError(_("Invalid db name %r") % db_name)
        safe = db_name.replace("'", "''")
        rc, out, _err = self._docker_exec_sql(
            "SELECT 1 FROM pg_database WHERE datname='%s'" % safe, timeout=30)
        return rc == 0 and out.strip() == '1'

    def _pg_db_initialized(self, db_name):
        """Return True iff ``db_name`` has ``base`` fully installed."""
        self.ensure_one()
        if not self._DB_IDENT_RE.match(db_name or ''):
            return False
        sql = (
            "SELECT 1 FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname='public' AND c.relname='ir_module_module' "
            "AND EXISTS (SELECT 1 FROM ir_module_module "
            "WHERE name='base' AND state='installed')"
        )
        _rc, out, _err = self._docker_exec_sql(sql, db=db_name, timeout=60)
        return out.strip() == '1'

    def _pg_clone_db(self, source, target):
        """``CREATE DATABASE target WITH TEMPLATE source``. Postgres
        copies the data files at the storage layer — seconds, no Odoo
        init runs. ``source`` must have no active connections (or be
        flagged ``datistemplate=true``) for the clone to succeed."""
        self.ensure_one()
        for ident in (source, target):
            if not ident or not self._DB_IDENT_RE.match(ident):
                raise UserError(
                    _("Refusing to clone with invalid identifier %r") % ident
                )
        sql = 'CREATE DATABASE "%s" WITH TEMPLATE "%s"' % (target, source)
        rc, out, err = self._docker_exec_sql(sql, timeout=600)
        if rc != 0:
            raise UserError(_(
                "Failed to clone database from template:\n%s"
            ) % (err or out))

    def _pg_mark_template(self, db_name, flag=True):
        """Toggle ``datistemplate`` on a DB — lets it be a clone source
        without requiring the absence of connections, and tells our
        Odoo workers to skip it (not a customer database)."""
        self.ensure_one()
        if not self._DB_IDENT_RE.match(db_name or ''):
            return
        safe = db_name.replace("'", "''")
        sql = "UPDATE pg_database SET datistemplate=%s WHERE datname='%s'" % (
            'true' if flag else 'false', safe)
        self._docker_exec_sql(sql, timeout=30)

    def _pg_drop_db(self, db_name):
        """Drop a database (best-effort). ``WITH (FORCE)`` (PG13+)
        disconnects any lingering backends first, same effect as the
        ssh_docker era's separate ``dropdb --force`` shell-out."""
        self.ensure_one()
        if not self._DB_IDENT_RE.match(db_name or ''):
            return
        self._pg_mark_template(db_name, flag=False)
        self._docker_exec_sql(
            'DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % db_name, timeout=120)

    def _pg_ensure_db_with_grants(self, db_name):
        """Create ``db_name`` if it doesn't exist yet. No separate grant
        step is needed (see class-docstring note above): the role used
        to connect already owns everything it creates on this
        single-tenant Postgres server."""
        self.ensure_one()
        if not self._DB_IDENT_RE.match(db_name or ''):
            raise UserError(_("Invalid db name %r") % db_name)
        if self._pg_db_exists(db_name):
            return
        rc, out, err = self._docker_exec_sql(
            'CREATE DATABASE "%s"' % db_name, timeout=120)
        if rc != 0:
            raise UserError(_(
                "Failed to create database '%s':\n%s"
            ) % (db_name, err or out))

    # -- pod-exec transport (Kubernetes only; see /ROADMAP.md §3.1) --

    def _docker_exec_python(self, py_script, env=None, timeout=600):
        """Run ``py_script`` inside the instance's Odoo pod.

        Values that need to reach the script (db names, passwords) go
        via env vars so shell-quoting can't bite us. ``odoo.tools.config``
        is preloaded so the script can call into ``odoo.service.db``
        functions immediately. Returns ``(exit_code, stdout, stderr)``.
        """
        prelude = (
            "import os, sys\n"
            "import odoo\n"
            "import odoo.tools\n"
            "odoo.tools.config.parse_config(['-c','/etc/odoo/odoo.conf'])\n"
        )
        full_script = prelude + py_script
        command = "python3 - <<'SAAS_DBOPS_EOF'\n%s\nSAAS_DBOPS_EOF" % full_script
        r = self._compute_driver().exec(
            self._compute_handle(), command, env=env, timeout=timeout)
        return (r.rc, r.stdout, r.stderr)

    _PSQL_CONN_PRELUDE = (
        "import configparser, os, subprocess, sys\n"
        "cfg = configparser.ConfigParser()\n"
        "cfg.read('/etc/odoo/odoo.conf')\n"
        "o = cfg['options']\n"
        "env = dict(os.environ)\n"
        "env['PGPASSWORD'] = o.get('db_password', '')\n"
        "conn = ['-h', o.get('db_host', 'localhost'), '-p', o.get('db_port', '5432'),\n"
        "        '-U', o.get('db_user', 'odoo')]\n"
    )

    def _docker_exec_sql(self, sql, db='postgres', timeout=60):
        """Run a single SQL statement via ``psql`` inside the pod,
        against this instance's own dedicated Postgres server.

        Connection info is read from ``/etc/odoo/odoo.conf`` INSIDE the
        pod (see class docstring above) rather than from any
        ``saas.server``/``saas.instance`` field — there is no longer a
        separately-configured ``psql_port`` (Phase 1 deleted it along
        with the rest of the ssh_docker host model).
        """
        script = (
            self._PSQL_CONN_PRELUDE
            + "p = subprocess.run(['psql'] + conn + ['-d', %s, '-tA', '-c', %s],\n"
              "                   env=env, capture_output=True, text=True)\n"
              "sys.stdout.write(p.stdout)\n"
              "sys.stderr.write(p.stderr)\n"
              "sys.exit(p.returncode)\n"
        ) % (repr(db), repr(sql))
        command = "python3 - <<'SAAS_SQL_EOF'\n%s\nSAAS_SQL_EOF" % script
        r = self._compute_driver().exec(self._compute_handle(), command, timeout=timeout)
        return (r.rc, r.stdout, r.stderr)

    def _docker_exec_psql_file(self, path, db, timeout=600):
        """Run ``psql -f <path>`` inside the pod against ``db`` — same
        connection resolution as :meth:`_docker_exec_sql`, used where the
        SQL to run is too large/complex for a single ``-c`` argument
        (restoring a plain-SQL dump)."""
        script = (
            self._PSQL_CONN_PRELUDE
            + "p = subprocess.run(['psql'] + conn + ['-d', %s, '-f', %s],\n"
              "                   env=env, capture_output=True, text=True)\n"
              "sys.stdout.write(p.stdout)\n"
              "sys.stderr.write(p.stderr)\n"
              "sys.exit(p.returncode)\n"
        ) % (repr(db), repr(path))
        command = "python3 - <<'SAAS_PSQLF_EOF'\n%s\nSAAS_PSQLF_EOF" % script
        r = self._compute_driver().exec(self._compute_handle(), command, timeout=timeout)
        return (r.rc, r.stdout, r.stderr)

    def hosting_db_list(self):
        """List databases this instance's customer owns.

        Calls ``odoo.service.db.list_dbs(force=True)`` inside the pod —
        that's the exact function ``/web/database/list`` backs onto —
        additionally filtered by the instance prefix in Python so a
        customer can never see (or operate on) another tenant's
        database. Returns a list of dicts: ``{'name': str, 'admin_login': str}``.
        """
        self._ensure_hosting_for_db_ops()
        prefix = self._hosting_db_prefix()
        script = (
            "from odoo.service.db import list_dbs\n"
            "from odoo.sql_db import db_connect\n"
            "prefix = os.environ.get('SAAS_DB_PREFIX', '')\n"
            "names = [d for d in list_dbs(force=True) if d.startswith(prefix)]\n"
            "print('---SAAS_DB_LIST_BEGIN---')\n"
            "for n in names:\n"
            "    login = ''\n"
            "    try:\n"
            "        with db_connect(n).cursor() as cr:\n"
            "            cr.execute(\n"
            "                \"SELECT u.login FROM res_users u \"\n"
            "                \"JOIN ir_model_data m ON m.res_id = u.id \"\n"
            "                \"AND m.model = 'res.users' \"\n"
            "                \"WHERE m.module = 'base' \"\n"
            "                \"AND m.name = 'user_admin' LIMIT 1\")\n"
            "            row = cr.fetchone()\n"
            "            if row:\n"
            "                login = row[0] or ''\n"
            "    except Exception:\n"
            "        pass\n"
            "    print('%s|%s' % (n, login))\n"
            "print('---SAAS_DB_LIST_END---')\n"
        )
        try:
            exit_code, stdout, stderr = self._docker_exec_python(
                script, env={'SAAS_DB_PREFIX': prefix}, timeout=60)
        except Exception:
            _logger.exception(
                "hosting_db_list: pod-exec failed for %s", self.subdomain)
            raise UserError(_(
                "We couldn't reach your instance just now. Please "
                "try again in a moment."
            ))
        if exit_code != 0:
            _logger.warning(
                "hosting_db_list failed for %s: exit=%s stderr=%r stdout=%r",
                self.subdomain, exit_code,
                (stderr or '')[-500:], (stdout or '')[-200:],
            )
            try:
                self._append_log(
                    "Database list lookup failed (exit=%s). "
                    "Last stderr: %s"
                    % (exit_code, (stderr or '').strip()[-300:]),
                )
            except Exception:
                pass
            raise UserError(_(
                "We couldn't load your list of databases right now. "
                "Please try again in a moment, or contact support if "
                "the problem continues."
            ))
        rows = []
        capturing = False
        for line in stdout.splitlines():
            line = line.strip()
            if line == '---SAAS_DB_LIST_BEGIN---':
                capturing = True
                continue
            if line == '---SAAS_DB_LIST_END---':
                break
            if capturing and line:
                if '|' in line:
                    name, login = line.split('|', 1)
                else:
                    name, login = line, ''
                rows.append({'name': name, 'admin_login': login})
        return rows

    def hosting_sql_query(self, db_name, query, limit=1000):
        """Run a **read-only** SQL query against one of the customer's
        databases — the Odoo.sh-style SQL console. See
        :meth:`hosting_db_list` for the ownership/safety rationale;
        the statement itself runs inside a rolled-back ``READ ONLY``
        transaction so INSERT/UPDATE/DELETE/DDL are refused by
        Postgres, not by SQL parsing.

        Returns ``{'columns': [...], 'rows': [[...]], 'rowcount': int,
        'truncated': bool, 'error': str|None}``.
        """
        self._ensure_hosting_for_db_ops()
        names = {d['name'] for d in self.hosting_db_list()}
        if db_name not in names:
            raise UserError(_("Unknown database for this instance."))
        if not (query or '').strip():
            raise UserError(_("Enter a SQL query to run."))
        try:
            limit = max(1, min(int(limit or 1000), 10000))
        except (TypeError, ValueError):
            limit = 1000
        q_b64 = base64.b64encode((query or '').encode('utf-8')).decode('ascii')
        script = (
            "import os, json, base64\n"
            "from odoo.sql_db import db_connect\n"
            "db = os.environ['SAAS_SQL_DB']\n"
            "q = base64.b64decode(os.environ['SAAS_SQL_B64']).decode('utf-8')\n"
            "lim = int(os.environ['SAAS_SQL_LIMIT'])\n"
            "out = {'columns': [], 'rows': [], 'rowcount': 0,"
            " 'truncated': False, 'error': None}\n"
            "cr = db_connect(db).cursor()\n"
            "try:\n"
            "    cr.execute('SET TRANSACTION READ ONLY')\n"
            "    cr.execute(q)\n"
            "    if cr.description:\n"
            "        out['columns'] = [d.name for d in cr.description]\n"
            "        rows = cr.fetchmany(lim + 1)\n"
            "        out['truncated'] = len(rows) > lim\n"
            "        rows = rows[:lim]\n"
            "        def _cell(v):\n"
            "            if v is None or isinstance(v, (bool, int, float, str)):\n"
            "                return v\n"
            "            return str(v)\n"
            "        out['rows'] = [[_cell(c) for c in r] for r in rows]\n"
            "    out['rowcount'] = cr.rowcount\n"
            "except Exception as e:\n"
            "    out['error'] = str(e)\n"
            "finally:\n"
            "    try:\n"
            "        cr.rollback()\n"
            "    except Exception:\n"
            "        pass\n"
            "    try:\n"
            "        cr.close()\n"
            "    except Exception:\n"
            "        pass\n"
            "print('---SAAS_SQL_BEGIN---')\n"
            "print(base64.b64encode(json.dumps(out).encode('utf-8'))"
            ".decode('ascii'))\n"
            "print('---SAAS_SQL_END---')\n"
        )
        try:
            exit_code, stdout, stderr = self._docker_exec_python(
                script,
                env={
                    'SAAS_SQL_DB': db_name,
                    'SAAS_SQL_B64': q_b64,
                    'SAAS_SQL_LIMIT': str(limit),
                },
                timeout=60,
            )
        except Exception:
            _logger.exception(
                "hosting_sql_query: pod-exec failed for %s", self.subdomain)
            raise UserError(_(
                "We couldn't reach your instance just now. "
                "Please try again in a moment."))
        if exit_code != 0:
            _logger.warning(
                "hosting_sql_query failed for %s: exit=%s stderr=%r",
                self.subdomain, exit_code, (stderr or '')[-500:])
            raise UserError(_(
                "The SQL console couldn't run your query right now."))
        payload = None
        capturing = False
        for line in stdout.splitlines():
            line = line.strip()
            if line == '---SAAS_SQL_BEGIN---':
                capturing = True
                continue
            if line == '---SAAS_SQL_END---':
                break
            if capturing and line:
                payload = line
        if not payload:
            raise UserError(_("The SQL console returned no result."))
        try:
            return json.loads(base64.b64decode(payload).decode('utf-8'))
        except Exception:
            _logger.exception(
                "hosting_sql_query: bad payload for %s", self.subdomain)
            raise UserError(_(
                "The SQL console returned an unreadable result."))

    def _hosting_xmlrpc_db_proxy(self):
        """Return an XML-RPC proxy for this instance's ``db`` service."""
        import xmlrpc.client
        import ssl

        if not self.url:
            raise UserError(_("Instance has no URL yet — is it deployed?"))
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        url = '%s/xmlrpc/2/db' % self.url.rstrip('/')
        return xmlrpc.client.ServerProxy(url, context=ctx, allow_none=True)

    def hosting_db_create(self, name, login, password, lang='en_US',
                          country_code=None):
        """Create a customer database by cloning the per-instance template.

        1. Validate the requested name.
        2. Ensure the per-instance template ``__odoo_template_<sub>``
           exists (built once, slow; every call after is a SELECT).
        3. ``CREATE DATABASE <new> WITH TEMPLATE <template>`` — atomic,
           seconds, no Odoo init runs.
        4. Clone the template's filestore to the new DB's path.
        5. Patch the cloned admin user's login / password / lang and
           the company country.

        Any failure after the clone rolls back — drops the DB and its
        filestore — so a retry starts from a clean slate.
        """
        self._ensure_hosting_for_db_ops()
        name = self._hosting_db_full_name(name)
        login = (login or 'admin').strip()
        if not password:
            raise UserError(_("Initial admin password is required."))

        existing = {r['name'] for r in self.hosting_db_list()}
        if name in existing:
            raise UserError(_("Database '%s' already exists.") % name)

        template = self._hosting_ensure_template_db()

        self._append_log(
            "Cloning '%s' from template '%s'..." % (name, template)
        )
        self._pg_clone_db(template, name)

        try:
            self._hosting_clone_filestore(template, name)
        except Exception as e:
            self._pg_drop_db(name)
            raise UserError(_(
                "Database '%s' was cloned but filestore copy failed; "
                "rolled back:\n%s"
            ) % (name, e))

        try:
            self._hosting_patch_admin_creds(
                db_name=name, login=login, password=password,
                lang=lang or 'en_US', country_code=country_code,
            )
        except Exception as e:
            try:
                self._hosting_drop_filestore(name)
            except Exception:
                pass
            self._pg_drop_db(name)
            raise UserError(_(
                "Database '%s' cloned but admin credential patch "
                "failed; rolled back:\n%s"
            ) % (name, e))

        self._append_log("Database '%s' ready." % name)
        return name

    def hosting_db_create_async(self, name, login, password,
                                lang='en_US', country_code=None):
        """Queue a database create and return the tracking record."""
        self._ensure_hosting_for_db_ops()
        full_name = self._hosting_db_full_name(name)
        if not password:
            raise UserError(_("Initial admin password is required."))
        existing = {r['name'] for r in self.hosting_db_list()}
        if full_name in existing:
            raise UserError(_("Database '%s' already exists.") % full_name)
        Op = self.env['saas.instance.db.operation']
        running_create = Op.search([
            ('instance_id', '=', self.id),
            ('operation', '=', 'create'),
            ('state', '=', 'running'),
        ], limit=1)
        if running_create:
            raise UserError(_(
                "A database is already being created on this instance "
                "(%s). Please wait for it to finish before starting "
                "another."
            ) % running_create.db_name)

        op = Op.create({
            'instance_id': self.id,
            'db_name': full_name,
            'operation': 'create',
        })
        # NOT routed through saas.job (ARCH-004): create carries the new DB's
        # login/password as args, and the queue PERSISTS args to the job row —
        # which would store those secrets in the DB (SEC-002). run_in_background
        # passes them in memory only.
        run_in_background(
            op, '_run_create',
            method_args=(
                login, password, lang or 'en_US', country_code or None,
            ),
            thread_name='saas_db_create_%s' % full_name,
            heartbeat_field='last_heartbeat',
        )
        return op

    def hosting_db_duplicate(self, source, new_name):
        """Duplicate a database via the instance's XML-RPC db service."""
        self._ensure_hosting_for_db_ops()
        source = self._hosting_db_full_name(source)
        new_name = self._hosting_db_full_name(new_name)
        existing = {r['name'] for r in self.hosting_db_list()}
        if source not in existing:
            raise UserError(_("Source database '%s' does not exist.") % source)
        if new_name in existing:
            raise UserError(_("Target database '%s' already exists.") % new_name)

        import xmlrpc.client
        proxy = self._hosting_xmlrpc_db_proxy()
        master_pwd = self.sudo().admin_password
        try:
            proxy.duplicate_database(master_pwd, source, new_name)
        except xmlrpc.client.Fault as e:
            msg = (e.faultString or '').strip() or str(e)
            raise UserError(_("We couldn't duplicate the database: %s") % msg)
        except Exception:
            raise UserError(_(
                "We couldn't reach your instance just now. Please make "
                "sure it's running and try again."
            ))
        return new_name

    def hosting_db_duplicate_async(self, source, new_name):
        """Queue a database duplicate and return the tracking record."""
        self._ensure_hosting_for_db_ops()
        source_full = self._hosting_db_full_name(source)
        new_full = self._hosting_db_full_name(new_name)
        existing = {r['name'] for r in self.hosting_db_list()}
        if source_full not in existing:
            raise UserError(_("Source database '%s' does not exist.") % source_full)
        if new_full in existing:
            raise UserError(_("Target database '%s' already exists.") % new_full)
        Op = self.env['saas.instance.db.operation']
        if Op.search_count([
            ('instance_id', '=', self.id),
            ('db_name', '=', new_full),
            ('state', '=', 'running'),
        ]):
            raise UserError(
                _("A duplicate to '%s' is already in progress.") % new_full
            )
        op = Op.create({
            'instance_id': self.id,
            'db_name': new_full,
            'source_db': source_full,
            'operation': 'duplicate',
        })
        self.env['saas.job'].enqueue(
            op, '_run_duplicate', channel='dbop',
            lock_key='instance:%s' % self.id, idempotent=False, max_attempts=1)
        return op

    # Minimum length for a reset password.
    _ADMIN_PASSWORD_MIN_LENGTH = 6
    # Module names accepted by the upgrade actions. Validated
    # server-side; ``shlex``/``repr`` quoting on top is defense in depth.
    _UPGRADE_MODULE_RE = re.compile(r'^[a-z_][a-z0-9_]{0,63}$')

    def hosting_db_upgrade_module(self, name, module):
        """Recovery tool: ``odoo -u <module> -d <db> --stop-after-init``.

        Useful when the live Odoo is broken (500 on every request),
        where XML-RPC/live-exec into a running worker isn't reliable.
        Unlike the ssh_docker era, the pod is NOT stopped first — pod-exec
        is the only transport this method has, so stopping the pod would
        cut off the very channel used to run the recovery command. The
        CLI invocation still runs as an independent, ``--stop-after-init``
        one-shot process reading ``odoo.conf`` directly, so it works even
        while the live gunicorn workers are wedged.
        """
        self._ensure_hosting_for_db_ops()
        name = self._hosting_db_full_name(name)
        if name not in {r['name'] for r in self.hosting_db_list()}:
            raise UserError(
                _("Database '%s' does not belong to this instance.") % name
            )
        module = (module or '').strip().lower()
        if not module:
            raise UserError(_("Please type the feature you want to repair."))
        if module != 'all' and not self._UPGRADE_MODULE_RE.match(module):
            raise UserError(_(
                "'%s' isn't a valid feature name. Use lowercase letters, "
                "digits and underscores, or 'all' to repair everything."
            ) % module)

        self._append_log(
            "Running 'odoo -d %s -u %s' (recovery, one-shot)..."
            % (name, module)
        )
        run_args = (
            'odoo -d %s -u %s --stop-after-init --no-http '
            '--workers=0 --log-level=info'
        ) % (shlex.quote(name), shlex.quote(module))
        result = self._compute_driver().exec(
            self._compute_handle(), run_args, timeout=1800)
        output = (result.stdout or '') + (result.stderr or '')
        if not result.ok:
            exc = UserError(_(
                "The repair didn't complete successfully. See the "
                "report for details."
            ))
            exc._saas_upgrade_output = output
            raise exc
        return output

    def hosting_db_upgrade_module_async(self, name, module):
        """Queue an ``odoo -u <module>`` recovery upgrade and return the op."""
        self._ensure_hosting_for_db_ops()
        full_name = self._hosting_db_full_name(name)
        module_norm = (module or '').strip().lower()
        if not module_norm:
            raise UserError(_("Please type the feature you want to repair."))
        if module_norm != 'all' and not self._UPGRADE_MODULE_RE.match(module_norm):
            raise UserError(_(
                "'%s' isn't a valid feature name. Use lowercase letters, "
                "digits and underscores, or 'all' to repair everything."
            ) % module_norm)
        Op = self.env['saas.instance.db.operation']
        if Op.search_count([
            ('instance_id', '=', self.id),
            ('db_name', '=', full_name),
            ('state', '=', 'running'),
        ]):
            raise UserError(_(
                "Another operation is already in progress on '%s'."
            ) % full_name)
        op = Op.create({
            'instance_id': self.id,
            'db_name': full_name,
            'operation': 'upgrade',
            'module_name': module_norm,
        })
        self.env['saas.job'].enqueue(
            op, '_run_upgrade', channel='dbop',
            lock_key='instance:%s' % self.id, idempotent=False, max_attempts=1)
        return op

    def _parse_upgrade_modules(self, modules):
        """Normalise + validate a customer-typed module list."""
        raw = (modules or '').replace(',', ' ').split()
        seen, out = set(), []
        for token in raw:
            m = token.strip().lower()
            if not m or m in seen:
                continue
            seen.add(m)
            if m == 'all':
                return ['all']
            if not self._UPGRADE_MODULE_RE.match(m):
                raise UserError(_(
                    "'%s' isn't a valid module name. Use lowercase "
                    "letters, digits and underscores (e.g. 'sale', "
                    "'stock_account')."
                ) % token)
            out.append(m)
        if not out:
            raise UserError(_("Please enter at least one module to upgrade."))
        return out

    def hosting_db_upgrade_modules(self, name, modules):
        """Upgrade one or more modules on a customer DB with NO downtime.

        Runs Odoo's own ``button_immediate_upgrade`` inside the *live*
        pod via ``driver.exec()`` — the pod IS "the running service" on
        Kubernetes (no separate "docker exec into a running service" vs
        "docker compose run a fresh one" distinction to preserve), so
        this needs no stop/start at all.
        """
        self._ensure_hosting_for_db_ops()
        name = self._hosting_db_full_name(name)
        if name not in {r['name'] for r in self.hosting_db_list()}:
            raise UserError(
                _("Database '%s' does not belong to this instance.") % name
            )
        mod_list = self._parse_upgrade_modules(modules)

        script = (
            "from odoo.modules.registry import Registry\n"
            "from odoo import api, SUPERUSER_ID\n"
            "db = os.environ['SAAS_DB']\n"
            "names = [m for m in os.environ['SAAS_MODULES'].split() if m]\n"
            "registry = Registry(db)\n"
            "cr = registry.cursor()\n"
            "env = api.Environment(cr, SUPERUSER_ID, {})\n"
            "Mod = env['ir.module.module']\n"
            "if names == ['all']:\n"
            "    mods = Mod.search([('state', '=', 'installed')])\n"
            "else:\n"
            "    mods = Mod.search([('name', 'in', names)])\n"
            "    found = set(mods.mapped('name'))\n"
            "    missing = [n for n in names if n not in found]\n"
            "    if missing:\n"
            "        sys.stderr.write('SAAS_NOT_FOUND:' + ','.join(missing) + '\\n')\n"
            "        sys.exit(2)\n"
            "    bad = mods.filtered(lambda m: m.state != 'installed')\n"
            "    if bad:\n"
            "        sys.stderr.write('SAAS_NOT_INSTALLED:' + ','.join(bad.mapped('name')) + '\\n')\n"
            "        sys.exit(2)\n"
            "if not mods:\n"
            "    sys.stderr.write('SAAS_NOTHING\\n')\n"
            "    sys.exit(2)\n"
            "targets = ','.join(sorted(mods.mapped('name')))\n"
            "print('---SAAS_UPGRADE_BEGIN---')\n"
            "print('upgrading=%s' % targets)\n"
            "sys.stdout.flush()\n"
            "mods.button_immediate_upgrade()\n"
            "print('upgraded=%s' % targets)\n"
            "print('---SAAS_UPGRADE_END---')\n"
            "sys.stdout.flush()\n"
            "os._exit(0)\n"
        )
        script_env = {'SAAS_DB': name, 'SAAS_MODULES': ' '.join(mod_list)}
        ec, sout, serr = self._docker_exec_python(
            script, env=script_env, timeout=1800)
        combined = (sout or '') + (serr or '')

        if 'SAAS_NOT_FOUND:' in combined:
            bad = combined.split('SAAS_NOT_FOUND:', 1)[1].splitlines()[0]
            raise UserError(_(
                "These modules aren't installed on this database: %s. "
                "Check the names and try again."
            ) % bad)
        if 'SAAS_NOT_INSTALLED:' in combined:
            bad = combined.split('SAAS_NOT_INSTALLED:', 1)[1].splitlines()[0]
            raise UserError(_(
                "These modules exist but aren't installed, so there's "
                "nothing to upgrade: %s."
            ) % bad)
        if 'SAAS_NOTHING' in combined:
            raise UserError(_("No installed modules matched your request."))
        if ec != 0 or '---SAAS_UPGRADE_END---' not in sout:
            exc = UserError(_(
                "The upgrade didn't complete successfully. See the "
                "report below for details."
            ))
            exc._saas_upgrade_output = combined
            raise exc
        return combined

    def hosting_db_upgrade_modules_async(self, name, modules):
        """Queue a no-downtime module upgrade and return the tracking op."""
        self._ensure_hosting_for_db_ops()
        full_name = self._hosting_db_full_name(name)
        mod_list = self._parse_upgrade_modules(modules)
        if full_name not in {r['name'] for r in self.hosting_db_list()}:
            raise UserError(
                _("Database '%s' does not belong to this instance.") % full_name
            )
        Op = self.env['saas.instance.db.operation']
        if Op.search_count([
            ('instance_id', '=', self.id),
            ('db_name', '=', full_name),
            ('state', '=', 'running'),
        ]):
            raise UserError(_(
                "Another operation is already in progress on '%s'. "
                "Please wait for it to finish."
            ) % full_name)
        op = Op.create({
            'instance_id': self.id,
            'db_name': full_name,
            'operation': 'upgrade',
            'module_name': ' '.join(mod_list),
        })
        self.env['saas.job'].enqueue(
            op, '_run_upgrade_live', channel='dbop',
            lock_key='instance:%s' % self.id, idempotent=False, max_attempts=1)
        return op

    def hosting_db_restore_prepare_upload(self, name):
        """Create a placeholder backup record + a presigned PUT URL so
        the customer can upload their OWN local Odoo backup (.zip)
        straight to the bucket from the browser."""
        self._ensure_hosting_for_db_ops()
        full = self._hosting_db_full_name(name)
        if full in {r['name'] for r in self.hosting_db_list()}:
            raise UserError(_(
                "A database named '%s' already exists. Choose a different "
                "name — restore creates a new database from your backup."
            ) % full)
        if self.plan_id and self.plan_id.is_trial_plan:
            raise UserError(_(
                "Restore isn't available on trial plans. Please upgrade "
                "to a paid plan."
            ))
        Backup = self.env['saas.instance.backup']
        now = fields.Datetime.now()
        ts = now.strftime('%Y-%m-%d_%H-%M-%S')
        object_key = 'ondemand/restore-upload/%s_%s.zip' % (full, ts)
        backup = Backup.create({
            'instance_id': self.id,
            'db_name': full,
            'name': 'Restore upload %s' % full,
            'state': 'running',
            'is_full_instance': False,
            'ephemeral': True,
            'format': 'zip',
            'bucket_path': object_key,
            'expires_at': now + datetime.timedelta(hours=2),
        })
        upload_url = backup._generate_presigned_put_url(object_key)
        return backup, upload_url

    def hosting_db_restore_from_upload(self, backup_id):
        """Verify an uploaded object, then restore it into its db_name."""
        self._ensure_hosting_for_db_ops()
        backup = self.env['saas.instance.backup'].browse(backup_id)
        if (not backup.exists() or backup.instance_id != self
                or not backup.ephemeral or backup.is_full_instance):
            raise UserError(_("That upload isn't available to restore."))
        size = backup._bucket_object_size(backup.bucket_path) or 0
        if not size:
            raise UserError(_(
                "We couldn't find your uploaded file. The upload may not "
                "have finished — please try again."
            ))
        backup.write({
            'state': 'done',
            'size_mb': round(size / (1024 * 1024), 2),
        })
        Op = self.env['saas.instance.db.operation']
        if Op.search_count([
            ('instance_id', '=', self.id),
            ('db_name', '=', backup.db_name),
            ('state', '=', 'running'),
        ]):
            raise UserError(_(
                "Another operation is already in progress on '%s'. Please "
                "wait for it to finish."
            ) % backup.db_name)
        op = Op.create({
            'instance_id': self.id,
            'db_name': backup.db_name,
            'operation': 'restore',
        })
        self.env['saas.job'].enqueue(
            op, '_run_restore', args=(backup.id,), channel='dbop',
            lock_key='instance:%s' % self.id, idempotent=False, max_attempts=1)
        return op

    def hosting_db_reset_admin_password(self, name, new_password,
                                        login=None):
        """Reset an administrator's password on a customer database.
        See the original design note: which user gets reset, in order,
        is ``login`` (if given) -> ``base.user_admin`` (if active) ->
        oldest active member of ``base.group_system`` -> oldest active
        internal user."""
        self._ensure_hosting_for_db_ops()
        name = self._hosting_db_full_name(name)
        if name not in {r['name'] for r in self.hosting_db_list()}:
            raise UserError(
                _("Database '%s' does not belong to this instance.") % name
            )
        if not new_password:
            raise UserError(_("New password is required."))
        if len(new_password) < self._ADMIN_PASSWORD_MIN_LENGTH:
            raise UserError(_(
                "Password must be at least %d characters."
            ) % self._ADMIN_PASSWORD_MIN_LENGTH)

        script = (
            "from odoo.modules.registry import Registry\n"
            "from odoo import api, SUPERUSER_ID\n"
            "registry = Registry(os.environ['SAAS_DB'])\n"
            "with registry.cursor() as cr:\n"
            "    env = api.Environment(cr, SUPERUSER_ID, {})\n"
            "    Users = env['res.users']\n"
            "    target = (os.environ.get('SAAS_TARGET_LOGIN') or '').strip()\n"
            "    if target:\n"
            "        user = Users.search([('login', '=', target)], limit=1)\n"
            "        if not user:\n"
            "            raise SystemExit('NO_SUCH_USER')\n"
            "    else:\n"
            "        user = env.ref('base.user_admin', raise_if_not_found=False)\n"
            "        if not (user and user.active):\n"
            "            grp = env.ref('base.group_system', raise_if_not_found=False)\n"
            "            pool = grp.users if grp else Users\n"
            "            cands = pool.filtered(lambda u: u.active and not u.share)\n"
            "            if not cands:\n"
            "                cands = Users.search("
            "[('active', '=', True), ('share', '=', False)])\n"
            "            user = cands.sorted('id')[:1]\n"
            "    if not user:\n"
            "        raise SystemExit('NO_ADMIN_USER')\n"
            "    user.password = os.environ['SAAS_NEW_PW']\n"
            "    cr.commit()\n"
            "    print('---SAAS_PW_RESET_BEGIN---')\n"
            "    print('login=%s' % (user.login or ''))\n"
            "    print('---SAAS_PW_RESET_END---')\n"
        )
        script_env = {'SAAS_DB': name, 'SAAS_NEW_PW': new_password}
        if login:
            script_env['SAAS_TARGET_LOGIN'] = login.strip()
        exit_code, stdout, stderr = self._docker_exec_python(
            script, env=script_env, timeout=120)
        combined = (stdout or '') + (stderr or '')
        if 'NO_SUCH_USER' in combined:
            raise UserError(_(
                "No user with login '%s' exists on '%s'. Leave the login "
                "blank to reset the main administrator instead."
            ) % (login, name))
        if 'NO_ADMIN_USER' in combined:
            raise UserError(_(
                "We couldn't find an administrator account on '%s' to "
                "reset. If every admin user was removed, please contact "
                "support."
            ) % name)
        if exit_code != 0 or '---SAAS_PW_RESET_BEGIN---' not in stdout:
            raise UserError(_(
                "We couldn't reset the admin password for '%s' just now. "
                "Please try again, or contact support if the problem "
                "continues."
            ) % name)
        reset_login = ''
        capturing = False
        for line in stdout.splitlines():
            line = line.strip()
            if line == '---SAAS_PW_RESET_BEGIN---':
                capturing = True
                continue
            if line == '---SAAS_PW_RESET_END---':
                break
            if capturing and line.startswith('login='):
                reset_login = line[len('login='):]
        return reset_login or (login or 'admin')

    def hosting_db_drop(self, name):
        """Drop a customer database at the PG level (force-terminates
        connections, drops atomically)."""
        self._ensure_hosting_for_db_ops()
        name = self._hosting_db_full_name(name)
        if name not in {r['name'] for r in self.hosting_db_list()}:
            raise UserError(
                _("Database '%s' does not belong to this instance.") % name
            )
        self._pg_drop_db(name)
        self.env['saas.audit.log'].saas_audit(
            'db_drop', model='saas.instance', res_id=self.id,
            res_name=self.subdomain, detail='Dropped database %s' % name)
        try:
            self._hosting_drop_filestore(name)
        except Exception:
            _logger.warning(
                "Dropped DB '%s' but filestore cleanup failed; orphaned "
                "files remain at its filestore path.", name,
            )
        return name

    def hosting_db_drop_async(self, name):
        """Queue a database drop and return the tracking record."""
        self._ensure_hosting_for_db_ops()
        full_name = self._hosting_db_full_name(name)
        Op = self.env['saas.instance.db.operation']
        if Op.search_count([
            ('instance_id', '=', self.id),
            ('db_name', '=', full_name),
            ('state', '=', 'running'),
        ]):
            raise UserError(
                _("A drop of '%s' is already in progress.") % full_name
            )
        op = Op.create({
            'instance_id': self.id,
            'db_name': full_name,
            'operation': 'drop',
        })
        self.env['saas.job'].enqueue(
            op, '_run_drop', channel='dbop',
            lock_key='instance:%s' % self.id, idempotent=False, max_attempts=1)
        return op

    def hosting_db_backup(self, name, backup_format='zip'):
        """Create the instance's single on-demand backup of one database.

        Policy: an instance keeps AT MOST ONE on-demand backup at a
        time, ephemeral (reaped within an hour). Reuses
        ``_run_portal_backup``, which honours the record's ``db_name``
        and ``ephemeral`` flag.
        """
        self.ensure_one()
        self._ensure_hosting_for_db_ops()
        full = self._hosting_db_full_name(name)
        if full not in {r['name'] for r in self.hosting_db_list()}:
            raise UserError(
                _("Database '%s' does not belong to this instance.") % full
            )
        if self.plan_id and self.plan_id.is_trial_plan:
            raise UserError(_(
                "Backups are not available on trial plans. Please "
                "upgrade to a paid plan."
            ))

        Backup = self.env['saas.instance.backup']
        self.env.cr.execute(
            "SELECT id FROM saas_instance WHERE id = %s FOR UPDATE",
            (self.id,),
        )
        if Backup.search_count([
            ('instance_id', '=', self.id),
            ('state', '=', 'running'),
        ]):
            raise UserError(_(
                "A backup is already in progress on this instance. "
                "Please wait for it to finish."
            ))

        old = Backup.search([
            ('instance_id', '=', self.id),
            ('is_full_instance', '=', False),
        ])
        for b in old:
            try:
                b._delete_from_bucket()
            except Exception:
                _logger.warning(
                    "Couldn't delete bucket object for on-demand backup "
                    "%s; removing record anyway.", b.id,
                )
            b.unlink()

        fmt = 'dump' if backup_format == 'dump' else 'zip'
        now_str = fields.Datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        backup = Backup.create({
            'instance_id': self.id,
            'db_name': full,
            'name': 'backup_%s_%s.%s' % (full, now_str, fmt),
            'state': 'running',
            'is_full_instance': False,
            'ephemeral': True,
            'format': fmt,
        })
        self.env['saas.job'].enqueue(
            backup, '_run_portal_backup',
            channel='backup', lock_key='instance:%s' % self.id,
            max_attempts=1,
        )
        return backup

    def _hosting_template_db_name(self):
        """Per-instance template DB name — outside the customer's
        prefix namespace, so it never appears in ``hosting_db_list``
        and can't be targeted by a customer-typed name."""
        self.ensure_one()
        safe = (self.subdomain or '').replace('-', '_').lower()
        return '__odoo_template_%s' % safe

    def _hosting_ensure_template_db(self):
        """Return a ready-to-clone per-instance template DB, building
        it if necessary. Self-healing and concurrency-safe (serialised
        by a per-instance in-process lock)."""
        self.ensure_one()
        template = self._hosting_template_db_name()
        with _hosting_template_build_lock(self.id):
            if self._pg_db_initialized(template):
                self._pg_mark_template(template, flag=True)
                return template

            if self._pg_db_exists(template):
                self._append_log(
                    "Template '%s' exists but is incomplete (previous "
                    "build interrupted) — dropping and rebuilding."
                    % template
                )
                self._pg_drop_db(template)
                try:
                    self._hosting_drop_filestore(template)
                except Exception:
                    pass

            return self._hosting_build_template_db(template)

    def _hosting_build_template_db(self, template):
        """Build ``template`` from scratch: createdb -> init -> verify.

        Unlike the ssh_docker era, this does NOT stop/destroy the pod
        first — there is no separate one-shot "docker compose run --rm"
        container to isolate the init from the live service, so the
        one-time ``odoo -i base`` runs via a plain ``driver.exec()``
        against the live pod instead (same one-shot ``--stop-after-init``
        invocation as the recovery path in
        :meth:`hosting_db_upgrade_module`). This is a real, accepted
        trade-off: a broken/version-mismatched customer module on the
        image's default addons path could in principle abort this
        init — the ssh_docker version worked around that by building
        with a customer-addons-stripped path, which has no equivalent
        here since there's no separate host-side ``odoo.conf`` to grep.
        """
        self.ensure_one()
        self._append_log(
            "Bootstrapping per-instance template DB '%s' (one-time, "
            "~60-90s)..." % template
        )
        self._pg_ensure_db_with_grants(template)
        self._pg_mark_template(template, flag=True)

        run_args = (
            'odoo -d %s -i base --without-demo=all --stop-after-init '
            '--no-http --workers=0 --log-level=info'
        ) % shlex.quote(template)
        result = self._compute_driver().exec(
            self._compute_handle(), run_args, timeout=1800)
        init_output = (result.stdout or '') + (result.stderr or '')

        if not result.ok or not self._pg_db_initialized(template):
            try:
                self._pg_drop_db(template)
            except Exception:
                pass
            try:
                self._hosting_drop_filestore(template)
            except Exception:
                pass
            raise UserError(_(
                "Couldn't prepare the database template for this "
                "instance (the one-time setup failed).\n\n"
                "Last setup output:\n%s"
            ) % (init_output[-6000:] or '(no output captured)'))

        self._append_log("Template DB '%s' ready." % template)
        return template

    def _hosting_filestore_path(self, db_name):
        """In-pod path to a DB's Odoo filestore — the operator mounts
        the filestore PVC at ``/var/lib/odoo`` and sets ``data_dir`` to
        match (see compute/operator/internal/resources/{configmap,
        deployment}.go), so this needs no host-path/JuiceFS-mount
        distinction the ssh_docker era had."""
        return '/var/lib/odoo/filestore/%s' % db_name

    def _hosting_clone_filestore(self, source_db, target_db):
        """Clone the template's filestore directory to the new DB's
        path, inside the pod — no sudo/chown needed (unlike ssh_docker,
        there's no separate SSH user; the copy runs as the same
        container user that already owns every file involved)."""
        self.ensure_one()
        src = self._hosting_filestore_path(source_db)
        dst = self._hosting_filestore_path(target_db)
        cmd = (
            'rm -rf %(dst)s && '
            'if [ -d %(src)s ]; then cp -a %(src)s %(dst)s; '
            'else mkdir -p %(dst)s; fi'
        ) % {'src': shlex.quote(src), 'dst': shlex.quote(dst)}
        r = self._compute_driver().exec(self._compute_handle(), cmd, timeout=600)
        if not r.ok:
            raise UserError(_("Failed to clone filestore:\n%s") % (r.stderr or r.stdout))

    def _hosting_drop_filestore(self, db_name):
        """Remove a DB's filestore directory. Best-effort."""
        self.ensure_one()
        if not self._DB_IDENT_RE.match(db_name or ''):
            return
        path = self._hosting_filestore_path(db_name)
        self._compute_driver().exec(
            self._compute_handle(), 'rm -rf %s' % shlex.quote(path), timeout=120)

    def _hosting_patch_admin_creds(self, db_name, login, password,
                                   lang, country_code):
        """Set admin login / password / lang on a freshly-cloned DB via
        the ORM (inside the pod) so Odoo's password hashing runs."""
        script = (
            "from contextlib import closing\n"
            "import odoo\n"
            "from odoo import api, SUPERUSER_ID\n"
            "from odoo.modules.registry import Registry\n"
            "registry = Registry(os.environ['SAAS_DB_NAME'])\n"
            "with closing(registry.cursor()) as cr:\n"
            "    env = api.Environment(cr, SUPERUSER_ID, {})\n"
            "    admin = env.ref('base.user_admin')\n"
            "    vals = {\n"
            "        'login': os.environ['SAAS_DB_LOGIN'],\n"
            "        'password': os.environ['SAAS_DB_PWD'],\n"
            "        'lang': os.environ['SAAS_DB_LANG'],\n"
            "    }\n"
            "    if '@' in os.environ['SAAS_DB_LOGIN']:\n"
            "        vals['email'] = os.environ['SAAS_DB_LOGIN']\n"
            "    admin.write(vals)\n"
            "    cc = os.environ.get('SAAS_DB_CC') or ''\n"
            "    if cc:\n"
            "        country = env['res.country'].search("
            "            [('code', 'ilike', cc)], limit=1)\n"
            "        if country:\n"
            "            env['res.company'].browse(1).write({\n"
            "                'country_id': country.id,\n"
            "                'currency_id': country.currency_id.id,\n"
            "            })\n"
            "    cr.commit()\n"
            "print('OK')\n"
        )
        env = {
            'SAAS_DB_NAME': db_name,
            'SAAS_DB_LANG': lang,
            'SAAS_DB_PWD': password,
            'SAAS_DB_LOGIN': login,
            'SAAS_DB_CC': country_code or '',
        }
        exit_code, stdout, stderr = self._docker_exec_python(
            script, env=env, timeout=120)
        if exit_code != 0 or 'OK' not in (stdout or ''):
            raise UserError(_(
                "Could not patch admin credentials:\n%s\n%s"
            ) % ((stdout or '')[-1000:], (stderr or '')[-500:]))

    def _do_restore_backup(self, backup_id):
        """Restore a backup — replace target DB and filestore.

        Unlike the ssh_docker era, the pod is never stopped: pod-exec
        IS the transport every step below uses, so stopping it would
        cut off the channel itself. Lingering connections to the
        TARGET database are instead dropped at the Postgres level
        (``pg_terminate_backend``) right before ``DROP DATABASE``, which
        achieves the same "no live backend survives" guarantee without
        needing the pod down — so this single code path now covers both
        hosting (other DBs on the instance keep serving) and service
        instances (previously handled by stopping/restarting the whole
        container).
        """
        self.ensure_one()
        backup = self.env['saas.instance.backup'].browse(backup_id)
        db_name = backup.db_name or self.subdomain
        name_re = (
            re.compile(r'^[a-z][a-z0-9_-]{0,62}$')
            if self.is_hosting else SUBDOMAIN_RE
        )
        if not name_re.match(db_name or ''):
            raise UserError(
                _("Refusing to restore: invalid db name %r") % db_name
            )

        manifest = backup._read_manifest_safe()
        backup_version = (manifest or {}).get('odoo_version') if isinstance(manifest, dict) else None
        if backup_version and self.odoo_version_id and \
                backup_version != self.odoo_version_id.name:
            raise UserError(_(
                "Backup was taken on Odoo version %s but this instance "
                "runs %s. Aborting to avoid silent schema corruption."
            ) % (backup_version, self.odoo_version_id.name))

        driver = self._compute_driver()
        handle = self._compute_handle()

        self._append_log("Releasing connections to database '%s'..." % db_name)
        safe_db = db_name.replace("'", "''")
        try:
            self._docker_exec_sql(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname='%s' AND pid <> pg_backend_pid()" % safe_db,
                timeout=30,
            )
        except Exception as e:
            self._append_log("Note: connection release failed (%s); continuing." % e)

        self._append_log("Downloading backup...")
        download_url = backup._generate_presigned_url()
        tmp_zip = '/tmp/saas_restore_%s.zip' % db_name
        extract_dir = '/tmp/saas_restore_%s' % db_name
        r = driver.exec(
            handle,
            'curl -fsSL -o %s %s' % (shlex.quote(tmp_zip), shlex.quote(download_url)),
            timeout=600,
        )
        if not r.ok:
            raise UserError(_("Failed to download backup:\n%s\n%s") % (r.stdout, r.stderr))

        self._append_log("Validating backup archive...")
        r = driver.exec(
            handle, 'python3 -m zipfile -l %s' % shlex.quote(tmp_zip), timeout=120)
        if not r.ok:
            raise UserError(_(
                "The backup file isn't a valid .zip archive (it may be "
                "corrupt or have uploaded incompletely). Nothing was "
                "changed."
            ))
        if 'dump.sql' not in r.stdout:
            raise UserError(_(
                "This .zip doesn't look like an Odoo database backup — "
                "it has no dump.sql inside. Nothing was changed."
            ))

        self._append_log("Extracting...")
        driver.exec(handle, 'rm -rf %s && mkdir -p %s' % (
            shlex.quote(extract_dir), shlex.quote(extract_dir)))
        r = driver.exec(
            handle,
            'python3 -m zipfile -e %s %s' % (
                shlex.quote(tmp_zip), shlex.quote(extract_dir)),
            timeout=300,
        )
        if not r.ok:
            raise UserError(_("Failed to extract backup:\n%s\n%s") % (r.stdout, r.stderr))

        r = driver.exec(
            handle,
            'test -s %s && echo OK || echo MISSING'
            % shlex.quote('%s/dump.sql' % extract_dir),
            timeout=60,
        )
        if 'OK' not in r.stdout:
            raise UserError(_(
                "The backup is missing its database dump after "
                "extraction — aborting before any change."
            ))

        self._append_log("Dropping current database...")
        rc, out, err = self._docker_exec_sql(
            'DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % db_name, timeout=120)
        if rc != 0:
            raise UserError(_(
                "dropdb failed for %s — aborting restore:\n%s"
            ) % (db_name, err or out))
        rc, out, err = self._docker_exec_sql(
            'CREATE DATABASE "%s"' % db_name, timeout=120)
        if rc != 0:
            raise UserError(_(
                "createdb failed for %s — aborting restore:\n%s"
            ) % (db_name, err or out))

        self._append_log("Restoring database...")
        dump_path = '%s/dump.sql' % extract_dir
        rc, out, err = self._docker_exec_psql_file(dump_path, db_name, timeout=600)
        if rc != 0:
            self._append_log("Restore output:\n%s" % (out or '')[-2000:])
            raise UserError(_("Database restore failed:\n%s") % (err or '')[-500:])

        self._append_log("Restoring filestore...")
        filestore_src = '%s/filestore' % extract_dir
        filestore_dst = self._hosting_filestore_path(db_name)
        fs_cmd = (
            'rm -rf %(dst)s && mkdir -p %(dst)s && '
            'if [ -d %(src)s ]; then cp -a %(src)s/. %(dst)s/; fi'
        ) % {'dst': shlex.quote(filestore_dst), 'src': shlex.quote(filestore_src)}
        driver.exec(handle, fs_cmd, timeout=300)

        driver.exec(handle, 'rm -rf %s %s' % (
            shlex.quote(tmp_zip), shlex.quote(extract_dir)))

        self.state = 'running'
        self.pending_operation = False
        self._append_log("Backup '%s' restored successfully." % backup.name)
        self._safe_refresh_usage()

    def action_restore_backup(self, backup_id):
        """Restore a per-database backup to this instance (async)."""
        self.ensure_one()
        if self.state not in ('running', 'stopped'):
            raise UserError(
                _("Instance must be Running or Stopped to restore a backup.")
            )

        backup = self.env['saas.instance.backup'].browse(backup_id)
        if not backup.exists() or backup.instance_id != self:
            raise UserError(_("Invalid backup."))
        if backup.state != 'done':
            raise UserError(_("Only completed backups can be restored."))
        if backup.is_full_instance:
            raise UserError(_(
                "This is a full-instance snapshot — use "
                "action_restore_full_instance, not action_restore_backup."
            ))
        if not backup.bucket_path:
            raise UserError(_(
                "Backup record has no cloud object path — nothing to "
                "download. This row is probably a failed or partial "
                "backup; delete it and create a fresh one."
            ))

        self.env.cr.execute(
            "SELECT id FROM saas_instance WHERE id = %s FOR UPDATE",
            (self.id,),
        )
        if self.pending_operation == 'restore' or self.state == 'provisioning':
            raise UserError(_("A restore is already in progress for this instance."))

        prev_state = self.state
        self.pre_provisioning_state = prev_state
        self.pending_operation = 'restore'
        self.state = 'provisioning'
        self._append_log("Restore from backup '%s' queued..." % backup.name)
        self.env['saas.audit.log'].saas_audit(
            'instance_restore_backup', model='saas.instance', res_id=self.id,
            res_name=self.subdomain,
            detail='Restore from backup %r (id=%s) queued' % (backup.name, backup.id))
        self.env['saas.job'].enqueue(
            self, '_do_restore_backup', args=(backup.id,), channel='restore',
            lock_key='instance:%s' % self.id, max_attempts=1,
            on_error='_on_background_error', on_error_args=(prev_state,))
        return True

    def action_restore_full_instance(self, backup_id):
        """Restore a full-instance backup (operator-native pg_dump +
        filestore tar) onto this SAME, already-running instance.

        Unlike the ssh_docker/restic era, this is no longer restricted
        to hosting instances: the operator's ``spec.backup`` mechanism
        produces the same db.dump + filestore.tar.gz shape for every
        instance type, so a service instance's single-DB "full
        instance" backup is restorable the same way.
        """
        self.ensure_one()
        if self.state not in ('running', 'stopped'):
            raise UserError(
                _("Instance must be Running or Stopped to restore.")
            )
        if not self.daily_backup_enabled:
            raise UserError(_(
                "Restore is part of the Daily Snapshots feature. "
                "Please enable Daily Backups (and complete the payment) "
                "before restoring — once active, the Restore button "
                "becomes available."
            ))

        backup = self.env['saas.instance.backup'].browse(backup_id)
        if not backup.exists() or backup.instance_id != self:
            raise UserError(_("Invalid backup."))
        if backup.state != 'done':
            raise UserError(_("Only completed backups can be restored."))
        if not backup.is_full_instance:
            raise UserError(_(
                "This backup is per-database. Use the per-DB restore button."
            ))

        self.env.cr.execute(
            "SELECT id FROM saas_instance WHERE id = %s FOR UPDATE",
            (self.id,),
        )
        if self.pending_operation == 'restore' or self.state == 'provisioning':
            raise UserError(_("A restore is already in progress for this instance."))

        prev_state = self.state
        self.pre_provisioning_state = prev_state
        self.pending_operation = 'restore'
        self.state = 'provisioning'
        self._append_log("Full-instance restore from '%s' queued..." % backup.name)
        self.env['saas.audit.log'].saas_audit(
            'instance_restore_full', model='saas.instance', res_id=self.id,
            res_name=self.subdomain,
            detail='Full-instance restore from %r (id=%s) queued' % (backup.name, backup.id))
        self.env['saas.job'].enqueue(
            backup, '_do_restore_full_instance', args=(self.id,), channel='restore',
            lock_key='instance:%s' % self.id, max_attempts=1,
            on_error='_on_restore_full_instance_error', on_error_args=(self.id, prev_state))
        return True

    def _backup_bucket_prefix(self):
        """Stable per-instance object-storage prefix the operator's own
        backup CronJob writes under (see ``set_scheduled_backup``'s
        ``prefix`` kwarg and compute/tools/backup-tool/run-backup.sh's
        ``DESTINATION_PREFIX``)."""
        self.ensure_one()
        return 'backups/%s' % (self.subdomain or '')

    def _sync_scheduled_backup(self):
        """Push ``daily_backup_enabled`` (net of ``daily_backup_suspended``)
        onto the OdooInstance CR's ``spec.backup`` via
        ``KubernetesDriver.set_scheduled_backup``. Call this ONCE
        whenever either field actually changes state — NOT on every
        cron tick; the operator's own CronJob owns the actual nightly
        schedule/execution from here on.
        """
        self.ensure_one()
        if not self.docker_server_id:
            return
        effective = bool(self.daily_backup_enabled) and not self.daily_backup_suspended
        kwargs = {}
        if effective:
            Backup = self.env['saas.instance.backup']
            try:
                cfg = Backup._get_backup_config()
            except UserError:
                _logger.warning(
                    "Cannot enable scheduled backup for %s: backup "
                    "storage isn't configured (SaaS Manager > Settings).",
                    self.subdomain)
                return
            kwargs = dict(
                bucket=cfg['bucket'],
                prefix=self._backup_bucket_prefix(),
                access_key=cfg['access_key'],
                secret_key=cfg['secret_key'],
                endpoint=cfg['endpoint'] or '',
            )
        try:
            self._compute_driver().set_scheduled_backup(
                self._compute_handle(), enabled=effective, **kwargs)
        except Exception as e:
            _logger.warning(
                "Failed to sync scheduled backup for %s: %s", self.subdomain, e)

    def _ensure_webhooks_registered(self):
        """Verify and register webhooks for all repos that need them.

        Called at the end of deploy/redeploy to guarantee that every repo
        with ``webhook_enabled=True`` and a token actually has a working
        webhook on the Git provider.  Logs the outcome per repo so the
        operator can see exactly what happened.
        """
        self.ensure_one()
        repos = self.repo_ids.filtered(
            lambda r: r.state == 'cloned' and r.webhook_enabled
        )
        if not repos:
            return

        for repo in repos:
            token = repo.sudo().github_token
            if not token:
                self._append_log(
                    "Webhook skipped for %s: no access token. "
                    "Client must provide a token for auto-deploy."
                    % repo.name
                )
                continue

            # Already registered and valid?
            if repo.webhook_provider_id:
                try:
                    if repo._verify_webhook_on_provider():
                        self._append_log(
                            "Webhook verified for %s (provider ID: %s)."
                            % (repo.name, repo.webhook_provider_id)
                        )
                        continue
                except Exception:
                    pass
                # Stale provider ID — clear and re-register
                repo.webhook_provider_id = False

            # Register (or re-register)
            self._append_log(
                "Registering webhook for %s..." % repo.name
            )
            try:
                success = repo._register_webhook_with_retry()
                if not success:
                    self._append_log(
                        "WARNING: Webhook registration failed for %s. "
                        "Auto-deploy will NOT work until this is fixed. "
                        "Check the access token and web.base.url setting."
                        % repo.name
                    )
            except Exception as e:
                _logger.warning(
                    "Webhook registration error for %s: %s", repo.name, e,
                )
                self._append_log(
                    "WARNING: Webhook registration error for %s: %s"
                    % (repo.name, e)
                )

    # Cap the in-DB log to the last ~64 KB so the column does not grow
    # unbounded. Postgres TOAST writes the whole column on every UPDATE,
    # and `_append_log` is called dozens of times per deploy.
    _PROVISIONING_LOG_MAX = 64 * 1024

    def _append_log(self, message):
        """Append a timestamped message to provisioning_log (size-bounded)."""
        timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        line = '[%s] %s\n' % (timestamp, message)
        current = self.provisioning_log or ''
        new_log = current + line
        if len(new_log) > self._PROVISIONING_LOG_MAX:
            # Drop the oldest data, but keep complete lines.
            new_log = new_log[-self._PROVISIONING_LOG_MAX:]
            nl = new_log.find('\n')
            if nl >= 0 and nl < len(new_log) - 1:
                new_log = '... [truncated] ...\n' + new_log[nl + 1:]
        self.provisioning_log = new_log

    def _render_template(self, template_name, context):
        """Render a Jinja2 template from the templates/ directory."""
        template = _JINJA_ENV.get_template(template_name)
        return template.render(context)

    # Keys a tenant must NEVER set via Extra Configuration:
    #  - logfile / log_db / pidfile / data_dir: would write unbounded files to
    #    disk. logfile is the key one — without it Odoo logs to stdout, which
    #    Docker caps (json-file max-size 10m x3 = 30MB), so a tenant can't fill
    #    the host disk with logs. log_db would bloat the database instead.
    #  - db_* / admin_passwd / list_db: would let a tenant repoint its database
    #    or change the master password.
    _FORBIDDEN_EXTRA_CONFIG = {
        'logfile', 'log_db', 'log_db_level', 'pidfile', 'data_dir',
        'db_host', 'db_port', 'db_user', 'db_password', 'db_name',
        'db_sslmode', 'db_template', 'admin_passwd', 'list_db', 'dbfilter',
    }

    def _parse_extra_config(self):
        """Parse the extra_config text field into a dict, dropping any key in
        ``_FORBIDDEN_EXTRA_CONFIG`` so a tenant can never override logging,
        data paths or DB/credentials from odoo.conf (see the constraint for the
        user-facing rejection — this strip is the belt-and-braces guarantee)."""
        self.ensure_one()
        result = {}
        if self.extra_config:
            for line in self.extra_config.strip().splitlines():
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    key, _, value = line.partition('=')
                    key = key.strip()
                    if key.lower() in self._FORBIDDEN_EXTRA_CONFIG:
                        continue
                    result[key] = value.strip()
        return result or None

    @api.constrains('extra_config')
    def _check_extra_config(self):
        """Reject blocked keys at save time with a clear message — so a tenant
        knows WHY their override didn't apply (rather than it being silently
        dropped). 'logfile' is the important one: it keeps logs on stdout
        (Docker-capped at 30MB) instead of growing a file that fills the disk."""
        for rec in self:
            if not rec.extra_config:
                continue
            bad = []
            for line in rec.extra_config.splitlines():
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    key = line.partition('=')[0].strip().lower()
                    if key in self._FORBIDDEN_EXTRA_CONFIG:
                        bad.append(key)
            if bad:
                raise ValidationError(_(
                    "These options can't be set in Extra Configuration: %s.\n\n"
                    "In particular 'logfile' is blocked on purpose — your logs "
                    "always stream to the platform (and are size-capped) so they "
                    "can never fill your instance's disk. View them under Logs."
                ) % ', '.join(sorted(set(bad))))

    # Advisory-lock namespace for server allocation (distinct from the port
    # allocator's 0x5AA5_0001) — serializes concurrent allocations per region.
    _ALLOC_LOCK_NAMESPACE = 0x5AA5_0002

    def _allocate_servers(self):
        """Auto-allocate a compute cluster using a multi-level strategy.

        Respects ``provisioning_mode``:
        - **manual**: skip allocation entirely (operator assigns servers).
        - **strict**: Level 1 only — fail hard if no capacity.
        - **flexible** (default): Level 1 → Level 2 → Level 3.

        Levels (flexible mode):
            1. Ideal — least-loaded cluster with available capacity.
            2. Overcommit — any cluster with ``allow_overcommit`` enabled,
               ignoring capacity limits.
            3. Pending — no server assigned; instance enters
               ``pending_provision`` state and waits for capacity.

        A tenant's database lives inside the cluster (CloudNativePG/managed
        StatefulSet), not on a separate ``saas.server`` — there is no
        separate DB-server allocation step.

        Returns True if a server was assigned, False if the instance was
        marked as pending (caller should abort deployment).
        """
        self.ensure_one()
        Server = self.env['saas.server']
        mode = self.provisioning_mode or 'flexible'

        # -- Manual mode: operator is responsible for server assignment --
        if mode == 'manual':
            return bool(self.docker_server_id)

        if self.docker_server_id:
            return True

        plan = self.plan_id
        # Region the instance must stay within (co-location). Legacy
        # instances have no region -> no constraint (today's behaviour).
        region = self.region_id

        # Serialize allocation per region: without this, two concurrent
        # deploys both read the same least-loaded host (capacity is only
        # committed once an instance flips to provisioning/running) and both
        # pick it -> overcommit past max_instances/cpu/ram. The xact lock is
        # held until COMMIT, by which point THIS instance is assigned and
        # marked provisioning, so the next allocator counts it. (max_*=0
        # still means "unlimited" by design — the lock only makes a
        # CONFIGURED limit race-safe.)
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (self._ALLOC_LOCK_NAMESPACE, region.id if region else 0),
        )
        # Drop any cached capacity computed before the lock — a concurrent
        # allocator may have just committed an instance onto a candidate.
        Server.invalidate_model(
            ['instance_count', 'allocated_cpu', 'allocated_ram_gb'])

        # Level 1 — Ideal allocation (respect capacity)
        if mode == 'strict':
            self.docker_server_id = Server._allocate_docker_server(
                plan=plan, raise_on_failure=True, region=region,
            )
            self._append_log(
                "Allocated compute cluster (strict): %s"
                % self.docker_server_id.name
            )
            return True

        server = Server._allocate_docker_server(plan=plan, region=region)
        if server:
            self.docker_server_id = server
            self._append_log(
                "Allocated compute cluster (ideal): %s" % server.name
            )
            return True

        # Level 2 — Overcommit fallback
        server = Server._allocate_overcommit_server(plan=plan, region=region)
        if server:
            self.docker_server_id = server
            self.is_overcommitted = True
            self._append_log(
                "Allocated compute cluster (overcommit): %s" % server.name
            )
            _logger.warning(
                "Instance %s allocated to overcommitted cluster %s (plan: %s).",
                self.subdomain, server.name, plan.name if plan else 'none',
            )
            return True

        # Level 3 — No server available → pending
        self._mark_as_pending()
        return False

    def _mark_as_pending(self):
        """Set instance to pending_provision — deployment deferred until
        server capacity becomes available.

        Initialises ``pending_provision_since`` on first entry so the
        retry cron can drive exponential back-off and give up cleanly
        after the max wait window.
        """
        self.ensure_one()
        self.state = 'pending_provision'
        if not self.pending_provision_since:
            self.pending_provision_since = fields.Datetime.now()
        self._append_log(
            "No server available — instance marked as pending provision. "
            "Deployment will be retried automatically when capacity is freed."
        )
        _logger.info(
            "Instance %s moved to pending_provision (no available server).",
            self.subdomain,
        )

    def _validate_deploy_fields(self):
        """Validate all required fields before deployment.

        Kubernetes is the only compute backend now: it authenticates via
        the region's kubeconfig (``KubernetesDriver``), not SSH, and its
        database lives inside the cluster (CloudNativePG), not on a
        separate ``db_server_id`` — the ssh_docker-only checks that used
        to follow (SSH key pair, IP address, DB server) were removed
        along with that backend.
        """
        self.ensure_one()
        errors = []
        if not self.subdomain:
            errors.append(_("Subdomain is required."))
        if not self.docker_server_id:
            errors.append(_("Docker Server is required."))
        if not self.odoo_version_id:
            errors.append(_("Odoo Version is required."))
        if not self.partner_id:
            errors.append(_("Customer is required."))
        if not self.odoo_version_id or not self.odoo_version_id.docker_image:
            errors.append(_("Docker image is not set on the selected Odoo version."))
        if not self.odoo_version_id or not self.odoo_version_id.docker_image_tag:
            errors.append(_("Docker image tag is not set on the selected Odoo version."))
        if errors:
            raise ValidationError('\n'.join(str(e) for e in errors))

    # ========== Resource Usage ==========

    @staticmethod
    def _format_bytes(size_bytes):
        """Format bytes into a human-readable string."""
        if size_bytes < 1024:
            return '%d B' % size_bytes
        elif size_bytes < 1024 ** 2:
            return '%.1f KB' % (size_bytes / 1024.0)
        elif size_bytes < 1024 ** 3:
            return '%.1f MB' % (size_bytes / 1024.0 ** 2)
        else:
            return '%.2f GB' % (size_bytes / 1024.0 ** 3)

    # Pending-provision retry tuning.
    _PENDING_MAX_WAIT_HOURS = 24
    _PENDING_BACKOFF_BASE_MIN = 5

    # Max instances a single cron run will pull into memory / iterate (PERF-003).
    # Bounds memory and runtime per run; the remainder is processed on the next
    # run (searches are oldest-first so nothing starves).
    _CRON_BATCH_SIZE = 500

    @api.model
    def _cron_retry_pending_provision(self):
        """Cron: attempt to deploy instances stuck in pending_provision.

        After the ARCH-004 retry cutover this cron handles ONE concern:
        CAPACITY-WAITING. ``pending_provision`` is now set only by
        ``_mark_as_pending`` (no server available) — deploy *failures* are
        retried by the durable queue (max_attempts), not bounced back here.
        PROV-004's ``pending_retry_now`` still fast-tracks these when capacity
        or host health changes.

        Capacity-blocked instances can stay pending indefinitely if
        the operator never adds servers — without back-off this cron
        would call ``_allocate_servers`` every 5 minutes for days,
        filling logs and burning resources on a futile loop.

        Strategy:
        - Exponential back-off: skip the instance until at least
          ``BASE * 2^attempts`` minutes have passed since the last
          attempt. Caps naturally as ``attempts`` grows.
        - Hard escalation: if cumulative pending time exceeds
          ``_PENDING_MAX_WAIT_HOURS`` (default 24h), mark the
          instance as ``failed`` so the operator gets paged and the
          customer sees a clear error instead of silent waiting.
        """
        now = fields.Datetime.now()
        max_wait = datetime.timedelta(hours=self._PENDING_MAX_WAIT_HOURS)
        # Oldest-pending first, bounded per run (PERF-003) so a backlog can't
        # load the whole table into one cron run.
        pending = self.search(
            [('state', '=', 'pending_provision')],
            order='pending_provision_since asc, id asc',
            limit=self._CRON_BATCH_SIZE,
        )
        if not pending:
            return
        if len(pending) == self._CRON_BATCH_SIZE:
            _logger.info(
                "retry-pending cron hit the %d batch cap; remaining instances "
                "are handled next run.", self._CRON_BATCH_SIZE)
        retried = 0
        escalated = 0
        for instance in pending:
            since = instance.pending_provision_since
            attempts = instance.pending_provision_attempts or 0
            # Capacity/health just changed -> retry immediately, skipping the
            # back-off window (PROV-004). The flag is consumed in the retry
            # write below (so a rollback re-arms it for the next tick).
            force = instance.pending_retry_now
            # Hard cap: give up + flag for operator attention.
            if since and now - since > max_wait:
                instance.write({
                    'state': 'failed',
                    'pre_provisioning_state': 'pending_provision',
                })
                instance._append_log(
                    "Provisioning gave up after %d hours of waiting for "
                    "server capacity. Please contact support so we can "
                    "expand capacity and resume your deployment."
                    % self._PENDING_MAX_WAIT_HOURS
                )
                _logger.error(
                    "Instance %s escalated to 'failed' after %dh "
                    "pending_provision wait — operator must add capacity.",
                    instance.subdomain, self._PENDING_MAX_WAIT_HOURS,
                )
                self.env.cr.commit()
                escalated += 1
                continue
            # Exponential back-off: only retry if the back-off window
            # since the last attempt has elapsed.
            backoff_min = min(
                self._PENDING_BACKOFF_BASE_MIN * (2 ** attempts),
                # Hard cap each gap at 2h so we don't go silent for
                # days inside the 24h overall budget.
                120,
            )
            last_attempt = instance.write_date or since
            if not force and last_attempt and (
                now - last_attempt < datetime.timedelta(minutes=backoff_min)
            ):
                continue
            try:
                instance.write({
                    'pending_provision_attempts': attempts + 1,
                    'pending_retry_now': False,
                })
                instance.action_deploy()
                self.env.cr.commit()
                retried += 1
            except Exception:
                self.env.cr.rollback()
                _logger.exception(
                    "Cron: retry failed for pending instance %s",
                    instance.subdomain,
                )
        if retried or escalated:
            _logger.info(
                "Pending-provision cron: %d retried, %d escalated to "
                "failed (of %d pending).",
                retried, escalated, len(pending),
            )

    @api.model
    def _saas_flag_pending_for_retry(self):
        """Mark every pending-provision instance for an immediate retry on the
        next cron tick, bypassing the back-off (PROV-004). Called when server
        capacity/health changes so a queued deploy doesn't wait hours for its
        next back-off slot once capacity exists. Returns the count flagged."""
        pending = self.sudo().search([
            ('state', '=', 'pending_provision'),
            ('pending_retry_now', '=', False),
        ])
        if pending:
            pending.write({'pending_retry_now': True})
            _logger.info(
                "Flagged %d pending-provision instance(s) for immediate retry "
                "after a capacity/health change.", len(pending))
        return len(pending)

    # Operations that can be safely re-run if interrupted. Anything else
    # (delete, cancel, suspend) must go to a manual-review state because
    # the partial side-effects on the remote server make "resume" unsafe.
    _RECOVERABLE_OPERATIONS = ('deploy', 'redeploy', 'restart', 'start', 'restore')

    # Per-instance advisory-lock namespace for long background operations
    # (deploy). A live op holds pg_advisory_lock(NS, id) on its own connection
    # for its whole duration; the stuck-recovery cron probes it to avoid
    # re-queueing an operation that is genuinely still running.
    _OP_LOCK_NAMESPACE = 0x5AA5_0003

    def _operation_is_alive(self):
        """True if a live background operation currently holds this instance's
        operation advisory lock. We pg_try_advisory_lock: if we acquire it, no
        live op holds it (release + report dead); if we can't, an op is alive."""
        self.ensure_one()
        self.env.cr.execute(
            "SELECT pg_try_advisory_lock(%s, %s)",
            (self._OP_LOCK_NAMESPACE, self.id))
        acquired = self.env.cr.fetchone()[0]
        if acquired:
            self.env.cr.execute(
                "SELECT pg_advisory_unlock(%s, %s)",
                (self._OP_LOCK_NAMESPACE, self.id))
            return False
        return True

    def _cron_recover_stuck_provisioning(self):
        """Cron: recover instances stuck in ``provisioning`` after a restart.

        Idempotent operations (deploy/redeploy/restart/start/restore) are
        re-queued. Destructive operations (cancel/delete/suspend) are NOT
        auto-reverted — doing so would mark a half-deleted instance as
        "running" again. They are routed to ``failed`` for manual review.

        Thresholds: non-restore ops use a tight 15-min cutoff so a stuck
        instance self-heals quickly. For ``restore`` we HEALTH-GATE recovery:
        a real restore is brief and keeps the container DOWN during its
        destructive phase, so once a restore has been stuck past a short
        window (8 min) AND the container is back UP and healthy, the restore
        has finished or its thread died — safe to recover in minutes instead
        of making the customer stare at "Provisioning". If the container is
        still DOWN we hold off until a 90-min hard backstop, because a genuinely
        slow restore (large snapshot) legitimately has the container down and
        recovering early could clobber it.
        """
        now = fields.Datetime.now()
        normal_cutoff = now - datetime.timedelta(minutes=15)
        restore_health_cutoff = now - datetime.timedelta(minutes=8)
        restore_hard_cutoff = now - datetime.timedelta(minutes=90)
        stuck = self.search([
            ('state', '=', 'provisioning'),
            '|',
            '&', ('pending_operation', '=', 'restore'),
                 ('write_date', '<', restore_health_cutoff),
            '&', ('pending_operation', '!=', 'restore'),
                 ('write_date', '<', normal_cutoff),
        ], order='write_date asc, id asc', limit=self._CRON_BATCH_SIZE)
        if not stuck:
            return
        if len(stuck) == self._CRON_BATCH_SIZE:
            _logger.info(
                "recover-stuck cron hit the %d batch cap; remaining instances "
                "are handled next run.", self._CRON_BATCH_SIZE)
        Job = self.env['saas.job'].sudo()
        for instance in stuck:
            try:
                # ARCH-004 retry cutover: the durable queue owns crash recovery
                # for queued operations (deploy via the reaper, db-ops, etc.).
                # If a non-terminal job targets this instance, leave it to the
                # queue — recovering here would race the reaper / double-run.
                # This cron now only rescues LEGACY run_in_background ops that
                # have no job row (e.g. restart/redeploy/start).
                if Job.search_count([
                        ('model', '=', 'saas.instance'),
                        ('res_id', '=', instance.id),
                        ('state', 'in', ('pending', 'running'))]):
                    continue
                # Liveness gate: a long deploy is ONE transaction, so its
                # write_date stays frozen and it looks "stuck" — but it holds
                # the operation advisory lock. If a live op holds it, leave it
                # alone (re-queueing here is the double-deploy bug).
                if instance._operation_is_alive():
                    _logger.info(
                        "[recover] %s still holds its operation lock — alive, "
                        "skipping", instance.subdomain)
                    continue
                op = instance.pending_operation
                # Restore health gate: between the 8-min and 90-min marks, only
                # recover if the container is actually UP (restore done/dead).
                # A live restore keeps it down, so a down container = still
                # running → wait for the hard backstop.
                if op == 'restore' and (instance.write_date or now) >= restore_hard_cutoff:
                    try:
                        healthy = instance._compute_driver().health(
                            instance._compute_handle()).running
                    except Exception:
                        healthy = False
                    if not healthy:
                        continue
                prev_state = instance.pre_provisioning_state or 'failed'
                if op and op not in self._RECOVERABLE_OPERATIONS:
                    instance._append_log(
                        "Recovery aborted: operation '%s' was in progress "
                        "and cannot be safely auto-reverted. Marked as failed "
                        "for manual review." % op
                    )
                    instance.write({
                        'state': 'failed',
                        'pre_provisioning_state': False,
                        'pending_operation': False,
                        'last_error': 'Server restarted during %s — manual review required' % op,
                        'last_error_date': fields.Datetime.now(),
                    })
                else:
                    instance._append_log(
                        "Recovered from stuck provisioning state "
                        "(likely caused by a server restart)."
                    )
                    instance._on_background_error(
                        Exception("Server restarted during provisioning"),
                        prev_state,
                    )
                    instance.pending_operation = False
                self.env.cr.commit()
            except Exception:
                self.env.cr.rollback()
                _logger.exception(
                    "Cron: recovery failed for stuck instance %s",
                    instance.subdomain,
                )

    def _cron_verify_webhooks(self):
        """Cron: verify and re-register webhooks for all running instances.

        Runs every 6 hours.  For each running instance with repos that
        have ``webhook_enabled=True``, verifies the webhook is still
        active on the Git provider and re-registers if needed.
        """
        instances = self.search([
            ('state', '=', 'running'),
        ])
        if not instances:
            return
        for instance in instances:
            repos_needing_webhook = instance.repo_ids.filtered(
                lambda r: r.state == 'cloned'
                and r.webhook_enabled
                and r.sudo().github_token
                and not r.webhook_provider_id
            )
            if not repos_needing_webhook:
                continue
            try:
                _logger.info(
                    "Cron: re-registering %d webhook(s) for %s",
                    len(repos_needing_webhook), instance.subdomain,
                )
                instance._ensure_webhooks_registered()
                self.env.cr.commit()
            except Exception:
                self.env.cr.rollback()
                _logger.exception(
                    "Cron: webhook verification failed for %s",
                    instance.subdomain,
                )

    @staticmethod
    def _parse_ram_string(ram_str):
        """Parse a RAM string like '512m', '1g', '2G' into bytes."""
        if not ram_str:
            return 0
        ram_str = ram_str.strip().lower()
        multipliers = {'k': 1024, 'm': 1024**2, 'g': 1024**3, 't': 1024**4}
        for suffix, mult in multipliers.items():
            if ram_str.endswith(suffix):
                try:
                    return float(ram_str[:-1]) * mult
                except (ValueError, TypeError):
                    return 0
        try:
            return float(ram_str)
        except (ValueError, TypeError):
            return 0

    def _snapshot_total_bytes(self):
        """Current total snapshot footprint for this instance, in bytes.

        Snapshots are restic (deduplicated), and every full-instance
        backup record stores the whole repo's size at that run — so the
        most recent completed record IS the current total footprint.
        Summing records would over-count by the number of runs. We take
        the latest, with a small fallback to the max of recent records in
        case the latest run couldn't capture ``restic stats``.
        """
        self.ensure_one()
        recent = self.env['saas.instance.backup'].sudo().search([
            ('instance_id', '=', self.id),
            ('is_full_instance', '=', True),
            ('state', '=', 'done'),
        ], order='create_date desc', limit=5)
        if not recent:
            return 0
        size_mb = recent[0].size_mb or 0.0
        if size_mb <= 0:
            size_mb = max((b.size_mb or 0.0) for b in recent)
        return int(size_mb * 1024 * 1024)

    # ------------------------------------------------------------------
    # Usage metrics: CPU/RAM from the region's Prometheus, storage
    # measured inside the web pod (see KubernetesDriver.usage_by_tenant /
    # measure_storage / usage_history)
    # ------------------------------------------------------------------
    def _usage_targets(self):
        """The records usage can be measured for: running, on Kubernetes."""
        return self.filtered(
            lambda r: r.state == 'running'
            and r.docker_server_id.compute_driver == 'kubernetes')

    def _plan_limits(self):
        """(cpu cores, RAM bytes, storage bytes) the plan allows per pod /
        in total; 0 = no limit set."""
        self.ensure_one()
        plan = self.plan_id
        return (
            (plan.cpu_limit or 0.0) if plan else 0.0,
            self._parse_ram_string(plan.ram_limit) if plan else 0,
            (self.effective_storage_limit_gb or 0.0) * 1024 ** 3,
        )

    @staticmethod
    def _pct(value, limit):
        return round(value / limit * 100.0, 1) if limit else 0.0

    def _cpu_ram_vals(self, cpu_cores, mem_bytes):
        cpu_limit, ram_limit, _storage = self._plan_limits()
        cpu_pct = self._pct(cpu_cores, cpu_limit)
        ram_pct = self._pct(mem_bytes, ram_limit)
        ram = self._format_bytes(mem_bytes)
        return {
            'cpu_usage': '%.1f%%' % cpu_pct,
            'cpu_usage_pct': cpu_pct,
            'ram_usage': ('%s / %s' % (ram, self._format_bytes(ram_limit))
                          if ram_limit else ram),
            'ram_percent': '%.1f%%' % ram_pct,
            'ram_usage_pct': ram_pct,
        }

    def _storage_vals(self, filestore_bytes, db_bytes):
        total = filestore_bytes + db_bytes
        return {
            'disk_usage': self._format_bytes(filestore_bytes),
            'db_size': self._format_bytes(db_bytes),
            'total_storage': self._format_bytes(total),
            'total_storage_bytes': float(total),
            'storage_usage_pct': self._pct(total, self._plan_limits()[2]),
        }

    def _refresh_usage(self, strict=False):
        """Measure and store usage for the running Kubernetes records in
        ``self``: CPU/RAM for every tenant of a server in one Prometheus
        round-trip (best effort — a region may have no Prometheus), storage
        per tenant inside its web pod. ``strict`` re-raises a failed storage
        measurement instead of logging it; each record is committed by the
        caller."""
        from ..drivers.kubernetes_driver import PrometheusUnavailable
        by_server = {}
        for inst in self._usage_targets():
            by_server.setdefault(inst.docker_server_id, self.browse())
            by_server[inst.docker_server_id] |= inst
        for insts in by_server.values():
            driver = insts[0]._compute_driver()
            try:
                usage = driver.usage_by_tenant()
            except PrometheusUnavailable as e:
                _logger.info("usage: CPU/RAM unavailable for %s: %s",
                             insts[0].docker_server_id.name, e)
                usage = None
            failures = 0
            for inst in insts:
                handle = inst._compute_handle()
                vals = {'usage_last_updated': fields.Datetime.now()}
                if usage is not None:
                    u = usage.get(driver._cr_name(handle), {})
                    vals.update(inst._cpu_ram_vals(
                        u.get('cpu_cores', 0.0), u.get('mem_bytes', 0.0)))
                if failures < _USAGE_MAX_STORAGE_FAILURES:
                    try:
                        vals.update(inst._storage_vals(**driver.measure_storage(handle)))
                        failures = 0
                    except Exception as e:
                        if strict:
                            raise
                        failures += 1
                        _logger.warning("usage: storage measurement failed for %s: %s",
                                        inst.subdomain, e)
                        if failures == _USAGE_MAX_STORAGE_FAILURES:
                            # Likely the cluster itself is unreachable: don't
                            # pay a timeout per remaining tenant this run.
                            _logger.warning(
                                "usage: skipping storage for the rest of %s this run",
                                inst.docker_server_id.name)
                inst.write(vals)

    def action_refresh_usage(self):
        """Fetch CPU, RAM, filestore and database size for these instances."""
        self._refresh_usage()
        return True

    def _safe_refresh_usage(self):
        """Refresh resource usage, silently ignoring errors."""
        try:
            self._refresh_usage()
        except Exception:
            _logger.warning("usage refresh failed for %s", self.mapped('subdomain'),
                            exc_info=True)

    def _strict_refresh_usage(self):
        """Refresh usage, raising if storage cannot be measured.

        Use this from places (downgrade gate, billing) where acting on
        stale or zero data could let a customer move to a plan that
        cannot accommodate them. CPU/RAM stay best effort: nothing gates
        on them."""
        self.ensure_one()
        if not self._usage_targets():
            raise UserError(_(
                "Usage can only be measured while the instance is running."))
        self._refresh_usage(strict=True)

    @api.model
    def _cron_refresh_usage(self):
        """Cron: refresh stored usage for running instances, least recently
        measured first (storage is one pod exec per tenant, so a run is
        bounded; the rest follow on the next run)."""
        instances = self.search([
            ('state', '=', 'running'),
            ('docker_server_id.compute_driver', '=', 'kubernetes'),
        ], order='usage_last_updated asc nulls first, id',
            limit=_USAGE_REFRESH_BATCH)
        instances._refresh_usage()
        return len(instances)

    def _get_live_metrics(self):
        """Current CPU/RAM (% of plan) for the dashboard's live poll: one
        instant Prometheus query for this tenant, cached a few seconds per
        instance so any number of viewers costs one query. Nothing is
        written. ``{'cpu', 'ram', 'at', 'available'}``."""
        self.ensure_one()
        mono = time.monotonic()
        with _LIVE_METRICS_GUARD:
            hit = _LIVE_METRICS_CACHE.get(self.id)
            if hit and mono - hit[0] < LIVE_METRICS_CACHE_TTL:
                return hit[1]
        payload = {'cpu': 0.0, 'ram': 0.0, 'at': '', 'available': False}
        if self._usage_targets():
            try:
                driver = self._compute_driver()
                handle = self._compute_handle()
                u = driver.usage_by_tenant(handle).get(driver._cr_name(handle), {})
                vals = self._cpu_ram_vals(
                    u.get('cpu_cores', 0.0), u.get('mem_bytes', 0.0))
                payload = {
                    'cpu': vals['cpu_usage_pct'], 'ram': vals['ram_usage_pct'],
                    'at': fields.Datetime.to_string(fields.Datetime.now()),
                    'available': True,
                }
            except Exception as e:
                _logger.info("live metrics unavailable for %s: %s", self.subdomain, e)
        with _LIVE_METRICS_GUARD:
            _LIVE_METRICS_CACHE[self.id] = (mono, payload)
        return payload

    def _get_metric_series(self, hours=24, max_points=240):
        """CPU/RAM (% of plan) and storage history for THIS instance over
        the last ``hours`` hours, from Prometheus, at ≤ ``max_points``
        points. Storage comes from kubelet PVC stats when the cluster's
        storage driver reports them, otherwise it's the last measured value
        (flat). Shape: {'samples': [{t, cpu, ram, storage_mb,
        storage_pct}], ...}. Tenant isolation is the caller's job (only call
        for an owned instance)."""
        self.ensure_one()
        hours = max(1, min(int(hours or 24), METRIC_RETENTION_DAYS * 24))
        step = max(15, int(hours * 3600 / max(1, max_points)))
        cpu_limit, ram_limit, storage_limit = self._plan_limits()
        samples = []
        available = False
        if self.docker_server_id.compute_driver == 'kubernetes':
            end = time.time()
            try:
                series = self._compute_driver().usage_history(
                    self._compute_handle(), end - hours * 3600, end, step)
                available = True
            except Exception as e:
                _logger.info("metrics history unavailable for %s: %s",
                             self.subdomain, e)
                series = {}
            cpu = dict(series.get('cpu_cores') or [])
            ram = dict(series.get('mem_bytes') or [])
            vol = dict(series.get('volume_bytes') or [])
            flat_storage = self.total_storage_bytes or 0.0
            for ts in sorted(set(cpu) | set(ram)):
                stored = vol.get(ts, flat_storage)
                samples.append({
                    't': datetime.datetime.fromtimestamp(
                        ts, datetime.timezone.utc).replace(tzinfo=None)
                        .isoformat() + 'Z',
                    'cpu': self._pct(cpu.get(ts, 0.0), cpu_limit),
                    'ram': self._pct(ram.get(ts, 0.0), ram_limit),
                    'storage_mb': round(stored / 1024 ** 2, 2),
                    'storage_pct': self._pct(stored, storage_limit),
                })
        plan = self.plan_id
        return {
            'instance': self.subdomain,
            'hours': hours,
            'bucket_seconds': step,
            'retention_days': METRIC_RETENTION_DAYS,
            'available': available,
            'plan': {
                'cpu_limit': plan.cpu_limit if plan else 0,
                'ram_limit': plan.ram_limit if plan else '',
                'storage_limit_gb': plan.storage_limit if plan else 0,
            },
            'samples': samples,
        }

    # ========== Deploy Flow ==========

    def _do_deploy_after_payment(self):
        """Background deploy triggered by payment — instance already in 'paid' state."""
        self.ensure_one()
        self.action_deploy()

    def action_deploy(self):
        """Full deployment flow: provision Docker container over SSH (async).

        Allowed from ``paid``, ``failed``, and ``pending_provision`` states.
        Also allowed from ``draft`` when no plan is set (internal / test
        instances).

        In **flexible** provisioning mode the instance may transition to
        ``pending_provision`` instead of deploying immediately when no
        server capacity is available.
        """
        for rec in self:
            if rec.state == 'draft' and rec.plan_id and not rec.is_trial:
                raise UserError(
                    _("Instance '%s' has a plan assigned. "
                      "Please use 'Confirm & Bill' to create a sale order and "
                      "invoice before deploying.") % rec.subdomain
                )
            if rec.state not in ('draft', 'paid', 'failed', 'pending_provision'):
                raise UserError(
                    _("Cannot deploy instance '%s': must be in Draft "
                      "(trial/no plan), Paid, Failed, or Pending Provision "
                      "state (current: %s).")
                    % (rec.subdomain, rec.state)
                )

            servers_ready = rec._allocate_servers()
            if not servers_ready:
                # Instance moved to pending_provision — skip deployment
                continue

            rec._validate_deploy_fields()

            if not rec.db_user:
                rec.db_user = rec._generate_db_user()
            if not rec.db_password:
                rec.db_password = rec._generate_random_password()
            if not rec.admin_password:
                rec.admin_password = rec._generate_random_password()

            if not rec.deploy_retry_count:
                rec.provisioning_log = ''
            rec.pre_provisioning_state = 'failed'
            rec.pending_operation = 'deploy'
            rec.state = 'provisioning'
            rec._append_log(
                "Deployment queued (attempt %d). Running in background..."
                % (rec.deploy_retry_count + 1)
            )
            self.env['saas.audit.log'].saas_audit(
                'instance_deploy', model='saas.instance', res_id=rec.id,
                res_name=rec.subdomain,
                detail='Deployment queued (attempt %d)' % (rec.deploy_retry_count + 1))
            # Durable queue OWNS deploy retries + crash recovery (ARCH-004 retry
            # cutover): the queue retries _do_deploy up to max_deploy_retries
            # with back-off (instance stays 'provisioning' across attempts), and
            # because the deploy is idempotent (re-runnable — mkdir -p, template
            # clone is guarded, etc.) the reaper requeues it if a worker dies.
            # _on_background_error fires only on TERMINAL failure → 'failed'.
            # No secret args. (Capacity-waiting still flows through
            # pending_provision + _cron_retry_pending_provision; per-host death
            # is moot — _allocate_servers keeps the same host once assigned.)
            self.env['saas.job'].enqueue(
                rec, '_do_deploy', channel='deploy',
                lock_key='instance:%s' % rec.id,
                max_attempts=max(1, rec.max_deploy_retries + 1), idempotent=True,
                on_error='_on_background_error', on_error_args=('failed',))

    def _record_build(self, source, state='success', commit_message=False,
                      log=False):
        """Record a build/deploy event for the Odoo.sh-style History timeline.
        Hosting only; never raises into the deploy flow."""
        self.ensure_one()
        if not self.is_hosting:
            return
        # Snapshot the tail of the deploy log so a build's detail view shows
        # what actually happened, not just a status.
        if not log and state != 'running':
            log = (self.provisioning_log or '')[-8000:]
        try:
            self.env['saas.build'].sudo().create({
                'instance_id': self.id,
                'repo_id': self.repo_ids[:1].id or False,
                'branch': self._env_branch(),
                'source': source,
                'state': state,
                'stage': 'done' if state != 'running' else 'queued',
                'commit_message': commit_message or False,
                'date_done': fields.Datetime.now() if state != 'running' else False,
                'log': (log or '')[:8000] or False,
            })
        except Exception:
            _logger.exception("Failed to record build for %s", self.subdomain)

    def _do_deploy(self):
        """Deploy, holding a per-instance advisory lock for the whole run.

        The deploy is a single long transaction, so its ``write_date`` is frozen
        and the stuck-recovery cron would otherwise see it as "stuck" and
        re-queue a SECOND deploy onto the same host/ports/DB (the double-deploy
        race). Holding the operation lock lets the cron detect a live deploy via
        ``_operation_is_alive``. Released in ``finally`` (and by Postgres if the
        process dies, since it's a session lock on this thread's connection)."""
        self.ensure_one()
        self.env.cr.execute(
            "SELECT pg_advisory_lock(%s, %s)",
            (self._OP_LOCK_NAMESPACE, self.id))
        try:
            return self._do_deploy_locked()
        finally:
            self.env.cr.execute(
                "SELECT pg_advisory_unlock(%s, %s)",
                (self._OP_LOCK_NAMESPACE, self.id))

    def _do_deploy_locked(self):
        """Internal deploy logic for a single record.

        Kubernetes is the only compute backend now — this just delegates
        to ``_do_deploy_locked_kubernetes``. Kept as a separate method
        (rather than merging the two) since ``_do_deploy`` and callers
        reference ``_do_deploy_locked`` by name, and a future second
        backend would branch here again.
        """
        self.ensure_one()
        return self._do_deploy_locked_kubernetes()

    def _do_deploy_locked_kubernetes(self):
        """Provisioning a BRAND-NEW instance directly on Kubernetes.

        No SSH/mkdir/chown, no ``_provision_postgresql``, no manual
        ``-i base`` init — ``compute/operator``'s ``reconcileInitJob``
        already runs that automatically and gates the web Deployment on
        it succeeding (``internal/controller/odooinstance_controller.go``,
        ``internal/resources/init_job.go``), so a bare ``create()`` with no
        ``restore`` key is a complete, self-initializing fresh tenant.

        TLS/ingress is exclusively Kubernetes-native (Ingress +
        cert-manager, ``region.native_ingress_tls``) — there is no SSH
        fallback to an external Nginx host any more (that path was
        ssh_docker-only and has been removed along with the rest of that
        backend). A region without ``native_ingress_tls`` simply can't
        deploy; fix the region's setup, don't add a workaround here.
        """
        self.ensure_one()
        server = self.docker_server_id
        region = server.region_id
        if not (region and region.native_ingress_tls):
            raise UserError(_(
                "Cannot deploy '%s' on Kubernetes: server '%s's region "
                "does not have native_ingress_tls configured (Region > "
                "Kubeconfig tab). Kubernetes deploys are TLS-terminated "
                "natively by the cluster's own Ingress + cert-manager — "
                "there is no other supported ingress path."
            ) % (self.subdomain, server.name))

        self._append_log("Creating Kubernetes instance...")
        from ..drivers.base import ComputeSpec
        replicas = self.compute_tier_id.replicas or 1
        env = {
            'domain': self.name,
            'odoo_version': self.odoo_version_id.name,
            'replicas': replicas,
            'tls_enabled': True,
            'tls_issuer_name': region.tls_cluster_issuer,
            'tls_issuer_kind': 'ClusterIssuer',
            **self._k8s_plan_resources(),
        }
        spec = ComputeSpec(
            container_name=self._get_container_name(),
            image=self.odoo_version_id._get_docker_image(),
            # KubernetesDriver.create() ignores spec.instance_path and
            # derives its own namespace from container_name — there is no
            # host filesystem path to report (ssh_docker's docker_base_path
            # is gone).
            instance_path='',
            http_port=0,
            longpolling_port=0,
            db_name=self.subdomain,
            db_host='',
            env=env,
        )
        driver = self._compute_driver()
        handle = driver.create(spec)
        self._append_log("Waiting for the instance to become healthy...")
        self._data_service()._wait_until_healthy(driver, handle, timeout=600)

        self._append_log(
            "Kubernetes-native Ingress + cert-manager handle TLS "
            "directly for this region — no external Nginx step needed."
        )

        self.state = 'running'
        self.deploy_retry_count = 0
        self.last_error = False
        self.last_error_date = False
        self.pending_operation = False
        self.pending_provision_since = False
        self.pending_provision_attempts = 0
        self._append_log("Deployment completed successfully. State: running.")
        self._safe_refresh_usage()
        self._record_build('initial', 'success', commit_message='Deployment')
        self._send_notification('saas_core.mail_template_saas_deployed')
        if self.daily_backup_enabled:
            # Covers the checkout-time case: daily_backup_enabled can be
            # set True directly on create() (paid add-on chosen at
            # checkout), before this instance had any compute to sync
            # onto. Every LATER toggle (payment webhook, suspend/resume)
            # already calls _sync_scheduled_backup itself — calling it
            # again here on every deploy is harmless (idempotent CR
            # patch).
            self._sync_scheduled_backup()
        if self.is_trial:
            self._sync_partner_trial()
        if self._build_sources() or self._build_pip_lines():
            # Bake the product's/instance's repos and pip packages into an
            # image and roll it out; the plain version image serves until then.
            try:
                self.action_build_and_deploy('initial')
            except Exception as e:
                _logger.exception("Could not queue the first build for %s", self.subdomain)
                self._append_log("WARNING: could not start the code build: %s" % e)

    # ========== Lifecycle Actions ==========

    def action_stop(self):
        """Stop the compute workload and set state to stopped (async)."""
        for rec in self:
            if rec.state != 'running':
                raise UserError(
                    _("Cannot stop instance '%s': must be in Running state (current: %s).")
                    % (rec.subdomain, rec.state)
                )
            prev_state = rec.state
            rec.pre_provisioning_state = prev_state
            rec.pending_operation = 'stop'
            rec.state = 'provisioning'
            rec._append_log("Stop queued. Running in background...")
            run_in_background(
                rec, '_do_stop',
                error_method='_on_background_error',
                error_args=(prev_state,),
                thread_name='saas_stop_%s' % rec.subdomain,
            )

    def _do_stop(self):
        """Stop container (runs in background thread). Routed via ComputeDriver."""
        self.ensure_one()
        try:
            self._compute_driver().stop(self._compute_handle())
        except Exception as e:
            raise UserError(
                _("Failed to stop container '%s':\n%s")
                % (self._get_container_name(), e)
            )
        self.state = 'stopped'
        self.pending_operation = False
        self._append_log("Instance stopped successfully.")

    def action_restart(self):
        """Restart the compute workload (async)."""
        for rec in self:
            if rec.state not in ('running', 'stopped', 'suspended'):
                raise UserError(
                    _("Cannot restart instance '%s': must be Running, Stopped, or Suspended (current: %s).")
                    % (rec.subdomain, rec.state)
                )
            prev_state = rec.state
            rec.pre_provisioning_state = prev_state
            rec.pending_operation = 'restart'
            rec.state = 'provisioning'
            rec._append_log("Restart queued. Running in background...")
            # Durable queue (ARCH-004): restart is recoverable + idempotent, so a
            # worker that dies mid-restart is re-run by the reaper (completing the
            # op) rather than left for recover-stuck to merely revert.
            self.env['saas.job'].enqueue(
                rec, '_do_restart', channel='deploy',
                lock_key='instance:%s' % rec.id, idempotent=True, max_attempts=2,
                on_error='_on_background_error', on_error_args=(prev_state,))

    def _do_restart(self):
        """Restart container (runs in background thread). Routed via ComputeDriver."""
        self.ensure_one()
        try:
            self._compute_driver().restart(self._compute_handle())
        except Exception as e:
            raise UserError(
                _("Failed to restart container '%s':\n%s")
                % (self._get_container_name(), e)
            )
        self.state = 'running'
        self.pending_operation = False
        self._append_log("Instance restarted successfully.")
        self._safe_refresh_usage()
        # Resume suspended Staging/Development servers when the project (which
        # may have been suspended for non-payment) comes back online.
        if self.environment == 'production':
            suspended_children = self.child_env_ids.filtered(
                lambda c: c.state == 'suspended')
            if suspended_children:
                try:
                    suspended_children.action_restart()
                except Exception:
                    _logger.exception(
                        "Failed to resume child environments for %s",
                        self.subdomain)

    def action_suspend(self):
        """Stop container and set state to suspended (async)."""
        # Cascade: suspending a Production project also suspends its running
        # Staging/Development servers (they share the project's billing fate).
        cascade = self.filtered(
            lambda r: r.environment == 'production'
        ).child_env_ids.filtered(lambda c: c.state == 'running')
        if cascade:
            cascade.action_suspend()
        for rec in self:
            if rec.state != 'running':
                raise UserError(
                    _("Cannot suspend instance '%s': must be in Running state (current: %s).")
                    % (rec.subdomain, rec.state)
                )
            prev_state = rec.state
            rec.pre_provisioning_state = prev_state
            rec.pending_operation = 'suspend'
            rec.state = 'provisioning'
            rec._append_log("Suspend queued. Running in background...")
            run_in_background(
                rec, '_do_suspend',
                error_method='_on_background_error',
                error_args=(prev_state,),
                thread_name='saas_suspend_%s' % rec.subdomain,
            )

    def _do_suspend(self):
        """Suspend container (runs in background thread). Routed via ComputeDriver."""
        self.ensure_one()
        try:
            self._compute_driver().stop(self._compute_handle())
        except Exception as e:
            raise UserError(
                _("Failed to stop container '%s':\n%s")
                % (self._get_container_name(), e)
            )
        self.state = 'suspended'
        self.pending_operation = False
        self._append_log("Instance suspended successfully.")

    def action_cancel(self):
        """Cancel the instance, cleaning up any infrastructure that was created.

        Handles both fully deployed instances and partially provisioned
        ones (e.g. directories created, database provisioned, but
        container never started).

        Retention: the most recent successful full-instance snapshot
        is kept (along with its data in the restic repo) so the
        customer can ``action_reactivate`` later and restore from it
        through the standard /backups portal flow. Everything else
        (other snapshots, on-demand backups, instance files, container,
        databases, PG role, nginx vhost) is deleted. See
        ``_do_delete_instance`` for the step-by-step.
        """
        # Cascade: cancelling a Production project tears down all of its
        # Staging/Development servers too (no orphaned containers).
        cascade = self.filtered(
            lambda r: r.environment == 'production'
        ).child_env_ids.filtered(
            lambda c: c.state not in ('cancelled', 'cancelled_by_client'))
        if cascade:
            cascade.action_cancel()
        for rec in self:
            if rec.docker_server_id:
                # Infrastructure may exist (fully or partially) — clean up
                prev_state = rec.state
                rec.pre_provisioning_state = prev_state
                rec.pending_operation = 'cancel'
                rec.state = 'provisioning'
                rec._append_log("Cancellation queued. Cleaning up infrastructure...")
                run_in_background(
                    rec, '_do_delete_instance',
                    error_method='_on_background_error',
                    error_args=(prev_state,),
                    thread_name='saas_cancel_%s' % rec.subdomain,
                )
            else:
                # No server assigned — nothing to clean up
                rec._void_unpaid_invoices_refund_credit()
                rec.state = 'cancelled'

    def action_draft(self):
        """Reset to draft state."""
        allowed = (
            'failed', 'cancelled', 'pending_payment', 'paid',
            'pending_provision',
        )
        for rec in self:
            if rec.state not in allowed:
                raise UserError(
                    _("Can only reset to draft from Failed, Cancelled, "
                      "Pending Payment, Paid, or Pending Provision state.")
                )
            rec.state = 'draft'

    def _drop_postgresql(self):
        """Drop ALL databases owned by this instance's role + the role.

        Service instances historically had exactly one DB (= subdomain),
        but hosting instances let the customer create N databases all
        owned by ``db_user``. Listing by owner catches both shapes —
        legacy service single-DB and modern hosting multi-DB — so a
        cancelled instance never leaks a database behind.
        """
        self.ensure_one()
        psql_server = self.db_server_id
        if not psql_server:
            return

        db_user = self.db_user
        if db_user and not DB_USER_RE.match(db_user):
            _logger.error(
                "Refusing to drop role with invalid identifier %r", db_user,
            )
            db_user = None
        if not db_user:
            return  # nothing else we can do safely

        # Per-DB name regex used as a defense-in-depth filter when we
        # iterate the list of owned DBs. Allows a leading underscore
        # so the per-instance Odoo template (``__odoo_template_<sub>``)
        # is included — otherwise it'd leak on cancel.
        owned_name_re = re.compile(r'^[_a-z][a-z0-9_-]{0,62}$')
        safe_user = db_user.replace("'", "''")

        with psql_server._get_ssh_connection() as ssh:
            # 1. Enumerate every database currently owned by our role.
            #    Filtering by owner means we won't accidentally drop a
            #    catalog DB (template0, postgres) which is owned by a
            #    different role. We DO include the per-instance Odoo
            #    template (datistemplate=true, owned by our role) so
            #    a cancelled instance doesn't leak templates either.
            list_sql = (
                "SELECT datname, datistemplate FROM pg_database "
                "WHERE datdba = (SELECT oid FROM pg_roles WHERE rolname='%s') "
                "ORDER BY datname"
            ) % safe_user
            list_cmd = "sudo -u postgres psql -tA -F '|' -c %s" % shlex.quote(list_sql)
            exit_code, stdout, stderr = ssh.execute(list_cmd)
            if exit_code != 0:
                _logger.warning(
                    "Failed to list databases owned by %s: %s",
                    db_user, stderr or stdout,
                )
                owned = []
            else:
                owned = []
                for line in stdout.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split('|', 1)
                    name = parts[0].strip()
                    is_template = (
                        parts[1].strip() == 't' if len(parts) > 1 else False
                    )
                    owned.append((name, is_template))

            # 2. Drop each owned DB. ``--force`` terminates any open
            #    sessions first; ``--if-exists`` swallows a race.
            #    Templates need datistemplate=false first or dropdb
            #    refuses.
            for db_name, is_template in owned:
                if not owned_name_re.match(db_name):
                    _logger.warning(
                        "Skipping drop of suspicious-looking db name %r",
                        db_name,
                    )
                    continue
                if is_template:
                    flag_cmd = (
                        "sudo -u postgres psql -c "
                        "\"UPDATE pg_database SET datistemplate=false "
                        "WHERE datname='%s'\""
                    ) % db_name.replace("'", "''")
                    ssh.execute(flag_cmd)
                drop_cmd = (
                    'sudo -u postgres dropdb --force --if-exists %s'
                    % shlex.quote(db_name)
                )
                ec, out, err = ssh.execute(drop_cmd)
                if ec != 0:
                    _logger.warning(
                        "Failed to drop database %s: %s",
                        db_name, err or out,
                    )
                else:
                    self._append_log("Dropped database '%s'." % db_name)

            # 3. Drop the role last (after all its DBs are gone).
            drop_role_cmd = (
                "sudo -u postgres psql -tc "
                "\"SELECT 1 FROM pg_roles WHERE rolname='%s'\" "
                "| grep -q 1 "
                "&& sudo -u postgres dropuser %s"
            ) % (safe_user, shlex.quote(db_user))
            ec, out, err = ssh.execute(drop_role_cmd)
            if ec != 0:
                _logger.warning(
                    "Failed to drop role %s: %s", db_user, err or out,
                )

    def action_delete_instance(self):
        """Remove container, volumes, network, database, db user, and instance folder (async)."""
        for rec in self:
            if rec.state == 'provisioning':
                raise UserError(
                    _("Cannot delete instance '%s' while it is being provisioned.")
                    % rec.subdomain
                )
            prev_state = rec.state
            rec.pre_provisioning_state = prev_state
            rec.pending_operation = 'delete'
            rec.state = 'provisioning'
            rec._append_log("Deletion queued. Running in background...")
            self.env['saas.audit.log'].saas_audit(
                'instance_delete', model='saas.instance', res_id=rec.id,
                res_name=rec.subdomain,
                detail='Deletion queued (was %s)' % prev_state)
            # Durable queue (ARCH-004 Phase 3). Delete is non-idempotent, so the
            # job-reaper won't auto-retry it; _on_background_error restores the
            # prior state on failure.
            self.env['saas.job'].enqueue(
                rec, '_do_delete_instance', channel='deploy',
                lock_key='instance:%s' % rec.id, max_attempts=1,
                on_error='_on_background_error', on_error_args=(prev_state,))
        return True

    def _do_delete_instance(self):
        """Delete instance (runs in background thread).

        Cancellation rule — every cancel drops **every** pre-existing
        snapshot. A fresh snapshot is taken immediately beforehand
        and becomes the single retained one. We never fall back to
        an older snapshot: if the fresh capture fails, nothing is
        retained and the old ones still get cleaned up.

        Order of operations:
        1. Snapshot the IDs of all existing snapshots — these are
           ALL slated for deletion regardless of what happens next.
        2. Take a fresh full-instance snapshot (needs container
           alive). That row is the only one allowed to survive.
        3. Tear down infrastructure (container, files, nginx, PG).
        4. Prune the restic repo to keep only the fresh snapshot's
           run tag (or wipe everything if no fresh snapshot).
        5. Delete every backup record that isn't the fresh one.
        6. Set state = cancelled.
        """
        self.ensure_one()
        server = self.docker_server_id
        # TODO(k8s-teardown-replacement): _get_instance_path() was
        # ssh_docker's host-filesystem path (server.docker_base_path,
        # removed with that backend) and is only used below inside the
        # SSH teardown block, which already fails at
        # _get_ssh_connection() and is caught — this never has a real
        # value to compute for Kubernetes, so skip it rather than crash
        # the whole delete/cancel flow before it even gets there.
        instance_path = ''
        Backup = self.env['saas.instance.backup'].sudo()

        # 0. Settle the money FIRST, then commit it, so the customer's wallet
        # credit can never be stranded behind a flaky infrastructure teardown.
        # Voiding the abandoned unpaid invoices refunds any reserved credit
        # idempotently; committing it here makes it durable even if a later
        # teardown step raises and the surrounding transaction is rolled back.
        self._void_unpaid_invoices_refund_credit()
        self.env.cr.commit()

        # 1. Record the snapshots that existed BEFORE this cancellation.
        # Every one of them is going away — we never keep a "most
        # recent existing" one. The retained slot is reserved for the
        # fresh snapshot taken in step 2 (and only that).
        pre_existing_ids = set(Backup.search([
            ('instance_id', '=', self.id),
            ('is_full_instance', '=', True),
        ]).ids)

        # 2. Take a fresh snapshot if the instance was actually
        # deployed (there's a DB + filestore to capture). Best-effort
        # — if it fails, no snapshot is retained, and the pre-existing
        # ones are still deleted in step 5.
        prev = self.pre_provisioning_state or self.state
        was_deployed = prev in ('running', 'stopped', 'suspended', 'failed')
        fresh_ok = False
        if was_deployed:
            try:
                self._append_log(
                    "Taking a final snapshot so you can restore your "
                    "data later if you reactivate this instance..."
                )
                Backup._create_full_instance_backup_sync(self)
                fresh_ok = True
                self._append_log("Final snapshot complete.")
            except Exception:
                _logger.exception(
                    "Final snapshot before cancellation failed for %s",
                    self.subdomain,
                )
                self._append_log(
                    "Couldn't take a fresh final snapshot — older "
                    "snapshots will still be removed and nothing "
                    "will be retained for restore."
                )
        else:
            self._append_log(
                "Skipping final snapshot — instance was never fully "
                "deployed. Any older snapshots will still be removed."
            )

        # The retained backup is ONLY the freshly-taken one. We look
        # for a successful full-instance restic row whose id wasn't
        # in the pre-cancellation set. If the fresh capture failed,
        # this is empty — and nothing gets retained.
        retained_backup = Backup.browse()
        if fresh_ok:
            retained_backup = Backup.search([
                ('instance_id', '=', self.id),
                ('is_full_instance', '=', True),
                ('format', '=', 'operator'),
                ('state', '=', 'done'),
                ('id', 'not in', list(pre_existing_ids)),
            ], order='create_date desc', limit=1)
        if retained_backup:
            self._append_log(
                "Will retain only the fresh snapshot '%s' (taken %s). "
                "All previous snapshots will be removed."
                % (
                    retained_backup.name,
                    retained_backup.create_date and
                    retained_backup.create_date.strftime('%Y-%m-%d %H:%M UTC')
                    or 'unknown',
                )
            )
        else:
            self._append_log(
                "No snapshot will be retained — every snapshot for "
                "this instance is being deleted."
            )

        # 2. Unregister webhooks from Git providers
        for repo in self.repo_ids.filtered(lambda r: r.webhook_provider_id):
            try:
                self._append_log("Removing webhook for %s..." % repo.name)
                repo._unregister_webhook_from_provider()
                repo.webhook_provider_id = False
                self._append_log("Webhook removed for %s." % repo.name)
            except Exception as e:
                self._append_log(
                    "WARNING: Failed to remove webhook for %s: %s"
                    % (repo.name, e)
                )

        # 3. Tear down infrastructure (tolerant of partial state). The money
        # has already been settled and committed above, so a teardown failure
        # (e.g. the host is unreachable) must NEVER abort the cancellation and
        # bounce the instance back to life — that's what would strand credit.
        # We flag any leftover infra for the reactivate flow / ops to reap and
        # carry on to finalise the cancel.
        try:
            with server._get_ssh_connection() as ssh:
                # Stop + purge container/network/volumes if they exist (best-effort)
                try:
                    self._compute_driver(connection=ssh).destroy(
                        self._compute_handle(), purge=True)
                except Exception as e:
                    self._append_log(
                        "docker compose down: %s (may not exist yet)" % e
                    )

                # Remove instance directory if it exists
                exit_code, stdout, stderr = ssh.execute(
                    'sudo rm -rf %s' % shlex.quote(instance_path),
                )
                if exit_code != 0:
                    self._append_log(
                        "WARNING: Failed to remove directory: %s" % stderr
                    )
                    _logger.warning(
                        "Failed to remove instance dir %s: %s",
                        instance_path, stderr,
                    )

                # Remove Nginx config and SSL certificate (if configured).
                # Track failures so the reactivate flow retries — leaving
                # a vhost behind is harmless on a stopped container, but
                # it'd cause a conflict if the customer reactivates with
                # a different topology.
                try:
                    proxy_server = self.domain_id.proxy_server_id
                    if proxy_server and proxy_server != self.docker_server_id:
                        with proxy_server._get_ssh_connection() as proxy_ssh:
                            self._remove_nginx(proxy_ssh)
                    else:
                        self._remove_nginx(ssh)
                except Exception:
                    _logger.exception(
                        "Nginx cleanup failed during cancellation of %s",
                        self.subdomain,
                    )
                    self.nginx_cleanup_pending = True
                    self._append_log(
                        "Couldn't fully remove the web proxy config — "
                        "we'll retry automatically the next time you "
                        "reactivate."
                    )
        except Exception:
            _logger.exception(
                "Could not reach the host to tear down infrastructure during "
                "cancellation of %s — finalising the cancel anyway; leftover "
                "container/files/proxy flagged for cleanup.", self.subdomain,
            )
            self.nginx_cleanup_pending = True
            self.pg_cleanup_pending = True
            self._append_log(
                "Couldn't reach the server to fully tear down infrastructure. "
                "Your subscription is cancelled and billing has stopped — any "
                "leftover resources will be reaped automatically."
            )

        # Drop database and role (safe if they don't exist). On
        # failure we set a flag the reactivate flow will retry — and
        # we don't lose visibility, because the operator log gets
        # the traceback via ``_logger.exception``.
        try:
            self._drop_postgresql()
        except Exception:
            _logger.exception(
                "PostgreSQL cleanup failed during cancellation of %s",
                self.subdomain,
            )
            self.pg_cleanup_pending = True
            self._append_log(
                "Couldn't fully clean up the database tier yet — "
                "we'll retry automatically the next time you reactivate."
            )

        # 4. Prune old full-instance snapshot data. Two cases:
        #    a) We have a fresh retained snapshot → keep ONLY it; every
        #       other snapshot's bucket data + record is dropped.
        #    b) No retained snapshot → wipe every full-instance snapshot
        #       so no old snapshot data lingers in the bucket.
        if retained_backup:
            try:
                others = Backup.search([
                    ('instance_id', '=', self.id),
                    ('is_full_instance', '=', True),
                    ('id', '!=', retained_backup.id),
                ])
                for b in others:
                    if b.bucket_path:
                        Backup._delete_bucket_prefix(b.bucket_path)
                others.unlink()
                self._append_log(
                    "Pruned old snapshot data — only the fresh "
                    "snapshot remains in cloud storage."
                )
            except Exception:
                _logger.exception(
                    "Failed to prune old snapshots for cancelled %s",
                    self.subdomain,
                )
                self._append_log(
                    "Couldn't fully clean up old snapshot data. The "
                    "retained snapshot is still available; orphan data "
                    "(if any) will be reaped on the next backup."
                )
        else:
            try:
                all_full = Backup.search([
                    ('instance_id', '=', self.id),
                    ('is_full_instance', '=', True),
                ])
                for b in all_full:
                    if b.bucket_path:
                        Backup._delete_bucket_prefix(b.bucket_path)
                all_full.unlink()
                self._append_log(
                    "Removed all snapshot data from cloud storage."
                )
            except Exception:
                _logger.exception(
                    "Failed to wipe snapshot data for cancelled %s",
                    self.subdomain,
                )
                self._append_log(
                    "Couldn't fully clean up snapshot data; orphan "
                    "objects (if any) will be reaped later."
                )

        # 5. Delete every REMAINING backup record (and its bucket object)
        # EXCEPT the one we're retaining. Step 4 above already deleted
        # every OTHER full-instance (``is_full_instance``) row and its
        # (multi-object, ``_delete_bucket_prefix``) bucket data, so what's
        # left here is per-database on-demand/legacy rows, each a single
        # object — plain ``_delete_from_bucket`` on its ``bucket_path``
        # is enough. The retained row stays so it shows up on /backups
        # after reactivation and the customer can hit Restore.
        all_backups = Backup.search([('instance_id', '=', self.id)])
        rows_to_drop = (
            all_backups - retained_backup if retained_backup else all_backups
        )
        for backup in rows_to_drop:
            try:
                if backup.bucket_path:
                    backup._delete_from_bucket()
            except Exception:
                _logger.warning(
                    "Failed to delete bucket object for %s on cancel of %s",
                    backup.bucket_path, self.subdomain,
                )
            try:
                backup.with_context(_skip_bucket_delete=True).unlink()
            except Exception:
                _logger.exception(
                    "Failed to unlink backup row %s on cancel of %s",
                    backup.id, self.subdomain,
                )

        # Old-style legacy zip stored in ``retained_backup_path`` (Char
        # field) from a prior cancellation — drop it now since the new
        # retention model uses a saas.instance.backup row.
        if self.retained_backup_path:
            try:
                stale = Backup.new({
                    'instance_id': self.id,
                    'bucket_path': self.retained_backup_path,
                })
                stale._delete_from_bucket()
            except Exception:
                _logger.warning(
                    "Failed to delete legacy retained backup %s for %s",
                    self.retained_backup_path, self.subdomain,
                )
            self.retained_backup_path = False

        # Reset repo statuses — infrastructure no longer exists
        for repo in self.repo_ids:
            repo.write({
                'state': 'pending',
                'webhook_enabled': False,
                'webhook_provider_id': False,
                'last_pull': False,
                'error_message': False,
            })

        # Wallet credit on abandoned unpaid invoices was already refunded and
        # committed at the very top of this method (step 0), so it can't be
        # stranded by a teardown failure. A second call here would be a no-op
        # (the invoices are already cancelled) — left out intentionally.
        self.state = 'cancelled'
        self.pending_operation = False
        # Snapshot subscription ends with the instance. Without this
        # the cancelled-instance card would still show "Daily Backups
        # Active" and an unlocked Restore button next to the retained
        # snapshot — both wrong. The customer can re-subscribe after
        # they reactivate; ``action_reactivate`` already clears these
        # again as a belt-and-braces. The storage cost of the retained
        # snapshot is recovered later by ``_get_retained_snapshot_fee``
        # (months retained × size × per-GB rate) when the customer
        # restores it.
        self.write({
            'daily_backup_enabled': False,
            # Compute tier is reset to the default (free) tier the same
            # way — a cancelled instance shouldn't show as being on a
            # paid tier; the customer re-selects (and pays for) one again
            # after reactivating.
            'compute_tier_id': self.env['saas.compute.tier'].get_default().id,
            **self._teardown_billing_vals(),
        })
        if retained_backup:
            self._append_log(
                "Cancellation complete. Snapshot '%s' is retained — "
                "after reactivation you can restore from it on the "
                "Snapshots page." % retained_backup.name
            )
        else:
            self._append_log(
                "Cancellation complete. No snapshot retained."
            )

    def _on_background_error(self, exception, prev_state):
        """Handle background operation failure.

        For deploy failures: if retries remain, queue the instance back to
        ``pending_provision`` so the cron picks it up automatically.
        Otherwise fall back to ``failed``.

        For non-deploy failures: revert to the previous state.
        """
        error_msg = str(exception)
        self._append_log("OPERATION FAILED: %s" % error_msg)
        self.env['saas.alert']._notify(
            'operation_failed',
            '%s on %s failed' % (self.pending_operation or 'operation', self.subdomain),
            level='error', detail=error_msg)
        self.last_error = error_msg
        self.last_error_date = fields.Datetime.now()
        self.pre_provisioning_state = False
        self.pending_operation = False

        if prev_state == 'failed':
            # This was a deploy attempt (error_args=('failed',)). The durable
            # queue OWNS deploy retries now (max_attempts=max_deploy_retries+1
            # with back-off) — this handler only runs on TERMINAL failure (the
            # queue exhausted its attempts or the job is unrecoverable), so go
            # straight to 'failed'. (Retrying here too would double-retry.)
            self.state = 'failed'
            if self.max_deploy_retries:
                self._append_log(
                    "Deployment failed after all automatic retries (max %d). "
                    "Manual intervention required." % self.max_deploy_retries
                )
        else:
            self.state = prev_state

    def action_create_backup(self):
        """Create a backup in the background."""
        self.ensure_one()

        # Block backups for trial plans
        if self.plan_id and self.plan_id.is_trial_plan:
            raise UserError(_("Backups are not available on trial plans. Please upgrade to a paid plan."))

        # Lock the instance row for the duration of the backup-create check
        # so two concurrent portal clicks cannot both pass the "no running
        # backup" guard and both spawn a thread.
        self.env.cr.execute(
            "SELECT id FROM saas_instance WHERE id = %s FOR UPDATE",
            (self.id,),
        )

        # Block if a backup is already running (re-read after the row lock)
        Backup = self.env['saas.instance.backup']
        running = Backup.search_count([
            ('instance_id', '=', self.id),
            ('state', '=', 'running'),
        ])
        if running:
            raise UserError(_("A backup is already in progress. Please wait for it to finish."))

        # Create the running record FIRST so concurrent clicks see it.
        now_str = fields.Datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        backup = Backup.create({
            'instance_id': self.id,
            'name': 'backup_%s' % now_str,
            'state': 'running',
        })

        # Auto-rotate AFTER creating the new record: if we're now at the
        # retention limit + 1, delete the oldest. Doing this before creation
        # could leave the customer with one fewer backup if creation fails.
        # Trials get no backups; everyone else keeps the last
        # DEFAULT_MAX_BACKUPS copies (fixed platform-wide retention).
        if self.plan_id and not self.plan_id.is_trial_plan:
            done_backups = Backup.search([
                ('instance_id', '=', self.id),
                ('state', '=', 'done'),
            ], order='create_date asc')
            while len(done_backups) >= DEFAULT_MAX_BACKUPS:
                oldest = done_backups[0]
                self._append_log(
                    "Auto-removing oldest backup '%s' (limit: %d)."
                    % (oldest.name, DEFAULT_MAX_BACKUPS)
                )
                oldest._delete_from_bucket()
                oldest.unlink()
                done_backups -= oldest

        self._append_log("Backup queued. Running in background...")
        # Durable queue (ARCH-004 Phase 1): starts promptly via the immediate
        # worker, but survives a crash (reaper) and records failures.
        self.env['saas.job'].enqueue(
            backup, '_run_portal_backup',
            channel='backup', lock_key='instance:%s' % self.id,
            max_attempts=1,
        )
        return True

    def action_view_logs(self):
        """Open a live log stream for this instance's Odoo container."""
        self.ensure_one()
        return {
            'type': 'ir.actions.client',
            'tag': 'container_logs_stream',
            'name': _("Logs: %s") % self.name,
            'context': {
                'stream_url': '/saas/instance/%d/logs/stream' % self.id,
                'container_name': self._get_container_name(),
                'tail': 100,
            },
        }

    _CRASH_LOOP_THRESHOLD = 5

    def reconcile(self, connection=None):
        """Phase 3: drive THIS instance's container toward its ``desired_state``.

        Idempotent + crash-safe (safe to re-run mid-action): reads the actual
        container status via ``driver.health``, diffs it against the desired
        state, and applies the single minimal action to converge.
        ``connection`` is accepted for call-site compatibility (an
        ssh_docker-era batching parameter) but unused by KubernetesDriver.
        Returns the action taken (for tests/logs). Skips instances
        mid-operation (a deploy/op owns the container then)."""
        self.ensure_one()
        desired = self.desired_state
        if desired == 'ignore' or not self.docker_server_id or self.pending_operation:
            return 'skipped'
        driver = self._compute_driver(connection=connection)
        handle = self._compute_handle()
        health = driver.health(handle)
        status = health.status or 'not_found'
        self.actual_state = status
        self.last_reconcile = fields.Datetime.now()
        action = 'none'

        if desired == 'running':
            crash_looping = (
                status == 'restarting'
                or (status in ('exited', 'dead')
                    and health.restart_count >= self._CRASH_LOOP_THRESHOLD))
            if crash_looping:
                # Restarting a crash-looper is futile + burns resources. Break the
                # loop: stop it, park as 'stopped', surface why (redeploy allowed).
                _logger.warning(
                    "[reconcile] %s crash-looping (status=%s restarts=%s) — stopping",
                    self.subdomain, status, health.restart_count)
                driver.stop(handle)
                self._append_log(
                    "Container kept crashing on startup (status=%s, restarts=%s) — "
                    "auto-stopped to break the loop. Review your custom modules / "
                    "Python packages, then redeploy." % (status, health.restart_count))
                self.write({
                    'state': 'stopped',
                    'last_error': 'Container crash-looped on startup and was '
                                  'auto-stopped. Review your custom code / packages, '
                                  'then redeploy.',
                    'last_error_date': fields.Datetime.now(),
                })
                action = 'stopped_crashloop'
            elif status == 'not_found':
                # The workload is GONE. driver.start() re-creates it (for
                # Kubernetes: the operator's own reconciler already
                # normally handles this on its own via the Deployment
                # controller — this is a backstop). There is no ssh_docker
                # "compose files missing, escalate to a full redeploy" path
                # any more (action_redeploy/_do_redeploy were ssh_docker-
                # only and were removed with that backend) — if start()
                # itself fails, that's surfaced as a failure, not escalated.
                _logger.warning(
                    "[reconcile] %s container missing — recreating", self.subdomain)
                self._append_log("Container not found — auto-recreating.")
                try:
                    driver.start(handle)
                    action = 'recreated'
                except Exception as e:
                    _logger.warning(
                        "[reconcile] %s recreate failed (%s)",
                        self.subdomain, str(e)[-200:])
                    self._append_log(
                        "Auto-recreate failed: %s" % str(e)[-300:])
                    action = 'recreate_failed'
            elif status in ('exited', 'dead'):
                # Genuine one-off down → bring it back (health-gated by start()).
                _logger.warning(
                    "[reconcile] %s is %s — restarting", self.subdomain, status)
                self._append_log(
                    "Container found in '%s' state — auto-restarting." % status)
                driver.start(handle)
                action = 'started'
            elif status == 'running' and self.last_error:
                self.last_error = False   # healthy again → clear stale error
                action = 'cleared_error'
        elif desired == 'stopped':
            if status in ('running', 'restarting'):
                _logger.warning(
                    "[reconcile] %s should be stopped but is %s — stopping",
                    self.subdomain, status)
                driver.stop(handle)
                action = 'stopped'
        return action

    @api.model
    def _cron_reconcile(self):
        """Phase 3: the single idempotent loop that drives every provisioned
        tenant toward its desired state. Replaces the ad-hoc health-check.

        No longer grouped/batched by an SSH connection per server (that was
        an ssh_docker-only optimization) — KubernetesDriver talks to each
        instance's cluster directly via its region's kubeconfig, so there
        is no per-host connection to share across instances here.
        """
        instances = self.search([
            ('state', 'in', list(self._RECONCILE_RUNNING + self._RECONCILE_STOPPED)),
            ('docker_server_id', '!=', False),
        ])
        if not instances:
            return 0
        acted = 0
        for inst in instances:
            try:
                if inst.reconcile() not in ('skipped', 'none'):
                    acted += 1
            except Exception:
                _logger.exception(
                    "[reconcile] failed for instance %s", inst.name)
        return acted

    @api.model
    def _cron_check_container_health(self):
        """[Phase 3] Back-compat shim: the health check is now the reconciler."""
        return self._cron_reconcile()

    def _cron_check_storage_limits(self):
        """Check total storage of running instances and suspend those exceeding their plan limit.

        TODO(k8s-metrics-replacement): this used to batch-refresh each
        instance's usage over SSH (``_refresh_usage_with_ssh``, now removed
        along with ssh_docker — see ``action_refresh_usage``) before
        evaluating capacity. There is no Kubernetes-native usage
        measurement yet, so this only evaluates capacity against whatever
        ``total_storage_bytes`` already holds (stale/zero until Phase 5
        wires up a real refresh) rather than refreshing it here.
        """
        # Bounded per run (PERF-003): a large fleet must not pull every running
        # instance into one cron transaction. Least-recently-touched first so
        # coverage rotates across runs when the fleet exceeds the cap.
        instances = self.search([
            ('state', '=', 'running'),
            ('plan_id', '!=', False),
            ('plan_id.storage_limit', '>', 0),
        ], order='write_date asc, id asc', limit=self._CRON_BATCH_SIZE)
        if not instances:
            return
        if len(instances) == self._CRON_BATCH_SIZE:
            _logger.info(
                "storage-limit cron hit the %d batch cap; the rest are checked "
                "on subsequent runs (oldest-touched first).",
                self._CRON_BATCH_SIZE)

        # v47: evaluate the capacity state machine (warn → full → grace →
        # paused). NEVER an automatic charge.
        for instance in instances:
            try:
                instance._evaluate_capacity()
            except Exception:
                _logger.exception(
                    "Capacity evaluation failed for %s",
                    instance.subdomain)

    # ================================================================
    #  Storage capacity (v47) — the "capacity upgrade experience"
    #  Positive framing only. Two remedies: upgrade OR buy storage blocks.
    #  Suspension happens ONLY after a configurable grace period, and is
    #  presented as a reversible "paused" state, never a penalty.
    # ================================================================
    @api.model
    def _storage_grace_days(self):
        try:
            return max(0, int(self.env['ir.config_parameter'].sudo().get_param(
                'saas_master.storage_grace_days', '7') or 0))
        except (TypeError, ValueError):
            return 7

    def _evaluate_capacity(self):
        """Advance the capacity state machine from the latest usage and send
        AT MOST one friendly notice per stage. Transitions:

            <80%  → ok        (clears any prior state, re-arms notices)
            ≥80%  → warn80    (soft, proactive "expand any time")
            ≥100% → full      (anchor grace clock; "expand to keep scaling")
            full > grace days → restricted (workspace paused; instant fix)

        Dropping back below 100% clears the grace clock immediately, and
        below 80% returns to ok — a customer who expands or frees space is
        never penalised. Returns the new state."""
        self.ensure_one()
        limit = self.effective_storage_limit_gb or 0.0
        if limit <= 0:
            return self.storage_state
        used = (self.total_storage_bytes or 0.0) / (1024 ** 3)
        pct = used / limit * 100.0
        today = fields.Date.today()
        vals = {}
        new_state = self.storage_state

        if pct < 80:
            new_state = 'ok'
            if self.storage_over_since:
                vals['storage_over_since'] = False
            if self.storage_last_notice:
                vals['storage_last_notice'] = False
        elif pct < 100:
            new_state = 'warn80'
            if self.storage_over_since:
                vals['storage_over_since'] = False
        else:
            # At/over capacity.
            if not self.storage_over_since:
                vals['storage_over_since'] = today
                over_days = 0
            else:
                over_days = (today - self.storage_over_since).days
            grace = self._storage_grace_days()
            new_state = 'restricted' if over_days > grace else (
                'grace' if self.storage_over_since and over_days >= 1 else 'full')

        if new_state != self.storage_state:
            vals['storage_state'] = new_state
        if vals:
            self.write(vals)

        # Notify once per stage (warn80 / full / grace reminders / restricted).
        self._notify_capacity(new_state, pct)
        # Enforce only at restricted, only after grace, framed as "paused".
        if new_state == 'restricted':
            self._capacity_pause()
        return new_state

    def _notify_capacity(self, state, pct):
        """One friendly, positively-framed notice per stage. Grace sends a
        gentle reminder each day it advances."""
        self.ensure_one()
        if state in ('ok', None):
            return
        # grace reminders repeat (one per day); others fire once per stage.
        already = self.storage_last_notice
        if state != 'grace' and already == state:
            return
        if state == 'grace' and already == 'grace':
            # still remind, but don't spam more than once per cron/day
            pass
        template = {
            'warn80': 'saas_core.mail_template_saas_capacity_warn',
            'full': 'saas_core.mail_template_saas_capacity_full',
            'grace': 'saas_core.mail_template_saas_capacity_full',
            'restricted': 'saas_core.mail_template_saas_capacity_paused',
        }.get(state)
        if template:
            try:
                self._send_notification(template)
            except Exception:
                _logger.exception("Capacity notice failed for %s", self.subdomain)
        self.storage_last_notice = state
        self._append_log(
            "Capacity notice (%s): %.0f%% of %g GB used."
            % (state, pct, self.effective_storage_limit_gb))

    def _capacity_pause(self):
        """Pause the workspace after grace — reversible the instant the
        customer upgrades or adds storage. Reuses the suspend path but is
        ALWAYS presented as 'paused, data safe', never a penalty."""
        self.ensure_one()
        if self.state == 'running':
            try:
                self.action_suspend()
            except Exception:
                _logger.exception(
                    "Capacity pause: suspend failed for %s; marking suspended.",
                    self.subdomain)
                self.state = 'suspended'
        elif self.state == 'stopped':
            self.state = 'suspended'

    def _capacity_summary(self):
        """Customer-facing capacity view — POSITIVE framing only, two clear
        actions. Drives the portal banner, emails and the 'Fix Now' buttons.
        Never uses punitive/limit-violation language."""
        self.ensure_one()
        block_gb, block_price = self.env['saas.pricing.engine'].storage_block_config()
        cap = self.effective_storage_limit_gb or 0.0
        used = round((self.total_storage_bytes or 0.0) / (1024 ** 3), 2)
        pct = round(used / cap * 100, 1) if cap > 0 else 0.0
        state = self.storage_state or 'ok'
        grace = self._storage_grace_days()
        grace_left = None
        if self.storage_over_since:
            grace_left = max(0, grace - (fields.Date.today() - self.storage_over_since).days)
        copy = {
            'ok': ("", "", "neutral"),
            'warn80': (
                "Your workspace is filling up",
                "You've used %.0f%% of your storage. Expand any time to keep "
                "scaling smoothly — upgrade your plan or add a storage block."
                % pct, "info"),
            'full': (
                "Your workspace has reached capacity",
                "Your service is running normally. To keep adding data, "
                "expand your capacity — upgrade your plan or add storage.",
                "warning"),
            'grace': (
                "Time to expand your workspace",
                "You're at full capacity. Upgrade your plan or add storage "
                "within %s day(s) to keep everything running smoothly."
                % (grace_left if grace_left is not None else grace), "warning"),
            'restricted': (
                "Your workspace is paused — your data is safe",
                "Upgrade your plan or add storage to switch it back on "
                "instantly. Nothing has been lost.", "paused"),
        }.get(state, ("", "", "neutral"))
        return {
            'state': state,
            'title': copy[0],
            'message': copy[1],
            'tone': copy[2],
            'used_gb': used,
            'capacity_gb': cap,
            'usage_pct': pct,
            'blocks_owned': self.extra_storage_blocks,
            'block_gb': block_gb,
            'block_price': round(block_price, 2),
            'grace_days_left': grace_left,
            'currency': self.env.company.currency_id.name or 'USD',
        }

    # ========== Trial Expiry ==========

    @api.model
    def _cron_check_trial_expiry(self):
        """Suspend running trial instances whose client trial period has expired.

        A partner is considered expired only when their trial end date is
        set AND in the past AND at least one trial flag is True. The
        previous predicate broke the OR/AND grouping and would match any
        partner with a flag regardless of date, mass-suspending paying
        customers.
        """
        today = fields.Date.today()
        expired_partners = self.env['res.partner'].search([
            ('saas_trial_end_date', '!=', False),
            ('saas_trial_end_date', '<=', today),
            '|',
            ('saas_trial_used', '=', True),
            ('saas_hosting_trial_used', '=', True),
        ])
        if not expired_partners:
            return
        # Find their running trial instances
        expired_instances = self.search([
            ('state', '=', 'running'),
            ('is_trial', '=', True),
            ('partner_id', 'in', expired_partners.ids),
        ])
        for instance in expired_instances:
            try:
                instance._on_trial_expired()
                instance.action_suspend()
                instance._append_log(
                    "AUTO-SUSPENDED: Client free trial expired on %s. "
                    "Please subscribe to a paid plan to reactivate."
                    % instance.partner_id.saas_trial_end_date
                )
                instance.message_post(
                    body=_(
                        "Free trial expired. Instance suspended. "
                        "Please subscribe to continue using the service."
                    ),
                    message_type='notification',
                )
                instance._send_notification(
                    'saas_core.mail_template_saas_suspended'
                )
                self.env.cr.commit()
                _logger.info(
                    "Trial instance %s suspended: client %s trial ended %s",
                    instance.subdomain, instance.partner_id.name,
                    instance.partner_id.saas_trial_end_date,
                )
            except Exception:
                self.env.cr.rollback()
                _logger.exception(
                    "Failed to suspend trial instance %s", instance.subdomain,
                )

    # ========== Repo Management ==========
    #
    # _update_repo_config_and_restart/_restart_container/_apply_pip_packages/
    # _deploy_pip_packages were removed: all four were ssh_docker-only
    # (docker-compose re-render + docker-exec pip install), calling the
    # now-deleted _render_and_write_configs/_ensure_can_ssh, with no
    # Kubernetes equivalent and no test coverage. Their call sites in
    # saas_website/controllers/api.py and saas_core/models/
    # saas_instance_repo.py are left as-is (out of scope here) and will
    # raise AttributeError until a later phase reimplements this via
    # driver.exec().

    # ========== Plan resize (resource side) ==========
    # Recurring billing, dunning and plan upgrade/downgrade (the commercial
    # side) live in saas_billing/models/saas_instance.py.

    # Pod requests are this fraction of the plan's limits (the driver's own
    # 250m/1 CPU and 512Mi/2Gi defaults use the same ratio), with a floor so
    # a tiny plan still schedules sensibly.
    _K8S_REQUEST_RATIO = 0.25
    _K8S_MIN_CPU_REQUEST_M = 100
    _K8S_MIN_MEM_REQUEST_MI = 128

    def _k8s_plan_resources(self):
        """The plan's CPU/RAM/workers as KubernetesDriver resource keys
        (``cpu_limit``/``cpu_request``/``mem_limit``/``mem_request`` in
        Kubernetes quantity form, plus ``workers``). Empty when the
        instance has no plan; a plan field left at 0/blank is omitted so
        the driver's default applies for it."""
        self.ensure_one()
        plan = self.plan_id
        if not plan:
            return {}
        vals = {}
        cpu_m = int(round((plan.cpu_limit or 0.0) * 1000))
        if cpu_m > 0:
            vals['cpu_limit'] = '%dm' % cpu_m
            vals['cpu_request'] = '%dm' % max(
                self._K8S_MIN_CPU_REQUEST_M,
                int(cpu_m * self._K8S_REQUEST_RATIO))
        mem_mi = int(self._parse_ram_string(plan.ram_limit) // (1024 ** 2))
        if mem_mi > 0:
            vals['mem_limit'] = '%dMi' % mem_mi
            vals['mem_request'] = '%dMi' % max(
                self._K8S_MIN_MEM_REQUEST_MI,
                int(mem_mi * self._K8S_REQUEST_RATIO))
        vals['workers'] = plan.workers or 0
        return vals

    def _update_container_resources(self):
        """Apply the current plan's CPU/RAM/workers to the running pod (the
        operator rolls the Deployment). Called after a plan change is
        applied. Raises if the cluster rejects the patch — callers log it
        and leave the plan change in place so an admin can redeploy."""
        self.ensure_one()
        if self.docker_server_id.compute_driver != 'kubernetes':
            return
        res = self._k8s_plan_resources()
        if not res.get('cpu_limit') or not res.get('mem_limit'):
            # Nothing concrete to apply (plan without limits): keep what
            # the pod already runs with rather than resetting to defaults.
            return
        self._compute_driver().set_resources(
            self._compute_handle(),
            cpu_request=res['cpu_request'], cpu_limit=res['cpu_limit'],
            mem_request=res['mem_request'], mem_limit=res['mem_limit'],
            workers=res['workers'])
        self._append_log(
            "Resources updated to the %s plan: %s CPU, %s RAM, %d worker(s)."
            % (self.plan_id.name, res['cpu_limit'], res['mem_limit'],
               res['workers']))

    # ========== Billing hooks ==========
    # No-op defaults that saas_billing overrides. Core never reads or
    # writes a billing field directly — it calls these instead, so the
    # dependency stays one-way (saas_billing -> saas_core).

    def _void_unpaid_invoices_refund_credit(self):
        """Hook: void this instance's unpaid invoices on cancellation and
        refund any wallet credit they consumed (saas_billing)."""
        return

    def _has_overdue_invoices_past_grace(self):
        """Hook: True if an invoice is overdue past the grace period, which
        blocks a portal start (saas_billing)."""
        return False

    def _teardown_billing_vals(self):
        """Hook: extra values written when a cancellation teardown
        completes, clearing billing state (saas_billing)."""
        return {}

    def _env_child_extra_vals(self):
        """Hook: extra create() values for a Staging/Development child,
        e.g. the parent's billing period (saas_billing)."""
        return {}

    def _on_trial_expired(self):
        """Hook: called for each trial instance just before the trial-expiry
        cron suspends it (saas_billing clears an unpaid pending upgrade)."""
        return

    def _status_dict_billing_extra(self):
        """Hook: billing keys merged into ``_get_status_dict()``
        (saas_billing)."""
        return {}

    # ========== Email Notifications ==========

    def _send_notification(self, template_xmlid):
        """Send an email notification using the given mail template XML ID."""
        self.ensure_one()
        template = self.env.ref(template_xmlid, raise_if_not_found=False)
        if template:
            template.send_mail(self.id, force_send=False)

    # ========== Deployment Status Endpoint Helper ==========

    def _get_status_dict(self):
        """Return a dict suitable for JSON API responses."""
        self.ensure_one()
        return {
            'id': self.id,
            'state': self.state,
            'state_label': dict(
                self._fields['state'].selection
            ).get(self.state, self.state),
            'url': self.url or '',
            'provisioning_log': self.provisioning_log or '',
            'backup_running': bool(self.backup_ids.filtered(lambda b: b.state == 'running')),
            'db_ops_running': bool(self._hosting_reconcile_db_ops()),
            **self._status_dict_billing_extra(),
        }

    # Skip the expensive ``hosting_db_list`` reconcile for the first
    # ~90 s of an op's lifetime — that's the normal completion window
    # for create (clone + bootstrap) and duplicate. Reconciling sooner
    # would SSH into the docker host on every 5-second poll for no
    # reason; the happy path is just ``search_count`` on the state.
    _DB_OP_RECONCILE_GRACE_SECONDS = 90

    def _hosting_reconcile_db_ops(self):
        """Self-heal stuck DB-op tracking rows and return the live set.

        The background worker normally flips ``state`` to ``done`` /
        ``failed`` itself — but that flip can be missed when the
        XML-RPC call to the customer's instance hangs (no socket
        timeout in ``xmlrpc.client``), when the HTTP worker is
        recycled by ``--limit-time-real`` mid-thread, or when a
        registry reload after module install severs the connection.
        Reality (the actual list of databases) is the source of
        truth, so we reconcile against it whenever an op has been
        running longer than the typical completion window. Returns
        the recordset of ops still running so callers can decide
        whether to keep polling.
        """
        self.ensure_one()
        Op = self.env['saas.instance.db.operation'].sudo()
        stuck = Op.search([
            ('instance_id', '=', self.id),
            ('state', '=', 'running'),
        ])
        if not stuck:
            return stuck
        # Listing requires a reachable hosting instance with a docker
        # server — otherwise we'd surface the listing error as a
        # "stuck" status check.
        if not (self.is_hosting and self.state in ('running', 'provisioning')
                and self.docker_server_id):
            return stuck
        # Only reconcile against the live DB list once the youngest op
        # is past the typical completion window. Keeps the happy-path
        # poll a cheap ``search_count`` instead of an SSH round trip.
        cutoff = fields.Datetime.now() - datetime.timedelta(
            seconds=self._DB_OP_RECONCILE_GRACE_SECONDS,
        )
        if not any(op.create_date and op.create_date < cutoff for op in stuck):
            return stuck
        try:
            db_names = {r['name'] for r in self.hosting_db_list()}
        except Exception:
            return stuck
        still_running = self.env['saas.instance.db.operation']
        for op in stuck:
            if op.operation in ('create', 'duplicate'):
                if op.db_name in db_names:
                    op.state = 'done'
                    continue
            elif op.operation == 'drop':
                if op.db_name not in db_names:
                    op.state = 'done'
                    continue
            still_running |= op
        return still_running

    # ========== Portal Self-Service Actions ==========

    def action_portal_restart(self):
        """Restart from portal — only allowed for running instances."""
        self.ensure_one()
        if self.state != 'running':
            raise UserError(_("Instance must be running to restart."))
        self.action_restart()

    def action_portal_stop(self):
        """Stop from portal — only allowed for running instances."""
        self.ensure_one()
        if self.state != 'running':
            raise UserError(_("Instance must be running to stop."))
        self.action_stop()

    def action_portal_start(self):
        """Start from portal — only allowed for stopped instances with no overdue invoices."""
        self.ensure_one()
        if self.state != 'stopped':
            raise UserError(_("Instance must be stopped to start."))
        if self._has_overdue_invoices_past_grace():
            raise UserError(
                _("Cannot start instance: you have overdue invoices. "
                  "Please complete your payment first.")
            )
        self.action_restart()

    def _retry_pending_cleanup(self):
        """Retry cancellation cleanup steps that previously failed.

        Called from ``action_reactivate`` before we clear the old
        infrastructure FKs — without this retry, a transient SSH /
        Postgres failure during the original cancellation would
        leave stale resources on disk and the next deploy would
        either inherit them silently (privacy concern) or fail
        because of name clashes.

        Idempotent: ``_drop_postgresql`` is no-op if the role / DB
        doesn't exist, and ``_remove_nginx`` skips when the vhost
        is gone. Either retry can fail again — the flags stay set
        and we'll try once more on the next reactivation attempt.
        """
        self.ensure_one()
        if self.pg_cleanup_pending and self.db_server_id:
            try:
                self._drop_postgresql()
                self.pg_cleanup_pending = False
                self._append_log(
                    "Cleaned up old database resources from the "
                    "previous cancellation."
                )
            except Exception:
                _logger.exception(
                    "Retry PG cleanup still failing for %s",
                    self.subdomain,
                )
                self._append_log(
                    "Some database resources from the previous "
                    "cancellation couldn't be cleaned up yet — "
                    "we'll keep retrying."
                )
        if self.nginx_cleanup_pending and self.docker_server_id:
            try:
                proxy_server = self.domain_id.proxy_server_id
                if proxy_server and proxy_server != self.docker_server_id:
                    with proxy_server._get_ssh_connection() as proxy_ssh:
                        self._remove_nginx(proxy_ssh)
                else:
                    with self.docker_server_id._get_ssh_connection() as ssh:
                        self._remove_nginx(ssh)
                self.nginx_cleanup_pending = False
                self._append_log(
                    "Cleaned up old web proxy config from the "
                    "previous cancellation."
                )
            except Exception:
                _logger.exception(
                    "Retry nginx cleanup still failing for %s",
                    self.subdomain,
                )

