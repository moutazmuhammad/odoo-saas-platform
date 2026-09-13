import base64
import datetime
import json
import logging
import math
import os
import re
import secrets
import shlex
import string
import threading
import time
from dateutil.relativedelta import relativedelta
from jinja2 import Environment, FileSystemLoader

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

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

# Untranslated technical tokens used as sale.order.origin so the dunning
# and renewal lookups work regardless of UI language. These must never be
# wrapped in _() — they are matched verbatim by _get_all_invoices().
ORIGIN_INITIAL = 'SAAS:INITIAL:%s'
ORIGIN_RENEWAL = 'SAAS:RENEWAL:%s'
ORIGIN_SUBSCRIPTION = 'SAAS:SUBSCRIPTION:%s'
ORIGIN_PLAN_UPGRADE = 'SAAS:UPGRADE:%s'
ORIGIN_DATA_RESTORATION = 'SAAS:RESTORATION:%s'
ORIGIN_BACKUP_ADDON = 'SAAS:BACKUP-ADDON:%s'
ORIGIN_STORAGE_BLOCK = 'SAAS:STORAGE-BLOCK:%s'
# Adding a Staging/Development environment server: the prorated activation
# invoice that gates provisioning of a child env (mirrors STORAGE-BLOCK).
ORIGIN_ENVIRONMENT = 'SAAS:ENVIRONMENT:%s'
# Origins considered "optional" for dunning purposes (won't trigger suspension).
# Daily-backup add-on is opt-in — a missed payment for it shouldn't take down
# the whole instance the customer still uses every day.
OPTIONAL_INVOICE_ORIGIN_PREFIXES = (
    'SAAS:SUBSCRIPTION:', 'SAAS:UPGRADE:', 'SAAS:BACKUP-ADDON:',
    # Buying storage blocks is opt-in — an unpaid block invoice must not
    # suspend the workspace (the customer simply doesn't get the extra room).
    'SAAS:STORAGE-BLOCK:',
    # Adding an env server is opt-in — an unpaid env invoice must not suspend
    # the project; the child simply stays unprovisioned until paid.
    'SAAS:ENVIRONMENT:',
)
# Days past a daily-backup add-on invoice's due date before snapshots are
# paused. Snapshots resume automatically once the invoice is paid.
DAILY_BACKUP_SUSPEND_GRACE_DAYS = 3

# v47: ALL promotions and discounts are removed. The only pricing
# variation is monthly vs annual (computed in saas.pricing.engine). There
# is no trial promo, no promo cycles, no stacking.

# Auto-renew retry schedule (A1): days AFTER the invoice DUE date on which the
# saved payment method is re-charged if still unpaid. The renewal-date
# attempt is day 0; these are the follow-ups. All fall inside the default
# 7-day grace period, so suspension only happens after the last retry.
PAYMENT_RETRY_OFFSET_DAYS = (1, 3, 5)

# Auto-renew reminders (A1): days BEFORE the renewal date to notify.
RENEWAL_REMINDER_OFFSET_DAYS = (7, 1)

# ----------------------------------------------------------------------
# Live metrics sampling — measurement is decoupled from viewing so it
# scales with the number of *watched instances*, not the number of
# viewers. The portal polls a cheap cached endpoint (which marks the
# instance "watched"); a single advisory-locked sampler measures watched
# instances every few seconds, ONE ssh/`docker stats` per host.
# ----------------------------------------------------------------------
LIVE_METRICS_WATCH_TTL = 25          # secs a poll keeps an instance "watched"
LIVE_METRICS_SAMPLE_INTERVAL = 5     # secs between sampler ticks
LIVE_METRICS_SAMPLER_MAX_RUN = 50    # secs a single cron run loops before exiting
LIVE_METRICS_SEED_STALE = 10         # secs of staleness before a poll seeds a sample
_LIVE_METRICS_LOCK_KEY = 738291014   # pg advisory lock id (single sampler cluster-wide)
_LIVE_SAMPLE_SEED_AT = {}            # instance_id -> monotonic ts of last seed spawn
_LIVE_SAMPLE_SEED_GUARD = threading.Lock()

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
        string='Docker Server',
        tracking=True,
        ondelete='restrict',
        index=True,
        domain="[('is_docker_host', '=', True)]",
        help='Physical server where the Docker container for this instance runs. '
             'Leave empty for automatic allocation based on server capacity.',
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
    support_plan_id = fields.Many2one(
        'saas.support.plan',
        string='Support Plan',
        ondelete='restrict',
        default=lambda self: self.env['saas.support.plan']._get_default(),
        help='Paid support tier for this instance (P3). A flat monthly fee '
             'billed alongside the plan; not scaled by region. Defaults to '
             'the free best-effort tier; the customer can pick a higher one '
             'at create / upgrade.',
    )
    db_server_id = fields.Many2one(
        'saas.server',
        string='Database Server',
        tracking=True,
        ondelete='restrict',
        index=True,
        domain="[('is_db_server', '=', True)]",
        help='PostgreSQL server that hosts the database for this instance. '
             'Auto-derived from the Docker server topology when left empty.',
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

    # ========== Sales & Invoicing ==========
    sale_order_id = fields.Many2one(
        'sale.order',
        string='Sale Order',
        tracking=True,
        ondelete='set null',
        index=True,
        help='Sale order linked to this instance.',
    )
    restoration_invoice_id = fields.Many2one(
        'account.move',
        string='Restoration Invoice',
        readonly=True,
        ondelete='set null',
        help='Unpaid restoration fee invoice. Instance is suspended until paid.',
    )
    restore_banner_dismissed = fields.Boolean(
        string='Restore Banner Dismissed',
        default=False,
        help='Client dismissed the data restore suggestion banner.',
    )
    sale_order_count = fields.Integer(
        string='Sale Orders',
        compute='_compute_sale_order_count',
    )
    invoice_count = fields.Integer(
        string='Invoices',
        compute='_compute_invoice_count',
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
    daily_backup_pending_invoice_id = fields.Many2one(
        'account.move',
        string='Daily Backup Pending Invoice',
        tracking=True,
        copy=False,
        ondelete='set null',
        help='Unpaid invoice gating the activation of daily backups. '
             'Set when the customer clicks "Enable Daily Backups" on the '
             'portal; cleared (and daily_backup_enabled flipped to True) '
             'as soon as the invoice transitions to paid / in_payment.',
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
    # Backup billing follows the main subscription's billing_period and is
    # aligned to the plan renewal date, so a monthly plan bills the add-on
    # monthly and a yearly plan bills it yearly (12x up-front), merged into
    # the plan renewal invoice.
    daily_backup_next_invoice_date = fields.Date(
        string='Daily Backup Next Invoice',
        copy=False,
        help='When the next daily-backup charge is due. The add-on follows '
             'the subscription period and is aligned to the plan renewal '
             'date, so this normally equals ``next_invoice_date`` and the '
             'charge is merged into the plan renewal invoice.',
    )
    daily_backup_last_invoice_date = fields.Date(
        string='Daily Backup Last Invoice',
        copy=False,
        help='Most recent month the daily-backup add-on was billed for.',
    )
    # ---------- Saved card + auto-renewal ----------
    # ``payment_token_id`` holds the saved card that renewal crons
    # charge automatically. It's captured the first time the customer
    # pays an activation invoice with "Save my card" ticked. The
    # customer can clear it at any time from the portal billing
    # settings; clearing it disables both auto-renew toggles.
    payment_token_id = fields.Many2one(
        'payment.token',
        string='Saved Card',
        copy=False,
        ondelete='set null',
        help='Card used for auto-renewal. Captured on the first '
             'tokenized activation payment; cleared when the customer '
             'removes it from billing settings.',
    )
    auto_renew_subscription = fields.Boolean(
        string='Auto-renew Subscription',
        copy=False,
        default=True,
        help='When enabled and a saved card is on file, the monthly / '
             'yearly subscription invoice is charged automatically on '
             'renewal. When disabled the invoice is still issued, but '
             'the customer pays it manually.',
    )
    auto_renew_daily_backup = fields.Boolean(
        string='Auto-renew Daily Backups',
        copy=False,
        default=True,
        help='Legacy toggle. The daily-backup charge now rides on the '
             'subscription renewal invoice, so it is auto-charged together '
             'with the plan whenever Auto-renew Subscription is on and a '
             'card is on file; there is no separate backup invoice to '
             'auto-charge.',
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

    # ========== Billing Period (per-instance) ==========
    billing_period = fields.Selection(
        [('monthly', 'Monthly'), ('yearly', 'Yearly')],
        string='Billing Period',
        default='monthly',
        help='Billing cycle chosen by the client for this instance.',
    )

    # ========== Pending Upgrade (awaiting payment) ==========
    pending_plan_id = fields.Many2one(
        'saas.plan',
        string='Pending Upgrade Plan',
        ondelete='set null',
        help='Plan the client has chosen but not yet paid for. '
             'Applied automatically once payment is confirmed.',
    )
    pending_billing_period = fields.Selection(
        [('monthly', 'Monthly'), ('yearly', 'Yearly')],
        string='Pending Billing Period',
    )
    pending_change_invoice_id = fields.Many2one(
        'account.move',
        string='Pending Change Invoice',
        ondelete='set null',
        copy=False,
        help='Invoice the client must pay for the pending plan change. '
             'Explicit link so the payment hook and the safety-net cron '
             'can match the payment exactly, even if sale_order_id is '
             'later overwritten by another flow.',
    )


    # ========== Scheduled Downgrade ==========
    scheduled_plan_id = fields.Many2one(
        'saas.plan',
        string='Scheduled Downgrade Plan',
        ondelete='set null',
        help='Lower plan to switch to at the end of the current billing cycle.',
    )
    scheduled_billing_period = fields.Selection(
        [('monthly', 'Monthly'), ('yearly', 'Yearly')],
        string='Scheduled Billing Period',
    )

    # ========== Recurring Billing ==========
    next_invoice_date = fields.Date(
        string='Next Invoice Date',
        tracking=True,
        index=True,
        help='Date on which the next recurring invoice will be generated. '
             'Set automatically after the first payment.',
    )
    last_invoice_date = fields.Date(
        string='Last Invoice Date',
        readonly=True,
    )
    suspension_warning_sent = fields.Boolean(
        default=False,
        help='Whether a suspension warning email has been sent for the '
             'current overdue period.',
    )
    # Auto-renew reminder flags (A1) — reset every cycle in
    # ``_set_next_invoice_date`` / on renewal so the 7-day and 1-day
    # notices fire once per period.
    renewal_reminder_7d_sent = fields.Boolean(default=False, copy=False)
    renewal_reminder_1d_sent = fields.Boolean(default=False, copy=False)

    # Saved payment method chosen for auto-renew (A1). Provider-agnostic
    # wrapper that stores ONLY safe references (provider id + external
    # customer / token refs); the legacy ``payment_token_id`` is kept in
    # sync for the existing charge path.
    saas_payment_method_id = fields.Many2one(
        'saas.payment.method', string='Auto-renew Payment Method',
        copy=False, ondelete='set null',
    )

    # Wallet (A4): surplus prepaid value to move into the customer's
    # wallet when a pending plan change is actually applied (on payment),
    # so unused subscription value is never forfeited on an upgrade.
    pending_wallet_credit = fields.Float(
        string='Pending Wallet Credit', copy=False, default=0.0,
    )

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
    pending_storage_blocks = fields.Integer(
        string='Pending Storage Blocks', copy=False, default=0,
        help='Blocks from a storage purchase awaiting payment; added to '
             'extra_storage_blocks once the activation invoice is paid.',
    )
    storage_block_pending_invoice_id = fields.Many2one(
        'account.move', string='Storage Block Invoice', copy=False,
        ondelete='set null',
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
    env_pending_invoice_id = fields.Many2one(
        'account.move', string='Environment Activation Invoice', copy=False,
        ondelete='set null',
        help='On a Staging/Development child: the prorated activation invoice '
             'that gates its provisioning. Cleared once paid.')
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
    # --- Mid-cycle slot RESERVATION (buy capacity without a server) -------
    # The customer reserves N Staging/Development slots (paid, no repo needed,
    # no server created). Within the reserved count he then creates/deletes
    # servers freely — deleting frees a slot for reuse, it never refunds or
    # lowers the count. These hold a reservation purchase until its invoice
    # is paid, at which point the slots are granted (mirrors storage blocks).
    reserved_staging_pending = fields.Integer(
        string='Pending Reserved Staging Slots', copy=False, default=0,
        help='Staging slots to grant once the reservation invoice is paid.')
    reserved_dev_pending = fields.Integer(
        string='Pending Reserved Development Slots', copy=False, default=0,
        help='Development slots to grant once the reservation invoice is paid.')
    slot_reservation_pending_invoice_id = fields.Many2one(
        'account.move', string='Slot Reservation Invoice', copy=False,
        ondelete='set null',
        help='The prorated invoice for a mid-cycle slot reservation. The slots '
             'are granted once it is paid; cleared then.')
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

    # ===== Phase 4: per-tenant margin (revenue − infra cost) =====
    margin_currency_id = fields.Many2one(
        'res.currency', compute='_compute_margin', string='Margin Currency')
    monthly_cost = fields.Monetary(
        string='Infra Cost / month', compute='_compute_margin',
        currency_field='margin_currency_id', store=False,
        help='Phase 4: provisioned CPU/RAM + used storage × this server\'s rate '
             'card. For a Production env, includes its child (staging/dev) costs.')
    monthly_revenue = fields.Monetary(
        string='Revenue / month', compute='_compute_margin',
        currency_field='margin_currency_id', store=False,
        help='Monthly-equivalent recurring revenue (plan + support, period-normalized). '
             'Child environments bill via the parent, so their own revenue is 0.')
    monthly_margin = fields.Monetary(
        string='Margin / month', compute='_compute_margin',
        currency_field='margin_currency_id', store=False,
        help='Revenue − infra cost. Negative = this tenant loses money.')
    margin_pct = fields.Float(
        string='Margin %', compute='_compute_margin',
        help='Margin as a percentage of revenue.')
    is_profitable = fields.Boolean(
        string='Profitable', compute='_compute_margin', search='_search_profitable',
        help='True when monthly margin ≥ 0.')

    def _instance_infra_cost(self):
        """Own monthly infra cost from the server rate card (excludes children)."""
        self.ensure_one()
        srv = self.docker_server_id
        if not srv:
            return 0.0
        plan = self.plan_id
        cpu = plan.cpu_limit or 0.0
        ram_gb = (self._parse_ram_string(plan.ram_limit) / (1024 ** 3)) if plan and plan.ram_limit else 0.0
        storage_gb = self.storage_used_gb or 0.0
        return (cpu * (srv.cost_per_cpu_month or 0.0)
                + ram_gb * (srv.cost_per_gb_ram_month or 0.0)
                + storage_gb * (srv.cost_per_gb_storage_month or 0.0))

    def _instance_monthly_revenue(self):
        """Monthly-equivalent recurring revenue. Children bill via the parent, so
        they contribute 0 (their cost rolls up to the parent's margin)."""
        self.ensure_one()
        if self.parent_id:
            return 0.0
        plan = self.plan_id
        if not plan:
            return 0.0
        base = (plan.yearly_price or plan.price * 12) / 12.0 \
            if self.billing_period == 'yearly' else plan.price
        # Support is a flat monthly price (per the pricing rules: support/backup
        # are flat ×12 for yearly), so it's the same per month regardless of period.
        support = self.support_plan_id.monthly_price if self.support_plan_id else 0.0
        return (base or 0.0) + (support or 0.0)

    @api.depends('plan_id', 'billing_period', 'support_plan_id', 'storage_used_gb',
                 'docker_server_id.cost_per_cpu_month',
                 'docker_server_id.cost_per_gb_ram_month',
                 'docker_server_id.cost_per_gb_storage_month',
                 'child_env_ids.storage_used_gb', 'child_env_ids.plan_id')
    def _compute_margin(self):
        company_cur = self.env.company.currency_id
        for rec in self:
            rec.margin_currency_id = (rec.plan_id.currency_id or company_cur)
            # Production rolls up its children's infra cost; children show their own.
            cost = rec._instance_infra_cost()
            if not rec.parent_id:
                cost += sum(c._instance_infra_cost() for c in rec.child_env_ids)
            revenue = rec._instance_monthly_revenue()
            rec.monthly_cost = cost
            rec.monthly_revenue = revenue
            rec.monthly_margin = revenue - cost
            rec.margin_pct = (100.0 * (revenue - cost) / revenue) if revenue else 0.0
            rec.is_profitable = (revenue - cost) >= 0

    @api.model
    def _cron_flag_unprofitable_tenants(self):
        """Phase 4.3.4 alert: log + chatter-notify Production tenants whose monthly
        margin is negative, so ops can act (reprice, resize, or cut). Cheap: runs
        over billable parentless instances and posts only on a state change."""
        recs = self.search([
            ('parent_id', '=', False), ('plan_id', '!=', False),
            ('state', 'in', ('running', 'suspended'))])
        flagged = recs.filtered(lambda r: not r.is_profitable)
        for r in flagged:
            _logger.warning(
                "[margin] tenant %s is UNPROFITABLE: revenue=%.2f cost=%.2f margin=%.2f",
                r.subdomain, r.monthly_revenue, r.monthly_cost, r.monthly_margin)
            try:
                r.message_post(body=_(
                    "⚠️ Running at a loss: revenue %(rev).2f − infra cost %(cost).2f "
                    "= %(margin).2f / month. Review pricing or resources.") % {
                    'rev': r.monthly_revenue, 'cost': r.monthly_cost,
                    'margin': r.monthly_margin})
            except Exception:
                pass
        return len(flagged)

    def _search_profitable(self, operator, value):
        # Lightweight search: compute on the candidate set (paid, parentless).
        recs = self.search([('parent_id', '=', False), ('plan_id', '!=', False)])
        ids = [r.id for r in recs if r.is_profitable]
        want = value if operator == '=' else not value
        return [('id', 'in' if want else 'not in', ids)]

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
    metrics_watch_until = fields.Datetime(
        string='Live Metrics Watched Until',
        readonly=True,
        copy=False,
        help='Bumped each time the portal polls live metrics for this '
             'instance. The live-metrics sampler only measures instances '
             'watched within this window, so cost scales with viewers '
             'present, not with the whole fleet.',
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
        help='Phase 2.2: when set, deploy runs THIS immutable image '
             '(registry@sha256:… or <registry>/tenant-<sub>:<sha>) instead of '
             'odoo-light + mounted source/addons. Set by the build pipeline; '
             'rollback = point it at a previous build digest and redeploy. '
             'Empty = legacy source-clone + build-on-host mode.',
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
            if rec.docker_server_id and (not rec.xmlrpc_port or not rec.longpolling_port):
                rec._auto_assign_ports()
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

    @api.depends('sale_order_id')
    def _compute_sale_order_count(self):
        for rec in self:
            rec.sale_order_count = 1 if rec.sale_order_id else 0

    @api.depends('sale_order_id', 'sale_order_id.invoice_ids', 'partner_id', 'name', 'subdomain')
    def _compute_invoice_count(self):
        for rec in self:
            rec.invoice_count = len(rec._get_all_invoices())

    def _instance_origin_tokens(self):
        """Return the list of untranslated origin tokens this instance uses."""
        self.ensure_one()
        ref = self.name or self.subdomain
        if not ref:
            return []
        return [
            ORIGIN_INITIAL % ref,
            ORIGIN_RENEWAL % ref,
            ORIGIN_SUBSCRIPTION % ref,
            ORIGIN_PLAN_UPGRADE % ref,
            ORIGIN_DATA_RESTORATION % ref,
            ORIGIN_BACKUP_ADDON % ref,
        ]

    def _get_all_invoices(self):
        """Return all invoices related to this instance across all sale orders.

        Matches sale orders by exact untranslated origin tokens. Also
        accepts legacy translated origins ("Renewal: …", "Subscription: …"
        in any language) for backwards compatibility with records created
        before token-based origins were introduced.
        """
        self.ensure_one()
        instance_ref = self.name or self.subdomain
        if not instance_ref:
            return self.env['account.move']
        expected_origins = self._instance_origin_tokens() + [instance_ref]
        domain = [
            ('partner_id', '=', self.partner_id.id),
            '|',
            ('origin', 'in', expected_origins),
            ('id', '=', self.sale_order_id.id if self.sale_order_id else 0),
        ]
        sale_orders = self.env['sale.order'].search(domain)
        # Defensive: also pick up legacy SOs where origin contains the
        # instance ref preceded by a known label in any locale.
        if not sale_orders or self.sale_order_id not in sale_orders:
            legacy = self.env['sale.order'].search([
                ('partner_id', '=', self.partner_id.id),
                ('origin', 'ilike', instance_ref),
            ])
            sale_orders |= legacy.filtered(
                lambda s: s.origin and instance_ref in s.origin
            )
        if not sale_orders:
            return self.env['account.move']
        return sale_orders.mapped('invoice_ids')

    # Invoice origin prefixes the client may NOT cancel — these are
    # mandatory (the dunning system enforces payment). Everything else
    # (plan upgrades, the daily-backup add-on, …) is optional and the
    # client can decline it from the portal.
    _NON_CANCELLABLE_INVOICE_PREFIXES = (
        'SAAS:INITIAL:', 'SAAS:RENEWAL:', 'SAAS:RESTORATION:',
        # Legacy translated prefixes (pre token-based origins).
        'Renewal:', 'Data restoration:',
    )

    def _invoice_is_client_cancellable(self, invoice):
        """True if the client may cancel this unpaid invoice (it's optional,
        not an initial subscription / renewal / restoration)."""
        self.ensure_one()
        if not invoice or invoice.state != 'posted':
            return False
        if invoice.payment_state in ('paid', 'in_payment'):
            return False
        # A never-deployed order still awaiting its first payment can always be
        # abandoned by the client ("don't complete the purchase"): nothing was
        # provisioned, so cancelling just frees the subdomain. The
        # non-cancellable rule below only guards LIVE instances (renewals /
        # restorations the dunning system must enforce).
        if self.state in ('pending_payment', 'draft'):
            return True
        origins = invoice.line_ids.sale_line_ids.order_id.mapped('origin')
        return not any(
            o and any(o.startswith(p) for p in self._NON_CANCELLABLE_INVOICE_PREFIXES)
            for o in origins
        )

    def _get_cancellable_unpaid_invoice(self):
        """The single unpaid, client-cancellable invoice for this instance
        (or empty recordset). Used to offer a "Decline / Cancel" action
        instead of nagging the customer to pay forever."""
        self.ensure_one()
        for inv in self._get_all_invoices().filtered(
            lambda i: i.state == 'posted'
            and i.payment_state not in ('paid', 'in_payment')
            and i.amount_residual > 0
        ).sorted('create_date', reverse=True):
            if self._invoice_is_client_cancellable(inv):
                return inv
        return self.env['account.move']

    def action_client_cancel_invoice(self, invoice):
        """Client declines an optional unpaid invoice: cancel it, undo any
        pending plan change, and — if the instance was never deployed
        (draft / pending_payment) — cancel the instance so the subdomain is
        freed. Returns a short status string: 'cancelled' | 'instance_cancelled'.
        Raises UserError if the invoice isn't client-cancellable."""
        self.ensure_one()
        if not self._invoice_is_client_cancellable(invoice):
            raise UserError(_(
                "This invoice is required and can't be cancelled. Please "
                "complete the payment or contact support."
            ))
        from markupsafe import Markup
        invoice.button_cancel()

        if self.pending_plan_id:
            self._append_log("Pending upgrade cancelled by client.")
            self.message_post(body=Markup(
                "<b>Client cancelled plan upgrade payment</b><br/>"
                "Was upgrading to: <b>%s</b><br/>Invoice: %s"
            ) % (self.pending_plan_id.name, invoice.name))
            try:
                self._send_notification(
                    'saas_core.mail_template_saas_payment_cancelled')
            except Exception:
                _logger.exception("payment-cancelled notice failed for %s", self.id)
            self.write({
                'pending_plan_id': False,
                'pending_billing_period': False,
                'pending_change_invoice_id': False,
            })

        if self.state in ('pending_payment', 'draft'):
            subdomain = self.name or self.subdomain
            self.write({
                'state': 'cancelled_by_client',
                'cancellation_reason': (
                    "Client declined the initial order before payment.\n"
                    "Invoice: %s\nSubdomain: %s" % (invoice.name, subdomain)
                ),
            })
            self._append_log("Order declined by client. Subdomain released.")
            try:
                self._send_notification(
                    'saas_core.mail_template_saas_payment_cancelled')
            except Exception:
                _logger.exception("order-cancelled notice failed for %s", self.id)
            return 'instance_cancelled'
        return 'cancelled'

    # ========== Sales & Invoicing Actions ==========

    def _get_daily_backup_product(self):
        """Return the singleton product.product for the daily-backup add-on.

        Created on first use. Used both in the one-time purchase
        invoice (when the customer enables the feature) and as a
        recurring line on subsequent renewal invoices.
        """
        product = self.env['product.product'].sudo().search(
            [('default_code', '=', 'SAAS-BACKUP-ADDON')], limit=1,
        )
        if not product:
            product = self.env['product.product'].sudo().create({
                'name': 'Daily Backups Add-on',
                'default_code': 'SAAS-BACKUP-ADDON',
                'type': 'service',
                'list_price': 0.0,
                'sale_ok': True,
                'purchase_ok': False,
                'taxes_id': [(5, 0, 0)],
            })
        return product

    def _get_daily_backup_price(self):
        """Monthly price of the daily-backup add-on for THIS instance.

        Usage-based: the storage actually consumed is rounded UP to the
        next whole GB and charged at the configured per-GB rate
        (``saas_master.snapshot_price_per_gb``, default $0.40/GB), 1 GB
        minimum. "Consumed" = the deduplicated snapshot repo footprint
        (what the snapshots really occupy in the bucket); before the
        first snapshot exists (activation invoice) we fall back to the
        instance's measured used storage, since that is what the first
        snapshot will capture. Re-evaluated on every monthly renewal, so
        the charge follows the customer's data over time.
        """
        self.ensure_one()
        # Delegate to the pricing engine so the checkout quote, the portal
        # and the recurring invoice all charge the SAME number.
        used_bytes = self._snapshot_total_bytes() or self.total_storage_bytes or 0
        return self.env['saas.pricing.engine'].daily_backup_price(
            used_bytes=used_bytes,
        )

    def _get_retained_snapshot_fee(self):
        """One-off charge for restoring the snapshot retained after the
        instance was deleted.

        Computed, not configured: the number of months the snapshot sat
        in cloud storage after cancellation × its size rounded UP to the
        next whole GB × the per-GB monthly rate
        (``saas_master.snapshot_price_per_gb``). A started month counts
        as a whole month (minimum 1). No retained snapshot → 0.
        """
        self.ensure_one()
        retained = self.env['saas.instance.backup'].sudo().search([
            ('instance_id', '=', self.id),
            ('is_full_instance', '=', True),
            ('state', '=', 'done'),
        ], order='create_date desc', limit=1)
        if not retained and not self.retained_backup_path:
            return 0.0
        per_gb = self.env['saas.pricing.engine'].snapshot_price_per_gb()
        if per_gb <= 0:
            return 0.0
        gb = max(1, math.ceil((retained.size_mb or 0.0) / 1024.0))
        months = 1
        if retained and retained.create_date:
            # A5: whole CALENDAR months retained (no days/30 approximation).
            months = max(1, self.env['saas.pricing.engine'].months_between(
                retained.create_date.date(), fields.Date.today()))
        # This is a FIXED one-off fee — never touched by the yearly
        # subscription discount (A5 discount scope).
        return round(months * gb * per_gb, 2)

    def action_purchase_daily_backup(self):
        """Create an unpaid invoice for the backup add-on.

        Sequence:
        1. Sale order with a single ``Daily Backups Add-on`` line at
           the monthly add-on price from settings.
        2. Confirm SO → create + post the invoice.
        3. Store it on ``daily_backup_pending_invoice_id``. Once it
           transitions to ``paid``/``in_payment``, the
           ``account.move.write`` override below flips
           ``daily_backup_enabled`` to True and clears the pointer.

        Caller (portal route) then redirects to our custom checkout
        page where the customer pays.
        """
        self.ensure_one()
        # Snapshots are available to BOTH hosting and managed-services
        # instances (the one app-level add-on a service gets); only trials
        # are excluded.
        if self.is_trial:
            raise UserError(_(
                "Daily backups can't be purchased on a trial plan."
            ))
        if self.daily_backup_enabled:
            raise UserError(_(
                "Daily backups are already enabled on this instance."
            ))
        if self.daily_backup_pending_invoice_id:
            existing = self.daily_backup_pending_invoice_id
            if existing.state == 'posted' and existing.payment_state not in (
                'paid', 'in_payment', 'reversed', 'invoicing_legacy',
            ):
                # An unpaid invoice already exists — return it instead
                # of creating a second one. Portal redirects there.
                return existing
            # Old invoice is paid (shouldn't happen — hook would have
            # cleared this), or cancelled. Clear and re-issue.
            self.daily_backup_pending_invoice_id = False

        monthly_price = self._get_daily_backup_price()
        if monthly_price <= 0:
            raise UserError(_(
                "Daily backup pricing isn't configured. Ask the "
                "platform operator to set the monthly add-on price "
                "in SaaS settings."
            ))

        # The add-on now follows the SUBSCRIPTION's billing period so it can
        # ride on the plan's renewal invoice from here on. Enabling mid-cycle
        # is a one-off, PRORATED catch-up charge for the time left until the
        # plan renews (monthly plan → rest of this month; yearly plan → rest
        # of this year), after which the account.move payment hook aligns the
        # add-on's next-invoice date to the plan's renewal date and every
        # future charge is merged into the renewal (one bill, same period).
        today = fields.Date.today()
        period = self.billing_period or 'monthly'
        period_label = 'Yearly' if period == 'yearly' else 'Monthly'
        months = self.env['saas.pricing.engine'].period_months(period)
        full = months * monthly_price
        charge = full
        if self.next_invoice_date and self.last_invoice_date:
            total_days = (self.next_invoice_date - self.last_invoice_date).days
            left = (self.next_invoice_date - today).days
            if total_days > 0 and 0 < left < total_days:
                charge = round(full * left / total_days, 2)

        product = self._get_daily_backup_product()
        pricelist = self.partner_id.property_product_pricelist
        line_name = _(
            'Daily Backups Add-on (%s) — %s (prorated to your renewal date)'
        ) % (period_label, self.name or self.subdomain)
        order_lines = [(0, 0, {
            'product_id': product.id,
            'name': line_name,
            'product_uom_qty': 1,
            'price_unit': charge,
        })]

        order_vals = {
            'partner_id': self.partner_id.id,
            'origin': ORIGIN_BACKUP_ADDON % (self.name or self.subdomain),
            'order_line': order_lines,
        }
        if pricelist:
            order_vals['pricelist_id'] = pricelist.id
        order = self.env['sale.order'].sudo().create(order_vals)
        order.action_confirm()
        invoice = order._create_invoices()
        invoice.action_post()
        self.write({'daily_backup_pending_invoice_id': invoice.id})
        self._append_log(
            "Daily-backup add-on activation invoice %s created — %s period, "
            "prorated catch-up %.2f (full %.2f)." % (
                invoice.name, period_label, charge, full)
        )
        return invoice

    def _get_billing_product(self):
        """Return the default product.product used on SaaS sale order lines.

        Creates it on first use if it doesn't exist yet.
        """
        product = self.env['product.product'].sudo().search(
            [('default_code', '=', 'SAAS-SUB')], limit=1,
        )
        if not product:
            product = self.env['product.product'].sudo().create({
                'name': 'SaaS Subscription',
                'default_code': 'SAAS-SUB',
                'type': 'service',
                'list_price': 0.0,
                'sale_ok': True,
                'purchase_ok': False,
                'taxes_id': [(5, 0, 0)],
            })
        return product

    def _support_order_line(self, period, period_label):
        """Sale-order line tuple for the instance's support plan, or None.

        Support is a flat MONTHLY fee billed on the same cycle as the plan;
        on a yearly plan it's charged x12 (qty=12) so the support term
        matches the plan term. The free/default plan (price 0) adds nothing,
        so this is behaviour-neutral until support is priced and picked."""
        self.ensure_one()
        support = self.support_plan_id
        if not support or support.monthly_price <= 0:
            return None
        months = 12 if period == 'yearly' else 1
        return (0, 0, {
            'product_id': self._get_billing_product().id,
            'name': _('Support: %s (%s) — %s') % (
                support.name, period_label, self.name or self.subdomain,
            ),
            'product_uom_qty': months,
            'price_unit': support.monthly_price,
        })

    def _snapshot_order_line(self, period=None):
        """Sale-order line tuple for the daily-backup add-on over ONE billing
        period, or None. The add-on now follows the SUBSCRIPTION's period
        (like the support plan): a monthly plan bills one month (qty 1); a
        yearly plan bills the whole year up-front (qty 12) at the same per-
        month rate — so it can be folded into the plan's renewal invoice and
        the customer gets a single, period-aligned bill. The per-GB rate is
        a flat monthly fee and is NEVER discounted on yearly billing.

        ``period`` defaults to the instance's own billing period. Price is
        storage-aware + lock-aware via ``_get_daily_backup_price``.

        Snapshots apply to BOTH hosting and services subscribers (the only
        app-level feature a managed services instance gets); the gate is the
        subscription flag, not the product type."""
        self.ensure_one()
        if not self.daily_backup_enabled:
            return None
        price = self._get_daily_backup_price()
        if price <= 0:
            return None
        period = period or self.billing_period or 'monthly'
        months = self.env['saas.pricing.engine'].period_months(period)
        period_label = 'Yearly' if period == 'yearly' else 'Monthly'
        return (0, 0, {
            'product_id': self._get_daily_backup_product().id,
            'name': _('Daily Backups Add-on (%s) — %s') % (
                period_label, self.name or self.subdomain,
            ),
            'product_uom_qty': months,
            'price_unit': price,
        })

    # ==================================================================
    #  Storage blocks (v47) — a PURCHASED recurring add-on that expands
    #  capacity. Never an automatic usage charge.
    # ==================================================================
    def _get_storage_block_product(self):
        product = self.env['product.product'].sudo().search(
            [('default_code', '=', 'SAAS-STORAGE-BLOCK')], limit=1)
        if not product:
            product = self.env['product.product'].sudo().create({
                'name': 'Extra Storage Block',
                'default_code': 'SAAS-STORAGE-BLOCK',
                'type': 'service', 'list_price': 0.0,
                'sale_ok': True, 'purchase_ok': False, 'taxes_id': [(5, 0, 0)],
            })
        return product

    def _storage_block_order_line(self, period, blocks=None):
        """Recurring SO line for PURCHASED storage blocks on the given period
        (qty = blocks × months), or None. Block price is a flat monthly fee —
        billed ×months on yearly, never discounted (only resources are)."""
        self.ensure_one()
        blocks = self.extra_storage_blocks if blocks is None else blocks
        if blocks <= 0:
            return None
        block_gb, block_price = self.env['saas.pricing.engine'].storage_block_config()
        if block_gb <= 0 or block_price <= 0:
            return None
        months = self.env['saas.pricing.engine'].period_months(period)
        return (0, 0, {
            'product_id': self._get_storage_block_product().id,
            'name': _('Storage: %d × %d GB block(s) (%s) — %s') % (
                blocks, block_gb,
                'Yearly' if period == 'yearly' else 'Monthly',
                self.name or self.subdomain),
            'product_uom_qty': blocks * months,
            'price_unit': block_price,
        })

    def action_purchase_storage_block(self, qty=1):
        """Buy ``qty`` storage blocks: issue a prorated activation invoice for
        the remainder of the current cycle; on payment, increment
        ``extra_storage_blocks`` (raising effective capacity). Recurs on every
        renewal thereafter via ``_storage_block_order_line``."""
        self.ensure_one()
        qty = max(1, int(qty or 1))
        block_gb, block_price = self.env['saas.pricing.engine'].storage_block_config()
        if block_gb <= 0 or block_price <= 0:
            raise UserError(_(
                "Storage blocks aren't available yet. Please contact support "
                "or upgrade your plan to expand capacity."))
        # Prorate the first charge over the days left in the current cycle so
        # the customer only pays for what's left until it joins the renewal.
        months = self.env['saas.pricing.engine'].period_months(
            self.billing_period or 'monthly')
        full = qty * block_price * months
        charge = full
        if self.next_invoice_date and self.last_invoice_date:
            total_days = (self.next_invoice_date - self.last_invoice_date).days
            left = (self.next_invoice_date - fields.Date.today()).days
            if total_days > 0 and 0 < left < total_days:
                charge = round(full * left / total_days, 2)
        pricelist = self.partner_id.property_product_pricelist
        order_lines = [(0, 0, {
            'product_id': self._get_storage_block_product().id,
            'name': _('Add %d × %d GB storage block(s) — %s (prorated)') % (
                qty, block_gb, self.name or self.subdomain),
            'product_uom_qty': 1,
            'price_unit': charge,
        })]
        wallet_line, wallet_amount = self._wallet_credit_line(order_lines)
        if wallet_line:
            order_lines.append(wallet_line)
        order_vals = {
            'partner_id': self.partner_id.id,
            'origin': ORIGIN_STORAGE_BLOCK % (self.name or self.subdomain),
            'order_line': order_lines,
        }
        if pricelist:
            order_vals['pricelist_id'] = pricelist.id
        order = self.env['sale.order'].sudo().create(order_vals)
        order.action_confirm()
        invoice = order._create_invoices()
        invoice.action_post()
        self._wallet_settle_consumption(invoice, wallet_amount)
        # Remember how many blocks this purchase activates on payment.
        self.write({
            'pending_storage_blocks': (self.pending_storage_blocks or 0) + qty,
            'storage_block_pending_invoice_id': invoice.id,
        })
        self._append_log(
            "Storage block purchase: %d × %d GB, activation invoice %s (%.2f)."
            % (qty, block_gb, invoice.name, charge))
        if invoice.amount_total <= 0:
            self._activate_pending_storage_blocks()
        return invoice

    def _activate_pending_storage_blocks(self):
        """Apply purchased blocks after payment: raise capacity + clear the
        capacity warning immediately (instant 'Fix Now' recovery)."""
        self.ensure_one()
        pending = self.pending_storage_blocks or 0
        if pending <= 0:
            return
        self.write({
            'extra_storage_blocks': (self.extra_storage_blocks or 0) + pending,
            'pending_storage_blocks': 0,
            'storage_block_pending_invoice_id': False,
        })
        self._append_log("Storage capacity expanded by %d block(s)." % pending)
        self.message_post(body=_(
            "Storage expanded — your workspace now has more room. Thanks for "
            "scaling with us."))
        # Re-evaluate so a paused/at-capacity workspace recovers at once.
        try:
            if self.state == 'suspended':
                self.action_reactivate() if hasattr(self, 'action_reactivate') else None
            self._evaluate_capacity()
        except Exception:
            _logger.exception("Capacity re-eval after block activation failed for %s",
                              self.subdomain)

    def action_release_storage_block(self, qty=1):
        """Release ``qty`` storage blocks at the next cycle (lowers capacity +
        recurring charge). Blocked if releasing would drop capacity below
        current usage (same 75% headroom guard as a plan downgrade)."""
        self.ensure_one()
        qty = max(1, int(qty or 1))
        if qty > (self.extra_storage_blocks or 0):
            raise UserError(_("You don't have that many storage blocks."))
        block_gb, _bp = self.env['saas.pricing.engine'].storage_block_config()
        new_cap = self.effective_storage_limit_gb - qty * block_gb
        used = (self.total_storage_bytes or 0.0) / (1024 ** 3)
        if new_cap > 0 and used >= self.DOWNGRADE_THRESHOLD * new_cap:
            raise UserError(_(
                "You're using too much storage to release that capacity. "
                "Free up space first, then you can release storage."))
        self.write({'extra_storage_blocks': self.extra_storage_blocks - qty})
        self._append_log("Released %d storage block(s)." % qty)
        return True

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

    def _env_server_price(self, period=None):
        """Per-server price of a Staging/Development server for THIS project
        (lowest spec, region-scaled, × env_price_factor) for ``period``. Single
        source of truth via the pricing engine — used by the configurator, the
        one-click create flow, and the recurring renewal line."""
        self.ensure_one()
        anchor = self._env_anchor()
        period = period or anchor.billing_period or 'monthly'
        return self.env['saas.pricing.engine'].env_server_price(
            billing=period,
            region=anchor.region_id.id if anchor.region_id else None)

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

    def _environment_order_lines(self, period):
        """Recurring SO lines for the project's purchased Staging/Development
        SLOTS — one line per slot at the lowest-spec env price. Billing
        follows the entitlement (what the customer paid for), not how many
        children they've actually spun up: creating within the slots is
        free, so renewal must bill slots, not live servers. Only meaningful
        on the Production anchor; children never self-bill."""
        self.ensure_one()
        if self.environment != 'production':
            return []
        price = self._env_server_price(period)
        if price <= 0:
            return []
        labels = dict(self._fields['environment'].selection)
        period_label = 'Yearly' if period == 'yearly' else 'Monthly'
        lines = []
        for env_type, count in (('staging', self.staging_slots or 0),
                                ('development', self.dev_slots or 0)):
            for i in range(max(0, count)):
                lines.append((0, 0, {
                    'product_id': self._get_billing_product().id,
                    'name': _('%s slot #%d (%s)') % (
                        labels.get(env_type, env_type), i + 1, period_label),
                    'product_uom_qty': 1,
                    'price_unit': price,
                }))
        return lines

    def _initial_environment_order_lines(self, period, period_label):
        """Initial-invoice lines for the env servers chosen at checkout
        (counts in pending_staging_count/pending_dev_count). One line per
        server at the lowest-spec env price for ``period``."""
        self.ensure_one()
        if self.environment != 'production':
            return []
        price = self._env_server_price(period)
        if price <= 0:
            return []
        labels = dict(self._fields['environment'].selection)
        lines = []
        for env_type, count in (
                ('staging', self.pending_staging_count or 0),
                ('development', self.pending_dev_count or 0)):
            for i in range(max(0, count)):
                lines.append((0, 0, {
                    'product_id': self._get_billing_product().id,
                    'name': _('%s server #%d (%s) — %s') % (
                        labels.get(env_type, env_type), i + 1, period_label,
                        self.name or self.subdomain),
                    'product_uom_qty': 1,
                    'price_unit': price,
                }))
        return lines

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
        child = self.env['saas.instance'].sudo().create({
            'subdomain': self._unique_env_subdomain(name),
            'domain_id': self.domain_id.id,
            'partner_id': self.partner_id.id,
            'saas_product_id': self.saas_product_id.id,
            'plan_id': self._get_env_plan().id,
            'odoo_version_id': self.odoo_version_id.id,
            'region_id': self.region_id.id if self.region_id else False,
            'billing_period': self.billing_period or 'monthly',
            'environment': env_type,
            'parent_id': self.id,
            'state': 'draft',
        })
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
        """Provision a paid Staging/Development child: clear the activation
        invoice and deploy. Children never get their own billing cycle (the
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
        self.env_pending_invoice_id = False
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

    def action_reserve_environment_slots(self, env_type, qty=1):
        """Reserve (buy) ``qty`` Staging/Development slots on the project — paid
        capacity, NO Git repo required and NO server created. Prorated for the
        remainder of the cycle; on payment the slots are granted and the
        customer can create/delete servers within them freely. Mirrors
        ``action_purchase_storage_block``."""
        self.ensure_one()
        if self.environment != 'production':
            raise UserError(_(
                "Reserve environment slots from the Production server."))
        if self.is_trial:
            raise UserError(_(
                "Upgrade to a paid plan before reserving environment slots."))
        if env_type not in ('staging', 'development'):
            raise UserError(_("Unknown environment type."))
        qty = max(1, int(qty or 1))
        period = self.billing_period or 'monthly'
        full = self._env_server_price(period) * qty
        charge = full
        if self.next_invoice_date and self.last_invoice_date:
            total_days = (self.next_invoice_date - self.last_invoice_date).days
            left = (self.next_invoice_date - fields.Date.today()).days
            if total_days > 0 and 0 < left < total_days:
                charge = round(full * left / total_days, 2)
        label = dict(self._fields['environment'].selection).get(
            env_type, env_type)
        pricelist = self.partner_id.property_product_pricelist
        order_lines = [(0, 0, {
            'product_id': self._get_billing_product().id,
            'name': _('Reserve %d %s slot(s) — %s (prorated)') % (
                qty, label, self.name or self.subdomain),
            'product_uom_qty': 1,
            'price_unit': charge,
        })]
        wallet_line, wallet_amount = self._wallet_credit_line(order_lines)
        if wallet_line:
            order_lines.append(wallet_line)
        order_vals = {
            'partner_id': self.partner_id.id,
            'origin': ORIGIN_ENVIRONMENT % ('%d %s slot(s)' % (qty, label)),
            'order_line': order_lines,
        }
        if pricelist:
            order_vals['pricelist_id'] = pricelist.id
        order = self.env['sale.order'].sudo().create(order_vals)
        order.action_confirm()
        invoice = order._create_invoices()
        invoice.action_post()
        self._wallet_settle_consumption(invoice, wallet_amount)
        field = ('reserved_staging_pending' if env_type == 'staging'
                 else 'reserved_dev_pending')
        self.write({
            field: (self[field] or 0) + qty,
            'slot_reservation_pending_invoice_id': invoice.id,
        })
        self._append_log(
            "Reserve %d %s slot(s) — invoice %s (%.2f)."
            % (qty, label, invoice.name, charge))
        if invoice.amount_total <= 0:
            self._activate_reserved_slots()
            return {'auto_provisioned': True, 'reserved': qty}
        if self._auto_renew_method() and self._try_auto_charge_invoice(
                invoice, kind='subscription'):
            return {'auto_provisioned': True, 'reserved': qty}
        return {
            'auto_provisioned': False,
            'invoice_id': invoice.id,
            'checkout_url': '/my/instances/%s/checkout' % self.id,
        }

    def _activate_reserved_slots(self):
        """Grant the pending reserved slots after their invoice is paid."""
        self.ensure_one()
        staging = max(0, self.reserved_staging_pending or 0)
        dev = max(0, self.reserved_dev_pending or 0)
        if not (staging or dev):
            return
        self.write({
            'staging_slots': (self.staging_slots or 0) + staging,
            'dev_slots': (self.dev_slots or 0) + dev,
            'reserved_staging_pending': 0,
            'reserved_dev_pending': 0,
            'slot_reservation_pending_invoice_id': False,
        })
        self._append_log(
            "Reserved slots granted: %d staging, %d development." % (staging, dev))
        self.message_post(body=_(
            "Environment slots reserved — create servers within them anytime."))

    def action_release_environment_slots(self, env_type, qty=1):
        """Release (give up) ``qty`` reserved Staging/Development slots, lowering
        the recurring charge. Only FREE slots (reserved minus in-use) can be
        released — delete a server first to free its slot. The unused portion of
        the current cycle is credited back to the wallet."""
        self.ensure_one()
        if self.environment != 'production':
            raise UserError(_(
                "Release environment slots from the Production server."))
        if env_type not in ('staging', 'development'):
            raise UserError(_("Unknown environment type."))
        qty = max(1, int(qty or 1))
        slots = self._env_slots_for(env_type)
        used = self._env_used_for(env_type)
        free = slots - used
        if qty > free:
            label = dict(self._fields['environment'].selection).get(
                env_type, env_type)
            raise UserError(_(
                "Only %d free %s slot(s) can be released (the rest are in use). "
                "Delete a server first to free its slot.") % (free, label))
        field = ('staging_slots' if env_type == 'staging' else 'dev_slots')
        self.write({field: max(0, (self[field] or 0) - qty)})
        # Credit back the unused portion of the cycle for each released slot.
        remaining, _d, _t = self._proration_credit(
            self._env_server_price() * qty)
        if remaining > 0:
            self._grant_wallet_credit(
                remaining, origin='environment_slot_release',
                reason=_('Released %d %s slot(s)') % (qty, env_type))
        self._append_log(
            "Released %d %s slot(s) (now %d reserved)."
            % (qty, env_type, self[field]))
        return {'released': qty, 'slots': self[field]}

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

    # ==================================================================
    #  Wallet credit (A4) helpers
    #  ─ the single, reusable plumbing every invoice-creating flow uses
    #    so prepaid value is never lost and the promo / wallet are shown
    #    explicitly on the invoice as their own lines.
    # ==================================================================
    def _wallet(self, create=True):
        """This instance's customer wallet (commercial partner)."""
        self.ensure_one()
        return self.env['saas.wallet']._for_partner(
            self.partner_id, create=create)

    @staticmethod
    def _order_lines_subtotal(order_lines):
        """Sum of (qty × unit) over a list of (0,0,vals) SO-line commands."""
        total = 0.0
        for cmd in order_lines:
            vals = cmd[2] if len(cmd) > 2 and isinstance(cmd[2], dict) else {}
            total += (vals.get('price_unit') or 0.0) * (
                vals.get('product_uom_qty') or 0.0)
        return round(total, 2)

    def _wallet_credit_line(self, order_lines):
        """Negative SO line that applies available wallet credit to these
        lines, capped at their positive subtotal. Returns ``(line, amount)``
        or ``(None, 0.0)``. Locks the wallet so the amount shown on the
        invoice is exactly what gets debited (no race with another flow).
        The caller MUST call ``_wallet_settle_consumption`` after the
        invoice is posted."""
        self.ensure_one()
        wallet = self._wallet(create=False)
        if not wallet or wallet.balance <= 0:
            return None, 0.0
        wallet._lock()
        subtotal = self._order_lines_subtotal(order_lines)
        if subtotal <= 0:
            return None, 0.0
        amount = round(min(wallet.balance, subtotal), 2)
        if amount <= 0:
            return None, 0.0
        line = (0, 0, {
            'product_id': self._get_billing_product().id,
            'name': _('Wallet credit applied — %s') % (
                self.name or self.subdomain),
            'product_uom_qty': 1,
            'price_unit': -amount,
        })
        return line, amount

    def _wallet_settle_consumption(self, invoice, amount):
        """Debit the wallet for credit applied to ``invoice`` and link the
        ledger entry to the move (so it can be refunded if cancelled)."""
        self.ensure_one()
        if amount <= 0 or not invoice:
            return
        self._wallet(create=True)._consume(
            amount, origin='invoice_consumption',
            reason=_('Applied to invoice %s') % (invoice.name or ''),
            move=invoice, instance=self)

    def _grant_wallet_credit(self, amount, origin, reason=''):
        """Move unused/forfeited subscription value into the wallet so it is
        never lost (A4). No-op for non-positive amounts."""
        self.ensure_one()
        if (amount or 0.0) <= 0:
            return
        wallet = self._wallet(create=True)
        wallet._credit(round(amount, 2), origin=origin, reason=reason,
                       instance=self)
        self._append_log(
            "Wallet credited %.2f (%s). New balance: %.2f."
            % (amount, origin, wallet.balance))
        self.message_post(body=_(
            "%.2f added to your wallet — %s. Wallet balance: %.2f."
        ) % (amount, reason or origin, wallet.balance))

    def _void_unpaid_invoices_refund_credit(self):
        """Cancel every still-unpaid posted invoice for this instance,
        returning any wallet credit that was RESERVED on them.

        Wallet credit is reserved (consumed) when an invoice is posted so the
        figure shown on the invoice is exactly what gets debited (no race with
        a concurrent flow). That reservation is correct while the invoice can
        still be paid, but when the instance is cancelled those invoices are
        abandoned — without this, the reserved credit would be stranded
        forever. ``account.move.button_cancel`` already refunds the consumed
        credit idempotently (``_saas_refund_wallet_credit``), so cancelling
        the abandoned invoices makes the customer whole."""
        self.ensure_one()
        unpaid = self._get_all_invoices().filtered(
            lambda m: m.move_type == 'out_invoice'
            and m.state == 'posted'
            and m.payment_state not in ('paid', 'in_payment')
            and m.amount_residual > 0
        )
        for inv in unpaid:
            try:
                inv.button_cancel()
            except Exception:
                _logger.exception(
                    "Failed to void unpaid invoice %s while cancelling %s — "
                    "wallet credit may need a manual refund.",
                    inv.name, self.subdomain,
                )

    def action_confirm_and_bill(self):
        """Validate instance, create sale order, confirm it, and generate invoice.

        This is the single entry point for the billing flow:
        draft → pending_payment (or paid if zero-amount).
        """
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_("Instance must be in Draft state to confirm and bill."))
        if self.sale_order_id:
            raise UserError(_("A sale order already exists for this instance."))
        if not self.partner_id:
            raise UserError(_("Please set a customer before confirming."))
        if not self.plan_id:
            raise UserError(_("Please set a plan before confirming."))

        plan = self.plan_id
        period = self.billing_period or 'monthly'
        price = plan._get_price_for_period(period)
        period_label = 'Monthly' if period == 'monthly' else 'Yearly'
        # -- Build order lines (respect partner pricelist when available) --
        pricelist = self.partner_id.property_product_pricelist
        order_lines = [(0, 0, {
            'product_id': self._get_billing_product().id,
            'name': _('%s (%s) — %s') % (plan.name, period_label, self.name or self.subdomain),
            'product_uom_qty': 1,
            'price_unit': price,
        })]

        # Support plan (P5): bill it on the initial invoice too.
        support_line = self._support_order_line(period, period_label)
        if support_line:
            order_lines.append(support_line)

        # Daily-backup add-on chosen at checkout (daily_backup_enabled set at
        # instance creation). It's a flat MONTHLY fee, so charge one full
        # month upfront here and anchor its monthly cycle when the instance
        # is marked paid (see _set_next_invoice_date). Without this the
        # feature was switched on but never billed.
        if self.daily_backup_enabled:
            snapshot_line = self._snapshot_order_line()
            if snapshot_line:
                order_lines.append(snapshot_line)

        # Odoo.sh-style environments: Staging/Development servers chosen at
        # checkout (counts in pending_staging_count/pending_dev_count). Billed
        # one full period upfront here; the servers are spawned on payment and
        # then ride the renewal (see _environment_order_lines).
        order_lines += self._initial_environment_order_lines(period, period_label)

        # Wallet (A4): consume any available account credit on this invoice.
        wallet_line, wallet_amount = self._wallet_credit_line(order_lines)
        if wallet_line:
            order_lines.append(wallet_line)

        # -- Create & confirm sale order --
        order_vals = {
            'partner_id': self.partner_id.id,
            'origin': ORIGIN_INITIAL % (self.name or self.subdomain),
            'order_line': order_lines,
        }
        if pricelist:
            order_vals['pricelist_id'] = pricelist.id
        order = self.env['sale.order'].create(order_vals)
        order.action_confirm()
        self.sale_order_id = order

        # -- Create & post invoice --
        invoice = order._create_invoices()
        invoice.action_post()
        self._wallet_settle_consumption(invoice, wallet_amount)

        # -- Transition state & auto-deploy --
        if invoice.amount_total <= 0:
            self.state = 'paid'
            self._set_next_invoice_date()
            self._append_log(
                "Sale order %s confirmed. Zero-amount invoice — deploying automatically."
                % order.name
            )
            self.message_post(body=_(
                "Sale order %s confirmed. No payment required — deploying now."
            ) % order.name)
            self.action_deploy()
            return True
        else:
            self.state = 'pending_payment'
            self._append_log(
                "Sale order %s confirmed. Invoice %s awaiting payment. "
                "Instance will deploy automatically once paid."
                % (order.name, invoice.name)
            )
            self.message_post(body=_(
                "Sale order %s confirmed. Invoice %s created and awaiting payment. "
                "Instance will deploy automatically once paid."
            ) % (order.name, invoice.name))

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': invoice.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_mark_as_paid(self):
        """Manually mark as paid and auto-deploy (for wire transfers, trials, etc.)."""
        self.ensure_one()
        if self.state != 'pending_payment':
            raise UserError(_("Instance must be in 'Pending Payment' state."))
        self.state = 'paid'
        # Initialise the recurring billing schedule. Without this the
        # renewal cron never picks up the instance and the customer gets
        # an indefinite free subscription.
        self._set_next_invoice_date()
        self._append_log("Manually marked as paid. Deploying automatically.")
        self.message_post(body=_("Manually marked as paid — deploying now."))
        self.action_deploy()

    def action_view_sale_order(self):
        """Open the linked sale order."""
        self.ensure_one()
        if not self.sale_order_id:
            return False
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'sale.order',
            'res_id': self.sale_order_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_view_invoices(self):
        """Open all invoices related to this instance."""
        self.ensure_one()
        invoices = self._get_all_invoices()
        if not invoices:
            return False
        if len(invoices) == 1:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'account.move',
                'res_id': invoices.id,
                'view_mode': 'form',
                'target': 'current',
            }
        return {
            'type': 'ir.actions.act_window',
            'name': _('Invoices'),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('id', 'in', invoices.ids)],
            'target': 'current',
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

    # ---- Phase 2.2: immutable tenant images (registry by SHA) -------------
    def _tenant_base_image(self):
        """Registry ref of the immutable base image this tenant builds FROM,
        e.g. ``127.0.0.1:5000/odoo-base:18.0``. Requires the server's
        ``registry_host``; returns '' when the host uses legacy source-mount."""
        self.ensure_one()
        registry = (self.docker_server_id.registry_host or '').strip().rstrip('/')
        if not registry:
            return ''
        return '%s/odoo-base:%s' % (registry, self.odoo_version_id.docker_image_tag)

    def _render_tenant_dockerfile(self):
        """Render the platform-generated Dockerfile for this tenant's immutable
        image (base + pip + custom modules). Caller assembles the matching build
        context (requirements.txt + addons/) next to it."""
        self.ensure_one()
        pip_pkgs = bool((self.pip_packages or '').strip())
        return self._render_template('Dockerfile.tenant.jinja', {
            'base_image': self._tenant_base_image(),
            'pip_packages': pip_pkgs,
            'has_addons': bool(self._get_all_addons_paths()),
        })

    def _dedup_pip_lines(self):
        """Deduplicated, comment-stripped pip requirement lines (by package key)."""
        self.ensure_one()
        seen, out = set(), []
        for p in (self.pip_packages or '').splitlines():
            p = p.strip()
            if not p or p.startswith('#'):
                continue
            key = p.lower().split('=')[0].split('<')[0].split('>')[0].split('!')[0].split('[')[0].strip()
            if key not in seen:
                seen.add(key)
                out.append(p)
        return out

    def _build_and_push_tenant_image(self, source='redeploy'):
        """Phase 2.2.4: build this tenant's immutable image (FROM odoo-base +
        custom modules + pip) and push it to the registry. Records a saas.build
        with the resulting ``image_ref``/``image_digest`` and returns it.

        The build runs in a sandboxed, credential-less worker on the build host
        (see _image_build_cmd / 2.2.5). The image tag is a content hash of the
        build context, so an identical tenant config produces an identical tag
        (cache-friendly + idempotent)."""
        self.ensure_one()
        if not self._tenant_base_image():
            raise UserError(_(
                "No container registry configured on server '%s' (set "
                "registry_host) — cannot build an immutable image.")
                % self.docker_server_id.name)
        registry = self.docker_server_id.registry_host.strip().rstrip('/')
        instance_path = self._get_instance_path()
        dockerfile = self._render_tenant_dockerfile()
        reqs = self._dedup_pip_lines()
        build = self.env['saas.build'].sudo().create({
            'instance_id': self.id, 'source': source, 'state': 'running'})
        ctx = '%s/.image-build' % instance_path
        image = ''
        try:
            with self.docker_server_id._get_ssh_connection() as ssh:
                # Assemble a clean build context: Dockerfile + requirements + addons.
                ssh.execute('rm -rf %s && mkdir -p %s/addons' % (
                    shlex.quote(ctx), shlex.quote(ctx)))
                ssh.write_file('%s/Dockerfile' % ctx, dockerfile)
                ssh.write_file('%s/requirements.txt' % ctx,
                               ('\n'.join(reqs) + '\n') if reqs else '')
                ssh.execute(
                    'if [ -d %(ip)s/addons ]; then cp -a %(ip)s/addons/. %(c)s/addons/ 2>/dev/null || true; fi'
                    % {'ip': shlex.quote(instance_path), 'c': shlex.quote(ctx)})
                # Content-addressed tag: identical context -> identical tag.
                rc, tag, err = ssh.execute(
                    'cd %s && tar --sort=name --owner=0 --group=0 --mtime=@0 -cf - . 2>/dev/null '
                    '| sha256sum | cut -c1-12' % shlex.quote(ctx))
                tag = (tag or '').strip() or 'latest'
                image = '%s/tenant-%s:%s' % (registry, self.subdomain, tag)
                # Build (sandboxed) + push.
                rc, out, err = ssh.execute(
                    self._image_build_cmd(ctx, image), timeout=1800)
                if rc != 0:
                    raise UserError(_("Image build failed:\n%s") % (out or err)[-3000:])
                rc, out, err = ssh.execute(
                    'docker push %s 2>&1' % shlex.quote(image), timeout=900)
                if rc != 0:
                    raise UserError(_("Image push failed:\n%s") % (out or err)[-1500:])
                rc, digest, err = ssh.execute(
                    "docker inspect --format '{{index .RepoDigests 0}}' %s" % shlex.quote(image))
                digest = (digest or '').strip()
        except Exception as e:
            build._mark('failed', log=str(e)[:8000])
            raise
        build.write({
            'image_ref': image,
            'image_digest': digest or image,
            'state': 'success',
            'date_done': fields.Datetime.now(),
        })
        self._append_log("Built immutable image %s (digest %s)" % (image, digest or 'n/a'))
        return build

    def _image_build_cmd(self, ctx, image):
        """Return the `docker build` command for the build worker. Sandboxing
        (2.2.5): tenant builds run untrusted customer code (requirements.txt /
        module setup.py), so the RUN steps are confined to the egress-restricted
        ``saas-build`` network (see provision-build-sandbox.sh) — PyPI + DNS
        reachable, but cloud metadata + RFC1918 internals (PG, other tenants, the
        control plane) are firewalled. No platform secrets are in the context
        (Dockerfile + requirements + addons only); the push is a separate trusted
        step so untrusted RUN never sees registry creds. The legacy builder is
        used because it honours a custom --network (BuildKit only takes
        default/none/host). The FROM pull happens on the daemon, so the local
        registry base image still resolves."""
        return (
            'DOCKER_BUILDKIT=0 docker build --network %s --pull=false '
            '-t %s %s 2>&1' % (
                shlex.quote(self._build_sandbox_network()),
                shlex.quote(image), shlex.quote(ctx)))

    def _build_sandbox_network(self):
        """Name of the egress-restricted docker network for untrusted builds."""
        return 'saas-build'

    def deploy_immutable_image(self, build=None, source='redeploy'):
        """Phase 2.2.6: build (if needed) + deploy this tenant from an immutable
        image by digest. Sets ``deploy_image``, re-renders compose, and recreates
        the container via the driver. Returns the saas.build deployed."""
        self.ensure_one()
        build = build or self._build_and_push_tenant_image(source=source)
        ref = build.image_digest or build.image_ref
        if not ref:
            raise UserError(_("Build produced no image reference."))
        self.deploy_image = ref
        with self.docker_server_id._get_ssh_connection() as ssh:
            self._render_and_write_configs(ssh)
            # pull the image, then recreate the container on it
            ssh.execute('docker pull %s 2>&1' % shlex.quote(ref), timeout=900)
            driver = self._compute_driver(connection=ssh)
            handle = self._compute_handle()
            driver.destroy(handle)
            driver.start(handle)
        self._append_log("Deployed immutable image %s" % ref)
        return build

    def rollback_image(self, build):
        """Phase 2.2.7: redeploy a PREVIOUS successful build's image (one-command
        rollback). No rebuild — just repoint ``deploy_image`` + recreate."""
        self.ensure_one()
        build.ensure_one()
        if build.state != 'success' or not (build.image_digest or build.image_ref):
            raise UserError(_("Can only roll back to a successful build with an image."))
        return self.deploy_immutable_image(build=build, source='redeploy')

    # ---- Phase 1: ComputeDriver seam (additive; see docs/architecture) ----
    def _compute_handle(self):
        """Backend-agnostic handle describing this instance's compute workload."""
        self.ensure_one()
        from ..drivers.base import ComputeHandle
        server = self.docker_server_id
        return ComputeHandle(
            server_id=server.id,
            container_name=self._get_container_name(),
            instance_path=self._get_instance_path(),
            host=server.ip_v4 or '',
            http_port=self.xmlrpc_port or 0,
        )

    def _compute_driver(self, connection=None):
        """Return the ComputeDriver for this instance's backend, selected by the
        server's ``compute_driver`` (Phase 6). Business logic NEVER changes — it
        only ever calls ``self._compute_driver().X()``; swapping the backend is a
        new driver file + this one-line switch.

        Pass an open SSHConnection as ``connection`` to reuse it (for call sites
        already inside a ``with server._get_ssh_connection()`` block)."""
        self.ensure_one()
        server = self.docker_server_id
        if server.compute_driver == 'kubernetes':
            from ..drivers.kubernetes_driver import KubernetesDriver
            return KubernetesDriver(server, connection=connection)
        from ..drivers.ssh_docker_driver import SshDockerDriver
        return SshDockerDriver(server, connection=connection)

    def _data_service(self):
        """Return the DataService (snapshot/materialize primitives)."""
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

    def _get_all_addons_paths(self):
        """Return addons paths for odoo.conf from instance and product repos.

        All repos are cloned into the addons/ dir, already mounted at
        /mnt/extra-addons — no extra volume mounts needed.
        """
        self.ensure_one()

        # Instance-level repos
        instance_repos = self.repo_ids.filtered(lambda r: r.state == 'cloned')
        addons_paths = [r._get_container_addons_path() for r in instance_repos]

        # Product-level repos
        product = self.saas_product_id
        if product:
            for pr in product.repo_ids:
                addons_paths.append(pr._get_container_addons_path())

        return addons_paths

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

    def _provision_postgresql(self, create_db=True):
        """Ensure the PostgreSQL role exists, and optionally the per-instance DB.

        Hosting instances pass ``create_db=False`` — they get an empty
        Odoo container and create databases themselves via the master
        password (or, once portal CRUD ships, via /my/instances).
        The role still gets ``CREATEDB``, which is what makes that work.
        """
        self.ensure_one()
        psql_server = self.db_server_id
        if not psql_server:
            raise UserError(_("No database server configured on this instance."))

        db_user = self.db_user
        db_password = self.db_password

        if not DB_USER_RE.match(db_user):
            raise ValidationError(
                _("Database user '%s' contains unsafe characters.") % db_user
            )

        sql_script = (
            "DO $body$\n"
            "BEGIN\n"
            "  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = %(user_lit)s) THEN\n"
            "    EXECUTE format('CREATE ROLE %%I WITH LOGIN CREATEDB PASSWORD %%L', %(user_lit)s, %(pass_lit)s);\n"
            "  ELSE\n"
            "    EXECUTE format('ALTER ROLE %%I WITH LOGIN CREATEDB PASSWORD %%L', %(user_lit)s, %(pass_lit)s);\n"
            "  END IF;\n"
            "END $body$;\n"
        ) % {
            'user_lit': "$$%s$$" % db_user,
            'pass_lit': "$$%s$$" % db_password.replace("$$", "$ $"),
        }

        ensure_role_cmd = "sudo -u postgres psql <<'SAAS_END_SQL'\n%s\nSAAS_END_SQL" % sql_script

        with psql_server._get_ssh_connection() as ssh:
            self._append_log("Ensuring PostgreSQL role '%s'..." % db_user)
            exit_code, stdout, stderr = ssh.execute(ensure_role_cmd)
            self._append_log(
                "Role command result: exit=%s stdout=%s stderr=%s"
                % (exit_code, stdout.strip(), stderr.strip())
            )
            if exit_code != 0:
                raise UserError(
                    _("Failed to create/update PostgreSQL role '%s':\n%s")
                    % (db_user, stderr)
                )

            if not create_db:
                self._append_log(
                    "Skipping database creation (hosting instance — "
                    "customer creates DBs themselves)."
                )
                return

            db_name = self.subdomain
            if not SUBDOMAIN_RE.match(db_name):
                raise ValidationError(
                    _("Subdomain '%s' contains unsafe characters for a database name.") % db_name
                )

            create_db_cmd = (
                "sudo -u postgres psql -tc "
                "\"SELECT 1 FROM pg_database WHERE datname='%(db)s'\" "
                "| grep -q 1 "
                "|| sudo -u postgres createdb -O %(user)s %(db)s"
            ) % {'db': db_name, 'user': db_user}

            self._append_log("Ensuring database '%s'..." % db_name)
            exit_code, stdout, stderr = ssh.execute(create_db_cmd)
            self._append_log(
                "DB command result: exit=%s stdout=%s stderr=%s"
                % (exit_code, stdout.strip(), stderr.strip())
            )
            if exit_code != 0:
                raise UserError(
                    _("Failed to create database '%s':\n%s")
                    % (db_name, stderr)
                )

            # PG 15+ dropped the default `GRANT ALL ON SCHEMA public TO
            # PUBLIC`, so owning the database isn't enough — we have to
            # explicitly hand the public schema to our role or Odoo's
            # init blows up creating `base_registry_signaling`.
            self._pg_grant_public_schema(ssh, db_name, db_user)

    def _pg_grant_public_schema(self, ssh, db_name, db_user):
        """Give ``db_user`` full control of ``public`` in ``db_name``.

        Always run as ``postgres`` superuser because only the schema
        owner (postgres by default) can re-assign ownership or grant
        on it. Idempotent — re-running is a no-op on PostgreSQL.
        """
        # Defense in depth: callers validate these but we re-check
        # before formatting into raw SQL.
        # Allow a leading underscore so the per-instance template
        # name (``__odoo_template_<sub>``) passes — customer-facing
        # names go through the stricter ``_DB_NAME_RE`` regex earlier.
        if not re.match(r'^[_a-z][a-z0-9_-]{0,62}$', db_name or ''):
            raise UserError(_("Invalid db name %r") % db_name)
        if not DB_USER_RE.match(db_user or ''):
            raise UserError(_("Invalid db user %r") % db_user)
        sql = (
            'ALTER SCHEMA public OWNER TO "%(u)s"; '
            'GRANT ALL ON SCHEMA public TO "%(u)s";'
        ) % {'u': db_user}
        cmd = 'sudo -u postgres psql -d %s -v ON_ERROR_STOP=1 -c %s 2>&1' % (
            shlex.quote(db_name), shlex.quote(sql),
        )
        exit_code, stdout, stderr = ssh.execute(cmd)
        if exit_code != 0:
            raise UserError(
                _("Failed to grant public schema on '%s' to '%s':\n%s")
                % (db_name, db_user, stderr or stdout)
            )

    def _pg_ensure_db_with_grants(self, db_name):
        """Createdb (if needed) and hand the role full schema rights.

        Used by ``hosting_db_create`` for per-DB portal creation, where
        the Odoo CLI would otherwise let the new DB inherit PG 15+'s
        restrictive defaults and fail at init time.
        """
        self.ensure_one()
        psql_server = self.db_server_id
        if not psql_server:
            raise UserError(_("No database server configured on this instance."))
        # Allow a leading underscore so the per-instance template
        # name (``__odoo_template_<sub>``) passes — customer-facing
        # names go through the stricter ``_DB_NAME_RE`` regex earlier.
        if not re.match(r'^[_a-z][a-z0-9_-]{0,62}$', db_name or ''):
            raise UserError(_("Invalid db name %r") % db_name)
        if not DB_USER_RE.match(self.db_user or ''):
            raise UserError(_("Invalid db user %r") % self.db_user)

        db_user = self.db_user
        create_db_cmd = (
            "sudo -u postgres psql -tc "
            "\"SELECT 1 FROM pg_database WHERE datname='%(db)s'\" "
            "| grep -q 1 "
            "|| sudo -u postgres createdb -O %(user)s %(db)s"
        ) % {'db': db_name, 'user': db_user}

        with psql_server._get_ssh_connection() as ssh:
            exit_code, stdout, stderr = ssh.execute(create_db_cmd)
            if exit_code != 0:
                raise UserError(_(
                    "Failed to create database '%s':\n%s"
                ) % (db_name, stderr or stdout))
            self._pg_grant_public_schema(ssh, db_name, db_user)

    # ------------------------------------------------------------------
    # PG-level template helpers — production path for fast DB creation.
    # The template is initialised once per instance, then every
    # customer-requested DB is a near-instant CREATE DATABASE ...
    # WITH TEMPLATE off it. Avoids the racing-Odoo-worker problems that
    # plagued the per-create-init approach.
    # ------------------------------------------------------------------
    _DB_IDENT_RE = re.compile(r'^[_a-z][a-z0-9_-]{0,62}$')

    def _pg_db_exists(self, db_name):
        """Return True if ``db_name`` exists on the instance's db server."""
        self.ensure_one()
        psql_server = self.db_server_id
        if not psql_server or not db_name:
            return False
        if not self._DB_IDENT_RE.match(db_name):
            raise UserError(_("Invalid db name %r") % db_name)
        safe = db_name.replace("'", "''")
        cmd = (
            "sudo -u postgres psql -tA -c "
            "\"SELECT 1 FROM pg_database WHERE datname='%s'\""
        ) % safe
        with psql_server._get_ssh_connection() as ssh:
            exit_code, stdout, _ = ssh.execute(cmd)
        return exit_code == 0 and stdout.strip() == '1'

    def _pg_db_initialized(self, db_name):
        """Return True iff ``db_name`` has ``base`` fully installed.

        This is the "is the template (or DB) actually usable?" check,
        run as ``postgres`` straight on the db server — so it does NOT
        depend on the instance container being up (the post-init
        verification runs right after we bring the container back, when
        ``docker compose exec`` might not be ready yet). An empty or
        half-built DB makes the inner query error on the missing
        ``ir_module_module`` table; that prints nothing to stdout, so
        the ``== '1'`` test cleanly reads as "not initialised".
        """
        self.ensure_one()
        psql_server = self.db_server_id
        if not psql_server or not self._DB_IDENT_RE.match(db_name or ''):
            return False
        sql = (
            "SELECT 1 FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname='public' AND c.relname='ir_module_module' "
            "AND EXISTS (SELECT 1 FROM ir_module_module "
            "WHERE name='base' AND state='installed')"
        )
        cmd = 'sudo -u postgres psql -d %s -tA -c %s 2>/dev/null' % (
            shlex.quote(db_name), shlex.quote(sql),
        )
        with psql_server._get_ssh_connection() as ssh:
            _exit, stdout, _err = ssh.execute(cmd, timeout=60)
        return stdout.strip() == '1'

    def _pg_clone_db(self, source, target):
        """``CREATE DATABASE target WITH TEMPLATE source OWNER <role>``.

        Postgres copies the data files at the storage layer — typically
        seconds — without running any Odoo init. ``source`` must be
        flagged ``datistemplate=true`` (or have no active connections)
        for the clone to succeed.
        """
        self.ensure_one()
        psql_server = self.db_server_id
        if not psql_server:
            raise UserError(_("No database server configured."))
        for ident in (source, target, self.db_user):
            if not ident or not self._DB_IDENT_RE.match(ident):
                raise UserError(
                    _("Refusing to clone with invalid identifier %r") % ident
                )
        sql = (
            'CREATE DATABASE "%(t)s" WITH TEMPLATE "%(s)s" OWNER "%(u)s"'
        ) % {'t': target, 's': source, 'u': self.db_user}
        cmd = 'sudo -u postgres psql -v ON_ERROR_STOP=1 -c %s 2>&1' % (
            shlex.quote(sql),
        )
        with psql_server._get_ssh_connection() as ssh:
            exit_code, stdout, stderr = ssh.execute(cmd, timeout=600)
        if exit_code != 0:
            raise UserError(_(
                "Failed to clone database from template:\n%s"
            ) % (stderr or stdout))

    def _pg_mark_template(self, db_name, flag=True):
        """Toggle ``datistemplate`` on a DB.

        Marking as a template:
        * tells Postgres it can be used as a clone source without
          requiring the absence of connections;
        * tells our Odoo workers to skip it (they don't load DBs
          where ``datistemplate=true`` because they aren't customer
          databases).
        """
        self.ensure_one()
        psql_server = self.db_server_id
        if not psql_server or not self._DB_IDENT_RE.match(db_name or ''):
            return
        safe = db_name.replace("'", "''")
        sql = (
            "UPDATE pg_database SET datistemplate=%s "
            "WHERE datname='%s'"
        ) % ('true' if flag else 'false', safe)
        cmd = 'sudo -u postgres psql -v ON_ERROR_STOP=1 -c %s 2>&1' % (
            shlex.quote(sql),
        )
        with psql_server._get_ssh_connection() as ssh:
            ssh.execute(cmd)

    def _pg_drop_db(self, db_name):
        """Drop a database (best-effort). Used to clean up half-built
        templates or failed clones."""
        self.ensure_one()
        psql_server = self.db_server_id
        if not psql_server or not self._DB_IDENT_RE.match(db_name or ''):
            return
        # PG won't drop a database flagged ``datistemplate=true`` even
        # for superuser, so clear the flag first.
        self._pg_mark_template(db_name, flag=False)
        cmd = (
            'sudo -u postgres dropdb --force --if-exists %s 2>&1'
            % shlex.quote(db_name)
        )
        with psql_server._get_ssh_connection() as ssh:
            ssh.execute(cmd, timeout=120)

    def _ensure_can_ssh(self):
        """Validate that the instance has the necessary server config for SSH."""
        self.ensure_one()
        if not self.docker_server_id:
            raise ValidationError(_("No Docker server configured."))
        server = self.docker_server_id
        if not server.ssh_key_pair_id or not server.ssh_key_pair_id._private_key_b64():
            raise ValidationError(
                _("SSH key pair with private key is required on server '%s'.")
                % server.name
            )
        server._get_ssh_ip()

    # Advisory-lock namespace for server allocation (distinct from the port
    # allocator's 0x5AA5_0001) — serializes concurrent allocations per region.
    _ALLOC_LOCK_NAMESPACE = 0x5AA5_0002

    def _allocate_servers(self):
        """Auto-allocate Docker and DB servers using a multi-level strategy.

        Respects ``provisioning_mode``:
        - **manual**: skip allocation entirely (operator assigns servers).
        - **strict**: Level 1 only — fail hard if no capacity.
        - **flexible** (default): Level 1 → Level 2 → Level 3.

        Levels (flexible mode):
            1. Ideal — least-loaded host with available capacity.
            2. Overcommit — any host with ``allow_overcommit`` enabled,
               ignoring capacity limits.
            3. Pending — no server assigned; instance enters
               ``pending_provision`` state and waits for capacity.

        Returns True if a Docker server was assigned, False if the instance
        was marked as pending (caller should abort deployment).
        """
        self.ensure_one()
        Server = self.env['saas.server']
        mode = self.provisioning_mode or 'flexible'

        # -- Manual mode: operator is responsible for server assignment --
        if mode == 'manual':
            return bool(self.docker_server_id)

        # -- Docker server allocation --
        if not self.docker_server_id:
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
                    "Allocated Docker server (strict): %s"
                    % self.docker_server_id.name
                )
            else:
                server = Server._allocate_docker_server(plan=plan, region=region)
                if server:
                    self.docker_server_id = server
                    self._append_log(
                        "Allocated Docker server (ideal): %s" % server.name
                    )
                else:
                    # Level 2 — Overcommit fallback
                    server = Server._allocate_overcommit_server(plan=plan, region=region)
                    if server:
                        self.docker_server_id = server
                        self.is_overcommitted = True
                        self._append_log(
                            "Allocated Docker server (overcommit): %s"
                            % server.name
                        )
                        _logger.warning(
                            "Instance %s allocated to overcommitted "
                            "server %s (plan: %s).",
                            self.subdomain, server.name,
                            plan.name if plan else 'none',
                        )
                    else:
                        # Level 3 — No server available → pending
                        self._mark_as_pending()
                        return False

        # -- DB server allocation --
        if not self.db_server_id and self.docker_server_id:
            self._allocate_db_server()

        return True

    def _allocate_db_server(self):
        """Derive the DB server from the Docker host topology.

        Falls back to all-in-one detection, then any DB server.
        """
        self.ensure_one()
        Server = self.env['saas.server']
        docker_srv = self.docker_server_id

        if docker_srv.db_server_id:
            self.db_server_id = docker_srv.db_server_id
        elif docker_srv.is_db_server:
            self.db_server_id = docker_srv
        else:
            # Co-location: stay within the instance's region for the
            # generic DB-server fallback (the topology branches above are
            # already pinned to the chosen docker host).
            db_srv = Server.search(
                [('is_db_server', '=', True)]
                + Server._region_match_domain(self.region_id), limit=1,
            )
            if not db_srv:
                if self.provisioning_mode == 'flexible':
                    self._mark_as_pending()
                    return
                raise ValidationError(
                    _("No database server is configured. Please set up a "
                      "DB server or configure one on the Docker host '%s'.")
                    % docker_srv.name
                )
            self.db_server_id = db_srv

        self._append_log(
            "Allocated DB server: %s" % self.db_server_id.name
        )

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

    # PostgreSQL advisory-lock namespace key for per-server port allocation.
    # Arbitrary 32-bit constant; pairs with docker_server_id to form a
    # unique 64-bit key so two servers don't block each other.
    _PORT_ALLOC_LOCK_NAMESPACE = 0x5AA5_0001

    def _auto_assign_ports(self):
        """Auto-assign xmlrpc_port and longpolling_port if not already set.

        Uses a Postgres transaction-scoped advisory lock keyed by
        docker_server_id so concurrent provisioning on the same server
        cannot pick the same port pair. The previous SELECT FOR UPDATE
        only locked rows whose `xmlrpc_port>0` — racing transactions
        with NULL ports skipped each other and both wrote the same port,
        triggering an opaque IntegrityError after every other side-effect
        (DB created, container started) had run.
        """
        self.ensure_one()
        if self.xmlrpc_port and self.longpolling_port:
            return
        server_id = self.docker_server_id.id
        if not server_id:
            raise ValidationError(_(
                "Cannot allocate ports: docker server is not set."
            ))

        starting_port = int(self.env['ir.config_parameter'].sudo().get_param(
            'saas_master.default_instance_starting_port', '32000',
        ))

        # pg_advisory_xact_lock is released automatically at COMMIT/ROLLBACK.
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (self._PORT_ALLOC_LOCK_NAMESPACE, server_id),
        )

        # Now safely scan ALL ports (including ours) — we hold the lock.
        self.env.cr.execute(
            "SELECT xmlrpc_port, longpolling_port FROM saas_instance "
            "WHERE docker_server_id = %s AND id != %s "
            "AND state NOT IN ('cancelled', 'cancelled_by_client')",
            (server_id, self.id or 0),
        )
        used_ports = set()
        for row in self.env.cr.fetchall():
            if row[0]:
                used_ports.add(row[0])
            if row[1]:
                used_ports.add(row[1])

        candidate = starting_port
        while candidate < 65535:
            if candidate not in used_ports and (candidate + 1) not in used_ports:
                break
            candidate += 2

        if candidate >= 65535:
            raise ValidationError(
                _("No available port pair found on server '%s'.")
                % self.docker_server_id.name
            )

        self.xmlrpc_port = candidate
        self.longpolling_port = candidate + 1
        # Flush so the rest of this transaction sees the assignment and
        # other sessions can see it the moment we commit.
        self.env.cr.execute(
            "UPDATE saas_instance SET xmlrpc_port=%s, longpolling_port=%s "
            "WHERE id=%s",
            (candidate, candidate + 1, self.id),
        )

    def _validate_deploy_fields(self):
        """Validate all required fields before deployment."""
        self.ensure_one()
        errors = []
        if not self.subdomain:
            errors.append(_("Subdomain is required."))
        if not self.docker_server_id:
            errors.append(_("Docker Server is required."))
        if not self.db_server_id:
            errors.append(_("Database Server is required."))
        if not self.odoo_version_id:
            errors.append(_("Odoo Version is required."))
        if not self.partner_id:
            errors.append(_("Customer is required."))
        if not self.odoo_version_id or not self.odoo_version_id.docker_image:
            errors.append(_("Docker image is not set on the selected Odoo version."))
        if not self.odoo_version_id or not self.odoo_version_id.docker_image_tag:
            errors.append(_("Docker image tag is not set on the selected Odoo version."))
        server = self.docker_server_id
        if server and (not server.ssh_key_pair_id or not server.ssh_key_pair_id._private_key_b64()):
            errors.append(_("Docker server SSH key pair with private key is required."))
        if server:
            if server.ssh_connect_using == 'private_ip' and not server.private_ip_v4:
                errors.append(_("Docker server Private IP is required (SSH is set to use Private IP)."))
            elif server.ssh_connect_using == 'public_ip' and not server.ip_v4:
                errors.append(_("Docker server Public IP address is required."))
        psql = self.db_server_id
        if psql and (not psql.ssh_key_pair_id or not psql.ssh_key_pair_id._private_key_b64()):
            errors.append(_("Database server SSH key pair with private key is required."))
        if psql:
            if psql.ssh_connect_using == 'private_ip' and not psql.private_ip_v4:
                errors.append(_("Database server Private IP is required (SSH is set to use Private IP)."))
            elif psql.ssh_connect_using == 'public_ip' and not psql.ip_v4:
                errors.append(_("Database server Public IP address is required."))
            # Cross-server PostgreSQL traffic must stay on the private
            # network — without a private IP, _get_db_host() would fall
            # back to the public IP and send database traffic (and
            # password auth) over the internet. Same-server setups don't
            # need an IP for db_host (host.docker.internal / localhost).
            if server and psql != server and not psql.private_ip_v4:
                errors.append(_(
                    "Database server '%s' needs a Private IP: it is a "
                    "different machine than the Docker server, and "
                    "cross-server database traffic must use the private "
                    "network, never a public IP."
                ) % psql.name)
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

    def action_refresh_usage(self):
        """Fetch CPU, RAM, disk, and database size for this instance."""
        for rec in self:
            rec._ensure_can_ssh()
            with rec.docker_server_id._get_ssh_connection() as ssh:
                rec._refresh_usage_with_ssh(ssh)
        return True

    def _safe_refresh_usage(self):
        """Refresh resource usage, silently ignoring errors."""
        try:
            self._ensure_can_ssh()
            with self.docker_server_id._get_ssh_connection() as ssh:
                self._refresh_usage_with_ssh(ssh)
        except Exception:
            _logger.debug(
                "Failed to refresh usage for instance %s", self.subdomain,
                exc_info=True,
            )

    def _strict_refresh_usage(self):
        """Refresh usage, raising if it cannot be measured.

        Use this from places (downgrade gate, billing) where acting on
        stale or zero data could let a customer move to a plan that
        cannot accommodate them.
        """
        self.ensure_one()
        self._ensure_can_ssh()
        with self.docker_server_id._get_ssh_connection() as ssh:
            self._refresh_usage_with_ssh(ssh)

    @api.model
    def _cron_refresh_usage(self):
        """Cron: refresh resource usage for all running instances.

        Batched: opens ONE SSH per (docker_server, db_server) per pass,
        runs ONE psql query per db_server returning every DB size at
        once. The naive per-instance loop opened 2N fresh paramiko
        handshakes per cron run; this version drops that to ~M (number
        of distinct servers).
        """
        instances = self.search([
            ('state', '=', 'running'),
            ('docker_server_id', '!=', False),
        ])
        if not instances:
            return

        # Pre-fetch all DB sizes from each db_server in one query.
        db_sizes_by_server = {}
        for db_server in instances.mapped('db_server_id'):
            if not db_server:
                continue
            subdomains = [
                i.subdomain for i in instances
                if i.db_server_id == db_server
                and i.subdomain
                and SUBDOMAIN_RE.match(i.subdomain)
            ]
            if not subdomains:
                continue
            try:
                db_sizes_by_server[db_server.id] = \
                    db_server._fetch_database_sizes(subdomains)
            except Exception:
                _logger.exception(
                    "Cron: failed to batch-fetch DB sizes from server %s",
                    db_server.name,
                )
                db_sizes_by_server[db_server.id] = {}

        # Group by docker_server and refresh in one SSH session each.
        for docker_server in instances.mapped('docker_server_id'):
            server_instances = instances.filtered(
                lambda i: i.docker_server_id == docker_server
            )
            try:
                with docker_server._get_ssh_connection() as ssh:
                    for instance in server_instances:
                        try:
                            db_sizes = db_sizes_by_server.get(
                                instance.db_server_id.id, {}
                            )
                            instance._refresh_usage_with_ssh(
                                ssh,
                                precomputed_db_size=db_sizes.get(instance.subdomain),
                            )
                            self.env.cr.commit()
                        except Exception:
                            self.env.cr.rollback()
                            _logger.exception(
                                "Cron: failed to refresh usage for %s",
                                instance.subdomain,
                            )
            except Exception:
                _logger.exception(
                    "Cron: cannot reach docker server %s for usage refresh",
                    docker_server.name,
                )

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

    def _refresh_usage_with_ssh(self, ssh, precomputed_db_size=None):
        """Fetch resource usage relative to plan limits.

        CPU and RAM are reported as percentages of the plan's allocated
        resources (not the physical server), giving the client a clear
        picture of how much of *their* allocation they are consuming.

        When *precomputed_db_size* is provided (in bytes), skip the
        per-instance SSH to the DB server — the caller has already
        batched that query.
        """
        self.ensure_one()
        container_name = self._get_container_name()
        instance_path = self._get_instance_path()
        plan = self.plan_id

        # -- Plan limits --
        plan_cpu = plan.cpu_limit if plan else 0
        plan_ram_bytes = self._parse_ram_string(plan.ram_limit) if plan else 0
        plan_storage_gb = plan.storage_limit if plan else 0

        # -- Fetch container stats via the ComputeDriver (reuse this ssh) --
        _stats = self._compute_driver(connection=ssh).stats(self._compute_handle())
        exit_code, stdout, stderr = _stats.rc, _stats.stdout, _stats.stderr

        raw_cpu_pct = 0.0  # CPU % relative to host (e.g. 150% = 1.5 cores)
        ram_used_bytes = 0
        if exit_code == 0 and stdout.strip():
            parts = stdout.strip().split('||')
            # Parse CPU % (relative to host total CPUs)
            if len(parts) >= 1:
                try:
                    raw_cpu_pct = float(parts[0].strip().replace('%', ''))
                except (ValueError, TypeError):
                    pass
            # Parse RAM used from "XXMiB / YYMiB" or "XXGiB / YYGiB"
            if len(parts) >= 2:
                mem_parts = parts[1].strip().split('/')
                if mem_parts:
                    ram_used_bytes = self._parse_mem_value(mem_parts[0].strip())

        # -- Calculate CPU % relative to plan limit --
        # docker stats reports CPU% relative to ALL host cores.
        # E.g. on 8-core host using 1 core = 12.5%.
        # We need to convert: cores_used = raw_cpu_pct / 100
        # Then: plan_cpu_pct = (cores_used / plan_cpu) * 100
        cpu_pct = 0.0
        if plan_cpu > 0 and raw_cpu_pct > 0:
            cores_used = raw_cpu_pct / 100.0
            cpu_pct = min((cores_used / plan_cpu) * 100, 999)
        self.cpu_usage = '%.1f%%' % cpu_pct if cpu_pct else '0%'
        self.cpu_usage_pct = round(cpu_pct, 1)

        # -- Calculate RAM % relative to plan limit --
        ram_pct = 0.0
        if plan_ram_bytes > 0 and ram_used_bytes > 0:
            ram_pct = min((ram_used_bytes / plan_ram_bytes) * 100, 999)
        ram_used_str = self._format_bytes(ram_used_bytes) if ram_used_bytes else '0'
        ram_limit_str = plan.ram_limit.upper() if plan and plan.ram_limit else '?'
        self.ram_usage = '%s / %s' % (ram_used_str, ram_limit_str)
        self.ram_percent = '%.1f%%' % ram_pct if ram_pct else '0%'
        self.ram_usage_pct = round(ram_pct, 1)

        # -- Fetch disk usage of the instance folder (in bytes) --
        disk_cmd = 'du -sb %s 2>/dev/null | cut -f1' % shlex.quote(instance_path)
        exit_code, stdout, stderr = ssh.execute(disk_cmd)
        disk_bytes = 0
        if exit_code == 0 and stdout.strip():
            try:
                disk_bytes = int(stdout.strip())
            except (ValueError, TypeError):
                pass
        self.disk_usage = self._format_bytes(disk_bytes) if disk_bytes else ''

        # -- Fetch database size from PostgreSQL server (in bytes) --
        # Use the precomputed batched value when the caller (cron) has
        # already done the work; otherwise fall back to a per-instance
        # SSH to the DB host.
        db_bytes = 0
        if precomputed_db_size is not None:
            db_bytes = int(precomputed_db_size)
        elif self.db_server_id and self.subdomain and SUBDOMAIN_RE.match(self.subdomain):
            # Sum ALL of this customer's databases (the base <subdomain> DB
            # plus every <subdomain>_* DB) — a hosting customer can own
            # several, and they all count toward the storage allowance.
            try:
                db_bytes = self.db_server_id._fetch_database_sizes(
                    [self.subdomain],
                ).get(self.subdomain, 0)
            except Exception:
                _logger.warning(
                    "Failed to fetch DB size for instance %s", self.subdomain,
                )
        self.db_size = self._format_bytes(db_bytes) if db_bytes else ''

        # -- Total storage = instance data + db + HALF the snapshot footprint --
        # Snapshots NEVER consume the plan storage allowance: they are a
        # separate paid add-on billed per GB of their own footprint —
        # counting them here too would double-charge the customer.
        total_bytes = disk_bytes + db_bytes
        self.total_storage = self._format_bytes(total_bytes) if total_bytes else ''
        self.total_storage_bytes = total_bytes

        storage_pct = 0.0
        if plan_storage_gb > 0 and total_bytes > 0:
            storage_pct = (total_bytes / (plan_storage_gb * 1024**3)) * 100
        self.storage_usage_pct = round(storage_pct, 1)

        self.usage_last_updated = fields.Datetime.now()

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
    # Live metrics: cheap-poll heartbeat + decoupled, batched sampler
    # ------------------------------------------------------------------
    def _touch_metrics_watch(self):
        """Mark this instance as actively watched (called from the cheap
        poll endpoint) and, if the cached sample is stale, kick a one-off
        background measurement so the first paint is live without waiting
        for the next sampler tick."""
        self.ensure_one()
        now = fields.Datetime.now()
        # Extend the watch window, but only write when it would actually
        # change meaningfully — dedups writes across many concurrent
        # viewers polling the same instance.
        cur = self.metrics_watch_until
        if not cur or cur < now + datetime.timedelta(
            seconds=LIVE_METRICS_WATCH_TTL - 10,
        ):
            self.sudo().metrics_watch_until = now + datetime.timedelta(
                seconds=LIVE_METRICS_WATCH_TTL,
            )
        if self.state != 'running' or not self.docker_server_id:
            return
        last = self.usage_last_updated
        stale = (not last) or (now - last).total_seconds() > LIVE_METRICS_SEED_STALE
        if stale:
            self._maybe_seed_live_sample()

    def _maybe_seed_live_sample(self):
        """Spawn at most one background sample per instance per ~8s (per
        worker process) to cover the sampler's cold-start gap."""
        self.ensure_one()
        with _LIVE_SAMPLE_SEED_GUARD:
            mono = time.monotonic()
            if mono - _LIVE_SAMPLE_SEED_AT.get(self.id, 0.0) < 8:
                return
            _LIVE_SAMPLE_SEED_AT[self.id] = mono
        run_in_background(
            self.sudo(), '_sample_live_metrics_once',
            thread_name='saas_live_seed_%s' % self.id,
        )

    def _sample_live_metrics_once(self):
        """Background one-shot live sample for a single instance."""
        self.ensure_one()
        if not self.docker_server_id:
            return
        try:
            self._sample_live_metrics_for_host(self.docker_server_id.sudo())
            self.env.cr.commit()
        except Exception:
            _logger.warning(
                "One-shot live metrics sample failed for %s", self.subdomain,
            )

    def _sample_live_metrics_for_host(self, server):
        """Measure CPU/RAM for ALL instances in ``self`` (which must share
        ``server``) in a SINGLE ``docker stats`` call, and write the
        plan-relative percentages onto each record.

        This is the cost-scaling core: one SSH + one docker call per host
        covers every watched container on it, regardless of how many
        people are looking. Only CPU/RAM (cheap) — storage/DB stay on the
        10-minute full-refresh cron.
        """
        names = {}
        for inst in self:
            try:
                names[inst._get_container_name()] = inst
            except Exception:
                continue
        if not names:
            return
        # Host-batch sampler: ONE docker stats over every watched container on
        # this host, via the driver (the cost-scaling core — one SSH per host).
        from ..drivers.ssh_docker_driver import SshDockerDriver
        res = SshDockerDriver(server).stats_many(list(names))
        exit_code, stdout = res.rc, res.stdout
        if exit_code != 0 or not stdout:
            return
        now = fields.Datetime.now()
        for line in stdout.splitlines():
            parts = line.strip().split('||')
            if len(parts) < 3:
                continue
            inst = names.get(parts[0].strip())
            if not inst:
                continue
            try:
                raw_cpu = float(parts[1].replace('%', '').strip())
            except (ValueError, TypeError):
                raw_cpu = 0.0
            ram_used = self._parse_mem_value(parts[2].split('/')[0].strip())
            plan = inst.plan_id
            plan_cpu = plan.cpu_limit if plan else 0
            plan_ram = self._parse_ram_string(plan.ram_limit) if (
                plan and plan.ram_limit) else 0
            cpu_pct = 0.0
            if plan_cpu and raw_cpu > 0:
                cpu_pct = min((raw_cpu / 100.0) / plan_cpu * 100, 999)
            ram_pct = 0.0
            if plan_ram and ram_used > 0:
                ram_pct = min(ram_used / plan_ram * 100, 999)
            inst.cpu_usage_pct = round(cpu_pct, 1)
            inst.cpu_usage = '%.1f%%' % cpu_pct if cpu_pct else '0%'
            inst.ram_usage_pct = round(ram_pct, 1)
            inst.usage_last_updated = now

    @api.model
    def _cron_sample_live_metrics(self):
        """Continuously sample watched instances for ~50s, then exit (the
        1-minute cron re-enters). A Postgres advisory lock guarantees a
        single sampler across all workers/processes. Exits immediately
        when nobody is watching, so idle cost is ~zero."""
        self.env.cr.execute(
            "SELECT pg_try_advisory_lock(%s)", [_LIVE_METRICS_LOCK_KEY],
        )
        if not self.env.cr.fetchone()[0]:
            return  # another sampler already running
        try:
            start = time.monotonic()
            while time.monotonic() - start < LIVE_METRICS_SAMPLER_MAX_RUN:
                now = fields.Datetime.now()
                watched = self.search([
                    ('state', '=', 'running'),
                    ('metrics_watch_until', '>', now),
                    ('docker_server_id', '!=', False),
                ])
                if not watched:
                    break
                by_host = {}
                for inst in watched:
                    by_host.setdefault(inst.docker_server_id, self.browse())
                    by_host[inst.docker_server_id] |= inst
                for server, insts in by_host.items():
                    try:
                        insts._sample_live_metrics_for_host(server.sudo())
                    except Exception:
                        _logger.warning(
                            "Live metrics sampling failed on host %s",
                            server.id,
                        )
                # Commit each tick so the cheap poll endpoint (separate
                # transaction) sees fresh values immediately.
                self.env.cr.commit()
                time.sleep(LIVE_METRICS_SAMPLE_INTERVAL)
        finally:
            self.env.cr.execute(
                "SELECT pg_advisory_unlock(%s)", [_LIVE_METRICS_LOCK_KEY],
            )

    # ===== Per-customer metrics history (Odoo.sh-style, 14-day retention) =====
    @api.model
    def _cron_record_metrics(self):
        """Persist a performance sample for every running tenant (CPU/RAM/storage)
        into ``saas.instance.metric`` for the customer dashboard.

        Cheap + cost-scaling: ONE `docker stats` per host (reuses
        ``_sample_live_metrics_for_host``, which refreshes cpu/ram via the
        ComputeDriver), regardless of how many people are looking. Runs on a
        steady 5-minute cron so history exists even when nobody is watching live.
        Storage uses the latest value from the 10-min usage refresh (slow-moving).
        """
        instances = self.search([
            ('state', '=', 'running'), ('docker_server_id', '!=', False)])
        if not instances:
            return 0
        by_host = {}
        for inst in instances:
            by_host.setdefault(inst.docker_server_id, self.browse())
            by_host[inst.docker_server_id] |= inst
        now = fields.Datetime.now()
        Metric = self.env['saas.instance.metric'].sudo()
        rows = []
        for server, insts in by_host.items():
            try:
                insts._sample_live_metrics_for_host(server.sudo())
            except Exception:
                _logger.warning("metrics: host-batch sample failed on %s", server.id)
                continue
            for inst in insts:
                rows.append({
                    'instance_id': inst.id, 'ts': now,
                    'cpu_pct': inst.cpu_usage_pct or 0.0,
                    'ram_pct': inst.ram_usage_pct or 0.0,
                    'storage_mb': round((inst.total_storage_bytes or 0.0) / (1024 ** 2), 2),
                    'storage_pct': inst.storage_usage_pct or 0.0,
                })
        if rows:
            Metric.create(rows)
            self.env.cr.commit()
        return len(rows)

    @api.model
    def _cron_prune_metrics(self):
        """Enforce the retention window: drop samples older than 14 days."""
        from .saas_instance_metric import METRIC_RETENTION_DAYS
        self.env.cr.execute(
            "DELETE FROM saas_instance_metric WHERE ts < (now() at time zone 'UTC') "
            "- (%s || ' days')::interval", [METRIC_RETENTION_DAYS])
        return self.env.cr.rowcount

    def _get_metric_series(self, hours=24, max_points=240):
        """Return a downsampled metric series for THIS instance over the last
        ``hours`` hours, averaged into ≤ ``max_points`` time buckets (so a 14-day
        payload stays small). Shape: {'samples':[{t,cpu,ram,storage,storage_pct}], …}.
        Tenant isolation is the caller's job (only call for an owned instance)."""
        self.ensure_one()
        from .saas_instance_metric import METRIC_RETENTION_DAYS
        hours = max(1, min(int(hours or 24), METRIC_RETENTION_DAYS * 24))
        bucket_s = max(60, int(hours * 3600 / max(1, max_points)))
        self.env.cr.execute(
            """
            SELECT to_timestamp(floor(extract(epoch from ts) / %(b)s) * %(b)s) AT TIME ZONE 'UTC' AS bucket,
                   round(avg(cpu_pct)::numeric, 1)     AS cpu,
                   round(avg(ram_pct)::numeric, 1)     AS ram,
                   round(avg(storage_mb)::numeric, 2)  AS storage_mb,
                   round(avg(storage_pct)::numeric, 1) AS storage_pct
            FROM saas_instance_metric
            WHERE instance_id = %(iid)s
              AND ts >= (now() at time zone 'UTC') - (%(h)s || ' hours')::interval
            GROUP BY bucket ORDER BY bucket
            """,
            {'b': bucket_s, 'iid': self.id, 'h': hours},
        )
        samples = [{
            't': row[0].isoformat() + 'Z',
            'cpu': float(row[1] or 0.0),
            'ram': float(row[2] or 0.0),
            'storage_mb': float(row[3] or 0.0),
            'storage_pct': float(row[4] or 0.0),
        } for row in self.env.cr.fetchall()]
        plan = self.plan_id
        return {
            'instance': self.subdomain,
            'hours': hours,
            'bucket_seconds': bucket_s,
            'retention_days': METRIC_RETENTION_DAYS,
            'plan': {
                'cpu_limit': plan.cpu_limit if plan else 0,
                'ram_limit': plan.ram_limit if plan else '',
                'storage_limit_gb': plan.storage_limit if plan else 0,
            },
            'samples': samples,
        }

    @staticmethod
    def _parse_mem_value(mem_str):
        """Parse docker stats memory value like '152.4MiB' or '1.5GiB' into bytes."""
        if not mem_str:
            return 0
        mem_str = mem_str.strip().lower()
        multipliers = {
            'kib': 1024, 'mib': 1024**2, 'gib': 1024**3, 'tib': 1024**4,
            'kb': 1000, 'mb': 1000**2, 'gb': 1000**3, 'tb': 1000**4,
            'b': 1,
        }
        for suffix, mult in sorted(multipliers.items(), key=lambda x: -len(x[0])):
            if mem_str.endswith(suffix):
                try:
                    return float(mem_str[:-len(suffix)]) * mult
                except (ValueError, TypeError):
                    return 0
        try:
            return float(mem_str)
        except (ValueError, TypeError):
            return 0

    # ========== Config Rendering (DRY helper) ==========

    def _render_and_write_configs(self, ssh):
        """Render docker-compose.yml and odoo.conf from templates and write them via SSH."""
        self.ensure_one()
        instance_path = self._get_instance_path()
        all_addons_paths = self._get_all_addons_paths()

        # docker-compose.yml
        self._append_log("Writing docker-compose.yml...")
        # When the proxy is on a different machine, bind the published
        # ports to the Docker host's private interface so the remote
        # proxy can reach the container without exposing the instance on
        # the public internet (Docker-published ports bypass ufw, so a
        # 0.0.0.0 binding is world-reachable regardless of firewall
        # rules). 0.0.0.0 is the last-resort fallback when no private IP
        # is configured; same-machine proxy keeps loopback.
        proxy_server = self.domain_id.proxy_server_id
        needs_remote_access = proxy_server and proxy_server != self.docker_server_id
        if needs_remote_access:
            host_ip = self.docker_server_id.private_ip_v4 or '0.0.0.0'
        else:
            host_ip = '127.0.0.1'
        # Compute memory limits from plan RAM
        plan = self.plan_id
        ram_limit_str = plan.ram_limit if plan else ''
        ram_bytes = self._parse_ram_string(ram_limit_str)
        cpu_limit = plan.cpu_limit if plan else 0
        workers = plan.workers if plan else 2

        # Odoo memory limits per worker:
        #   soft = RAM / max(workers, 1) — recycle after current request
        #   hard = soft * 1.3 — kill worker immediately (last resort)
        # Docker limit = plan RAM * 1.3 — safety net above Odoo hard limit
        if ram_bytes and workers:
            limit_memory_soft = int(ram_bytes / max(workers, 1))
            limit_memory_hard = int(limit_memory_soft * 1.3)
            docker_mem_bytes = int(ram_bytes * 1.3)
        elif ram_bytes:
            limit_memory_soft = int(ram_bytes * 0.8)
            limit_memory_hard = int(ram_bytes)
            docker_mem_bytes = int(ram_bytes * 1.3)
        else:
            limit_memory_soft = 2684354560   # 2.5 GB default
            limit_memory_hard = 3355443200   # ~3.1 GB default
            docker_mem_bytes = 0             # no Docker limit

        # Per-instance overrides take priority over plan-computed values.
        auto_mem = '%dm' % (docker_mem_bytes // (1024 * 1024)) if docker_mem_bytes else ''
        docker_cpu = self.override_docker_cpu.strip() if self.override_docker_cpu else str(cpu_limit) if cpu_limit else ''
        docker_mem = self.override_docker_mem.strip() if self.override_docker_mem else auto_mem
        docker_swap = self.override_docker_swap.strip() if self.override_docker_swap else docker_mem

        # Requirements come from the connected repo(s)' requirements.txt — the
        # branch is the source of truth. Legacy fallback: the manual pip_packages
        # field, ONLY when no repo ships a requirements.txt.
        req_text = self._collect_repo_requirements(ssh)
        if not req_text.strip() and self.pip_packages:
            seen = set()
            unique_pkgs = []
            for p in self.pip_packages.splitlines():
                p = p.strip()
                if not p or p.startswith('#'):
                    continue
                key = p.lower().split('=')[0].split('<')[0].split('>')[0].split('!')[0].split('[')[0].strip()
                if key not in seen:
                    seen.add(key)
                    unique_pkgs.append(p)
            req_text = ('\n'.join(unique_pkgs) + '\n') if unique_pkgs else ''
        has_requirements = bool(req_text.strip())

        dc_context = {
            'odoo_image': self.odoo_version_id.docker_image,
            'odoo_version': self.odoo_version_id.docker_image_tag,
            'subdomain': self.subdomain,
            'host_ip': host_ip,
            'xmlrpc_port': self.xmlrpc_port,
            'longpolling_port': self.longpolling_port,
            'network_name': 'net_%s' % self.subdomain,
            'docker_cpu': docker_cpu,
            'docker_mem': docker_mem,
            'docker_swap': docker_swap,
            'pip_packages': has_requirements,
            'filestore_mount': self._get_filestore_mount(),
            # Phase 2.2: immutable image deploy (everything baked → no
            # source/addons/pip mounts). Empty = legacy source-mount mode.
            'tenant_image': (self.deploy_image or '').strip(),
        }
        dc_content = self._render_template('docker-compose.yml.jinja', dc_context)
        ssh.write_file('%s/docker-compose.yml' % instance_path, dc_content)
        self._append_log("docker-compose.yml written.")

        # Write requirements.txt (+ the pip entrypoint script) from the
        # repo-resolved requirements above.
        if has_requirements:
            ssh.write_file('%s/requirements.txt' % instance_path, req_text)
            pip_script = self._render_template('pip_install.sh', {})
            ssh.write_file('%s/pip_install.sh' % instance_path, pip_script)
            ssh.execute('chmod +x %s/pip_install.sh' % instance_path)
        else:
            ssh.write_file('%s/requirements.txt' % instance_path, '')

        # odoo.conf
        self._append_log("Writing odoo.conf...")
        psql_server = self.db_server_id
        db_host = self._get_db_host()
        extra_config = self._parse_extra_config()
        # Collect keys the admin has overridden so the template can
        # skip the auto-generated lines and avoid duplicates.
        override_keys = set(extra_config.keys()) if extra_config else set()
        # Phase 2.2: in immutable mode the custom modules are baked at
        # /opt/tenant-addons (not the mounted /mnt/extra-addons), so repo addons
        # paths are re-rooted there.
        immutable = bool((self.deploy_image or '').strip())
        conf_addons_paths = all_addons_paths
        if immutable:
            conf_addons_paths = [
                p.replace('/mnt/extra-addons', '/opt/tenant-addons')
                for p in all_addons_paths]
        conf_context = {
            'master_pass': self.admin_password,
            'db_host': db_host,
            'db_port': psql_server.psql_port or 5432,
            'db_user': self.db_user,
            'db_password': self.db_password,
            'proxy_mode': True,
            'workers': workers,
            'limit_memory_soft': limit_memory_soft,
            'limit_memory_hard': limit_memory_hard,
            'extra_config': extra_config,
            'override_keys': override_keys,
            'repo_addons_paths': conf_addons_paths,
            'immutable': immutable,
        }
        conf_content = self._render_template('odoo.conf.jinja', conf_context)
        ssh.write_file('%s/config/odoo.conf' % instance_path, conf_content)
        self._append_log("odoo.conf written.")

    # ========== Snapshot Restore Helper ==========

    def _restore_snapshot(self, ssh):
        """Download and restore a pre-built database snapshot from cloud storage.

        The snapshot zip is expected to contain:
        - dump.sql — PostgreSQL plain-text dump
        - filestore/ — Odoo filestore directory

        Returns True if a snapshot was restored, False if none configured.
        """
        self.ensure_one()
        product = self.saas_product_id
        if not product or not product.backup_bucket_path:
            self._append_log("No snapshot configured — starting with empty database.")
            return False

        instance_path = self._get_instance_path()
        db_name = self.subdomain

        # Generate presigned URL for the snapshot
        self._append_log(
            "Generating download URL for snapshot at %s..."
            % product.backup_bucket_path
        )
        download_url = product._generate_snapshot_download_url()

        tmp_zip = '%s/snapshot.zip' % instance_path
        extract_dir = '%s/snapshot_extract' % instance_path

        # 1. Download snapshot
        self._append_log("Downloading snapshot...")
        dl_cmd = 'curl -fsSL -o %s %s 2>&1' % (
            shlex.quote(tmp_zip), shlex.quote(download_url),
        )
        exit_code, stdout, stderr = ssh.execute(dl_cmd, timeout=600)
        if exit_code != 0:
            raise UserError(
                _("Failed to download snapshot:\n%s\n%s") % (stdout, stderr)
            )
        self._append_log("Snapshot downloaded.")

        # 2. Extract the zip
        self._append_log("Extracting snapshot...")
        ssh.execute('mkdir -p %s' % shlex.quote(extract_dir))
        extract_cmd = 'python3 -m zipfile -e %s %s 2>&1' % (
            shlex.quote(tmp_zip), shlex.quote(extract_dir),
        )
        exit_code, stdout, stderr = ssh.execute(extract_cmd, timeout=300)
        if exit_code != 0:
            raise UserError(
                _("Failed to extract snapshot:\n%s\n%s") % (stdout, stderr)
            )
        self._append_log("Snapshot extracted.")

        # 3. Restore dump.sql into the database
        dump_path = '%s/dump.sql' % extract_dir
        self._append_log("Restoring database from dump.sql into %s..." % db_name)

        psql_server = self.db_server_id
        db_host = self._get_db_host_for_ssh()
        db_port = psql_server.psql_port or 5432

        # Use psql on the Docker server to restore — connect to the DB server
        restore_cmd = (
            'PGPASSWORD=%s psql -h %s -p %d -U %s -d %s -f %s 2>&1'
        ) % (
            shlex.quote(self.db_password),
            shlex.quote(db_host),
            db_port,
            shlex.quote(self.db_user),
            shlex.quote(db_name),
            shlex.quote(dump_path),
        )
        exit_code, stdout, stderr = ssh.execute(restore_cmd, timeout=600)
        if exit_code != 0:
            self._append_log("Restore output:\n%s" % stdout[-2000:])
            raise UserError(
                _("Database restore failed:\n%s\n%s")
                % (stdout[-500:], stderr[-500:])
            )
        self._append_log("Database restored successfully.")

        # 4. Place filestore
        filestore_src = '%s/filestore' % extract_dir
        filestore_dst = '%s/data/odoo/filestore/%s' % (instance_path, db_name)
        data_dir = '%s/data' % instance_path
        self._append_log("Placing filestore...")
        odoo_image = self.odoo_version_id._get_docker_image()
        fs_cmd = (
            'mkdir -p %(dst)s && '
            'if [ -d %(src)s ]; then '
            '  cp -a %(src)s/. %(dst)s/; '
            'fi && '
            'sudo chmod -R 777 %(data)s'
        ) % {
            'dst': shlex.quote(filestore_dst),
            'src': shlex.quote(filestore_src),
            'data': shlex.quote(data_dir),
        }
        exit_code, stdout, stderr = ssh.execute(fs_cmd, timeout=300)
        if exit_code != 0:
            self._append_log("Warning: filestore placement issue: %s" % stderr)
        else:
            self._append_log("Filestore placed.")

        # 5. Cleanup temp files
        ssh.execute('rm -rf %s %s' % (
            shlex.quote(tmp_zip), shlex.quote(extract_dir),
        ))
        self._append_log("Snapshot temp files cleaned up.")

        return True

    def _validate_addons_manifests(self, ssh):
        """Check that every ``__manifest__.py`` under the instance addons is valid.

        Scans the instance's ``addons/`` directory on the host (which is
        mounted into the container).  If any manifest cannot be parsed by
        ``ast.literal_eval``, the deploy is aborted with a clear error
        listing the broken modules so the client can fix them.
        """
        self.ensure_one()
        instance_path = self._get_instance_path()
        addons_dir = '%s/addons' % instance_path
        script_path = '%s/_validate_manifests.py' % instance_path
        ssh.write_file(script_path, (
            "import ast, os, sys\n"
            "errors = []\n"
            "addons_dir = sys.argv[1]\n"
            "for root, dirs, files in os.walk(addons_dir):\n"
            "    if '__manifest__.py' in files:\n"
            "        path = os.path.join(root, '__manifest__.py')\n"
            "        try:\n"
            "            with open(path) as f:\n"
            "                ast.literal_eval(f.read())\n"
            "        except Exception as e:\n"
            "            errors.append('%s: %s' % (os.path.basename(root), e))\n"
            "if errors:\n"
            "    for err in errors:\n"
            "        print(err)\n"
            "    sys.exit(1)\n"
        ))
        self._append_log("Validating module manifests...")
        exit_code, stdout, stderr = ssh.execute(
            'python3 %s %s 2>&1' % (
                shlex.quote(script_path), shlex.quote(addons_dir),
            ),
            timeout=120,
        )
        ssh.execute('rm -f %s' % shlex.quote(script_path))
        if exit_code != 0 and stdout.strip():
            raise UserError(
                _("Custom repository contains modules with invalid manifests. "
                  "Please fix the following modules and try again:\n%s")
                % stdout.strip()
            )

    def _update_repo_submodules(self, ssh, repo_dir):
        """Init/update a repo's git submodules (Odoo.sh-style).

        A customer repo may pull in addons via ``.gitmodules``; fetch
        them so the submodule code lands in the addons path — exactly
        like Odoo.sh, with no UI step. No-op when the repo has no
        submodules.

        **Non-fatal by design:** a submodule that can't be fetched (e.g.
        a private absolute URL without credentials) logs a warning but
        never aborts the deploy — the customer's instance still comes up.
        Submodules with relative URLs inherit the parent's token auth.
        """
        self.ensure_one()
        # Skip silently if there are no submodules.
        code, _out, _err = ssh.execute(
            'test -f %s' % shlex.quote('%s/.gitmodules' % repo_dir),
        )
        if code != 0:
            return
        self._append_log("Initializing git submodules...")
        cmd = (
            'cd %s && git submodule sync --recursive 2>&1 && '
            'git submodule update --init --recursive 2>&1'
        ) % shlex.quote(repo_dir)
        code, stdout, stderr = ssh.execute(cmd, timeout=600)
        if code != 0:
            self._append_log(
                "Submodule init reported an issue (continuing without "
                "it):\n%s" % ((stdout or '') + (stderr or ''))[-400:]
            )
        else:
            self._append_log("Submodules initialized.")

    def _clone_product_repos(self, ssh):
        """Clone the product's GitHub repositories into the instance directory."""
        self.ensure_one()
        product = self.saas_product_id
        if not product or not product.repo_ids:
            return

        instance_path = self._get_instance_path()
        container_uid = self._get_container_uid(ssh)

        for repo in product.repo_ids:
            repo_dir = '%s/addons/%s' % (
                instance_path, repo._get_repo_dir_name(),
            )
            clone_url = repo._get_clone_url()

            self._append_log(
                "Cloning product repo %s (branch: %s)..." % (repo.repo_url, repo.branch)
            )
            ssh.execute('mkdir -p %s' % shlex.quote(
                '%s/addons' % instance_path
            ))
            # Remove existing if re-cloning
            ssh.execute('rm -rf %s' % shlex.quote(repo_dir))

            clone_cmd = (
                'git clone --branch %s --single-branch '
                '--depth 1 %s %s 2>&1'
            ) % (
                shlex.quote(repo.branch),
                shlex.quote(clone_url),
                shlex.quote(repo_dir),
            )
            exit_code, stdout, stderr = ssh.execute(clone_cmd, timeout=300)
            if exit_code != 0:
                raise UserError(
                    _("Failed to clone product repo '%s':\n%s\n%s")
                    % (repo.repo_url, stdout[-500:], stderr[-500:])
                )
            # Pull in any git submodules before fixing ownership so the
            # submodule files are chowned to the container user too.
            self._update_repo_submodules(ssh, repo_dir)
            ssh.execute(
                'sudo chown -R %s:%s %s && sudo chmod -R 777 %s'
                % (container_uid, container_uid,
                   shlex.quote(repo_dir), shlex.quote(repo_dir))
            )
            self._append_log("Repository %s cloned." % repo.name)

    def _pull_product_repos(self, ssh):
        """Pull latest changes for the product's repositories."""
        self.ensure_one()
        product = self.saas_product_id
        if not product or not product.repo_ids:
            return

        instance_path = self._get_instance_path()

        for repo in product.repo_ids:
            repo_dir = '%s/addons/%s' % (
                instance_path, repo._get_repo_dir_name(),
            )
            clone_url = repo._get_clone_url()

            # Check if repo dir exists (already cloned)
            exit_code, _, _ = ssh.execute(
                'test -d %s' % shlex.quote(repo_dir)
            )
            if exit_code != 0:
                # Not cloned yet — clone it
                self._clone_product_repos(ssh)
                return

            ssh.execute(
                'cd %s && git remote set-url origin %s'
                % (shlex.quote(repo_dir), shlex.quote(clone_url))
            )
            self._append_log("Pulling product repo %s..." % repo.name)
            pull_cmd = 'cd %s && git pull origin %s 2>&1' % (
                shlex.quote(repo_dir), shlex.quote(repo.branch),
            )
            exit_code, stdout, stderr = ssh.execute(pull_cmd, timeout=300)
            if exit_code != 0:
                raise UserError(
                    _("Git pull failed for product repo '%s':\n%s\n%s")
                    % (repo.name, stdout[-500:], stderr[-500:])
                )
            # Keep submodules in sync with the pulled commit, then fix
            # ownership so the container can read any newly fetched files.
            self._update_repo_submodules(ssh, repo_dir)
            try:
                container_uid = self._get_container_uid(ssh)
                ssh.execute(
                    'sudo chown -R %s:%s %s && sudo chmod -R 777 %s'
                    % (container_uid, container_uid,
                       shlex.quote(repo_dir), shlex.quote(repo_dir))
                )
            except Exception:
                pass
            self._append_log(
                "Pulled %s: %s" % (repo.name, stdout.strip()[:200])
            )

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

            rec._auto_assign_ports()
            if not rec.deploy_retry_count:
                rec.provisioning_log = ''
            rec.pre_provisioning_state = 'failed'
            rec.pending_operation = 'deploy'
            rec.state = 'provisioning'
            rec._append_log(
                "Deployment queued (attempt %d). Running in background..."
                % (rec.deploy_retry_count + 1)
            )
            # Durable queue OWNS deploy retries + crash recovery (ARCH-004 retry
            # cutover): the queue retries _do_deploy up to max_deploy_retries
            # with back-off (instance stays 'provisioning' across attempts), and
            # because the deploy is idempotent (re-runnable — mkdir -p, template
            # clone is guarded, etc.) the reaper requeues it if a worker dies.
            # _on_background_error fires only on TERMINAL failure → 'failed'.
            # No secret args. (Capacity-waiting still flows through
            # pending_provision + _cron_retry_pending_provision; per-host death
            # is moot — _allocate_servers keeps the same host once assigned.)
            self.env['saas.job']._enqueue(
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
        """Internal deploy logic for a single record."""
        self.ensure_one()

        server = self.docker_server_id
        instance_path = self._get_instance_path()
        container_name = self._get_container_name()

        with server._get_ssh_connection() as ssh:

            # Create folder structure
            self._append_log("Creating directory structure at %s" % instance_path)
            mkdir_cmd = (
                'sudo mkdir -p %(path)s/addons '
                '%(path)s/config '
                '%(path)s/data/odoo'
            ) % {'path': instance_path}
            exit_code, stdout, stderr = ssh.execute(mkdir_cmd)
            if exit_code != 0:
                raise UserError(
                    _("Failed to create directories:\n%s") % stderr
                )
            self._append_log("Directory structure created.")

            # Phase 2: when this server uses an object-storage filestore, create
            # the JuiceFS-backed dir that gets bind-mounted at the container's
            # /var/lib/odoo/filestore (chowned to the container uid below).
            filestore_mount = self._get_filestore_mount()
            if filestore_mount:
                self._append_log(
                    "Object-storage filestore enabled → %s" % filestore_mount)
                fs_ec, _o, fs_err = ssh.execute(
                    'sudo mkdir -p %s' % shlex.quote(filestore_mount))
                if fs_ec != 0:
                    raise UserError(
                        _("Failed to create object-storage filestore dir:\n%s")
                        % fs_err)

            # Set ownership so the container user can read/write volumes.
            self._append_log("Setting permissions...")
            container_uid = self._get_container_uid(ssh)
            extra_fs = (' %s' % shlex.quote(filestore_mount)) if filestore_mount else ''
            perms_cmd = (
                'sudo chown -R %(uid)s:%(uid)s %(path)s/data %(path)s/config %(path)s/addons%(fs)s && '
                'sudo chmod -R 777 %(path)s/data %(path)s/config %(path)s/addons%(fs)s'
            ) % {'path': instance_path, 'uid': container_uid, 'fs': extra_fs}
            exit_code, stdout, stderr = ssh.execute(perms_cmd)
            if exit_code != 0:
                raise UserError(
                    _("Failed to set permissions:\n%s") % stderr
                )
            self._append_log("Permissions set (UID=%s)." % container_uid)

            # Allow git to operate on directories owned by the container
            # user (UID differs from the SSH user running git commands).
            ssh.execute(
                "git config --global --add safe.directory '*' 2>/dev/null || true"
            )
            ssh.execute(
                "sudo git config --system --add safe.directory '*' 2>/dev/null || true"
            )

            # Render and write config files (initial — without custom repos)
            self._render_and_write_configs(ssh)

            # Create PostgreSQL user (and the per-instance DB for service
            # plans). Hosting instances get only the role — with CREATEDB
            # — so the customer can spin up databases themselves via the
            # Odoo database manager.
            if self.is_hosting:
                self._append_log("Creating PostgreSQL role (hosting — no DB)...")
                self._provision_postgresql(create_db=False)
                self._append_log("PostgreSQL role ready.")
            else:
                self._append_log("Creating PostgreSQL role and database...")
                self._provision_postgresql(create_db=True)
                self._append_log("PostgreSQL role and database ready.")

                # Restore pre-built database snapshot (if configured)
                snapshot_restored = self._restore_snapshot(ssh)

                if not snapshot_restored:
                    # No snapshot — initialize database with base modules
                    self._append_log("Initializing database...")
                    init_cmd = (
                        'cd %s && docker compose run --rm -T odoo '
                        'odoo -d %s '
                        '-i base '
                        '--without-demo=all '
                        '--stop-after-init '
                        '--no-http 2>&1'
                    ) % (
                        shlex.quote(instance_path),
                        shlex.quote(self.subdomain),
                    )
                    exit_code, stdout, stderr = ssh.execute(init_cmd, timeout=600)
                    self._append_log(
                        "Init output (last 1000 chars):\n%s" % stdout[-1000:]
                    )
                    if exit_code != 0:
                        raise UserError(
                            _("Database initialization failed:\n%s\n%s")
                            % (stdout[-500:], stderr[-500:])
                        )
                    self._append_log("Database initialized.")

            # Re-set permissions after init — docker compose run may
            # have created files as root inside the data directory.
            perms_cmd = (
                'sudo chown -R %(uid)s:%(uid)s %(path)s/data && '
                'sudo chmod -R 777 %(path)s/data'
            ) % {'path': instance_path, 'uid': container_uid}
            ssh.execute(perms_cmd)

            # Start the server (via ComputeDriver, reusing this ssh)
            self._append_log("Starting container with docker compose up -d...")
            try:
                self._compute_driver(connection=ssh).start(self._compute_handle())
            except Exception as e:
                raise UserError(
                    _("docker compose up failed:\n%s") % e
                )
            self._append_log("Container started.")

            # Wait for container to be ready (health-based poll, 30×2s ceiling)
            self._append_log("Waiting for container to be ready...")
            driver = self._compute_driver(connection=ssh)
            handle = self._compute_handle()
            health = driver.wait_until_running(handle, attempts=30, interval=2)
            if not health.running:
                logs_out = driver.logs(handle, tail=50)
                self._append_log(
                    "Container failed to start.\n"
                    "Container logs:\n%s"
                    % logs_out
                )
                raise UserError(
                    _("Container did not become ready within 60 seconds.\n"
                      "Container logs:\n%s")
                    % logs_out
                )
            self._append_log("Container is running.")

            # Clone repos AFTER container is running — ensures the instance
            # is functional before adding custom code and webhooks.

            # Clone product repositories
            self._clone_product_repos(ssh)

            # Clone customer instance repos (for hosting instances)
            if self.is_hosting and self.repo_ids:
                for repo in self.repo_ids.filtered(lambda r: r.state == 'pending'):
                    self._append_log("Cloning customer repo: %s (%s)..." % (repo.repo_url, repo.branch))
                    repo._clone_repo()

            # Check if any repos (product or instance) need to be in the
            # addons_path.  If so, re-render config and restart.
            all_addons = self._get_all_addons_paths()
            if all_addons:
                # Validate module manifests to catch broken modules early.
                self._validate_addons_manifests(ssh)
                # Re-render configs to include the addons paths and
                # restart to pick them up.
                self._render_and_write_configs(ssh)
                self._append_log("Restarting container to load custom addons...")
                try:
                    driver = self._compute_driver(connection=ssh)
                    handle = self._compute_handle()
                    driver.destroy(handle)
                    driver.start(handle)
                except Exception as e:
                    raise UserError(
                        _("docker compose restart failed:\n%s") % e
                    )
                self._append_log("Container restarted with custom addons.")

            # Ensure webhooks are registered for all repos that need them.
            # _clone_repo attempts registration but may fail silently
            # (e.g. race condition, transient API error).  This final
            # pass catches anything that slipped through.
            self._ensure_webhooks_registered()

            # Configure Nginx reverse proxy with SSL
            self._append_log("Configuring Nginx reverse proxy with SSL...")
            proxy_server = self.domain_id.proxy_server_id
            if proxy_server and proxy_server != self.docker_server_id:
                # Proxy is on a different server — deploy Nginx there,
                # pointing at the Docker host over the private network.
                with proxy_server._get_ssh_connection() as proxy_ssh:
                    self._provision_nginx(
                        proxy_ssh, backend_ip=self._get_proxy_backend_ip(),
                    )
            elif proxy_server:
                # Proxy and Docker are the same server — use localhost
                self._provision_nginx(ssh)
            else:
                # No proxy configured — deploy Nginx on the Docker server
                self._provision_nginx(ssh)
            self._append_log("Nginx configured successfully.")

        self.state = 'running'
        self.deploy_retry_count = 0
        self.last_error = False
        self.last_error_date = False
        self.pending_operation = False
        # Successfully out of pending_provision — reset back-off so a
        # later cancel + redeploy starts the counters fresh.
        self.pending_provision_since = False
        self.pending_provision_attempts = 0
        self._append_log("Deployment completed successfully. State: running.")
        self._safe_refresh_usage()
        self._record_build('initial', 'success', commit_message='Deployment')
        self._send_notification('saas_core.mail_template_saas_deployed')

        # Mark partner trial as used only after the deployment actually
        # succeeds.  This runs inside the background thread so a failed
        # deploy does not lock the customer out of retrying their trial.
        if self.is_trial:
            self._sync_partner_trial()

    # ========== Lifecycle Actions ==========

    def action_stop(self):
        """Stop the Docker container and set state to stopped (async)."""
        for rec in self:
            if rec.state != 'running':
                raise UserError(
                    _("Cannot stop instance '%s': must be in Running state (current: %s).")
                    % (rec.subdomain, rec.state)
                )
            rec._ensure_can_ssh()
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
        """Restart the Docker container via SSH (async)."""
        for rec in self:
            if rec.state not in ('running', 'stopped', 'suspended'):
                raise UserError(
                    _("Cannot restart instance '%s': must be Running, Stopped, or Suspended (current: %s).")
                    % (rec.subdomain, rec.state)
                )
            rec._ensure_can_ssh()
            prev_state = rec.state
            rec.pre_provisioning_state = prev_state
            rec.pending_operation = 'restart'
            rec.state = 'provisioning'
            rec._append_log("Restart queued. Running in background...")
            # Durable queue (ARCH-004): restart is recoverable + idempotent, so a
            # worker that dies mid-restart is re-run by the reaper (completing the
            # op) rather than left for recover-stuck to merely revert.
            self.env['saas.job']._enqueue(
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

    def _validate_repo_before_change(self, repo_url, branch, github_token=None):
        """Pre-flight a repository URL + branch (with optional token) via
        ``git ls-remote`` on the docker host, BEFORE writing the repo or
        redeploying. A wrong URL, missing branch, or bad token is rejected with
        a clear ``UserError`` and the running instance is left completely
        untouched — so a bad repo can never trigger a redeploy that
        crash-loops the customer's container.

        No-op when the instance has no docker host yet (the first deploy's
        clone step validates it there). Network/SSH hiccups are surfaced as a
        retryable error, never as a silent pass.
        """
        self.ensure_one()
        server = self.docker_server_id
        repo_url = (repo_url or '').strip()
        branch = (branch or 'main').strip() or 'main'
        if not server or not repo_url:
            return
        # SSRF guard: we're about to `git ls-remote` this URL from the docker
        # host, which can reach internal services — reject non-public hosts.
        from .saas_instance_repo import assert_safe_git_url
        assert_safe_git_url(repo_url)
        # Mirror saas.instance.repo._get_clone_url: strip any userinfo, then
        # inject the token for private HTTPS repos.
        url = re.sub(r'^(https?://)[^@/]+@', r'\1', repo_url)
        token = (github_token or '').strip()
        if token and url.startswith('https://'):
            url = 'https://x-access-token:%s@%s' % (token, url[len('https://'):])
        cmd = 'git ls-remote --heads %s %s 2>&1' % (
            shlex.quote(url), shlex.quote(branch))
        try:
            with server._get_ssh_connection() as ssh:
                exit_code, stdout, stderr = ssh.execute(cmd, timeout=60)
        except Exception as e:
            raise UserError(_(
                "Couldn't reach the repository to validate it. Please try "
                "again.\n%s") % str(e)[-300:])
        out = ((stdout or '') + (stderr or '')).strip()
        if token:                      # never echo the token back to the client
            out = out.replace(token, '***')
        if exit_code != 0:
            raise UserError(_(
                "The repository could not be accessed — check the URL, the "
                "branch, and the access token (private repos need a token).\n%s"
            ) % out[-400:])
        if ('refs/heads/%s' % branch) not in out:
            raise UserError(_(
                "Branch '%s' was not found in the repository. Check the "
                "branch name.") % branch)

    def action_redeploy(self):
        """Redeploy: clone pending repos, pull cloned repos, update config/mounts,
        install pending modules, and restart the container (async)."""
        for rec in self:
            # Suspended instances must NOT be redeployed via this path —
            # that would silently restore service to a non-paying or
            # trial-expired customer. Use action_reactivate / payment
            # flows instead, which validate billing.
            if rec.state not in ('running', 'stopped'):
                raise UserError(
                    _("Cannot redeploy instance '%s': must be Running or "
                      "Stopped (current: %s). Suspended instances must be "
                      "reactivated through the payment flow first.")
                    % (rec.subdomain, rec.state)
                )
            rec._ensure_can_ssh()
            prev_state = rec.state
            rec.pre_provisioning_state = prev_state
            rec.pending_operation = 'redeploy'
            rec.state = 'provisioning'
            rec._append_log("Redeployment queued. Running in background...")
            # Durable queue (ARCH-004): redeploy is recoverable + idempotent.
            self.env['saas.job']._enqueue(
                rec, '_do_redeploy', channel='deploy',
                lock_key='instance:%s' % rec.id, idempotent=True, max_attempts=2,
                on_error='_on_background_error', on_error_args=(prev_state,))

    def _collect_repo_requirements(self, ssh):
        """Combined ``requirements.txt`` content from the connected repos, read
        live from the docker host. This — not a manual field — is the source of
        the instance's Python dependencies. Returns '' if no repo ships one."""
        self.ensure_one()
        blocks = []
        for repo in self.repo_ids.filtered(lambda r: r.state == 'cloned'):
            req_path = '%s/requirements.txt' % repo._get_remote_repo_path()
            _code, out, _err = ssh.execute(
                'if [ -f %s ]; then cat %s; fi'
                % (shlex.quote(req_path), shlex.quote(req_path)))
            if out and out.strip():
                blocks.append('# from %s (%s)\n%s'
                              % (repo.name, repo.branch or '', out.strip()))
        return ('\n'.join(blocks) + '\n') if blocks else ''

    def _validate_requirements(self, ssh, content):
        """Dry-run ``pip install`` of ``content`` in a throwaway container of
        this instance's Odoo image — so a broken requirements.txt is caught
        BEFORE the live container is recreated. Returns (ok: bool, log: str)."""
        self.ensure_one()
        if not (content or '').strip():
            return True, ''
        image = self.odoo_version_id._get_docker_image()
        instance_path = self._get_instance_path()
        val_path = '%s/.req_validate.txt' % instance_path
        ssh.write_file(val_path, content)
        cmd = (
            'docker run --rm -v %s:/tmp/req.txt:ro --entrypoint sh %s -c %s 2>&1'
            % (shlex.quote(val_path), shlex.quote(image),
               shlex.quote('python3 -m pip install --dry-run --no-input '
                           '--break-system-packages --disable-pip-version-check '
                           '-r /tmp/req.txt'))
        )
        code, out, err = ssh.execute(cmd, timeout=420)
        ssh.execute('rm -f %s' % shlex.quote(val_path))
        return code == 0, ((out or '') + (err or '')).strip()

    def _allocate_ephemeral_ports(self, ssh):
        """Find two consecutive free TCP ports on the docker host for a
        transient green container (not the live pair). Returns (http, chat)."""
        self.ensure_one()
        _c, out, _e = ssh.execute(
            "ss -tlnH 2>/dev/null | awk '{print $4}' | sed 's/.*://' | sort -u")
        used = set((out or '').split())
        for p in range(34001, 34999, 2):
            if (str(p) not in used and str(p + 1) not in used
                    and p not in (self.xmlrpc_port, self.longpolling_port)):
                return p, p + 1
        raise UserError(_("No free ports available for a zero-downtime deploy."))

    def _flip_nginx(self, ssh, http_port, longpolling_port):
        """Point the nginx vhost at ``http_port``/``longpolling_port`` and
        gracefully reload (``systemctl reload`` — no dropped connections). The
        atomic traffic switch between the blue and green containers."""
        self.ensure_one()
        self._refresh_nginx_config(
            ssh, http_port=http_port, longpolling_port=longpolling_port)

    def _wait_until_healthy(self, ssh, timeout=180, container=None):
        """Boot check: after a (re)deploy, wait until Odoo actually ANSWERS
        inside the container — not merely that the container exists. We
        actively probe ``/web/login`` via ``docker exec`` (faster than the 30s
        healthcheck interval) and bail out early if the container has exited.
        Returns ``(ok: bool, logs: str)`` — ``logs`` is the container log tail
        on failure, so the customer sees WHY it didn't boot."""
        self.ensure_one()
        container = container or self._get_container_name()
        qn = shlex.quote(container)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _c, out, _e = ssh.execute(
                "docker inspect -f '{{.State.Status}}' %s 2>/dev/null" % qn)
            status = (out or '').strip()
            if status in ('exited', 'dead'):
                break  # crashed on boot — stop waiting
            if status == 'running':
                pc, _po, _pe = ssh.execute(
                    "docker exec %s curl -sf -o /dev/null --max-time 5 "
                    "http://localhost:8069/web/login" % qn)
                if pc == 0:
                    return True, ''
            time.sleep(5)
        _c, logs, _e = ssh.execute("docker logs --tail 200 %s 2>&1" % qn)
        return False, (logs or '').strip()[-6000:]

    def _do_redeploy(self):
        """Internal redeploy logic for a single record."""
        self.ensure_one()
        server = self.docker_server_id
        instance_path = self._get_instance_path()

        # 1. Clone any pending instance repos
        pending_repos = self.repo_ids.filtered(lambda r: r.state == 'pending')
        if pending_repos:
            pending_repos._clone_repo()

        # 2-5. Single SSH connection for pull, config update, and restart
        with server._get_ssh_connection() as ssh:
            # Pull all cloned instance repos
            cloned_repos = self.repo_ids.filtered(lambda r: r.state == 'cloned')
            # Capture each repo's currently-deployed commit so a boot-check
            # failure can roll the code back to this exact working state.
            prev_shas = {}
            for repo in cloned_repos:
                repo_path = repo._get_remote_repo_path()
                clone_url = repo._get_clone_url()
                ssh.execute(
                    'cd %s && git remote set-url origin %s'
                    % (shlex.quote(repo_path), shlex.quote(clone_url))
                )
                _c, _sha, _e = ssh.execute(
                    'cd %s && git rev-parse HEAD 2>/dev/null' % shlex.quote(repo_path))
                if _sha.strip():
                    prev_shas[repo.id] = _sha.strip()
                self._append_log("Pulling %s..." % repo.name)
                pull_cmd = 'cd %s && git pull origin %s 2>&1' % (
                    shlex.quote(repo_path), shlex.quote(repo.branch),
                )
                exit_code, stdout, stderr = ssh.execute(
                    pull_cmd, timeout=300,
                )
                if exit_code != 0:
                    repo.error_message = stdout + '\n' + stderr
                    raise UserError(
                        _("Git pull failed for '%s':\n%s\n%s")
                        % (repo.name, stdout[-500:], stderr[-500:])
                    )
                repo.last_pull = fields.Datetime.now()
                repo.error_message = False
                self._append_log(
                    "Pulled %s: %s" % (repo.name, stdout.strip()[:200])
                )

            # Pull product repos
            self._pull_product_repos(ssh)

            # Validate the repo's requirements.txt in a THROWAWAY container
            # before we touch the running one — a broken dependency must never
            # take the customer's instance down. On failure: record a failed
            # build (shown in Deployment history with the pip error) and abort,
            # leaving the container untouched on its previous working state.
            req_content = self._collect_repo_requirements(ssh)
            if req_content.strip():
                self._append_log("Validating requirements.txt from your repository...")
                ok, vlog = self._validate_requirements(ssh, req_content)
                if not ok:
                    self._record_build(
                        'redeploy', 'failed',
                        commit_message='requirements.txt failed to install',
                        log=vlog)
                    raise UserError(_(
                        "Deployment aborted: your requirements.txt could not be "
                        "installed, so your code was NOT deployed and your "
                        "instance is unchanged. See Deployment history for the "
                        "full error.\n\n%s") % (vlog[-1500:] or 'pip install failed'))
                self._append_log("requirements.txt validated successfully.")

            # Update docker-compose.yml and odoo.conf with the new code/mounts.
            self._append_log("Updating configuration...")
            self._render_and_write_configs(ssh)
            self._append_log("Configuration updated.")

            driver = self._compute_driver(connection=ssh)
            handle = self._compute_handle()
            blue = self._get_container_name()

            # Zero-downtime (blue/green) applies to source-mount instances whose
            # nginx lives on the same host (a remote proxy / immutable image use
            # the in-place recreate path below).
            proxy = self.domain_id.proxy_server_id
            zero_downtime = (
                not (self.deploy_image or '').strip()
                and (not proxy or proxy == self.docker_server_id))

            if zero_downtime:
                green = '%s_green' % blue
                green_file = '%s/docker-compose.green.yml' % instance_path
                green_proj = '%s_green' % self.subdomain

                def _green_down():
                    ssh.execute('cd %s && docker compose -f %s -p %s down 2>&1' % (
                        shlex.quote(instance_path), shlex.quote(green_file),
                        shlex.quote(green_proj)))
                    ssh.execute('rm -f %s' % shlex.quote(green_file))

                # 1. Stand up GREEN beside the live (blue) container on temp
                #    ports, running the NEW code, sharing the same DB+filestore.
                gp_http, gp_chat = self._allocate_ephemeral_ports(ssh)
                _c, canon, _e = ssh.execute(
                    'cat %s/docker-compose.yml' % shlex.quote(instance_path))
                green_compose = (
                    canon
                    .replace('container_name: %s' % blue,
                             'container_name: %s' % green)
                    .replace(':%d:8069' % self.xmlrpc_port, ':%d:8069' % gp_http)
                    .replace(':%d:8072' % self.longpolling_port,
                             ':%d:8072' % gp_chat))
                ssh.write_file(green_file, green_compose)
                self._append_log("Starting new version alongside the live one (zero-downtime)...")
                ssh.execute(
                    'cd %s && docker compose -f %s -p %s up -d 2>&1' % (
                        shlex.quote(instance_path), shlex.quote(green_file),
                        shlex.quote(green_proj)), timeout=420)

                # 2. Boot-check green. If it never answers, tear it down — the
                #    live container never moved, so the customer sees nothing.
                self._append_log("Waiting for the new version to boot...")
                ok, boot_logs = self._wait_until_healthy(ssh, container=green)
                if not ok:
                    _green_down()
                    self._record_build(
                        'redeploy', 'failed',
                        commit_message='New code failed to boot — kept the running version (zero downtime)',
                        log=boot_logs)
                    raise UserError(_(
                        "Deployment failed: your new code did not boot. Your "
                        "instance kept serving the previous version the entire "
                        "time (zero downtime). See Deployment history for the "
                        "boot log.\n\n%s")
                        % (boot_logs[-1500:] or 'Odoo did not become healthy'))

                # 3. Graceful nginx flip to green, then promote: recreate the
                #    canonical container on the new code (its brief reboot is
                #    masked by green still serving), flip back, retire green.
                self._append_log("New version healthy — switching traffic over...")
                self._flip_nginx(ssh, gp_http, gp_chat)
                try:
                    driver.destroy(handle)
                    driver.start(handle)
                except Exception as e:
                    self._flip_nginx(ssh, self.xmlrpc_port, self.longpolling_port)
                    _green_down()
                    raise UserError(_("Failed to recreate container '%s':\n%s") % (blue, e))
                canon_ok, canon_logs = self._wait_until_healthy(ssh)
                if not canon_ok:
                    # Extremely rare — green proved this exact code boots. Keep
                    # traffic on green and flag the main container for ops.
                    self._record_build(
                        'redeploy', 'failed',
                        commit_message='Promotion failed — serving from standby, needs attention',
                        log=canon_logs)
                    raise UserError(_(
                        "Your new code is live (served from a standby container) "
                        "but the main container did not come back up. Support "
                        "has been notified.\n\n%s") % (canon_logs[-1500:]))
                self._flip_nginx(ssh, self.xmlrpc_port, self.longpolling_port)
                _green_down()
                self._append_log("Deploy complete — zero downtime.")
            else:
                # Fallback (remote proxy / immutable image): in-place recreate
                # with a boot check + git rollback to the last working commit.
                self._append_log("Restarting container...")
                try:
                    driver.destroy(handle)
                    driver.start(handle)
                except Exception as e:
                    raise UserError(
                        _("Failed to restart container '%s':\n%s") % (blue, e))
                self._append_log("Waiting for Odoo to boot...")
                booted, boot_logs = self._wait_until_healthy(ssh)
                if not booted:
                    self._append_log(
                        "New code failed to boot — rolling back to the last "
                        "working version...")
                    for repo in cloned_repos:
                        sha = prev_shas.get(repo.id)
                        if sha:
                            ssh.execute('cd %s && git reset --hard %s' % (
                                shlex.quote(repo._get_remote_repo_path()),
                                shlex.quote(sha)))
                    self._render_and_write_configs(ssh)
                    try:
                        driver.destroy(handle)
                        driver.start(handle)
                    except Exception:
                        pass
                    self._wait_until_healthy(ssh, timeout=180)
                    self._record_build(
                        'redeploy', 'failed',
                        commit_message='Deploy rolled back — new code failed to boot',
                        log=boot_logs)
                    raise UserError(_(
                        "Deployment failed: your new code did not boot, so it "
                        "was rolled back to the last working version. Your "
                        "instance is running again on the previous code. See "
                        "Deployment history for the boot log.\n\n%s")
                        % (boot_logs[-1500:] or 'Odoo did not become healthy in time'))
                self._append_log("Odoo booted healthy.")

        # Ensure webhooks are registered for any new or updated repos
        self._ensure_webhooks_registered()

        # Restore the previous state instead of forcing 'running'.
        # A redeploy on a Stopped instance should leave it Stopped.
        target_state = self.pre_provisioning_state or 'running'
        if target_state not in ('running', 'stopped'):
            target_state = 'running'
        self.state = target_state
        self.pending_operation = False
        self._safe_refresh_usage()
        self._record_build('redeploy', 'success', commit_message='Manual re-deploy')

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
            rec._ensure_can_ssh()
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
                rec._ensure_can_ssh()
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
            rec._ensure_can_ssh()
            prev_state = rec.state
            rec.pre_provisioning_state = prev_state
            rec.pending_operation = 'delete'
            rec.state = 'provisioning'
            rec._append_log("Deletion queued. Running in background...")
            self.env['saas.audit.log']._saas_audit(
                'instance_delete', model='saas.instance', res_id=rec.id,
                res_name=rec.subdomain,
                detail='Deletion queued (was %s)' % prev_state)
            # Durable queue (ARCH-004 Phase 3). Delete is non-idempotent, so the
            # job-reaper won't auto-retry it; _on_background_error restores the
            # prior state on failure.
            self.env['saas.job']._enqueue(
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
        instance_path = self._get_instance_path()
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
                Backup._perform_full_instance_backup(self)
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
                ('format', '=', 'restic'),
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

        # 4. Prune the restic repo. Two cases:
        #    a) We have a fresh retained snapshot → keep ONLY its
        #       run tag; every other snapshot's data is dropped.
        #    b) No retained snapshot → wipe the entire repo so no
        #       old snapshot data lingers in the bucket.
        if retained_backup and retained_backup.restic_run_tag:
            try:
                self._restic_keep_only_run_tag(retained_backup.restic_run_tag)
                self._append_log(
                    "Pruned old snapshot data — only the fresh "
                    "snapshot remains in cloud storage."
                )
            except Exception:
                _logger.exception(
                    "Failed to prune restic repo for cancelled %s",
                    self.subdomain,
                )
                self._append_log(
                    "Couldn't fully clean up old snapshot data. The "
                    "retained snapshot is still available; orphan data "
                    "(if any) will be reaped on the next backup."
                )
        else:
            try:
                self._restic_wipe_repo()
                self._append_log(
                    "Removed all snapshot data from cloud storage."
                )
            except Exception:
                _logger.exception(
                    "Failed to wipe restic repo for cancelled %s",
                    self.subdomain,
                )
                self._append_log(
                    "Couldn't fully clean up snapshot data; orphan "
                    "objects (if any) will be reaped later."
                )

        # 5. Delete every backup record (and its bucket object) EXCEPT
        # the one we're retaining. The retained row stays so it shows
        # up on /backups after reactivation and the customer can hit
        # Restore. ``unlink`` cascades to ``_delete_from_bucket`` for
        # rows with a direct ``bucket_path`` (legacy zip / on-demand);
        # the restic-format rows don't have a single bucket key (data
        # lives across many objects managed by restic above), so step
        # 4 was responsible for those.
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
            'daily_backup_pending_invoice_id': False,
            'daily_backup_next_invoice_date': False,
            'daily_backup_last_invoice_date': False,
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

    def action_config(self):
        """Read odoo.conf from the server and display it in a popup."""
        self.ensure_one()
        self._ensure_can_ssh()
        server = self.docker_server_id
        instance_path = self._get_instance_path()
        conf_path = '%s/config/odoo.conf' % instance_path

        with server._get_ssh_connection() as ssh:
            exit_code, stdout, stderr = ssh.execute('cat %s' % shlex.quote(conf_path))
            if exit_code != 0:
                raise UserError(
                    _("Failed to read odoo.conf:\n%s") % stderr
                )

        return {
            'type': 'ir.actions.act_window',
            'name': _("odoo.conf — %s") % self.name,
            'res_model': 'saas.config.viewer',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_content': stdout},
        }

    def action_docker_compose(self):
        """Read docker-compose.yml from the server and display it in a popup."""
        self.ensure_one()
        self._ensure_can_ssh()
        server = self.docker_server_id
        instance_path = self._get_instance_path()
        compose_path = '%s/docker-compose.yml' % instance_path

        with server._get_ssh_connection() as ssh:
            exit_code, stdout, stderr = ssh.execute('cat %s' % shlex.quote(compose_path))
            if exit_code != 0:
                raise UserError(
                    _("Failed to read docker-compose.yml:\n%s") % stderr
                )

        return {
            'type': 'ir.actions.act_window',
            'name': _("docker-compose.yml — %s") % self.name,
            'res_model': 'saas.config.viewer',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_content': stdout},
        }

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

    def _do_retained_restore(self, source_id, prev_state, delete_after):
        """Restore the retained backup of *source_id* into this instance.

        Runs in a background thread (queued by the wizard). On failure
        the standard `_on_background_error` handler is invoked which
        restores `prev_state`.
        """
        self.ensure_one()
        source = self.env['saas.instance'].browse(source_id)
        if not source.exists() or not source.retained_backup_path:
            raise UserError(
                _("Source instance %s no longer has a retained backup.")
                % (source.name or source_id)
            )

        Backup = self.env['saas.instance.backup']
        backup = Backup.create({
            'instance_id': self.id,
            'name': 'restored_from_%s' % (source.subdomain or source.id),
            'bucket_path': source.retained_backup_path,
            'state': 'done',
        })

        self._append_log("Restoring retained backup '%s'..." % backup.name)
        try:
            self._pre_restore_setup()
            self._do_restore_backup(backup.id)
        finally:
            backup.unlink()

        try:
            self._ensure_webhooks_registered()
        except Exception as exc:
            _logger.warning(
                "Post-restore webhook setup failed for %s: %s",
                self.subdomain, exc,
            )

        if delete_after and source.retained_backup_path:
            try:
                Backup._delete_bucket_path(source.retained_backup_path)
                source.retained_backup_path = False
            except Exception:
                _logger.exception(
                    "Failed to delete retained backup from cloud for %s",
                    source.name,
                )

    def _run_daily_full_backup(self):
        """Durable-queue entry point for the nightly full-instance snapshot
        (hosting). Runs the same backup logic the cron used to run inline — now
        parallelised across instances by the queue's per-channel worker pool."""
        self.ensure_one()
        self.env['saas.instance.backup']._perform_full_instance_backup(self)

    def _run_daily_db_backup(self):
        """Durable-queue entry point for the nightly per-DB backup (managed
        services)."""
        self.ensure_one()
        self.env['saas.instance.backup']._perform_backup(self)

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
        self.env['saas.job']._enqueue(
            backup, '_run_portal_backup',
            channel='backup', lock_key='instance:%s' % self.id,
            max_attempts=1,
        )
        return True

    # TEST-BUTTON-REMOVE-ME — manual trigger for the daily-restic flow,
    # so QA can exercise the cron path without waiting for 03:00 UTC.
    # Delete this method (and the matching button in
    # saas_instance_views.xml) once snapshot testing is signed off.
    def action_test_run_daily_backup(self):
        """Fire ``_perform_full_instance_backup`` in a background thread.

        Same code path the daily cron uses. Returns immediately so the
        HTTP transaction doesn't hold a row-level write while restic is
        uploading — the cron itself runs in a fresh cursor for the same
        reason. Watch progress in the instance log stream.
        """
        self.ensure_one()
        if not self.is_hosting:
            raise UserError(_("Test button is for hosting instances only."))
        if not self.daily_backup_enabled:
            raise UserError(_(
                "Daily backups must be enabled before triggering a test run."
            ))
        if self.state != 'running':
            raise UserError(_("Instance must be Running to back it up."))

        Backup = self.env['saas.instance.backup'].sudo()
        running = Backup.search_count([
            ('instance_id', '=', self.id),
            ('state', '=', 'running'),
            ('is_full_instance', '=', True),
        ])
        if running:
            raise UserError(_(
                "A full-instance backup is already running on this "
                "instance. Wait for it to finish before triggering "
                "another test."
            ))

        self._append_log("TEST: manual daily-snapshot trigger queued.")
        run_in_background(
            Backup, '_perform_full_instance_backup_in_new_cursor',
            method_args=(self.id,),
            thread_name='saas_test_snapshot_%s' % self.subdomain,
        )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Snapshot started"),
                'message': _(
                    "Full-instance snapshot is running in the "
                    "background. Watch the instance log or the "
                    "Snapshots page — the new saas.instance.backup "
                    "row will flip to 'done' when restic finishes."
                ),
                'type': 'success',
                'sticky': False,
            },
        }

    def action_restore_backup(self, backup_id):
        """Restore a backup to this instance (async).

        Stops the container, drops the current database, restores the
        backup database and filestore, then restarts the container.
        """
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
                "This is a full-instance restic snapshot — use "
                "action_restore_full_instance, not action_restore_backup. "
                "The backend Restore button now dispatches automatically; "
                "if you're seeing this, a stale code path is still calling "
                "the zip restore on a restic backup."
            ))
        if not backup.bucket_path:
            raise UserError(_(
                "Backup record has no cloud object path — nothing to "
                "download. This row is probably a failed or partial "
                "backup; delete it and create a fresh one."
            ))

        self._ensure_can_ssh()
        # Lock the row + refuse if another restore is already in flight.
        # Concurrent restores would both run dropdb/createdb/psql -f
        # against the same DB and corrupt it.
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
        run_in_background(
            self, '_do_restore_backup',
            method_args=(backup.id,),
            error_method='_on_background_error',
            error_args=(prev_state,),
            thread_name='saas_restore_%s' % self.subdomain,
        )
        return True

    def action_restore_full_instance(self, backup_id):
        """Restore an entire hosting instance from a full-instance backup.

        Brings back every database, the filestore, custom addons,
        configuration files, docker-compose, and pip requirements at
        the exact point in time the backup was taken. Other databases
        currently on the instance are dropped — this is a full state
        replacement, not a merge.

        Before destroying anything, ``_do_restore_full_instance`` takes
        a fresh full-instance "pre-restore" snapshot. If that fails,
        the restore aborts and the current state is untouched.
        """
        self.ensure_one()
        if not self.is_hosting:
            raise UserError(_(
                "Full-instance restore is only available for hosting instances."
            ))
        if self.state not in ('running', 'stopped'):
            raise UserError(
                _("Instance must be Running or Stopped to restore.")
            )
        # Snapshots are a paid add-on. After a reactivation the
        # subscription is reset (see ``action_reactivate``), so the
        # customer must enable Daily Backups again — and pay the
        # activation invoice — before they can restore from the
        # snapshot we retained for them. Without this gate they could
        # get the snapshot feature's payoff for free post-cancellation.
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

        self._ensure_can_ssh()
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
        run_in_background(
            self, '_do_restore_full_instance',
            method_args=(backup.id,),
            error_method='_on_background_error',
            error_args=(prev_state,),
            thread_name='saas_restore_full_%s' % self.subdomain,
        )
        return True

    def _restore_log(self, message, level='info', commit=True):
        """Tagged log for the restore flow.

        Writes to ``self.provisioning_log`` (visible from the backend
        form view and from the customer portal) AND to the Odoo
        server log under ``odoo.addons.saas_core.restore`` so the
        operator can ``grep RESTORE`` and follow the whole flow.

        ``commit=True`` flushes the cursor after writing so the
        provisioning_log is visible to other workers in real time —
        important when the customer is watching the portal mid-restore.
        """
        self.ensure_one()
        tagged = '[RESTORE %s] %s' % (self.subdomain or self.id, message)
        try:
            self._append_log('[RESTORE] %s' % message)
        except Exception:
            _logger.exception("Failed to append restore log line")
        log_fn = getattr(_logger, level, _logger.info)
        log_fn(tagged)
        if commit:
            try:
                self.env.cr.commit()
            except Exception:
                pass

    def _do_restore_full_instance(self, backup_id):
        """Background worker: replace the entire instance from a snapshot.

        Dispatches by ``backup.format``:
        - ``restic`` → restic-based restore (new format, deduplicated).
        - ``zip``    → legacy single-zip flow, kept for backups taken
          before the restic switch.

        Restore is in-place — no new snapshot is created. The
        snapshot list the customer sees stays exactly as it was.
        """
        self.ensure_one()
        backup = self.env['saas.instance.backup'].browse(backup_id)
        self._restore_log(
            "Dispatcher entered. backup_id=%s name=%r format=%r "
            "is_full_instance=%s state=%r restic_run_tag=%r "
            "restic_db_names=%r" % (
                backup_id, backup.name, backup.format,
                backup.is_full_instance, backup.state,
                backup.restic_run_tag, backup.restic_db_names,
            )
        )
        if backup.format == 'restic':
            self._restore_log("Dispatching to restic restore path.")
            return self._do_restore_full_instance_restic(backup_id)
        self._restore_log("Dispatching to legacy zip restore path.")
        return self._do_restore_full_instance_zip(backup_id)

    def _do_restore_full_instance_zip(self, backup_id):
        """Legacy single-zip full-instance restore."""
        self.ensure_one()
        backup = self.env['saas.instance.backup'].browse(backup_id)
        server = self.docker_server_id
        container_name = self._get_container_name()
        instance_path = self._get_instance_path()
        psql_server = self.db_server_id
        db_host = self._get_db_host_for_ssh()
        db_port = psql_server.psql_port or 5432

        if not DB_USER_RE.match(self.db_user or ''):
            raise UserError(
                _("Refusing to restore: invalid db user %r") % self.db_user
            )

        # NB: no pre-restore safety snapshot — restore is in-place and
        # doesn't mint a new entry on the customer's snapshot list.

        # Compatibility check — refuse to restore across Odoo major
        # versions, otherwise schema migrations corrupt silently.
        manifest_version = None
        try:
            manifest = backup._read_manifest_safe() \
                if hasattr(backup, '_read_manifest_safe') else None
            if isinstance(manifest, dict):
                manifest_version = manifest.get('odoo_version')
        except Exception:
            pass
        if manifest_version and self.odoo_version_id \
                and manifest_version != self.odoo_version_id.name:
            raise UserError(_(
                "Backup was taken on Odoo %s but this instance now runs %s. "
                "Aborting to avoid silent schema corruption."
            ) % (manifest_version, self.odoo_version_id.name))

        with server._get_ssh_connection() as ssh:
            # 1. Stop the container — full restore replaces /data, so no
            # connections can be left open.
            self._append_log("Stopping container...")
            try:
                self._compute_driver(connection=ssh).destroy(self._compute_handle())
            except Exception as e:
                raise UserError(_(
                    "Failed to stop container before restore:\n%s"
                ) % e)

            # 2. Download + extract the backup zip
            self._append_log("Downloading backup...")
            download_url = backup._generate_presigned_url()
            tmp_zip = '/tmp/saas_full_restore_%s.zip' % self.subdomain
            extract_dir = '/tmp/saas_full_restore_%s' % self.subdomain

            dl_cmd = 'curl -fsSL -o %s %s 2>&1' % (
                shlex.quote(tmp_zip), shlex.quote(download_url),
            )
            exit_code, stdout, stderr = ssh.execute(dl_cmd, timeout=1800)
            if exit_code != 0:
                raise UserError(
                    _("Failed to download backup:\n%s\n%s") % (stdout, stderr)
                )

            self._append_log("Extracting...")
            ssh.execute('rm -rf %s && mkdir -p %s' % (
                shlex.quote(extract_dir), shlex.quote(extract_dir),
            ))
            # Use Python's zipfile module instead of the `unzip` binary
            # so we don't fail on docker hosts that don't ship it.
            exit_code, stdout, stderr = ssh.execute(
                'python3 -m zipfile -e %s %s 2>&1' % (
                    shlex.quote(tmp_zip), shlex.quote(extract_dir),
                ),
                timeout=1800,
            )
            if exit_code != 0:
                raise UserError(
                    _("Failed to extract backup:\n%s\n%s") % (stdout, stderr)
                )

            # 3. Restore the on-disk instance files (data, addons,
            # config, compose / requirements / pip script). We blow
            # away the target subdirs first — leaving stale files would
            # interleave with the snapshot.
            self._append_log("Restoring instance files...")
            container_uid = self._get_container_uid(ssh)
            wipe_and_copy_cmd = (
                # Wipe targets
                'sudo rm -rf %(ip)s/data %(ip)s/addons %(ip)s/config && '
                # Copy back from extraction (whichever subdirs the
                # backup contained)
                'for d in data addons config; do '
                '  if [ -d %(ed)s/$d ]; then '
                '    sudo cp -a %(ed)s/$d %(ip)s/$d; '
                '  fi; '
                'done && '
                # Top-level files
                'for f in docker-compose.yml requirements.txt pip_install.sh; do '
                '  if [ -f %(ed)s/$f ]; then '
                '    sudo cp -a %(ed)s/$f %(ip)s/$f; '
                '  fi; '
                'done && '
                # Re-apply container-friendly ownership/perms
                'sudo chown -R %(uid)s:%(uid)s %(ip)s/data %(ip)s/config %(ip)s/addons 2>/dev/null || true && '
                'sudo chmod -R 777 %(ip)s/data %(ip)s/config %(ip)s/addons 2>/dev/null || true'
            ) % {
                'ip': shlex.quote(instance_path),
                'ed': shlex.quote(extract_dir),
                'uid': container_uid,
            }
            exit_code, stdout, stderr = ssh.execute(wipe_and_copy_cmd, timeout=1800)
            if exit_code != 0:
                raise UserError(_(
                    "Failed to restore instance files:\n%s\n%s"
                ) % (stdout, stderr))

            # 4. Restore every DB dump. We do this before bringing the
            # container back up so Odoo doesn't autocreate empty schemas.
            dumps_dir = '%s/dumps' % extract_dir
            list_cmd = 'ls %s 2>/dev/null || true' % shlex.quote(dumps_dir)
            exit_code, listing, _ = ssh.execute(list_cmd)
            dump_files = [f for f in (listing or '').splitlines()
                          if f.strip().endswith('.sql')]
            for dump_file in dump_files:
                db = dump_file[:-4]  # strip .sql
                if not re.match(r'^[a-z][a-z0-9_-]{0,62}$', db):
                    self._append_log(
                        "Skipping suspicious dump filename: %r" % dump_file
                    )
                    continue
                self._append_log("Restoring database '%s'..." % db)
                self._restore_one_db_from_dump(
                    ssh, db, '%s/%s' % (dumps_dir, dump_file),
                    db_host=db_host, db_port=db_port,
                    psql_server=psql_server,
                )

            # 5. Cleanup temp files (before bringing container up, so a
            # subsequent failure doesn't leave the zip lying around).
            ssh.execute('rm -rf %s %s' % (
                shlex.quote(tmp_zip), shlex.quote(extract_dir),
            ))

            # 6. Start the container
            self._append_log("Starting container...")
            try:
                self._compute_driver(connection=ssh).start(self._compute_handle())
            except Exception:
                raise UserError(
                    _("Your data was restored, but the instance didn't "
                      "come back up automatically. Please contact support.")
                )

        self.state = 'running'
        self.pending_operation = False
        self._append_log("Full-instance restore from '%s' complete." % backup.name)
        self._safe_refresh_usage()

    def _do_restore_full_instance_restic(self, backup_id):
        """Restic-based full-instance restore.

        Looks up the snapshots tagged ``run=<backup.restic_run_tag>``
        in this instance's repo. Restores the filesystem snapshot
        in place (after stopping the container), then for each DB
        snapshot drops/recreates the target DB and pipes ``restic
        dump`` into ``psql``.
        """
        import json as _json
        import time as _time
        self.ensure_one()
        t0 = _time.time()
        Backup = self.env['saas.instance.backup'].sudo()
        backup = Backup.browse(backup_id)
        server = self.docker_server_id
        instance_path = self._get_instance_path()
        container_name = self._get_container_name()
        psql_server = self.db_server_id
        db_host = self._get_db_host_for_ssh()
        db_port = psql_server.psql_port or 5432

        self._restore_log(
            "Restic restore starting. docker_server=%r db_server=%r "
            "db_host=%r db_port=%s instance_path=%r container=%r "
            "db_user=%r" % (
                server.name, psql_server.name, db_host, db_port,
                instance_path, container_name, self.db_user,
            )
        )

        if not DB_USER_RE.match(self.db_user or ''):
            self._restore_log(
                "ABORT: invalid db_user %r" % self.db_user, level='error',
            )
            raise UserError(
                _("Refusing to restore: invalid db user %r") % self.db_user
            )
        if not backup.restic_run_tag:
            self._restore_log(
                "ABORT: backup has no restic_run_tag.", level='error',
            )
            raise UserError(_(
                "This snapshot is missing its reference data and can't "
                "be restored. Please pick another snapshot or contact "
                "support."
            ))

        # NB: no pre-restore safety snapshot. The customer doesn't want
        # the restore action to mint a new snapshot — it should restore
        # in place and leave the snapshot list untouched. The trade-off
        # is that there's no automatic roll-forward path if the restore
        # turns out to have been the wrong choice; the existing earlier
        # snapshots remain available, just nothing newly captured.

        gcs_path = None
        try:
            self._restore_log("Opening SSH to docker host %s..." % server.name)
            with server._get_ssh_connection() as ssh:
                self._restore_log("SSH connected.")
                Backup._ensure_restic_installed(ssh, server.name)
                self._restore_log("restic binary present on docker host.")
                gcs_path = Backup._stage_gcs_credentials(ssh, self)
                if gcs_path:
                    self._restore_log(
                        "Staged GCS credentials at %s." % gcs_path
                    )
                env = Backup._restic_env_vars(self, gcs_path)
                self._restore_log(
                    "restic env prepared: repo=%r keys=%r" % (
                        env.get('RESTIC_REPOSITORY'),
                        sorted(env.keys()),
                    )
                )

                # 1. Look up snapshot IDs by tag. JSON output is the
                # stable interface — text format changes across versions.
                self._restore_log(
                    "Step 1/5: listing snapshots tagged run=%s host=%s..."
                    % (backup.restic_run_tag, self.subdomain)
                )
                list_cmd = Backup._restic_cmd(
                    env,
                    ['snapshots', '--tag', 'run=' + backup.restic_run_tag,
                     '--host', shlex.quote(self.subdomain),
                     '--json'],
                )
                exit_code, stdout, stderr = ssh.execute(list_cmd, timeout=120)
                if exit_code != 0:
                    self._restore_log(
                        "restic snapshots FAILED exit=%s stdout=%r stderr=%r"
                        % (exit_code, stdout[-500:], stderr[-500:]),
                        level='error',
                    )
                    raise UserError(_(
                        "We couldn't open your snapshot storage. Please "
                        "try again in a moment."
                    ))
                try:
                    snapshots = _json.loads(stdout.strip() or '[]')
                except Exception:
                    self._restore_log(
                        "restic returned non-JSON: %r" % stdout[:500],
                        level='error',
                    )
                    raise UserError(_(
                        "We couldn't read your snapshot details. Please "
                        "contact support."
                    ))
                self._restore_log(
                    "Found %d snapshot(s) in repo for this run."
                    % len(snapshots)
                )
                if not snapshots:
                    self._restore_log(
                        "ABORT: no snapshots match run=%s — has the "
                        "retention policy pruned them?"
                        % backup.restic_run_tag,
                        level='error',
                    )
                    raise UserError(_(
                        "This snapshot is no longer available — it may "
                        "have rolled off as newer snapshots were taken. "
                        "Please pick a more recent one."
                    ))

                fs_snap = None
                db_snaps = []  # list of (db_name, snapshot_id)
                for s in snapshots:
                    tags = s.get('tags', []) or []
                    if 'fs' in tags:
                        fs_snap = s['id']
                    elif 'db' in tags:
                        # tags include "db=<name>" so we recover the DB
                        db_name = None
                        for t in tags:
                            if t.startswith('db=') and len(t) > 3:
                                db_name = t[3:]
                                break
                        if not db_name:
                            # Fall back to the original stdin filename
                            paths = s.get('paths') or []
                            if paths:
                                base = paths[-1].rsplit('/', 1)[-1]
                                if base.endswith('.sql'):
                                    db_name = base[:-4]
                        if db_name and re.match(
                            r'^[a-z][a-z0-9_-]{0,62}$', db_name,
                        ):
                            db_snaps.append((db_name, s['id']))
                self._restore_log(
                    "Snapshot mapping: fs_snap=%s db_snaps=%s"
                    % (fs_snap, db_snaps)
                )
                if not fs_snap:
                    self._restore_log(
                        "ABORT: no fs snapshot in run.", level='error',
                    )
                    raise UserError(_(
                        "This snapshot is incomplete — it doesn't "
                        "contain the file data we need. Please pick "
                        "another snapshot."
                    ))

                # 1.5 Enumerate CURRENT databases on the instance so we
                # can later drop the ones that exist now but weren't in
                # the snapshot. Restoring is a full state replacement —
                # if a database was created AFTER the snapshot was taken
                # it shouldn't survive the restore. Must run while the
                # container is still up (``hosting_db_list`` shells into
                # the Odoo container).
                snap_db_set = {db for db, _sid in db_snaps}
                try:
                    current_dbs = [
                        r['name'] for r in self.hosting_db_list()
                    ]
                except Exception as e:
                    # Don't abort the restore over a listing hiccup —
                    # log loudly so the operator knows extras weren't
                    # pruned. The user's data still gets restored.
                    self._restore_log(
                        "Could not enumerate current DBs to find extras "
                        "(restore will proceed, but DBs created since "
                        "the snapshot may survive): %r" % e,
                        level='warning',
                    )
                    current_dbs = []
                extras_to_drop = [
                    db for db in current_dbs
                    if db not in snap_db_set
                    and re.match(r'^[a-z][a-z0-9_-]{0,62}$', db)
                ]
                self._restore_log(
                    "Pre-restore DB diff: current=%s, in_snapshot=%s, "
                    "to_drop=%s" % (
                        current_dbs, sorted(snap_db_set), extras_to_drop,
                    )
                )

                # 2. Stop container before mutating files.
                self._restore_log("Step 2/5: stopping container...")
                t_stop = _time.time()
                try:
                    self._compute_driver(connection=ssh).destroy(self._compute_handle())
                except Exception as e:
                    self._restore_log(
                        "docker compose down FAILED: %s" % e, level='error')
                    raise UserError(_(
                        "We couldn't pause your instance to start the "
                        "restore. Please try again in a moment."
                    ))
                self._restore_log(
                    "Step 2/5 OK: container stopped in %.1fs."
                    % (_time.time() - t_stop)
                )

                # 3. Wipe the targets and restore the filesystem snapshot.
                # restic restore writes paths back to their original
                # absolute locations when --target /. We delete first to
                # avoid stale files left from the current state.
                container_uid = self._get_container_uid(ssh)
                self._restore_log(
                    "Step 3/5: wiping current files (container_uid=%s)..."
                    % container_uid
                )
                wipe_cmd = (
                    'sudo rm -rf %(ip)s/data %(ip)s/addons %(ip)s/config '
                    '%(ip)s/docker-compose.yml %(ip)s/requirements.txt '
                    '%(ip)s/pip_install.sh'
                ) % {'ip': shlex.quote(instance_path)}
                w_exit, w_out, w_err = ssh.execute(wipe_cmd, timeout=600)
                self._restore_log(
                    "Wipe exit=%s out=%r err=%r"
                    % (w_exit, w_out[-200:], w_err[-200:])
                )

                self._restore_log(
                    "Step 3/5: invoking 'sudo -E env … restic restore %s "
                    "--target /' (this may take minutes)..." % fs_snap
                )
                # See earlier comment block (moved to the helper): we
                # must wrap with ``sudo -E env`` so the KEY=val tokens
                # go to the ``env`` binary, not to sudo (which refuses
                # them under default sudoers policy).
                restore_cmd = (
                    'sudo -E env ' +
                    Backup._restic_cmd(
                        env,
                        ['restore', fs_snap, '--target', '/', '--quiet'],
                    )
                )
                t_fs = _time.time()
                exit_code, stdout, stderr = ssh.execute(
                    restore_cmd, timeout=7200,
                )
                if exit_code != 0:
                    self._restore_log(
                        "Step 3/5 FAILED: restic restore (fs) exit=%s\n"
                        "STDOUT:\n%s\nSTDERR:\n%s"
                        % (exit_code, stdout, stderr),
                        level='error',
                    )
                    raise UserError(_(
                        "We couldn't restore your files from this "
                        "snapshot. Please contact support."
                    ))
                self._restore_log(
                    "Step 3/5 OK: filesystem restored in %.1fs."
                    % (_time.time() - t_fs)
                )

                # Re-apply container ownership/perms.
                self._restore_log("Re-applying container ownership/perms...")
                ssh.execute(
                    'sudo chown -R %(uid)s:%(uid)s %(ip)s/data %(ip)s/config '
                    '%(ip)s/addons 2>/dev/null || true && '
                    'sudo chmod -R 777 %(ip)s/data %(ip)s/config %(ip)s/addons '
                    '2>/dev/null || true' % {
                        'ip': shlex.quote(instance_path),
                        'uid': container_uid,
                    },
                    timeout=300,
                )

                # Overwrite the snapshot's docker-compose.yml and
                # config/odoo.conf with freshly-rendered versions from
                # the CURRENT plan/instance state. The snapshot's
                # versions are whatever was on disk when the backup ran
                # — possibly a smaller plan, an old worker count, an
                # outdated DB host, etc. Re-rendering here guarantees
                # the customer ends up on the plan they actually own
                # right now, not the one they had a week ago.
                self._restore_log(
                    "Re-rendering docker-compose.yml + odoo.conf from "
                    "current plan/instance settings..."
                )
                self._render_and_write_configs(ssh)
                self._restore_log("Configs refreshed.")

                # 4. Per-DB restore: dump from restic stdin into psql.
                self._restore_log(
                    "Step 4/5: restoring %d database(s)..." % len(db_snaps)
                )
                for db, snap_id in db_snaps:
                    t_db = _time.time()
                    self._restore_log(
                        "Restoring database %r from snap %s..."
                        % (db, snap_id)
                    )
                    self._restic_restore_one_db(
                        ssh, Backup, env, snap_id, db,
                        db_host=db_host, db_port=db_port,
                        psql_server=psql_server,
                    )
                    self._restore_log(
                        "Database %r restored in %.1fs."
                        % (db, _time.time() - t_db)
                    )
                self._restore_log("Step 4/5 OK: all databases restored.")

                # 4b. Drop databases that exist NOW but weren't in the
                # snapshot. Restoring is a full state replacement, so a
                # DB the customer created after the snapshot was taken
                # has no reason to survive — leaving it would surprise
                # the customer ("I restored from a snapshot that had
                # only db_a, why is db_b still there?"). Bucket-side
                # ondemand backups for these DBs are reaped through the
                # backup ``unlink`` override below; restic full-instance
                # snapshots don't carry per-DB rows so there's nothing
                # to clean on that side.
                if extras_to_drop:
                    self._restore_log(
                        "Step 4b/5: dropping %d database(s) that exist "
                        "now but weren't in the snapshot: %s"
                        % (len(extras_to_drop), extras_to_drop)
                    )

                    def _drop_extra_db(db_name):
                        # Same drop dance as ``_restic_restore_one_db``
                        # but without the create+pipe-dump steps.
                        if psql_server == self.docker_server_id:
                            run = ssh.execute
                            close = lambda: None  # noqa: E731
                        else:
                            db_ssh_cm = psql_server._get_ssh_connection()
                            db_ssh = db_ssh_cm.__enter__()
                            run = db_ssh.execute
                            close = lambda: db_ssh_cm.__exit__(None, None, None)
                        try:
                            run(
                                "sudo -u postgres psql -c "
                                "\"SELECT pg_terminate_backend(pid) "
                                "FROM pg_stat_activity WHERE datname='%s' "
                                "AND pid <> pg_backend_pid();\" 2>&1"
                                % db_name.replace("'", "''")
                            )
                            ec, out, err = run(
                                'sudo -u postgres dropdb --force '
                                '--if-exists %s 2>&1'
                                % shlex.quote(db_name)
                            )
                            return ec, out, err
                        finally:
                            close()

                    for db in extras_to_drop:
                        try:
                            ec, out, err = _drop_extra_db(db)
                            if ec != 0:
                                self._restore_log(
                                    "WARN: dropdb extra %r exit=%s out=%r"
                                    % (db, ec, (out + err)[-300:]),
                                    level='warning',
                                )
                            else:
                                self._restore_log(
                                    "  -> dropped extra database %r" % db
                                )
                        except Exception as e:
                            # Best-effort: don't fail the restore over
                            # a stuck dropdb. Customer can manually
                            # drop the leftover from the Databases page.
                            self._restore_log(
                                "WARN: failed to drop extra %r: %r"
                                % (db, e), level='warning',
                            )
                        # Also reap any on-demand backup rows + bucket
                        # objects pointing at this DB — the row is now
                        # orphaned. ``unlink`` cascades to the bucket.
                        related = self.env['saas.instance.backup'].sudo().search([
                            ('instance_id', '=', self.id),
                            ('db_name', '=', db),
                            ('is_full_instance', '=', False),
                        ])
                        if related:
                            try:
                                related.unlink()
                                self._restore_log(
                                    "  -> reaped %d backup row(s) for %r"
                                    % (len(related), db)
                                )
                            except Exception as e:
                                self._restore_log(
                                    "WARN: failed to unlink backup rows "
                                    "for dropped %r: %r" % (db, e),
                                    level='warning',
                                )
                    self._restore_log("Step 4b/5 OK: extra DBs dropped.")

                # 5. Bring container back up. ``docker compose up -d``
                # is idempotent over container existence:
                #   - missing container → creates fresh
                #   - existing container with config drift → recreates
                #   - already-correct container (stopped) → starts
                # Combined with the docker-compose.yml we just
                # re-rendered, this guarantees the running container
                # matches the customer's CURRENT plan settings.
                self._restore_log(
                    "Step 5/5: 'docker compose up -d' (creates if "
                    "missing, recreates if config drifted)..."
                )
                t_up = _time.time()
                try:
                    self._compute_driver(connection=ssh).start(self._compute_handle())
                except Exception as e:
                    self._restore_log(
                        "Step 5/5 FAILED: docker compose up: %s" % e, level='error')
                    raise UserError(_(
                        "Your data was restored, but the instance didn't "
                        "come back up automatically. Please contact "
                        "support so we can bring it online."
                    ))
                self._restore_log(
                    "Step 5/5 OK: container up in %.1fs." % (_time.time() - t_up))
        finally:
            if gcs_path:
                try:
                    with server._get_ssh_connection() as ssh2:
                        Backup._unstage_gcs_credentials(ssh2, gcs_path)
                    self._restore_log("Cleaned up staged GCS credentials.")
                except Exception:
                    self._restore_log(
                        "Failed to clean up staged GCS credentials.",
                        level='warning',
                    )

        # Refresh nginx so any vhost / backend-ip / port change
        # captured on the saas.instance record is in effect. This is a
        # config-write + ``nginx -s reload`` only — certbot is skipped,
        # the cert is already on disk from the initial deploy.
        try:
            self._restore_log(
                "Post-restore: refreshing nginx config + reload..."
            )
            t_ng = _time.time()
            self._refresh_nginx_on_correct_host()
            self._restore_log(
                "Nginx refreshed in %.1fs." % (_time.time() - t_ng)
            )
        except Exception as e:
            # Don't fail the restore over an nginx hiccup — the
            # container is up, the data is restored, the customer can
            # reach the instance. Just surface the warning loudly.
            self._restore_log(
                "Nginx refresh FAILED (restore otherwise OK): %r" % e,
                level='error',
            )

        self.state = 'running'
        self.pending_operation = False
        self._restore_log(
            "Restore COMPLETE from backup %r (run_tag=%s) in %.1fs total."
            % (backup.name, backup.restic_run_tag, _time.time() - t0)
        )
        self._safe_refresh_usage()

    def _restic_restore_one_db(self, ssh, Backup, env, snap_id, db,
                               db_host, db_port, psql_server):
        """Drop+recreate ``db`` then pipe ``restic dump`` to psql.

        Mirrors ``_restore_one_db_from_dump`` (the zip path) but the
        SQL bytes come from ``restic dump`` instead of an extracted
        file. Keeping them separate avoids smuggling restic env vars
        through the simpler helper.
        """
        if not re.match(r'^[a-z][a-z0-9_-]{0,62}$', db):
            raise UserError(_("Refusing to restore bogus db name %r") % db)

        def _run_on_db_server(cmd):
            if psql_server == self.docker_server_id:
                return ssh.execute(cmd)
            with psql_server._get_ssh_connection() as db_ssh:
                return db_ssh.execute(cmd)

        self._restore_log(
            "  -> terminating active sessions on %r" % db, commit=False,
        )
        _run_on_db_server(
            "sudo -u postgres psql -c "
            "\"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname='%s' AND pid <> pg_backend_pid();\" 2>&1"
            % db.replace("'", "''")
        )
        self._restore_log("  -> dropdb %r" % db, commit=False)
        exit_code, stdout, stderr = _run_on_db_server(
            'sudo -u postgres dropdb --force --if-exists %s 2>&1'
            % shlex.quote(db)
        )
        if exit_code != 0:
            self._restore_log(
                "dropdb FAILED for %r exit=%s out=%r"
                % (db, exit_code, (stdout + stderr)[-500:]),
                level='error',
            )
            raise UserError(_(
                "dropdb %s failed:\n%s\n%s"
            ) % (db, stdout, stderr))
        self._restore_log(
            "  -> createdb %r owner=%r" % (db, self.db_user), commit=False,
        )
        exit_code, stdout, stderr = _run_on_db_server(
            'sudo -u postgres createdb -O %s %s 2>&1'
            % (shlex.quote(self.db_user), shlex.quote(db))
        )
        if exit_code != 0:
            self._restore_log(
                "createdb FAILED for %r exit=%s out=%r"
                % (db, exit_code, (stdout + stderr)[-500:]),
                level='error',
            )
            raise UserError(_(
                "createdb %s failed:\n%s\n%s"
            ) % (db, stdout, stderr))

        # Pipe restic dump → sed → psql on the docker host (it has the
        # network path to the db server already).
        #
        # The sed filter strips ``SET`` statements that the newer
        # pg_dump (PG 17+) shipped in our containers emits but older
        # PG servers don't understand. ``transaction_timeout`` is the
        # one that bit us in the field; we keep the pattern open-ended
        # so future PG-17/18-only knobs in the dump prologue won't
        # break restores against PG 15/16 servers. Real data lines
        # (``COPY``, ``INSERT``) are unaffected — the offending
        # statements are always single ``SET <name> = <value>;`` lines.
        dump_cmd = Backup._restic_cmd(
            env,
            ['dump', snap_id, shlex.quote('/%s.sql' % db)],
        )
        sed_filter = "sed -E '/^SET (transaction_timeout)\\s*=/d'"
        psql_cmd = (
            'PGPASSWORD=%s psql -h %s -p %d -U %s -d %s -q -v ON_ERROR_STOP=1'
        ) % (
            shlex.quote(self.db_password),
            shlex.quote(db_host),
            db_port,
            shlex.quote(self.db_user),
            shlex.quote(db),
        )
        pipeline = 'set -o pipefail; %s | %s | %s' % (
            dump_cmd, sed_filter, psql_cmd,
        )
        self._restore_log(
            "  -> piping restic dump → sed → psql for %r..." % db,
            commit=False,
        )
        exit_code, stdout, stderr = ssh.execute(pipeline, timeout=7200)
        if exit_code != 0:
            self._restore_log(
                "DB restore pipeline FAILED for %r exit=%s. Tail:\n%s"
                % (db, exit_code, (stdout + stderr)[-1500:]),
                level='error',
            )
            raise UserError(_(
                "Restore of '%s' failed:\n%s"
            ) % (db, (stderr or stdout)[-500:]))

    def _restore_one_db_from_dump(self, ssh, db, dump_path,
                                  db_host, db_port, psql_server):
        """Drop + recreate ``db`` then psql -f the dump in.

        Helper for ``_do_restore_full_instance``. Runs SQL on the db
        server via the ssh-connected docker host; falls back to a
        direct connection if the docker host is also the db server.
        """
        if not re.match(r'^[a-z][a-z0-9_-]{0,62}$', db):
            raise UserError(_("Refusing to restore bogus db name %r") % db)

        def _run_on_db_server(cmd):
            if psql_server == self.docker_server_id:
                return ssh.execute(cmd)
            with psql_server._get_ssh_connection() as db_ssh:
                return db_ssh.execute(cmd)

        # Terminate any leftover sessions first.
        _run_on_db_server(
            "sudo -u postgres psql -c "
            "\"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname='%s' AND pid <> pg_backend_pid();\" 2>&1"
            % db.replace("'", "''")
        )
        exit_code, stdout, stderr = _run_on_db_server(
            'sudo -u postgres dropdb --force --if-exists %s 2>&1'
            % shlex.quote(db)
        )
        if exit_code != 0:
            raise UserError(_(
                "dropdb failed for %s — aborting restore:\n%s\n%s"
            ) % (db, stdout, stderr))
        exit_code, stdout, stderr = _run_on_db_server(
            'sudo -u postgres createdb -O %s %s 2>&1'
            % (shlex.quote(self.db_user), shlex.quote(db))
        )
        if exit_code != 0:
            raise UserError(_(
                "createdb failed for %s — aborting restore:\n%s\n%s"
            ) % (db, stdout, stderr))

        # Apply the dump from the docker host (it has psql + network
        # to the db server already). Same PG-17→older-server filter as
        # the restic path; ``cat | sed | psql`` instead of ``psql -f``
        # so we can drop the offending SET line in-stream.
        restore_cmd = (
            "set -o pipefail; cat %s "
            "| sed -E '/^SET (transaction_timeout)\\s*=/d' "
            "| PGPASSWORD=%s psql -h %s -p %d -U %s -d %s 2>&1"
        ) % (
            shlex.quote(dump_path),
            shlex.quote(self.db_password),
            shlex.quote(db_host),
            db_port,
            shlex.quote(self.db_user),
            shlex.quote(db),
        )
        exit_code, stdout, stderr = ssh.execute(restore_cmd, timeout=3600)
        if exit_code != 0:
            self._append_log(
                "psql restore of %s last 1k chars:\n%s" % (db, stdout[-1000:])
            )
            raise UserError(_(
                "Restore of '%s' failed:\n%s"
            ) % (db, stderr[-500:]))

    def _do_restore_backup(self, backup_id):
        """Restore a backup — replace target DB and filestore (background).

        For service instances, the target DB is always ``self.subdomain``.
        For hosting instances, the target is ``backup.db_name`` (which can
        be one of many DBs on the container) so other databases on the
        same instance stay online during the restore.
        """
        self.ensure_one()
        backup = self.env['saas.instance.backup'].browse(backup_id)
        server = self.docker_server_id
        container_name = self._get_container_name()
        instance_path = self._get_instance_path()
        # Restore target = the database the backup was taken from. Falls
        # back to subdomain for legacy backups recorded before db_name.
        db_name = backup.db_name or self.subdomain
        psql_server = self.db_server_id
        db_host = self._get_db_host_for_ssh()
        db_port = psql_server.psql_port or 5432

        # Validate identifiers before any shell execution.
        # Hosting DB names are slightly more permissive than subdomains
        # (underscores allowed), so apply the right regex per case.
        name_re = (
            re.compile(r'^[a-z][a-z0-9_-]{0,62}$')
            if self.is_hosting else SUBDOMAIN_RE
        )
        if not name_re.match(db_name or ''):
            raise UserError(
                _("Refusing to restore: invalid db name %r") % db_name
            )
        if not DB_USER_RE.match(self.db_user or ''):
            raise UserError(
                _("Refusing to restore: invalid db user %r") % self.db_user
            )

        # Snapshot/backup version compatibility check (B7).
        manifest = backup._read_manifest_safe() if hasattr(backup, '_read_manifest_safe') else None
        backup_version = (manifest or {}).get('odoo_version') if isinstance(manifest, dict) else None
        if backup_version and self.odoo_version_id and \
                backup_version != self.odoo_version_id.name:
            raise UserError(_(
                "Backup was taken on Odoo version %s but this instance "
                "runs %s. Aborting to avoid silent schema corruption."
            ) % (backup_version, self.odoo_version_id.name))

        with server._get_ssh_connection() as ssh:
            # 1. Make sure no Odoo workers are holding the target DB open.
            # Service instances: stop the whole container (it only serves
            # this one DB anyway).
            # Hosting instances: ask Odoo to release just this DB so other
            # databases on the same container keep serving traffic.
            if self.is_hosting:
                self._append_log(
                    "Releasing connections to database '%s'..." % db_name
                )
                release_script = (
                    "from odoo.service import db\n"
                    "try:\n"
                    "    db._drop_conn(None, os.environ['SAAS_DB_NAME'])\n"
                    "except Exception as e:\n"
                    "    print('release-error:', e)\n"
                    "print('OK')\n"
                )
                # Best-effort: don't fail the restore if release errors.
                # The dropdb --force below still terminates any leftover
                # backends.
                try:
                    self._docker_exec_python(
                        ssh, release_script,
                        env={'SAAS_DB_NAME': db_name},
                        timeout=60,
                    )
                except Exception as e:
                    self._append_log(
                        "Note: _drop_conn failed (%s); continuing." % e
                    )
            else:
                self._append_log("Stopping container...")
                try:
                    self._compute_driver(connection=ssh).stop(self._compute_handle())
                except Exception as e:
                    raise UserError(_(
                        "Failed to stop container '%s' before restore — refusing "
                        "to drop the database while connections may still be open:\n%s"
                    ) % (container_name, e))

            # 2. Download backup from cloud
            self._append_log("Downloading backup...")
            download_url = backup._generate_presigned_url()
            tmp_zip = '/tmp/saas_restore_%s.zip' % db_name
            extract_dir = '/tmp/saas_restore_%s' % db_name

            dl_cmd = 'curl -fsSL -o %s %s 2>&1' % (
                shlex.quote(tmp_zip), shlex.quote(download_url),
            )
            exit_code, stdout, stderr = ssh.execute(dl_cmd, timeout=600)
            if exit_code != 0:
                raise UserError(
                    _("Failed to download backup:\n%s\n%s") % (stdout, stderr)
                )

            # 2b. Validate the archive BEFORE touching the database. The
            # destructive dropdb is below — if this isn't a real, intact
            # Odoo backup we must bail now, while the current database is
            # still untouched. ``zipfile -l`` reads only the central
            # directory (cheap, no full read) and fails outright on a
            # non-zip or a truncated/corrupt archive; we additionally
            # require ``dump.sql`` so a random valid zip can't slip
            # through and get half-restored.
            self._append_log("Validating backup archive...")
            v_ec, v_out, v_err = ssh.execute(
                'python3 -m zipfile -l %s 2>&1' % shlex.quote(tmp_zip),
                timeout=120,
            )
            if v_ec != 0:
                raise UserError(_(
                    "The backup file isn't a valid .zip archive (it may be "
                    "corrupt or have uploaded incompletely). Nothing was "
                    "changed."
                ))
            if 'dump.sql' not in v_out:
                raise UserError(_(
                    "This .zip doesn't look like an Odoo database backup — "
                    "it has no dump.sql inside. Nothing was changed."
                ))

            # 3. Extract
            self._append_log("Extracting...")
            ssh.execute('rm -rf %s && mkdir -p %s' % (
                shlex.quote(extract_dir), shlex.quote(extract_dir),
            ))
            exit_code, stdout, stderr = ssh.execute(
                'python3 -m zipfile -e %s %s 2>&1' % (
                    shlex.quote(tmp_zip), shlex.quote(extract_dir),
                ),
                timeout=300,
            )
            if exit_code != 0:
                raise UserError(
                    _("Failed to extract backup:\n%s\n%s") % (stdout, stderr)
                )

            # 3b. Confirm the dump actually extracted and is non-empty
            # before we drop the live database — last gate before the
            # destructive step.
            chk_ec, chk_out, _chk = ssh.execute(
                'test -s %s && echo OK || echo MISSING'
                % shlex.quote('%s/dump.sql' % extract_dir),
                timeout=60,
            )
            if 'OK' not in chk_out:
                raise UserError(_(
                    "The backup is missing its database dump after "
                    "extraction — aborting before any change."
                ))

            # 4. Drop current DB and recreate empty.
            # Both commands MUST succeed — otherwise psql -f below would
            # restore into the existing (non-empty) DB and produce a
            # silently corrupted half-merged database.
            self._append_log("Dropping current database...")
            def _run_on_db_server(cmd):
                if psql_server == server:
                    return ssh.execute(cmd)
                with psql_server._get_ssh_connection() as db_ssh:
                    return db_ssh.execute(cmd)

            # Terminate any lingering backends first (older PG ignores --force).
            _run_on_db_server(
                "sudo -u postgres psql -c "
                "\"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname='%s' AND pid <> pg_backend_pid();\" 2>&1"
                % db_name.replace("'", "''")
            )
            exit_code, stdout, stderr = _run_on_db_server(
                'sudo -u postgres dropdb --force --if-exists %s 2>&1'
                % shlex.quote(db_name)
            )
            if exit_code != 0:
                raise UserError(_(
                    "dropdb failed for %s — aborting restore:\n%s\n%s"
                ) % (db_name, stdout, stderr))
            exit_code, stdout, stderr = _run_on_db_server(
                'sudo -u postgres createdb -O %s %s 2>&1'
                % (shlex.quote(self.db_user), shlex.quote(db_name))
            )
            if exit_code != 0:
                raise UserError(_(
                    "createdb failed for %s — aborting restore:\n%s\n%s"
                ) % (db_name, stdout, stderr))

            # 5. Restore dump.sql
            self._append_log("Restoring database...")
            dump_path = '%s/dump.sql' % extract_dir
            restore_cmd = (
                'PGPASSWORD=%s psql -h %s -p %d -U %s -d %s -f %s 2>&1'
            ) % (
                shlex.quote(self.db_password),
                shlex.quote(db_host),
                db_port,
                shlex.quote(self.db_user),
                shlex.quote(db_name),
                shlex.quote(dump_path),
            )
            exit_code, stdout, stderr = ssh.execute(restore_cmd, timeout=600)
            if exit_code != 0:
                self._append_log("Restore output:\n%s" % stdout[-2000:])
                raise UserError(
                    _("Database restore failed:\n%s") % stderr[-500:]
                )

            # 6. Replace filestore
            self._append_log("Restoring filestore...")
            filestore_src = '%s/filestore' % extract_dir
            filestore_dst = '%s/data/odoo/filestore/%s' % (instance_path, db_name)
            data_dir = '%s/data' % instance_path
            odoo_image = self.odoo_version_id._get_docker_image()
            fs_cmd = (
                'rm -rf %(dst)s && mkdir -p %(dst)s && '
                'if [ -d %(src)s ]; then '
                '  cp -a %(src)s/. %(dst)s/; '
                'fi && '
                'sudo chmod -R 777 %(data)s'
            ) % {
                'dst': shlex.quote(filestore_dst),
                'src': shlex.quote(filestore_src),
                'data': shlex.quote(data_dir),
            }
            ssh.execute(fs_cmd, timeout=300)

            # 7. Cleanup temp files
            ssh.execute('rm -rf %s %s' % (
                shlex.quote(tmp_zip), shlex.quote(extract_dir),
            ))

            # 8. Restart the container — service only. Hosting kept it
            # running so other databases stayed online; nothing to start.
            if not self.is_hosting:
                self._append_log("Starting container...")
                try:
                    self._compute_driver(connection=ssh).start(self._compute_handle())
                except Exception as e:
                    raise UserError(
                        _("Failed to start container:\n%s") % e
                    )

        self.state = 'running'
        self.pending_operation = False
        self._append_log("Backup '%s' restored successfully." % backup.name)
        self._safe_refresh_usage()

    def _pre_restore_setup(self):
        """Set up repos, configs, and pip packages before a DB restore.

        Ensures the restored database will find all custom modules and
        Python packages it depends on.  Called by both the paid-restore
        flow and the admin wizard.
        """
        self.ensure_one()
        self._append_log("Pre-restore: setting up repos and packages...")

        # Re-enable webhooks for repos that have a token
        for repo in self.repo_ids:
            if repo.sudo().github_token and not repo.webhook_enabled:
                repo.webhook_enabled = True

        # Clone pending customer repos
        for repo in self.repo_ids.filtered(lambda r: r.state == 'pending'):
            self._append_log("Cloning repo %s (%s)..." % (repo.repo_url, repo.branch))
            try:
                repo._clone_repo()
            except Exception as e:
                self._append_log("WARNING: Failed to clone %s: %s" % (repo.name, e))

        # Clone product repos
        if self.saas_product_id and self.saas_product_id.repo_ids:
            self._ensure_can_ssh()
            with self.docker_server_id._get_ssh_connection() as ssh:
                self._clone_product_repos(ssh)

        # Re-render configs with all addons paths
        self._ensure_can_ssh()
        with self.docker_server_id._get_ssh_connection() as ssh:
            self._render_and_write_configs(ssh)

        # Install pip packages
        if self.pip_packages:
            self._append_log("Installing pip packages...")
            try:
                pkgs = [
                    p.strip() for p in self.pip_packages.splitlines()
                    if p.strip() and not p.strip().startswith('#')
                ]
                if pkgs:
                    self._ensure_can_ssh()
                    with self.docker_server_id._get_ssh_connection() as ssh:
                        install_cmd = (
                            'mkdir -p /var/lib/odoo/pip_packages && '
                            'pip3 install --target=/var/lib/odoo/pip_packages '
                            '--upgrade --no-warn-script-location %s'
                        ) % ' '.join(shlex.quote(p) for p in pkgs)
                        res = self._compute_driver(connection=ssh).exec(
                            self._compute_handle(), install_cmd,
                            shell='bash', timeout=300,
                        )
                        if res.ok:
                            self._append_log(
                                "Pip packages installed: %s" % ', '.join(pkgs)
                            )
                        else:
                            self._append_log(
                                "WARNING: pip install issues:\n%s"
                                % (res.stdout or res.stderr)[:500]
                            )
            except Exception as e:
                self._append_log("WARNING: pip install failed: %s" % e)

        self._append_log("Pre-restore setup complete.")

    def _do_paid_restore(self):
        """Restore retained backup after the restoration invoice is paid.

        Called in a background thread by the payment handler.
        """
        self.ensure_one()
        if not self.retained_backup_path:
            self._append_log("ERROR: No retained backup path — cannot restore.")
            self.restoration_invoice_id = False
            return

        # Set state so client sees "provisioning" instead of "running"
        self.state = 'provisioning'
        self._append_log("Restoring data from retained backup (paid)...")
        self.env.cr.commit()

        Backup = self.env['saas.instance.backup']
        backup = Backup.create({
            'instance_id': self.id,
            'name': 'restored_paid_%s' % fields.Datetime.now().strftime('%Y%m%d_%H%M%S'),
            'bucket_path': self.retained_backup_path,
            'state': 'done',
        })

        # Set up all repos, configs, and pip packages BEFORE restore
        self._pre_restore_setup()

        # Restore the backup (sets state back to 'running' on success)
        self._do_restore_backup(backup.id)
        backup.unlink()

        # Re-register webhooks
        try:
            self._ensure_webhooks_registered()
        except Exception:
            pass

        # Delete the retained backup from cloud storage
        retained_path = self.retained_backup_path
        if retained_path:
            try:
                temp = Backup.new({
                    'instance_id': self.id,
                    'bucket_path': retained_path,
                })
                temp._delete_from_bucket()
                self._append_log(
                    "Retained backup deleted from cloud: %s" % retained_path
                )
            except Exception:
                _logger.exception(
                    "Failed to delete retained backup from cloud for %s",
                    self.subdomain,
                )

        # Clear restoration references and dismiss banner
        self.write({
            'restoration_invoice_id': False,
            'retained_backup_path': False,
            'restore_banner_dismissed': True,
        })
        self._append_log("Data restoration completed successfully.")



    def _get_nginx_template_name(self):
        """Return the appropriate nginx template based on the Odoo version's nginx_template field."""
        self.ensure_one()
        if self.odoo_version_id.nginx_template == 'old':
            return 'nginx_old_odoo_versions.jinja'
        return 'nginx_new_odoo_versions.jinja'

    def _provision_nginx(self, ssh, backend_ip=None):
        """Obtain SSL certificate via Certbot and deploy Nginx config.

        Args:
            ssh: SSH connection to the server where Nginx will be configured
                 (proxy server or Docker server).
            backend_ip: IP address of the Docker server. When None (single-server
                        setup), the Nginx upstream uses 127.0.0.1. When set
                        (proxy server setup), it uses the Docker server's IP.
        """
        self.ensure_one()
        domain = self.name  # e.g. acme.odoo.example.com
        if not domain:
            raise UserError(_("Instance domain name is not set."))

        # Step 1: Obtain SSL certificate via Certbot
        self._append_log("Requesting SSL certificate for %s..." % domain)
        certbot_cmd = (
            'certbot certonly --nginx -d %s '
            '--non-interactive --agree-tos '
            '--register-unsafely-without-email 2>&1'
        ) % shlex.quote(domain)
        exit_code, stdout, stderr = ssh.execute(certbot_cmd, timeout=120)
        if exit_code != 0:
            # Try standalone mode as fallback
            self._append_log(
                "Certbot --nginx failed, trying standalone mode..."
            )
            certbot_cmd = (
                'certbot certonly --standalone -d %s '
                '--non-interactive --agree-tos '
                '--register-unsafely-without-email 2>&1'
            ) % shlex.quote(domain)
            exit_code, stdout, stderr = ssh.execute(certbot_cmd, timeout=120)
            if exit_code != 0:
                raise UserError(
                    _("Failed to obtain SSL certificate for '%s':\n%s\n%s")
                    % (domain, stdout[-500:], stderr[-500:])
                )
        self._append_log("SSL certificate obtained for %s." % domain)

        # Step 2: Render Nginx config from the appropriate template
        template_name = self._get_nginx_template_name()
        nginx_context = {
            'subdomain': self.subdomain,
            'subdomainchat': '%s-chat' % self.subdomain,
            'http_port': self.xmlrpc_port,
            'longpolling_port': self.longpolling_port,
            'domain': domain,
        }
        if backend_ip:
            nginx_context['backend_ip'] = backend_ip
        nginx_content = self._render_template(template_name, nginx_context)

        # Step 3+4: Atomically install the vhost and reload nginx, serialised
        # per-proxy and crash-safe (SCALE-001/003).
        self._append_log("Installing Nginx config for %s..." % self.subdomain)
        self._nginx_apply_vhost(ssh, nginx_content)
        self._append_log("Nginx reloaded successfully.")

    def _restic_wipe_repo(self):
        """Drop every snapshot in the instance's restic repo.

        Used by the cancellation flow when there is NO fresh snapshot
        to retain (e.g. the pre-cancel snapshot attempt failed or the
        instance was never deployed). ``forget --keep-last 0`` plus
        ``--prune`` removes every snapshot and frees the deduplicated
        data from the bucket. Best-effort: a failure here just leaves
        stale objects in cloud storage — it doesn't break cancellation.
        """
        self.ensure_one()
        if not self.docker_server_id:
            return
        Backup = self.env['saas.instance.backup'].sudo()
        gcs_path = None
        try:
            with self.docker_server_id._get_ssh_connection() as ssh:
                Backup._ensure_restic_installed(
                    ssh, self.docker_server_id.name,
                )
                gcs_path = Backup._stage_gcs_credentials(ssh, self)
                env = Backup._restic_env_vars(self, gcs_path)
                # ``--keep-last 0`` would be rejected by restic
                # (minimum 1); ``forget <id>`` per snapshot is the
                # safest cross-version way to drop everything. So:
                # list snapshot IDs, then forget them by id.
                list_cmd = Backup._restic_cmd(
                    env,
                    ['snapshots', '--json', '--quiet'],
                )
                ec, out, err = ssh.execute(list_cmd, timeout=600)
                if ec != 0:
                    # No repo or empty repo → nothing to wipe.
                    if 'unable to open config file' in (err or '').lower():
                        return
                    _logger.warning(
                        "restic snapshots on wipe for %s exit=%s "
                        "err=%r",
                        self.subdomain, ec, (err or '')[-300:],
                    )
                    return
                import json as _json
                try:
                    snaps = _json.loads(out or '[]')
                except Exception:
                    snaps = []
                ids = [s.get('short_id') or s.get('id') for s in snaps]
                ids = [i for i in ids if i]
                if not ids:
                    return
                cmd = Backup._restic_cmd(
                    env,
                    ['forget', '--prune', '--quiet'] + ids,
                )
                ec, out, err = ssh.execute(cmd, timeout=1800)
                if ec != 0:
                    _logger.warning(
                        "restic forget --prune (wipe) for %s exit=%s "
                        "out=%r err=%r",
                        self.subdomain, ec,
                        (out or '')[-300:], (err or '')[-300:],
                    )
        finally:
            if gcs_path:
                try:
                    with self.docker_server_id._get_ssh_connection() as ssh2:
                        Backup._unstage_gcs_credentials(ssh2, gcs_path)
                except Exception:
                    pass

    def _restic_keep_only_run_tag(self, run_tag):
        """Prune the instance's restic repo to a single retained run.

        Used by the cancellation flow to delete every snapshot except
        the one we keep so the customer can restore after reactivation.
        ``restic forget --keep-tag run=<tag>`` keeps anything carrying
        that tag and drops the rest; ``--prune`` then frees the
        deduplicated data of the dropped snapshots from the bucket.

        Best-effort: a failure here just leaves stale data in the
        bucket — it doesn't break the cancellation.
        """
        self.ensure_one()
        if not run_tag:
            return
        if not self.docker_server_id:
            return
        Backup = self.env['saas.instance.backup'].sudo()
        gcs_path = None
        try:
            with self.docker_server_id._get_ssh_connection() as ssh:
                Backup._ensure_restic_installed(
                    ssh, self.docker_server_id.name,
                )
                gcs_path = Backup._stage_gcs_credentials(ssh, self)
                env = Backup._restic_env_vars(self, gcs_path)
                cmd = Backup._restic_cmd(
                    env,
                    [
                        'forget', '--prune',
                        '--keep-tag', 'run=' + run_tag,
                        '--group-by', 'host,tags',
                        '--quiet',
                    ],
                )
                ec, out, err = ssh.execute(cmd, timeout=1800)
                if ec != 0:
                    _logger.warning(
                        "restic forget on cancel for %s exit=%s "
                        "out=%r err=%r",
                        self.subdomain, ec,
                        out[-300:], err[-300:],
                    )
        finally:
            if gcs_path:
                try:
                    with self.docker_server_id._get_ssh_connection() as ssh2:
                        Backup._unstage_gcs_credentials(ssh2, gcs_path)
                except Exception:
                    pass

    def _refresh_nginx_config(self, ssh, backend_ip=None,
                             http_port=None, longpolling_port=None):
        """Re-render the nginx vhost and reload — no certbot step.

        Used by the restore flow to apply any topology / port / plan
        change captured on the saas.instance record without paying the
        ~10–60 s cost of certbot (the cert is already there from the
        initial deploy and certbot is its own slow round trip).

        ``http_port``/``longpolling_port`` override the instance's stored
        ports — the blue/green deploy uses this to flip traffic to a
        transient green container without mutating the record.
        """
        self.ensure_one()
        domain = self.name
        if not domain:
            raise UserError(_("Instance domain name is not set."))
        template_name = self._get_nginx_template_name()
        nginx_context = {
            'subdomain': self.subdomain,
            'subdomainchat': '%s-chat' % self.subdomain,
            'http_port': http_port or self.xmlrpc_port,
            'longpolling_port': longpolling_port or self.longpolling_port,
            'domain': domain,
        }
        if backend_ip:
            nginx_context['backend_ip'] = backend_ip
        nginx_content = self._render_template(template_name, nginx_context)
        # Atomic, per-proxy-serialised install + reload (SCALE-001/003).
        self._nginx_apply_vhost(ssh, nginx_content)

    def _refresh_nginx_on_correct_host(self):
        """Pick the right server (proxy vs docker host) and call
        ``_refresh_nginx_config``. Mirrors the topology dispatch in
        ``_do_deploy`` so initial deploy and restore stay in sync.
        """
        self.ensure_one()
        proxy_server = self.domain_id.proxy_server_id
        if proxy_server and proxy_server != self.docker_server_id:
            with proxy_server._get_ssh_connection() as proxy_ssh:
                self._refresh_nginx_config(
                    proxy_ssh, backend_ip=self._get_proxy_backend_ip(),
                )
        else:
            with self.docker_server_id._get_ssh_connection() as ssh:
                self._refresh_nginx_config(ssh)

    def _nginx_apply_vhost(self, ssh, content):
        """Atomically install this instance's vhost and reload nginx.

        Serialised + crash-safe replacement for the old
        write-direct-then-`nginx -t`-then-reload sequence:

        * The rendered config is SFTP'd to a private temp file OUTSIDE
          ``sites-enabled`` (dot-prefixed under ``/etc/nginx``), so a partial
          write (SSH drop, disk full) can never land in the included glob and
          wedge ``nginx -t`` for every co-located tenant (SCALE-003).
        * The placement (``mv`` is atomic on one filesystem), validation and
          reload run inside a host-side ``flock`` so concurrent provisions on
          a SHARED proxy serialise instead of racing the reload (SCALE-001).
        * On a failed ``nginx -t`` the previous vhost is restored and nginx is
          NOT reloaded, so live routing is never disrupted by a bad deploy.
        """
        self.ensure_one()
        sub = self.subdomain
        path = '/etc/nginx/sites-enabled/%s' % sub
        tmp = '/etc/nginx/.saas-%s.tmp' % sub
        bak = '/etc/nginx/.saas-%s.bak' % sub
        terr = '/etc/nginx/.saas-%s.terr' % sub
        ssh.write_file(tmp, content)
        script = (
            "set -e\n"
            "mv -f {path} {bak} 2>/dev/null || true\n"
            "mv -f {tmp} {path}\n"
            "if ! nginx -t 2>{terr}; then\n"
            "  rm -f {path}; mv -f {bak} {path} 2>/dev/null || true\n"
            "  cat {terr} >&2; rm -f {terr}; exit 10\n"
            "fi\n"
            "rm -f {bak} {terr}\n"
            "systemctl reload nginx\n"
        ).format(
            path=shlex.quote(path), tmp=shlex.quote(tmp),
            bak=shlex.quote(bak), terr=shlex.quote(terr),
        )
        cmd = 'flock -w 60 %s bash -c %s' % (
            shlex.quote(_NGINX_LOCK_FILE), shlex.quote(script),
        )
        exit_code, stdout, stderr = ssh.execute(cmd, timeout=180)
        if exit_code != 0:
            # Best-effort cleanup of the temp (the script removes it on the
            # happy/rollback paths; this covers a flock timeout that never ran
            # the body).
            ssh.execute('rm -f %s' % shlex.quote(tmp))
            raise UserError(
                _("Nginx configuration failed:\n%s\n%s") % (stdout, stderr)
            )

    def _nginx_remove_vhost(self, ssh):
        """Remove this instance's vhost and reload nginx, under the per-proxy
        lock so the removal+reload can't race a concurrent provision's
        ``nginx -t``/reload on a shared proxy (SCALE-001)."""
        self.ensure_one()
        path = '/etc/nginx/sites-enabled/%s' % self.subdomain
        script = "rm -f {path}\nsystemctl reload nginx\n".format(
            path=shlex.quote(path))
        cmd = 'flock -w 60 %s bash -c %s' % (
            shlex.quote(_NGINX_LOCK_FILE), shlex.quote(script),
        )
        ssh.execute(cmd, timeout=120)

    def _remove_nginx(self, ssh):
        """Remove Nginx config and SSL certificate from the given server."""
        self.ensure_one()
        self._nginx_remove_vhost(ssh)
        if self.name:
            ssh.execute(
                'certbot delete --cert-name %s --non-interactive 2>&1'
                % shlex.quote(self.name)
            )

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
        state, and applies the single minimal action to converge. ``connection``
        reuses an open SSH for batching. Returns the action taken (for tests/logs).
        Skips instances mid-operation (a deploy/op owns the container then)."""
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
                # The container is GONE. driver.start() is `docker compose up -d`,
                # which recreates it from the on-host compose files. If those are
                # ALSO gone (host wiped / reprovisioned / failed migration),
                # compose-up fails — escalate to a full redeploy that re-renders
                # the configs and rebuilds it, instead of failing every cycle.
                _logger.warning(
                    "[reconcile] %s container missing — recreating", self.subdomain)
                self._append_log("Container not found — auto-recreating.")
                try:
                    driver.start(handle)            # compose up -d → recreate
                    action = 'recreated'
                except Exception as e:
                    _logger.warning(
                        "[reconcile] %s compose-up failed (%s) — full redeploy",
                        self.subdomain, str(e)[-200:])
                    self._append_log(
                        "Compose files missing — triggering a full redeploy to "
                        "rebuild the server from scratch.")
                    try:
                        self.action_redeploy()      # re-render configs + up (async)
                        action = 'recreating'
                    except Exception:
                        _logger.exception(
                            "[reconcile] redeploy escalation failed for %s",
                            self.subdomain)
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
        tenant toward its desired state. Replaces the ad-hoc health-check;
        grouped by Docker server for batched SSH."""
        instances = self.search([
            ('state', 'in', list(self._RECONCILE_RUNNING + self._RECONCILE_STOPPED)),
            ('docker_server_id', '!=', False),
        ])
        if not instances:
            return 0
        by_server = {}
        for inst in instances:
            by_server.setdefault(inst.docker_server_id, self.browse())
            by_server[inst.docker_server_id] |= inst
        acted = 0
        for server, insts in by_server.items():
            try:
                with server._get_ssh_connection() as ssh:
                    for inst in insts:
                        try:
                            if inst.reconcile(connection=ssh) not in ('skipped', 'none'):
                                acted += 1
                        except Exception:
                            _logger.exception(
                                "[reconcile] failed for instance %s", inst.name)
            except Exception:
                _logger.exception(
                    "[reconcile] cannot reach server %s", server.name)
        return acted

    @api.model
    def _cron_check_container_health(self):
        """[Phase 3] Back-compat shim: the health check is now the reconciler."""
        return self._cron_reconcile()

    def _cron_check_storage_limits(self):
        """Check total storage of running instances and suspend those exceeding their plan limit.

        Batches SSH calls by server to avoid opening N connections sequentially.
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

        # Pre-fetch DB sizes per db_server in a single batched query.
        db_sizes_by_server = {}
        for db_server in instances.mapped('db_server_id'):
            if not db_server:
                continue
            subdomains = [
                i.subdomain for i in instances
                if i.db_server_id == db_server
                and i.subdomain
                and SUBDOMAIN_RE.match(i.subdomain)
            ]
            if not subdomains:
                continue
            try:
                db_sizes_by_server[db_server.id] = \
                    db_server._fetch_database_sizes(subdomains)
            except Exception:
                _logger.exception(
                    "Storage cron: failed to batch-fetch sizes from %s",
                    db_server.name,
                )
                db_sizes_by_server[db_server.id] = {}

        # Group instances by docker server for batched SSH calls
        by_docker_server = {}
        for inst in instances:
            by_docker_server.setdefault(inst.docker_server_id.id, self.browse())
            by_docker_server[inst.docker_server_id.id] |= inst

        for server_id, server_instances in by_docker_server.items():
            server = server_instances[0].docker_server_id
            try:
                with server._get_ssh_connection() as ssh:
                    for instance in server_instances:
                        try:
                            db_sizes = db_sizes_by_server.get(
                                instance.db_server_id.id, {}
                            )
                            instance._refresh_usage_with_ssh(
                                ssh,
                                precomputed_db_size=db_sizes.get(instance.subdomain),
                            )
                        except Exception:
                            _logger.exception(
                                "Failed to refresh usage for instance %s (id=%s)",
                                instance.subdomain, instance.id,
                            )
                            continue

                        # v47: evaluate the capacity state machine (warn →
                        # full → grace → paused). NEVER an automatic charge.
                        try:
                            instance._evaluate_capacity()
                        except Exception:
                            _logger.exception(
                                "Capacity evaluation failed for %s",
                                instance.subdomain)
            except Exception:
                _logger.exception(
                    "Failed to connect to docker server id=%s for storage checks", server_id,
                )

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
                # Clear any pending upgrade that was never paid (capture
                # the plan name BEFORE clearing — otherwise the log entry
                # below would dereference a False record).
                if instance.pending_plan_id:
                    pending_name = instance.pending_plan_id.name
                    instance.write({
                        'pending_plan_id': False,
                        'pending_billing_period': False,
                        'pending_change_invoice_id': False,
                    })
                    instance._append_log(
                        "Pending upgrade to %s cleared (trial expired)."
                        % pending_name
                    )
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

    def _update_repo_config_and_restart(self):
        """Regenerate docker-compose.yml and odoo.conf with repo mounts, then restart."""
        self.ensure_one()
        self._ensure_can_ssh()
        server = self.docker_server_id

        with server._get_ssh_connection() as ssh:
            self._render_and_write_configs(ssh)

        # Restart the container
        self._restart_container()

    def _restart_container(self):
        """Restart the Docker container via docker compose."""
        self.ensure_one()
        self._ensure_can_ssh()
        server = self.docker_server_id
        instance_path = self._get_instance_path()

        with server._get_ssh_connection() as ssh:
            self._append_log("Restarting container...")
            # down + up (via driver, reusing this ssh) to pick up volume changes
            driver = self._compute_driver(connection=ssh)
            handle = self._compute_handle()
            try:
                driver.destroy(handle)
                driver.start(handle)
            except Exception as e:
                raise UserError(
                    _("Failed to restart container '%s':\n%s")
                    % (self._get_container_name(), e)
                )
            self._append_log("Container restarted successfully.")

    def _apply_pip_packages(self):
        """Force-install the instance's Python packages NOW via docker exec,
        capturing any failure so it can be shown to the customer.

        Returns (ok: bool, output: str). On failure ``pip_install_error`` is
        set to the pip output (surfaced in the portal) and ``ok`` is False —
        the caller decides whether to restart. On success the field is
        cleared and the requirements checksum is written so the container
        entrypoint won't redo the install on the next restart.

        Requires the container to be running (docker exec). ``--force
        -reinstall`` is used so a package is always (re)installed cleanly.
        """
        self.ensure_one()
        pkgs = [
            p.strip() for p in (self.pip_packages or '').splitlines()
            if p.strip() and not p.strip().startswith('#')
        ]
        if not pkgs:
            self.pip_install_error = False
            return True, ''
        self._ensure_can_ssh()
        self._append_log("Installing pip packages (forced): %s" % ', '.join(pkgs))
        # Install, then on success stamp the checksum so the boot-time
        # entrypoint skips a duplicate install. ``command`` is the literal
        # bash script — the driver quotes it (so awk's single quotes are fine).
        install = (
            'mkdir -p /var/lib/odoo/pip_packages && '
            'pip3 install --target=/var/lib/odoo/pip_packages --upgrade '
            '--force-reinstall --no-warn-script-location %s '
            '&& md5sum /etc/odoo/requirements.txt 2>/dev/null '
            "| awk '{print $1}' > /var/lib/odoo/pip_packages/.requirements.md5 "
            '&& rm -f /var/lib/odoo/pip_packages/.pip_error'
        ) % ' '.join(shlex.quote(p) for p in pkgs)
        with self.docker_server_id._get_ssh_connection() as ssh:
            res = self._compute_driver(connection=ssh).exec(
                self._compute_handle(), install, shell='bash', timeout=900)
        exit_code = res.rc
        output = (res.stdout or res.stderr or '').strip()
        if exit_code == 0:
            self.pip_install_error = False
            self._append_log("Pip packages installed: %s" % ', '.join(pkgs))
            return True, output
        # Keep only the tail — pip output can be long.
        self.pip_install_error = output[-4000:] or _("pip install failed.")
        self._append_log("ERROR: pip install failed:\n%s" % output[-1000:])
        return False, output

    def _deploy_pip_packages(self):
        """Persist requirements, force-install now (capturing errors), and
        restart only on success. Returns (ok, output). On failure the
        instance keeps running with its previous packages and the error is
        stored on ``pip_install_error`` for the portal to show."""
        self.ensure_one()
        self._ensure_can_ssh()
        # Regenerate requirements.txt / pip_install.sh from pip_packages so
        # a future rebuild is consistent with what we install now.
        with self.docker_server_id._get_ssh_connection() as ssh:
            self._render_and_write_configs(ssh)
        ok_install, output = self._apply_pip_packages()
        if ok_install:
            self._restart_container()
        return ok_install, output

    # ========== Recurring Billing ==========

    def _set_next_invoice_date(self):
        """Compute and write the next invoice date based on the instance billing period."""
        self.ensure_one()
        if not self.plan_id or self.is_trial:
            return
        today = fields.Date.today()
        period = self.billing_period or 'monthly'
        if period == 'yearly':
            interval = relativedelta(years=1)
        else:
            interval = relativedelta(months=1)
        self.next_invoice_date = today + interval
        self.last_invoice_date = today
        self.suspension_warning_sent = False
        # New cycle anchor: re-arm the auto-renew reminders (A1).
        self.renewal_reminder_7d_sent = False
        self.renewal_reminder_1d_sent = False

        # Anchor the daily-backup add-on's billing cycle the first time the
        # instance is activated with backups already enabled (e.g. the
        # customer ticked "daily backups" at checkout). The add-on now follows
        # the PLAN's period: the activation invoice already charged the whole
        # period (1 month / 12 months — see ``action_confirm_and_bill`` →
        # ``_snapshot_order_line``), so the next backup charge falls on the
        # plan's renewal date and is merged into that renewal. Only set it
        # once, and only when a price is actually configured — otherwise the
        # renewal cron would log "price not configured" every day.
        if (self.daily_backup_enabled
                and not self.daily_backup_next_invoice_date
                and self._get_daily_backup_price() > 0):
            self.daily_backup_last_invoice_date = today
            self.daily_backup_next_invoice_date = self.next_invoice_date

    # ============================================================
    # Saved card + auto-renewal
    # ============================================================
    def _capture_payment_token_from_invoice(self, invoice):
        """Persist the SAFE references of the tokenized method used to pay
        ``invoice`` so auto-renew can charge it later (A1).

        PCI scope: NOTHING sensitive is stored. We persist only the
        provider id + external customer ref + external token ref via
        ``saas.payment.method`` (which wraps Odoo's ``payment.token`` —
        itself SAQ-A: a provider-side reference plus a masked label). The
        token only exists when the customer opted to save the method
        (Odoo's ``tokenize`` flag), so retention stays customer-controlled.

        Accepts ``done`` and ``pending`` transactions: a token row is
        created before a 3DS-pending tx terminates, and it's gated by
        ``active`` so a tx that ultimately fails is never used."""
        self.ensure_one()
        if not invoice or self.saas_payment_method_id:
            # Don't overwrite an existing saved method — switching methods
            # is an explicit portal action.
            return
        tx = invoice.transaction_ids.filtered(
            lambda t: t.state in ('done', 'pending') and t.token_id
            and t.token_id.active
        )[:1]
        if not tx:
            return
        method = self.env['saas.payment.gateway']._save_method_from_transaction(
            self.partner_id, tx)
        if not method:
            return
        # Keep the legacy pointer in sync for the existing charge path and
        # make this the instance's auto-renew method.
        self.write({
            'saas_payment_method_id': method.id,
            'payment_token_id': method.token_id.id,
        })
        self._append_log(
            "Payment method saved for auto-renewal: %s (provider %s). You "
            "can remove or replace it any time from Billing settings."
            % (method.display_label or 'card', method.provider_code or '—')
        )

    def _try_auto_charge_invoice(self, invoice, kind):
        """Attempt to auto-charge ``invoice`` using the saved card.

        ``kind`` is 'subscription' or 'snapshot' — used only for log
        prefixes so the operator can tell renewal flows apart in
        the journal.

        Returns ``True`` only when the transaction reaches the
        terminal ``done`` state. ``pending`` (e.g. 3DS in flight) is
        treated as "wait and re-check" — the caller suppresses the
        payment-due email in that case so the customer isn't pinged
        for an invoice that may still settle. A genuine failure
        returns ``False`` and leaves the invoice unpaid for dunning.

        Pre-flight checks:
        - token must be active and its provider must be enabled, or
          we surface a "Please add a new card" message instead of
          letting Odoo throw deep in ``_send_payment_request``.
        - invoice currency must match the token's provider — a
          mismatch would charge the wrong amount or fail; we log
          and skip rather than try.
        """
        self.ensure_one()
        if not invoice or invoice.payment_state in ('paid', 'in_payment'):
            return False
        method = self._auto_renew_method()
        if not method:
            self._record_payment_attempt(
                invoice, 'failed',
                _("No saved payment method on file."))
            self._append_log(
                "Auto-renew skipped for invoice %s — no saved payment "
                "method. Please add one from Billing settings." % invoice.name)
            return False
        # Delegate the actual charge to the provider-agnostic gateway.
        state, message = self.env['saas.payment.gateway']._charge(method, invoice)
        self._record_payment_attempt(invoice, state, message)
        if state == 'done':
            self._append_log(
                "Auto-renew charged %s for invoice %s (%.2f %s)." % (
                    method.display_label or 'saved method', invoice.name,
                    invoice.amount_total, invoice.currency_id.name))
            return True
        if state == 'pending':
            # In-flight (3DS / async gateway). Suppress the payment-due
            # email; the dunning + retry crons revisit a stuck pending.
            _logger.info(
                "[AUTO-RENEW:%s] pending for %s invoice %s.",
                kind, self.subdomain, invoice.name)
            self._append_log(
                "Auto-renew for invoice %s is awaiting bank confirmation."
                % invoice.name)
            return True
        # Failure — leave the invoice for the retry schedule + dunning.
        _logger.info(
            "[AUTO-RENEW:%s] charge failed for %s invoice %s: %s",
            kind, self.subdomain, invoice.name, message)
        self._append_log(
            "Auto-renew charge for invoice %s did not go through (%s)."
            % (invoice.name, message))
        return False

    def _auto_renew_method(self):
        """The saved payment method auto-renew should use: the instance's
        chosen method, else the customer's default. Empty if none/disabled."""
        self.ensure_one()
        method = self.saas_payment_method_id
        if method and method.active and method.token_id and method.token_id.active:
            return method
        return self.env['saas.payment.method']._default_for_partner(
            self.partner_id)

    def _record_payment_attempt(self, invoice, state, message=''):
        """Append a ``saas.payment.attempt`` audit row for the retry trail."""
        self.ensure_one()
        if not invoice:
            return
        prior = self.env['saas.payment.attempt'].sudo().search_count(
            [('move_id', '=', invoice.id)])
        self.env['saas.payment.attempt'].sudo().create({
            'move_id': invoice.id,
            'instance_id': self.id,
            'attempt_no': prior + 1,
            'attempted_on': fields.Date.today(),
            'state': state,
            'message': (message or '')[:500],
        })

    # ========== Auto-renew reminders + retry schedule (A1) ==========
    @api.model
    def _cron_send_renewal_reminders(self):
        """Notify customers 7 days and 1 day before their renewal date so
        they (and their saved card) are ready for the auto-charge."""
        today = fields.Date.today()
        for offset, flag in (
            (7, 'renewal_reminder_7d_sent'),
            (1, 'renewal_reminder_1d_sent'),
        ):
            target = today + relativedelta(days=offset)
            instances = self.search([
                ('state', '=', 'running'),
                ('is_trial', '=', False),
                ('plan_id', '!=', False),
                # Children (staging/dev) never self-bill — the project's
                # Production anchor carries the whole subscription.
                ('parent_id', '=', False),
                ('next_invoice_date', '=', target),
                (flag, '=', False),
            ])
            for instance in instances:
                try:
                    instance._send_notification(
                        'saas_core.mail_template_saas_renewal_reminder')
                    instance.write({flag: True})
                    self.env.cr.commit()
                except Exception:
                    self.env.cr.rollback()
                    _logger.exception(
                        "Renewal reminder (%dd) failed for %s",
                        offset, instance.subdomain)

    @api.model
    def _cron_retry_failed_payments(self):
        """Re-attempt the auto-charge of still-unpaid mandatory invoices on
        the retry schedule (1, 3 and 5 days after the invoice date). Runs
        within the grace period, so suspension only follows the LAST retry
        (handled by the dunning cron)."""
        today = fields.Date.today()
        instances = self.search([
            ('state', 'in', ('running', 'stopped')),
            ('is_trial', '=', False),
            ('parent_id', '=', False),
            ('auto_renew_subscription', '=', True),
        ])
        for instance in instances:
            try:
                instance._retry_failed_payments(today)
                self.env.cr.commit()
            except Exception:
                self.env.cr.rollback()
                _logger.exception(
                    "Payment retry failed for %s", instance.subdomain)

    @staticmethod
    def _payment_due_date(move):
        """THE single payment timing anchor (v47/F4): every retry, reminder,
        dunning and suspension decision derives from this — the invoice due
        date, falling back to the invoice date. No other date math is used
        for payment timing anywhere, so the schedules can never diverge."""
        return move.invoice_date_due or move.invoice_date

    def _retry_failed_payments(self, today):
        """Retry every still-unpaid MANDATORY invoice whose age (measured
        from its DUE date — F4) matches the retry schedule and that hasn't
        already been attempted today."""
        self.ensure_one()
        if not self._auto_renew_method():
            return
        Attempt = self.env['saas.payment.attempt'].sudo()
        invoices = self._get_all_invoices().filtered(
            lambda m: m.move_type == 'out_invoice'
            and m.state == 'posted'
            and m.payment_state not in ('paid', 'in_payment')
            and self._payment_due_date(m)
            and not self._is_optional_invoice(m)
        )
        for inv in invoices:
            age = (today - self._payment_due_date(inv)).days
            if age not in PAYMENT_RETRY_OFFSET_DAYS:
                continue
            if Attempt.search_count([
                    ('move_id', '=', inv.id), ('attempted_on', '=', today)]):
                continue  # already tried today
            self._append_log(
                "Auto-renew retry (due+%d) for invoice %s." % (age, inv.name))
            self._try_auto_charge_invoice(inv, kind='retry')

    @api.model
    def _cron_generate_recurring_invoices(self):
        """Generate renewal invoices for running instances whose billing
        cycle has elapsed.  Skips trials and instances without a plan."""
        today = fields.Date.today()
        instances = self.search([
            ('state', '=', 'running'),
            ('is_trial', '=', False),
            ('plan_id', '!=', False),
            ('parent_id', '=', False),
            ('next_invoice_date', '<=', today),
        ])
        for instance in instances:
            try:
                instance._generate_renewal_invoice()
                self.env.cr.commit()
            except Exception:
                self.env.cr.rollback()
                _logger.exception(
                    "Failed to generate renewal invoice for %s",
                    instance.subdomain,
                )

    def _paid_pending_change_invoice(self):
        """Paid invoice for the current pending plan change, or empty.

        Prefers the explicit ``pending_change_invoice_id`` link. Falls
        back (for changes requested before that field existed) to the
        newest posted invoice of ``sale_order_id`` — but only when that
        order's origin is an upgrade/subscription token, so an older
        paid invoice can never re-apply a NEW unpaid pending change.
        """
        self.ensure_one()
        paid = ('paid', 'in_payment')
        empty = self.env['account.move']
        inv = self.pending_change_invoice_id
        if inv:
            if inv.state == 'posted' and inv.payment_state in paid:
                return inv
            return empty
        # Legacy fallback: _request_upgrade / action_subscribe_from_trial
        # always pointed sale_order_id at the change's own order.
        ref = self.name or self.subdomain
        order = self.sale_order_id
        if not (ref and order) or order.origin not in (
            ORIGIN_PLAN_UPGRADE % ref, ORIGIN_SUBSCRIPTION % ref,
        ):
            return empty
        latest = order.invoice_ids.filtered(
            lambda m: m.state == 'posted'
        ).sorted('create_date', reverse=True)[:1]
        if latest and latest.payment_state in paid:
            return latest
        return empty

    @api.model
    def _cron_apply_paid_pending_changes(self):
        """Safety net for the payment hook: apply pending plan changes
        whose invoice is already paid.

        The normal path is ``_saas_check_instance_payment()`` firing when
        the invoice's payment_state flips, then applying the change in a
        background thread. If that is ever missed (thread killed mid-way,
        payment post-processed while the module was upgrading, hook error
        rolled back with the payment retried independently), the customer
        has paid but stays on the old plan forever — there was no retry.
        This cron converges those stragglers.
        """
        instances = self.search([('pending_plan_id', '!=', False)])
        for instance in instances:
            try:
                invoice = instance._paid_pending_change_invoice()
                if not invoice:
                    continue
                method = ('_apply_pending_upgrade' if instance.is_trial
                          else '_apply_pending_plan_change')
                _logger.info(
                    "SaaS instance %s: invoice %s for pending change to %s "
                    "is paid but the change was never applied — applying "
                    "now (safety-net cron, %s).",
                    instance.subdomain, invoice.name,
                    instance.pending_plan_id.name, method,
                )
                instance._append_log(
                    "Pending plan change invoice %s is paid but the change "
                    "was never applied — applying now (safety-net cron)."
                    % invoice.name
                )
                getattr(instance, method)()
                self.env.cr.commit()
            except Exception:
                self.env.cr.rollback()
                _logger.exception(
                    "Safety-net cron failed to apply paid pending plan "
                    "change for %s", instance.subdomain,
                )

    @api.model
    def _daily_backup_unpaid_invoices(self):
        """Posted, still-unpaid invoices that cover this instance's daily
        backups, newest first. Two sources:

        1. Standalone backup add-on invoices (origin SAAS:BACKUP-ADDON) —
           the separate monthly cycle.
        2. **Merged renewals (M4):** when the snapshot month is folded into
           the main renewal (M3), the snapshot charge lives on a
           SAAS:RENEWAL invoice as a daily-backup product line. An unpaid
           such renewal means the backup month is unpaid, so it must pause
           snapshots exactly like an unpaid standalone backup invoice.
        """
        self.ensure_one()
        sub_ref = self.name or self.subdomain

        def _unpaid(moves):
            return moves.filtered(
                lambda m: m.state == 'posted'
                and m.payment_state not in (
                    'paid', 'in_payment', 'reversed', 'invoicing_legacy',
                )
                and m.amount_residual > 0
            )

        SO = self.env['sale.order'].sudo()
        # 1) standalone backup add-on invoices
        backup_orders = SO.search([
            ('origin', '=', ORIGIN_BACKUP_ADDON % sub_ref)])
        invs = _unpaid(backup_orders.invoice_ids)

        # 2) renewal invoices carrying a merged daily-backup line
        backup_product = self.env['product.product'].sudo().search(
            [('default_code', '=', 'SAAS-BACKUP-ADDON')], limit=1)
        if backup_product:
            renewal_orders = SO.search([
                ('origin', '=', ORIGIN_RENEWAL % sub_ref)])
            merged = _unpaid(renewal_orders.invoice_ids).filtered(
                lambda m: any(
                    line.product_id == backup_product
                    for line in m.invoice_line_ids
                )
            )
            invs |= merged

        return invs.sorted('invoice_date_due')

    def _sync_daily_backup_suspension(self):
        """Pause snapshots when the monthly add-on invoice is overdue;
        resume them once it's paid. Idempotent — safe to call from the
        renewal cron and from the payment hook."""
        self.ensure_one()
        # Applies to any instance with the add-on (hosting or services).
        if not self.daily_backup_enabled:
            return
        cutoff = fields.Date.today() - relativedelta(
            days=DAILY_BACKUP_SUSPEND_GRACE_DAYS,
        )
        overdue = self._daily_backup_unpaid_invoices().filtered(
            lambda m: m.invoice_date_due and m.invoice_date_due < cutoff
        )
        should_suspend = bool(overdue)
        if should_suspend and not self.daily_backup_suspended:
            self.daily_backup_suspended = True
            self._append_log(
                "Daily snapshots PAUSED — the monthly backup add-on "
                "invoice is overdue. They resume automatically once it's "
                "paid."
            )
            self.message_post(body=_(
                "Daily snapshots paused: the monthly backup add-on "
                "invoice is overdue. Snapshots resume automatically as "
                "soon as the invoice is paid."
            ))
        elif not should_suspend and self.daily_backup_suspended:
            self.daily_backup_suspended = False
            self._append_log(
                "Daily snapshots RESUMED — backup add-on is paid up."
            )
            self.message_post(body=_(
                "Daily snapshots resumed — your backup add-on is paid up."
            ))

    def _cron_renew_daily_backup_addons(self):
        """Maintain the daily-backup add-on: pause/resume snapshots based on
        whether the add-on is paid up.

        The add-on no longer has a billing cycle of its own: it follows the
        SUBSCRIPTION's period and its charge is merged into the plan's renewal
        invoice (see ``_generate_renewal_invoice``), so there is no standalone
        backup invoice to issue here. This cron only enforces the pause/resume
        of snapshots when a merged renewal carrying the backup line goes
        overdue. Skips trials and instances whose backup flag is off.
        """
        instances = self.search([
            ('state', '=', 'running'),
            ('is_trial', '=', False),
            ('daily_backup_enabled', '=', True),
        ])
        for instance in instances:
            try:
                instance._sync_daily_backup_suspension()
                self.env.cr.commit()
            except Exception:
                self.env.cr.rollback()
                _logger.exception(
                    "Daily-backup add-on maintenance failed for %s",
                    instance.subdomain,
                )

    def _generate_renewal_invoice(self):
        """Create a new sale order + invoice for the next billing period.

        If a downgrade is scheduled, apply it first so the renewal
        invoice uses the new (lower) plan price.
        """
        self.ensure_one()
        if self.is_trial:
            return

        # Idempotency / concurrency guard (BIZ-009): lock this instance row so
        # two overlapping cron runs can't both bill the same cycle. After the
        # lock is granted we re-read next_invoice_date from the row — under
        # READ COMMITTED the loser sees the value the winner already advanced
        # past today and returns, so each cycle is invoiced exactly once.
        today = fields.Date.today()
        self.env.cr.execute(
            "SELECT next_invoice_date FROM saas_instance WHERE id = %s FOR UPDATE",
            (self.id,))
        locked = self.env.cr.fetchone()
        db_next = locked[0] if locked else None
        if db_next and db_next > today:
            _logger.info(
                "Renewal for %s already billed this cycle (next_invoice_date=%s) "
                "— skipping duplicate.", self.subdomain, db_next)
            return
        # Ensure ORM reads below see the freshly-locked DB state.
        self.invalidate_recordset(['next_invoice_date'])

        # Apply scheduled downgrade at cycle boundary
        if self.scheduled_plan_id:
            old_plan = self.plan_id
            new_plan = self.scheduled_plan_id
            new_period = self.scheduled_billing_period or self.billing_period or 'monthly'
            self.write({
                'plan_id': new_plan.id,
                'billing_period': new_period,
                'scheduled_plan_id': False,
                'scheduled_billing_period': False,
            })
            self._append_log(
                "Scheduled downgrade applied at cycle end: %s → %s"
                % (old_plan.name if old_plan else 'None', new_plan.name)
            )
            self.message_post(body=_(
                "Downgrade applied: switched from %s to %s."
            ) % (old_plan.name if old_plan else '—', new_plan.name))

            # Update container resources for the lower plan
            if self.state == 'running':
                try:
                    self._update_container_resources()
                except Exception as e:
                    _logger.exception(
                        "Failed to update resources after downgrade for %s",
                        self.subdomain,
                    )
                try:
                    with self.docker_server_id._get_ssh_connection() as ssh:
                        self._render_and_write_configs(ssh)
                except Exception as e:
                    _logger.exception(
                        "Failed to regenerate configs after downgrade for %s",
                        self.subdomain,
                    )

            # Remove excess backups that exceed the new plan's lower limit
            try:
                self.env['saas.instance.backup']._cleanup_excess_for_instance(self)
            except Exception:
                _logger.exception(
                    "Failed to cleanup excess backups after downgrade for %s",
                    self.subdomain,
                )

        plan = self.plan_id
        if not plan:
            return

        period = self.billing_period or 'monthly'
        price = plan._get_price_for_period(period)
        period_label = 'Monthly' if period == 'monthly' else 'Yearly'

        pricelist = self.partner_id.property_product_pricelist
        order_lines = [(0, 0, {
            'product_id': self._get_billing_product().id,
            'name': _('%s (%s) — %s renewal') % (
                plan.name, period_label, self.name or self.subdomain,
            ),
            'product_uom_qty': 1,
            'price_unit': price,
        })]

        # Support plan (P5): a flat monthly fee billed on the SAME cycle as
        # the plan (see _support_order_line). The free/default plan and
        # unpriced plans add nothing (behaviour-neutral until configured).
        support_line = self._support_order_line(period, period_label)
        if support_line:
            order_lines.append(support_line)

        # Daily-backup add-on: the snapshot follows the PLAN's billing period
        # and is aligned to the plan's renewal date at activation, so it is
        # folded into THIS renewal for the SAME period (monthly plan → 1 month,
        # yearly plan → 12 months) whenever its due date has reached the
        # renewal. One bill, one cadence — no separate monthly backup invoice.
        # ``merge_snapshot`` is advanced together with next_invoice_date below
        # (pre-post, atomic) so the two cycles stay locked in step.
        merge_snapshot = (
            self.daily_backup_enabled
            and self.daily_backup_next_invoice_date
            and self.daily_backup_next_invoice_date <= self.next_invoice_date
        )
        if merge_snapshot:
            snapshot_line = self._snapshot_order_line(period)
            if snapshot_line:
                order_lines.append(snapshot_line)
            else:
                # price 0 / backups off between the check and here — don't
                # advance the backup date for a line we didn't add.
                merge_snapshot = False

        # v47: storage is billed ONLY for blocks the customer deliberately
        # PURCHASED (extra_storage_blocks) — never an automatic usage-based
        # overage. The recurring block line tracks the same period as the
        # plan (qty = blocks × months) so it matches monthly/yearly cadence.
        block_line = self._storage_block_order_line(period)
        if block_line:
            order_lines.append(block_line)

        # Odoo.sh-style environments: one recurring line per active Staging/
        # Development server in the project (lowest-spec env price). Children
        # never self-bill — their cost rides this Production renewal.
        order_lines += self._environment_order_lines(period)

        # Wallet (A4): consume available account credit on this renewal.
        wallet_line, wallet_amount = self._wallet_credit_line(order_lines)
        if wallet_line:
            order_lines.append(wallet_line)

        order_vals = {
            'partner_id': self.partner_id.id,
            'origin': ORIGIN_RENEWAL % (self.name or self.subdomain),
            'order_line': order_lines,
        }
        if pricelist:
            order_vals['pricelist_id'] = pricelist.id
        order = self.env['sale.order'].create(order_vals)
        order.action_confirm()
        # Keep sale_order_id pointing to the original SO for payment detection;
        # renewal invoices are tracked via partner + origin for dunning.
        invoice = order._create_invoices()

        # Advance the billing cycle *before* posting the invoice. account.move
        # post commits the move at the accounting layer; if we advanced the
        # date afterwards and any later step failed (mail, write), the cron
        # would re-post a duplicate renewal tomorrow.
        if period == 'yearly':
            interval = relativedelta(years=1)
        else:
            interval = relativedelta(months=1)
        renewal_vals = {
            'next_invoice_date': self.next_invoice_date + interval,
            'last_invoice_date': fields.Date.today(),
            'suspension_warning_sent': False,
            # New cycle: re-arm the 7-day / 1-day auto-renew reminders.
            'renewal_reminder_7d_sent': False,
            'renewal_reminder_1d_sent': False,
        }
        # If this invoice merged the snapshot, advance the backup's date by the
        # SAME interval as the plan so the two cycles stay aligned (next backup
        # charge rides the next renewal). Done in the same pre-post write as
        # next_invoice_date for atomicity + idempotency.
        if merge_snapshot:
            renewal_vals['daily_backup_last_invoice_date'] = fields.Date.today()
            renewal_vals['daily_backup_next_invoice_date'] = (
                self.next_invoice_date + interval
            )
        self.write(renewal_vals)
        invoice.action_post()
        # Settle wallet consumption now that the invoice exists (so the
        # ledger entry links to the move).
        self._wallet_settle_consumption(invoice, wallet_amount)
        self._append_log(
            "Renewal invoice %s created for %s period."
            % (invoice.name, period_label)
        )
        self.message_post(body=_(
            "Renewal invoice %s created (%s). Payment due.",
        ) % (invoice.name, period_label))

        # Auto-charge the saved card if subscription auto-renew is on
        # and a card is on file. Skip the payment-due notification if
        # the charge succeeds so customers aren't pinged for a bill
        # that's already settled.
        auto_paid = False
        if self.auto_renew_subscription and self._auto_renew_method():
            auto_paid = self._try_auto_charge_invoice(invoice, kind='subscription')

        # Send payment-due notification (best-effort: never roll back the
        # renewal if mail delivery fails). Skip when auto-charge already
        # paid the invoice.
        if not auto_paid:
            try:
                self._send_notification('saas_core.mail_template_saas_payment_due')
            except Exception:
                _logger.exception(
                    "Failed to send payment-due notification for renewal of %s",
                    self.subdomain,
                )

    # ========== Dunning / Grace Period ==========

    @api.model
    def _grace_period_days(self):
        """Platform-wide grace period (days) before an overdue instance is
        suspended. Configured once in Settings → 'Grace Period (Days)' and
        applied to every plan (no longer a per-plan field)."""
        try:
            return int(self.env['ir.config_parameter'].sudo().get_param(
                'saas_master.grace_period_days', '7') or 0)
        except (TypeError, ValueError):
            return 7

    @api.model
    def _cron_check_overdue_invoices(self):
        """Suspend instances whose invoices are overdue past the grace period.

        Checks both running AND stopped instances so that a customer
        cannot dodge suspension by stopping their instance before the
        cron runs.

        Also sends a warning email when the invoice first becomes overdue.
        """
        today = fields.Date.today()
        instances = self.search([
            ('state', 'in', ('running', 'stopped')),
            ('is_trial', '=', False),
            ('parent_id', '=', False),
            ('sale_order_id', '!=', False),
        ])
        for instance in instances:
            try:
                instance._check_dunning(today)
                self.env.cr.commit()
            except Exception:
                # Per-instance failure must NOT abort the whole pass.
                # Roll back the row and log loudly so ops can see
                # which instance got stuck — the next cron run will
                # retry. ``_check_dunning`` itself already handles
                # the common race (state changed mid-cron) silently,
                # so anything reaching here is genuinely unexpected.
                self.env.cr.rollback()
                _logger.exception(
                    "Dunning check crashed for %s — will retry next cron",
                    instance.subdomain,
                )

    def _is_optional_invoice(self, invoice):
        """Return True if the invoice is for an optional upgrade that
        the client can back out of without losing their current service."""
        so_origins = invoice.line_ids.sale_line_ids.order_id.mapped('origin')
        return any(
            origin and any(
                origin.startswith(prefix)
                for prefix in OPTIONAL_INVOICE_ORIGIN_PREFIXES
            )
            for origin in so_origins
        )

    def _check_dunning(self, today):
        """Check if any linked invoices are overdue and act accordingly.

        Searches ALL invoices related to this instance (across all sale
        orders, including renewals) so that unpaid renewal invoices are
        caught even though sale_order_id points to the original order.

        Skips optional upgrade/subscription invoices — the client may
        have requested an upgrade but decided not to pay. These should
        not cause suspension of an otherwise active subscription.

        Handles both running and stopped instances:
        - Running: calls action_suspend() to stop the container via SSH.
        - Stopped: container is already stopped, so we just mark the
          state as 'suspended' directly (no SSH needed).
        """
        self.ensure_one()
        if not self.partner_id:
            return

        # Find all invoices related to this instance (initial + renewals)
        all_invoices = self._get_all_invoices()
        if not all_invoices:
            return

        overdue_invoices = all_invoices.filtered(
            lambda m: m.move_type == 'out_invoice'
            and m.payment_state not in ('paid', 'in_payment')
            and m.state == 'posted'
            and m.invoice_date_due
            and m.invoice_date_due < today
        )
        if not overdue_invoices:
            return

        # Exclude optional upgrade invoices — these are charges the
        # client initiated but can choose not to pay (they keep their
        # current plan).  Only mandatory invoices (initial subscription,
        # renewals, data restoration) should trigger suspension.
        mandatory_overdue = overdue_invoices.filtered(
            lambda m: not self._is_optional_invoice(m)
        )
        if not mandatory_overdue:
            return
        overdue_invoices = mandatory_overdue

        grace_days = self._grace_period_days()
        oldest = min(overdue_invoices, key=lambda m: m.invoice_date_due)
        oldest_due = oldest.invoice_date_due
        days_overdue = (today - oldest_due).days

        if days_overdue > grace_days:
            # Grace period exceeded — suspend.
            #
            # Re-read state at the last possible moment: the cron's
            # initial ``search`` returned this instance as 'running'
            # or 'stopped' but other workers / actions may have moved
            # it since (provisioning, suspended, cancelled, …). Acting
            # on a stale state raised UserError inside the cron loop
            # in the past, which was silently swallowed → instance
            # stayed running with an overdue invoice (revenue leak).
            self.invalidate_recordset(['state'])
            current_state = self.state
            if current_state == 'running':
                # ``action_suspend`` queues the docker stop in a
                # background thread; we wrap it so a transient SSH
                # failure leaves the instance in 'suspended' state
                # rather than re-raising into the cron loop and
                # rolling back the whole dunning pass.
                try:
                    self.action_suspend()
                except UserError:
                    # Lost the race — state changed between recheck
                    # and the call. Next cron pass will revisit.
                    _logger.info(
                        "Dunning skipped %s: state changed mid-cron "
                        "(now %s); will retry on next run.",
                        self.subdomain, self.state,
                    )
                    return
                except Exception:
                    # SSH / docker failure — mark suspended in DB so
                    # access is denied; ops can investigate the host.
                    _logger.exception(
                        "Dunning action_suspend failed for %s; "
                        "forcing state=suspended in DB only.",
                        self.subdomain,
                    )
                    self.state = 'suspended'
                    self.pending_operation = False
            elif current_state == 'stopped':
                # Container is already stopped — just mark as suspended
                # so the customer cannot restart without paying.
                self.state = 'suspended'
            else:
                # Provisioning, already suspended, cancelled, failed,
                # etc. — nothing safe to do this pass.
                _logger.info(
                    "Dunning skipped %s: state is %s (not actionable).",
                    self.subdomain, current_state,
                )
                return
            self._append_log(
                "AUTO-SUSPENDED: Invoice %s overdue by %d days (grace: %d)."
                % (oldest.name, days_overdue, grace_days)
            )
            self._send_notification('saas_core.mail_template_saas_suspended')
        elif not self.suspension_warning_sent:
            # Within grace period — send warning
            self.suspension_warning_sent = True
            self._send_notification('saas_core.mail_template_saas_payment_due')
            self._append_log(
                "Payment overdue warning sent. Invoice %s due %s. "
                "Grace period: %d days."
                % (oldest.name, oldest_due, grace_days)
            )

    # ========== Plan Upgrade/Downgrade ==========

    def action_subscribe_from_trial(self, new_plan_id, billing_period='monthly'):
        """Start a trial-to-paid upgrade: create invoice and wait for payment.

        The actual plan switch happens in _apply_pending_upgrade() which is
        called automatically when payment is confirmed.

        Args:
            new_plan_id: ID of the target plan
            billing_period: 'monthly' or 'yearly'

        Returns the created invoice (or True if zero-amount).
        """
        self.ensure_one()
        if not self.is_trial:
            raise UserError(_("This instance is not on a trial plan."))
        if self.state not in ('running', 'suspended'):
            raise UserError(
                _("Instance must be running or suspended to subscribe.")
            )

        new_plan = self.env['saas.plan'].browse(int(new_plan_id))
        if not new_plan.exists():
            raise UserError(_("Invalid plan."))
        if new_plan.is_trial_plan:
            raise UserError(_("Cannot subscribe to another trial plan."))
        if (self.saas_product_id and new_plan.saas_product_ids
                and self.saas_product_id not in new_plan.saas_product_ids):
            raise UserError(_("Selected plan does not belong to this service."))

        if billing_period not in ('monthly', 'yearly'):
            billing_period = 'monthly'
        if billing_period == 'yearly' and not new_plan.yearly_price:
            billing_period = 'monthly'

        price = new_plan._get_price_for_period(billing_period)
        period_label = 'Monthly' if billing_period == 'monthly' else 'Yearly'

        # Store the chosen plan + period — applied on payment. v47: no promo.
        self.write({
            'pending_plan_id': new_plan.id,
            'pending_billing_period': billing_period,
        })

        order_lines = [(0, 0, {
            'product_id': self._get_billing_product().id,
            'name': _('%s (%s) — %s') % (
                new_plan.name, period_label, self.name or self.subdomain,
            ),
            'product_uom_qty': 1,
            'price_unit': price,
        })]
        # Consume any existing wallet balance.
        wallet_line, wallet_amount = self._wallet_credit_line(order_lines)
        if wallet_line:
            order_lines.append(wallet_line)

        # Create sale order and invoice
        pricelist = self.partner_id.property_product_pricelist
        order_vals = {
            'partner_id': self.partner_id.id,
            'origin': ORIGIN_SUBSCRIPTION % (self.name or self.subdomain),
            'order_line': order_lines,
        }
        if pricelist:
            order_vals['pricelist_id'] = pricelist.id
        order = self.env['sale.order'].create(order_vals)
        order.action_confirm()
        self.sale_order_id = order

        invoice = order._create_invoices()
        invoice.action_post()
        self._wallet_settle_consumption(invoice, wallet_amount)
        self.pending_change_invoice_id = invoice[:1]

        self._append_log(
            "Upgrade to %s (%s) requested. Invoice %s created — awaiting "
            "payment." % (new_plan.name, period_label, invoice.name)
        )
        self.message_post(body=_(
            "Upgrade to %s (%s) requested. Awaiting payment."
        ) % (new_plan.name, period_label))

        # Zero-amount plan: apply immediately
        if invoice.amount_total <= 0:
            self._apply_pending_upgrade()
            return True

        return invoice

    def _apply_pending_upgrade(self):
        """Apply the pending plan upgrade after payment is confirmed."""
        self.ensure_one()
        new_plan = self.pending_plan_id
        if not new_plan:
            return

        old_plan = self.plan_id
        was_suspended = self.state == 'suspended'
        was_trial = self.is_trial
        billing_period = self.pending_billing_period or 'monthly'

        # Reactivate FIRST if suspended. If the restart fails we don't
        # want to have already flipped is_trial=False — that would leave
        # the customer charged but with a permanently unreachable instance
        # and no way to retry.
        if was_suspended:
            self._append_log("Reactivating instance after paid subscription.")
            try:
                # Use _do_restart synchronously here (we're already inside
                # the background payment thread). action_restart would
                # spawn another bg thread and decouple error reporting.
                self._do_restart()
            except Exception as e:
                # Leave plan/trial unchanged so the customer can retry.
                _logger.exception(
                    "Restart failed during paid-upgrade for %s — aborting "
                    "plan switch so the customer is not charged for an "
                    "unreachable instance.", self.subdomain,
                )
                self._append_log(
                    "ERROR: restart failed during paid-upgrade — plan "
                    "switch deferred. %s" % e
                )
                raise

        self.write({
            'plan_id': new_plan.id,
            'is_trial': False,
            'billing_period': billing_period,
            'pending_plan_id': False,
            'pending_billing_period': False,
            'pending_change_invoice_id': False,
        })

        self._set_next_invoice_date()

        self._append_log(
            "Payment received. Plan upgraded: %s → %s"
            % (old_plan.name if old_plan else 'Trial', new_plan.name)
        )
        self.message_post(body=_(
            "Payment confirmed. Upgraded from %s to paid plan: %s."
        ) % ('trial' if was_trial else (old_plan.name if old_plan else 'plan'),
             new_plan.name))

        # Update container resources / regenerate configs (best effort —
        # the customer is already paid and reactivated, don't roll back
        # the upgrade if these fail; they can be retried by Redeploy).
        if self.state == 'running':
            try:
                self._update_container_resources()
            except Exception as e:
                _logger.exception(
                    "Failed to update container resources on subscription for %s",
                    self.subdomain,
                )
                self._append_log(
                    "WARNING: Plan updated but container resource update failed: %s" % e
                )
            try:
                with self.docker_server_id._get_ssh_connection() as ssh:
                    self._render_and_write_configs(ssh)
            except Exception:
                _logger.exception(
                    "Failed to regenerate configs on subscription for %s",
                    self.subdomain,
                )

    @staticmethod
    def _to_monthly_equivalent(price, period):
        """Normalize a period price to its monthly equivalent for comparison."""
        if period == 'yearly' and price > 0:
            return price / 12.0
        return price

    def action_request_plan_change(self, new_plan_id, billing_period=None):
        """Request a plan change from the portal.

        UPGRADE (new effective monthly cost > old effective monthly cost):
          - Calculate remaining value of current plan
          - Charge: new_plan_price - remaining_value (min 0)
          - Applied immediately after payment
          - Billing cycle resets from today

        DOWNGRADE (new effective monthly cost <= old effective monthly cost):
          - NO refund, NO credit, NO proration
          - Blocked if current DB size >= 75% of target plan's db_size_limit
          - Scheduled for end of current billing cycle
          - Client keeps current (higher) plan until then

        Upgrade vs downgrade is determined by comparing effective monthly
        costs so that switching periods (monthly ↔ yearly) is classified
        correctly.

        Returns:
          - Invoice record (upgrade, needs payment)
          - True (upgrade, zero charge)
          - 'scheduled' (downgrade scheduled)
        """
        self.ensure_one()
        # Lock the instance row to prevent concurrent plan changes
        # (two browser tabs submitting at the same time).
        self.env.cr.execute(
            "SELECT id FROM saas_instance WHERE id = %s FOR UPDATE NOWAIT",
            (self.id,),
        )
        # Re-read fields after lock to get latest state
        self.invalidate_recordset()

        if self.state not in ('running', 'stopped', 'suspended'):
            raise UserError(
                _("Can only change plan on running, stopped, or suspended instances.")
            )

        # Auto-cancel existing pending upgrade if client changes their mind.
        # This allows switching to a different plan without manual cancellation.
        if self.pending_plan_id:
            self._cancel_pending_upgrade()
        if self.scheduled_plan_id:
            raise UserError(_(
                "A downgrade is already scheduled. "
                "Please cancel the scheduled downgrade before requesting another plan change."
            ))

        new_plan = self.env['saas.plan'].browse(int(new_plan_id))
        if not new_plan.exists():
            raise UserError(_("Invalid plan."))
        if new_plan.is_trial_plan:
            raise UserError(_("Cannot switch to a trial plan."))
        billing_period = billing_period or self.billing_period or 'monthly'

        if new_plan.id == self.plan_id.id and billing_period == (self.billing_period or 'monthly'):
            raise UserError(_("Already on this plan and billing period."))

        # Block yearly → monthly on the same plan.
        # Annual subscribers must wait until their subscription period ends.
        if (new_plan.id == self.plan_id.id
                and (self.billing_period or 'monthly') == 'yearly'
                and billing_period == 'monthly'):
            raise UserError(_(
                "You cannot switch from yearly to monthly billing before your "
                "current annual subscription ends%s. "
                "Your yearly plan will remain active until then."
            ) % (
                ' (%s)' % self.next_invoice_date.strftime('%B %d, %Y')
                if self.next_invoice_date else ''
            ))
        new_price = new_plan._get_price_for_period(billing_period)
        old_period = self.billing_period or 'monthly'
        old_price = self.plan_id._get_price_for_period(old_period) if self.plan_id else 0

        # Monthly → Yearly on the same plan is always an immediate upgrade
        # (customer commits to paying more upfront, with remaining days credited).
        if (new_plan.id == self.plan_id.id
                and old_period == 'monthly'
                and billing_period == 'yearly'):
            return self._request_upgrade(new_plan, billing_period, new_price, old_price)

        # Compare effective monthly costs to correctly classify
        # cross-period changes (e.g. $10/month vs $100/year = $8.33/month)
        new_monthly = self._to_monthly_equivalent(new_price, billing_period)
        old_monthly = self._to_monthly_equivalent(old_price, old_period)

        if new_monthly > old_monthly:
            return self._request_upgrade(new_plan, billing_period, new_price, old_price)
        else:
            return self._request_downgrade(new_plan, billing_period)

    # ---------- AUTO-CANCEL PENDING ----------

    def _cancel_pending_upgrade(self):
        """Cancel the current pending upgrade and ALL of its unpaid invoices.

        Called automatically when the client selects a different plan
        while an upgrade is still awaiting payment. Cancels every unpaid
        invoice tied to either an upgrade SO or a subscription SO for
        this instance — using only `sale_order_id` would miss invoices
        from earlier upgrade rounds whose SO has already been replaced.
        """
        self.ensure_one()
        old_plan_name = (
            self.pending_plan_id.name if self.pending_plan_id else 'Unknown'
        )

        # Find any unpaid posted invoice originating from an upgrade
        # or subscription SO (both are "optional" — the client may back
        # out of paying without losing their current plan).
        invoices = self._get_all_invoices().filtered(
            lambda inv: (
                inv.state == 'posted'
                and inv.payment_state not in ('paid', 'in_payment')
                and inv.amount_residual > 0
                and self._is_optional_invoice(inv)
            )
        )
        for inv in invoices:
            try:
                inv.button_cancel()
            except Exception:
                _logger.exception(
                    "Failed to cancel optional invoice %s for instance %s",
                    inv.name, self.subdomain,
                )

        self._append_log(
            "Auto-cancelled pending upgrade to %s and %d unpaid invoice(s) "
            "(client selected a different plan)."
            % (old_plan_name, len(invoices))
        )
        self.write({
            'pending_plan_id': False,
            'pending_billing_period': False,
            'pending_change_invoice_id': False,
        })

    # ---------- UPGRADE ----------

    def _proration_credit(self, old_price):
        """Unused value of the CURRENT subscription period for ``old_price``,
        prorated over the ACTUAL elapsed cycle (A3).

        No artificial day deductions: the customer is credited for the full
        unused time (``remaining_days / total_days × old_price``). Uses the
        real cycle length (last_invoice_date → next_invoice_date), never a
        days/30 approximation. Returns ``(remaining_value, remaining_days,
        total_days)``."""
        self.ensure_one()
        today = fields.Date.today()
        if not (self.next_invoice_date and self.last_invoice_date):
            return 0.0, 0, 0
        total_days = (self.next_invoice_date - self.last_invoice_date).days
        remaining_days = (self.next_invoice_date - today).days
        if total_days <= 0 or remaining_days <= 0:
            return 0.0, max(0, remaining_days), max(0, total_days)
        remaining_value = round((old_price / total_days) * remaining_days, 2)
        return remaining_value, remaining_days, total_days

    def _request_upgrade(self, new_plan, billing_period, new_price, old_price):
        """Create the proration invoice for an upgrade. Applied on payment.

        Two cases, both customer-fair and free of the old churn arbitrage:

        * SAME billing period (monthly→monthly / yearly→yearly to a bigger
          plan): the current billing cycle is KEPT (never reset) and the
          customer is charged only the PRORATED DIFFERENCE between the new
          and old plan for the days left in the cycle. Because the cycle is
          not reset, a customer can't oscillate plan changes to refresh their
          remaining time and mint wallet credit. The invoice line is exactly
          the incremental cost — no hidden off-ledger discount.

        * PERIOD change (monthly→yearly) or no active cycle: a fresh period
          starts at the new price, the full unused value of the old period is
          credited as an EXPLICIT, visible negative invoice line (not baked
          into the price), and any surplus beyond the new price is carried to
          the wallet when the change is applied (A4 — prepaid value is never
          lost). The cycle is reset because the period genuinely restarts.

        Any existing wallet balance is also consumed on the invoice."""
        self.ensure_one()
        period_label = 'Yearly' if billing_period == 'yearly' else 'Monthly'
        old_period = self.billing_period or 'monthly'

        remaining_value, remaining_days, total_days = self._proration_credit(
            old_price)

        # Same-period upgrade keeps the cycle and charges only the difference.
        same_period = (old_period == billing_period) and total_days > 0
        surplus = 0.0
        order_lines = []

        if same_period:
            new_remaining = round(new_price * remaining_days / total_days, 2)
            charge = round(max(0.0, new_remaining - remaining_value), 2)
            line_name = _(
                '%s (%s) — %s — Plan upgrade (prorated difference for %d '
                'remaining day(s))'
            ) % (new_plan.name, period_label, self.name or self.subdomain,
                 remaining_days)
            order_lines.append((0, 0, {
                'product_id': self._get_billing_product().id,
                'name': line_name,
                'product_uom_qty': 1,
                'price_unit': charge,
            }))
            self._append_log(
                "Upgrade (same period): new_price=%.2f, prorated_diff=%.2f "
                "for %d/%d day(s) — billing cycle kept."
                % (new_price, charge, remaining_days, total_days))
        else:
            # Fresh period at the new price; full unused value credited as an
            # explicit line; surplus carried to the wallet on apply.
            applied_credit = round(min(remaining_value, new_price), 2)
            surplus = round(max(0.0, remaining_value - new_price), 2)
            order_lines.append((0, 0, {
                'product_id': self._get_billing_product().id,
                'name': _('%s (%s) — %s — Plan upgrade') % (
                    new_plan.name, period_label, self.name or self.subdomain),
                'product_uom_qty': 1,
                'price_unit': new_price,
            }))
            if applied_credit > 0:
                order_lines.append((0, 0, {
                    'product_id': self._get_billing_product().id,
                    'name': _('Credit — %d unused day(s) on %s') % (
                        remaining_days, self.plan_id.name),
                    'product_uom_qty': 1,
                    'price_unit': -applied_credit,
                }))
            self._append_log(
                "Upgrade (new period): new_price=%.2f, credit=%.2f, "
                "wallet_surplus=%.2f — billing cycle resets."
                % (new_price, applied_credit, surplus))

        # Store pending upgrade + the surplus to grant on apply.
        self.write({
            'pending_plan_id': new_plan.id,
            'pending_billing_period': billing_period,
            'pending_wallet_credit': surplus,
        })

        # Consume existing wallet balance on the upgrade invoice too.
        wallet_line, wallet_amount = self._wallet_credit_line(order_lines)
        if wallet_line:
            order_lines.append(wallet_line)

        # Create invoice
        pricelist = self.partner_id.property_product_pricelist
        order_vals = {
            'partner_id': self.partner_id.id,
            'origin': ORIGIN_PLAN_UPGRADE % (self.name or self.subdomain),
            'order_line': order_lines,
        }
        if pricelist:
            order_vals['pricelist_id'] = pricelist.id
        order = self.env['sale.order'].create(order_vals)
        order.action_confirm()
        self.sale_order_id = order

        invoice = order._create_invoices()
        invoice.action_post()
        self._wallet_settle_consumption(invoice, wallet_amount)
        self.pending_change_invoice_id = invoice[:1]

        self.message_post(body=_(
            "Upgrade to %s (%s) requested. Invoice %s (%.2f) — awaiting payment."
        ) % (new_plan.name, period_label, invoice.name, invoice.amount_total))

        # Zero charge: apply immediately
        if invoice.amount_total <= 0:
            self._apply_pending_plan_change()
            return True

        return invoice

    def _apply_pending_plan_change(self):
        """Apply a pending upgrade after payment is confirmed."""
        self.ensure_one()
        new_plan = self.pending_plan_id
        if not new_plan:
            return

        billing_period = self.pending_billing_period or self.billing_period or 'monthly'
        old_plan = self.plan_id
        old_period = self.billing_period or 'monthly'
        surplus = self.pending_wallet_credit or 0.0
        # Keep the existing billing cycle for a same-period upgrade — the
        # customer only paid the prorated DIFFERENCE, so the next renewal is
        # still due at the original date (and full new-plan price). Reset the
        # cycle only when the period actually changed (e.g. monthly→yearly) or
        # there is no active cycle to keep. This is what removes the old
        # "reset every upgrade" arbitrage.
        reset_cycle = (old_period != billing_period) or not (
            self.next_invoice_date and self.last_invoice_date)

        self.write({
            'plan_id': new_plan.id,
            'billing_period': billing_period,
            'pending_plan_id': False,
            'pending_billing_period': False,
            'pending_change_invoice_id': False,
            'pending_wallet_credit': 0.0,
        })

        # A4: any unused-value surplus beyond the upgrade price is moved into
        # the wallet now that the change is paid/applied — never forfeited.
        if surplus > 0:
            self._grant_wallet_credit(
                surplus, origin='upgrade_surplus',
                reason=_('Unused value from %s carried to wallet on upgrade')
                % (old_plan.name if old_plan else _('previous plan')))

        if reset_cycle:
            self._set_next_invoice_date()

        cycle_msg = 'reset' if reset_cycle else 'kept (prorated difference billed)'
        self._append_log(
            "Payment received. Plan upgraded: %s → %s. Billing cycle %s."
            % (old_plan.name if old_plan else 'None', new_plan.name, cycle_msg)
        )
        self.message_post(body=_(
            "Payment confirmed. Upgraded from %s to %s."
        ) % (old_plan.name if old_plan else '—', new_plan.name))

        if self.state == 'running':
            try:
                self._update_container_resources()
            except Exception as e:
                _logger.exception(
                    "Failed to update container resources for %s", self.subdomain,
                )
                self._append_log(
                    "WARNING: Plan updated but resource update failed: %s" % e
                )
            try:
                with self.docker_server_id._get_ssh_connection() as ssh:
                    self._render_and_write_configs(ssh)
            except Exception as e:
                _logger.exception(
                    "Failed to regenerate configs for %s", self.subdomain,
                )

    # ---------- DOWNGRADE ----------

    DOWNGRADE_THRESHOLD = 0.75  # 75% of target plan's db_size_limit

    def _request_downgrade(self, new_plan, billing_period):
        """Validate and schedule a downgrade for end of billing cycle.

        Blocked if current total storage >= 75% of the target plan's storage_limit.
        No refund, no credit, no proration.
        """
        self.ensure_one()

        # Refresh storage usage before checking threshold. Use the
        # strict variant — if we can't measure usage we MUST refuse the
        # downgrade rather than greenlight it on a stale/zero value
        # (the customer could otherwise be charged for a plan that can
        # never serve their data).
        try:
            self._strict_refresh_usage()
        except Exception as exc:
            raise UserError(_(
                "Cannot verify current storage usage — refusing the "
                "downgrade until usage can be measured. Please try again "
                "in a few minutes.\n\nDetails: %s"
            ) % exc)

        # --- Storage threshold check (75% of target plan limit) ---
        if new_plan.storage_limit > 0:
            current_usage_gb = (self.total_storage_bytes or 0) / (1024 ** 3)
            threshold_gb = self.DOWNGRADE_THRESHOLD * new_plan.storage_limit

            self._append_log(
                "Downgrade check: current_usage=%.2f GB, "
                "target_storage_limit=%.2f GB, threshold(75%%)=%.2f GB"
                % (current_usage_gb, new_plan.storage_limit, threshold_gb)
            )

            if current_usage_gb >= threshold_gb:
                raise UserError(_(
                    "Your current storage usage is too high for the selected plan.\n\n"
                    "Current usage: %.2f GB\n"
                    "Target plan limit: %.2f GB\n"
                    "Minimum required headroom: 25%% free (threshold: %.2f GB)\n\n"
                    "Please reduce your data before downgrading, "
                    "or choose a plan with a higher limit."
                ) % (current_usage_gb, new_plan.storage_limit, threshold_gb))

        # --- Schedule the downgrade ---
        self.write({
            'scheduled_plan_id': new_plan.id,
            'scheduled_billing_period': billing_period,
        })

        end_date = self.next_invoice_date or _('end of billing cycle')
        self._append_log(
            "Downgrade to %s scheduled for %s. No refund, no credit."
            % (new_plan.name, end_date)
        )
        self.message_post(body=_(
            "Downgrade to %s scheduled for %s. "
            "Your current plan remains active until then."
        ) % (new_plan.name, end_date))

        return 'scheduled'

    def action_cancel_scheduled_downgrade(self):
        """Cancel a pending scheduled downgrade."""
        self.ensure_one()
        if self.scheduled_plan_id:
            plan_name = self.scheduled_plan_id.name
            self.write({
                'scheduled_plan_id': False,
                'scheduled_billing_period': False,
            })
            self._append_log("Scheduled downgrade to %s cancelled." % plan_name)
            self.message_post(body=_(
                "Scheduled downgrade to %s has been cancelled."
            ) % plan_name)

    def _update_container_resources(self):
        """Update CPU/RAM limits on a running container via docker update.

        Respects per-instance overrides (override_docker_cpu,
        override_docker_mem) so admin changes take effect immediately
        without a full redeploy.
        """
        self.ensure_one()
        if not self.plan_id or self.state != 'running':
            return
        self._ensure_can_ssh()
        container_name = self._get_container_name()
        plan = self.plan_id

        # Resolve effective values (override > plan)
        cpu = self.override_docker_cpu.strip() if self.override_docker_cpu else str(plan.cpu_limit)
        ram_bytes = self._parse_ram_string(plan.ram_limit)
        auto_mem = '%dm' % (int(ram_bytes * 1.3) // (1024 * 1024)) if ram_bytes else ''
        mem = self.override_docker_mem.strip() if self.override_docker_mem else auto_mem
        swap = self.override_docker_swap.strip() if self.override_docker_swap else mem

        # docker update --cpus=X --memory=Y --memory-swap=Z container
        parts = ['docker update']
        if cpu:
            parts.append('--cpus=%s' % shlex.quote(cpu))
        if mem:
            parts.append('--memory=%s' % shlex.quote(mem))
        if swap:
            parts.append('--memory-swap=%s' % shlex.quote(swap))
        parts.append(shlex.quote(container_name))
        update_cmd = ' '.join(parts)

        with self.docker_server_id._get_ssh_connection() as ssh:
            exit_code, stdout, stderr = ssh.execute(update_cmd)
            if exit_code != 0:
                raise UserError(
                    _("Failed to update container resources:\n%s") % stderr
                )
            # Also regenerate docker-compose.yml so next restart uses new limits
            self._render_and_write_configs(ssh)

        self._append_log(
            "Container resources updated: CPU=%s, RAM=%s"
            % (plan.cpu_limit, plan.ram_limit)
        )

    # ========== Hosting: customer-facing DB management ==========
    # Run on the docker host via SSH + ``docker compose exec`` against
    # the running Odoo container. We use Odoo's own
    # ``odoo.service.db.exp_*`` functions for create / duplicate / drop
    # so passwords are hashed correctly and ``base`` is initialised the
    # same way the official /web/database/manager UI would.
    # Plain ``psql`` is enough for the list query.

    # PostgreSQL identifier rules: starts with a letter, [a-z0-9_-],
    # max 63 bytes. Reject the catalog DBs explicitly.
    _DB_NAME_RE = re.compile(r'^[a-z][a-z0-9_-]{0,62}$')
    # Reserved DB names — PostgreSQL system catalogs and Odoo defaults.
    _DB_RESERVED = frozenset([
        'postgres', 'template0', 'template1', 'odoo',
    ])
    # Hard floor for customer-typed suffixes. Anything shorter is
    # almost certainly a slip; reject before we waste a CLI init.
    _DB_NAME_MIN_LENGTH = 3

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
        # Underscore is the natural separator; subdomains use hyphens
        # so the boundary is unambiguous (``acme-prod`` + `_` + name).
        return '%s_' % sub if sub else ''

    def _validate_db_name(self, name):
        """Validate a raw customer-typed DB name.

        ``name`` is the bare value the customer entered, **without**
        the subdomain prefix. Returns the normalized (stripped +
        lower-cased) name on success, raises ``UserError`` with a
        specific message otherwise.

        Each branch surfaces a distinct error so the customer knows
        *why* their input was rejected, not just that it was.
        """
        # Strip + normalize. Doing this first lets us tell "empty"
        # apart from "wrong chars".
        raw = (name or '').strip()
        if not raw:
            raise UserError(_("Database name is required."))

        # Reject inputs that change after lower-casing — better to
        # be explicit than silently accept and produce something the
        # customer didn't type.
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

        # Catch the most common mistakes with specific messages
        # before falling through to the generic regex check.
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

        # Final regex check — catches anything the messages above
        # missed (shouldn't be reachable, but defensive).
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
        """Combine the instance prefix and the customer-typed suffix.

        Strips an already-applied prefix if the customer pastes the
        full name back (so re-entering ``acme_test`` doesn't produce
        ``acme_acme_test``). Enforces the 63-byte PG identifier limit
        on the FINAL name.
        """
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
        # ``provisioning`` is normally off-limits, but a restore-in-progress
        # legitimately needs to list databases for the pre-restore safety
        # snapshot (the container is still up at that point — we haven't
        # docker-compose-down'd yet). Allow only that specific transition.
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

    def _docker_exec_python(self, ssh, py_script, env=None, timeout=600):
        """Run ``py_script`` inside the instance's Odoo container.

        Values that need to reach the script (db names, passwords) go
        via env vars so shell-quoting can't bite us. ``odoo.tools.config``
        is preloaded so the script can call into ``odoo.service.db``
        functions immediately.

        Returns ``(exit_code, stdout, stderr)``.
        """
        # ``import odoo`` no longer auto-imports the ``tools`` submodule
        # in current Odoo (was implicit in older versions). The explicit
        # ``import odoo.tools`` keeps ``odoo.tools.config`` reachable
        # from the script regardless of upstream version.
        prelude = (
            "import os, sys\n"
            "import odoo\n"
            "import odoo.tools\n"
            "odoo.tools.config.parse_config(['-c','/etc/odoo/odoo.conf'])\n"
        )
        full_script = prelude + py_script
        # Routed via ComputeDriver.service_exec (reusing the caller's ssh). The
        # heredoc rides along as part of the command, exactly as before.
        command = "python3 - <<'SAAS_DBOPS_EOF'\n%s\nSAAS_DBOPS_EOF" % full_script
        r = self._compute_driver(connection=ssh).service_exec(
            self._compute_handle(), command, env=env, timeout=timeout)
        return (r.rc, r.stdout, r.stderr)

    def _docker_exec_sql(self, ssh, sql, db='postgres', timeout=60):
        """Run a single SQL via psql inside the container.

        ``db_password`` is passed via PGPASSWORD env so it doesn't show
        up in process listings.
        """
        psql_server = self.db_server_id
        command = "psql -h %s -p %s -U %s -d %s -tA -c %s" % (
            shlex.quote(self._get_db_host()),
            shlex.quote(str(psql_server.psql_port or 5432)),
            shlex.quote(self.sudo().db_user or ''),
            shlex.quote(db),
            shlex.quote(sql),
        )
        # Routed via ComputeDriver.service_exec (reusing the caller's ssh).
        r = self._compute_driver(connection=ssh).service_exec(
            self._compute_handle(), command,
            env={'PGPASSWORD': self.sudo().db_password or ''}, timeout=timeout)
        return (r.rc, r.stdout, r.stderr)

    def hosting_db_list(self):
        """List databases this instance's customer owns.

        Calls ``odoo.service.db.list_dbs(force=True)`` inside the
        container — that's the exact function ``/web/database/list``
        backs onto, and it already filters by the PG role owner from
        ``odoo.conf``. We additionally filter by the instance prefix
        in Python so a customer can never see (or operate on) another
        tenant's database, even if PG visibility somehow leaked.

        Going through Odoo's own helper instead of building raw SQL
        sidesteps three layers of quoting (Python -> shell -> psql)
        and the ``LIKE ... ESCAPE`` parser strictness that bit us in
        production.

        Returns a list of dicts: ``{'name': str}``.
        """
        self._ensure_hosting_for_db_ops()
        prefix = self._hosting_db_prefix()
        # Marker prefix/suffix so we can recover the list even if Odoo
        # logs decide to print something to stdout during init.
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
            with self.docker_server_id._get_ssh_connection() as ssh:
                exit_code, stdout, stderr = self._docker_exec_python(
                    ssh, script,
                    env={'SAAS_DB_PREFIX': prefix},
                    timeout=60,
                )
        except Exception:
            # SSH-level failure (host down, key rejected, timeout)
            # — turn it into a UserError so the controller's banner
            # catches it instead of bubbling up as a 500.
            _logger.exception(
                "hosting_db_list: SSH/transport failed for %s",
                self.subdomain,
            )
            raise UserError(
                _("We couldn't reach your instance just now. Please "
                  "try again in a moment.")
            )
        if exit_code != 0:
            # Operator visibility: the customer's message is intentionally
            # generic, but ops need the exit code + the last few lines of
            # stderr to debug (container down, compose service name
            # mismatch, python3 not in container, etc.). Log them.
            _logger.warning(
                "hosting_db_list failed for %s: exit=%s stderr=%r stdout=%r",
                self.subdomain, exit_code,
                (stderr or '')[-500:], (stdout or '')[-200:],
            )
            # Surface a short hint to the operator on the instance log
            # too — easier to find than grepping server logs.
            try:
                self._append_log(
                    "Database list lookup failed (exit=%s). "
                    "Last stderr: %s"
                    % (exit_code, (stderr or '').strip()[-300:]),
                )
            except Exception:
                pass
            raise UserError(
                _("We couldn't load your list of databases right now. "
                  "Please try again in a moment, or contact support if "
                  "the problem continues.")
            )
        # Pull `<name>|<admin_login>` pairs out from between the markers;
        # any unrelated log lines Odoo may have emitted are ignored.
        # ``_DB_NAME_RE`` rules out `|` in DB names, so a simple split
        # on the first `|` is safe.
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
        databases — the Odoo.sh-style SQL console.

        Safety, in layers:

        * The target DB must belong to this instance (checked against
          :meth:`hosting_db_list`, which is prefix-filtered) so one
          tenant can never reach another's data — even with several
          databases on the same server.
        * The statement runs inside a ``READ ONLY`` transaction that is
          always rolled back, so INSERT/UPDATE/DELETE/DDL are refused by
          Postgres itself rather than by fragile SQL parsing.
        * We go through Odoo's own ``db_connect`` *inside the container*
          (same path as :meth:`hosting_db_list`), so there is no
          Python -> shell -> psql quoting to get wrong and results come
          back as typed JSON. The query text travels base64-encoded in
          an env var.

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
            with self.docker_server_id._get_ssh_connection() as ssh:
                exit_code, stdout, stderr = self._docker_exec_python(
                    ssh, script,
                    env={
                        'SAAS_SQL_DB': db_name,
                        'SAAS_SQL_B64': q_b64,
                        'SAAS_SQL_LIMIT': str(limit),
                    },
                    timeout=60,
                )
        except Exception:
            _logger.exception(
                "hosting_sql_query: SSH failed for %s", self.subdomain)
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

    # ------------------------------------------------------------------
    # Customer DB management via XML-RPC to the instance's own ``db``
    # service. This is the same endpoint Odoo's /web/database/manager
    # uses — the request is handled by the LIVE Odoo worker process,
    # which means:
    #   * Registry.new(update_module=True) runs in the worker that's
    #     already fully initialised — no fresh-interpreter setup
    #     gotchas like our earlier ``docker exec python3 -`` attempts.
    #   * No racing init container vs running workers — there's only
    #     one process touching the DB.
    #   * No memory doubling.
    # The master password (`saas.instance.admin_password`) flows over
    # HTTPS in the request body; the customer never sees it.
    # ------------------------------------------------------------------
    def _hosting_xmlrpc_db_proxy(self):
        """Return an XML-RPC proxy for this instance's ``db`` service."""
        import xmlrpc.client
        import ssl

        if not self.url:
            raise UserError(_(
                "Instance has no URL yet — is it deployed?"
            ))
        # Some customer instances haven't issued a Let's Encrypt cert
        # yet (e.g. brand-new deploys). We trust our own infra so we
        # disable verification for these server-to-server calls.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        url = '%s/xmlrpc/2/db' % self.url.rstrip('/')
        return xmlrpc.client.ServerProxy(url, context=ctx, allow_none=True)

    def hosting_db_create(self, name, login, password, lang='en_US',
                          country_code=None):
        """Create a customer database by cloning the per-instance template.

        Production path, built to scale to many databases across many
        instances:

        1. Validate the requested name.
        2. Ensure the per-instance template ``__odoo_template_<sub>``
           exists. It's initialised once via a one-off
           ``odoo -i base`` container (slow, ~60-90s) the FIRST time
           a DB is created on the instance; every call after that is
           a single ``SELECT``. The live container is NOT stopped —
           the template lives outside the instance's dbfilter prefix
           so running workers never load it.
        3. ``CREATE DATABASE <new> WITH TEMPLATE <template>`` on the
           db server. Postgres copies the data files at the storage
           layer — seconds, no Odoo init runs. The new DB is always
           either fully present or fully absent; a half-built state
           (the empty-shell failure the XML-RPC path produced) is
           impossible because the clone is atomic.
        4. ``cp -a`` the template's filestore to the new DB's path.
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

        # 1. Ensure the per-instance template exists. First call: slow
        # (~60-90s init in a side container). Subsequent calls: one
        # SELECT.
        template = self._hosting_ensure_template_db()

        # 2. Clone the PG database from the template. Atomic, seconds.
        self._append_log(
            "Cloning '%s' from template '%s'..." % (name, template)
        )
        self._pg_clone_db(template, name)

        # 3. Clone the filestore. An Odoo DB is two things: the psql
        # database (cloned in step 2) AND a per-DB filestore directory.
        # ``CREATE DATABASE WITH TEMPLATE`` only covers the first;
        # without this the new DB's first request would 500 on the
        # missing attachments its cloned ir_attachment rows point at.
        try:
            self._hosting_clone_filestore(template, name)
        except Exception as e:
            self._pg_drop_db(name)
            raise UserError(_(
                "Database '%s' was cloned but filestore copy failed; "
                "rolled back:\n%s"
            ) % (name, e))

        # 4. Patch admin credentials. The cloned DB inherits the
        # template's placeholder admin user; replace its login /
        # password / lang with what the customer entered. On failure
        # we drop both the DB and its filestore so a retry is clean.
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

    def hosting_db_duplicate(self, source, new_name):
        """Duplicate a database via the instance's XML-RPC db service."""
        self._ensure_hosting_for_db_ops()
        source = self._hosting_db_full_name(source)
        new_name = self._hosting_db_full_name(new_name)
        existing = {r['name'] for r in self.hosting_db_list()}
        if source not in existing:
            raise UserError(
                _("Source database '%s' does not exist.") % source
            )
        if new_name in existing:
            raise UserError(
                _("Target database '%s' already exists.") % new_name
            )

        import xmlrpc.client
        proxy = self._hosting_xmlrpc_db_proxy()
        master_pwd = self.sudo().admin_password
        try:
            proxy.duplicate_database(master_pwd, source, new_name)
        except xmlrpc.client.Fault as e:
            msg = (e.faultString or '').strip() or str(e)
            raise UserError(_(
                "We couldn't duplicate the database: %s"
            ) % msg)
        except Exception:
            raise UserError(_(
                "We couldn't reach your instance just now. Please make "
                "sure it's running and try again."
            ))
        return new_name

    # Minimum length for a reset password — same floor we enforce on
    # database creation so a customer can't downgrade themselves to a
    # weaker password through the reset flow.
    _ADMIN_PASSWORD_MIN_LENGTH = 6

    # Module names accepted by ``hosting_db_upgrade_module``. Matches
    # Odoo's own technical-name rules plus the special ``all`` keyword.
    # Validated server-side; the input is interpolated into a shell
    # command on the docker host so a permissive pattern is a real
    # risk (``base; rm -rf /`` etc.). ``shlex.quote`` wraps it on top
    # of this check as defense in depth.
    _UPGRADE_MODULE_RE = re.compile(r'^[a-z_][a-z0-9_]{0,63}$')

    def hosting_db_upgrade_module(self, name, module):
        """Run ``odoo -u <module> -d <db>`` against the customer's container.

        Recovery tool: useful when the live Odoo is broken (500 every
        request), where XML-RPC into a running worker isn't available.

        Sequence:
        1. Stop the running container so it doesn't fight the one-shot
           CLI process for the registry/cursor.
        2. ``docker compose run --rm -T odoo odoo -d <db> -u <module>
           --stop-after-init --no-http --workers=0 --log-level=info``.
           The ``run`` (vs ``exec``) sub-command spins up a *separate*
           one-shot container with the same image and mounts, so it
           still works when the long-lived odoo service is stopped.
        3. Restart the container with ``docker compose up -d``.

        Returns the captured stdout+stderr. Raises ``UserError`` on
        any failure; the exception carries a ``_saas_upgrade_output``
        attribute with the partial output so the portal can render it
        even on a failed run.
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

        instance_path = self._get_instance_path()
        with self.docker_server_id._get_ssh_connection() as ssh:
            captured = []

            def _err(msg):
                # Attach the captured output to the exception so the
                # bg worker can persist it on the op record.
                exc = UserError(msg)
                exc._saas_upgrade_output = '\n'.join(captured)
                raise exc

            self._append_log(
                "Module upgrade: stopping container for '%s' "
                "(target db=%s, module=%s)..."
                % (self.subdomain, name, module)
            )
            ec, sout, serr = ssh.execute(
                'cd %s && docker compose stop odoo 2>&1'
                % shlex.quote(instance_path),
                timeout=120,
            )
            captured.append(
                '$ docker compose stop odoo (exit %s)\n%s' % (ec, sout + serr)
            )
            # ``stop`` exits 0 when nothing was running too, so we only
            # bail on hard SSH errors.
            if ec not in (0,):
                _err(_(
                    "Couldn't pause your instance before starting the "
                    "repair. Please try again in a moment."
                ))

            self._append_log(
                "Running 'odoo -d %s -u %s' on a one-shot container..."
                % (name, module)
            )
            run_cmd = (
                'cd %s && docker compose run --rm -T odoo '
                'odoo -d %s -u %s --stop-after-init --no-http '
                '--workers=0 --log-level=info 2>&1'
            ) % (
                shlex.quote(instance_path),
                shlex.quote(name),
                shlex.quote(module),
            )
            ec, sout, serr = ssh.execute(run_cmd, timeout=1800)
            captured.append(
                '$ %s\n%s' % (run_cmd, sout + serr)
            )
            upgrade_failed = ec != 0

            # Always try to bring the container back up, even if the
            # upgrade failed — otherwise the customer's site stays
            # offline indefinitely.
            self._append_log("Bringing container back up...")
            up_ec, up_out, up_err = ssh.execute(
                'cd %s && docker compose up -d 2>&1'
                % shlex.quote(instance_path),
                timeout=300,
            )
            captured.append(
                '$ docker compose up -d (exit %s)\n%s'
                % (up_ec, up_out + up_err)
            )

            if upgrade_failed:
                _err(_(
                    "The repair didn't complete successfully. See the "
                    "report for details."
                ))
            if up_ec != 0:
                _err(_(
                    "The repair finished, but your instance didn't come "
                    "back up automatically. See the report for details, "
                    "or contact support."
                ))

        return '\n'.join(captured)

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
        self.env['saas.job']._enqueue(
            op, '_run_upgrade', channel='dbop',
            lock_key='instance:%s' % self.id, idempotent=False, max_attempts=1)
        return op

    def _parse_upgrade_modules(self, modules):
        """Normalise + validate a customer-typed module list.

        Accepts comma- or space-separated technical names. Returns
        ``['all']`` if the customer asked to upgrade everything, else a
        de-duplicated list of validated module names. Same per-name
        validation as the recovery path so a name can never smuggle
        shell/CLI tokens downstream.
        """
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
        container via ``docker compose exec`` (no ``stop``, no one-shot
        ``run``): the module migration runs in a short-lived python
        process, and the running workers pick up the rebuilt registry
        through Odoo's standard registry-signaling — so the customer's
        site stays up throughout. There may be a brief blip while the
        migration holds its locks, but the instance never goes down.

        Contrast :meth:`hosting_db_upgrade_module` — the recovery path
        that *stops* the container, for when the live Odoo is already
        returning 500s and exec/XML-RPC into it won't work.

        Returns the captured stdout/stderr. Raises ``UserError`` on
        failure, with the captured output attached as
        ``_saas_upgrade_output`` so the portal can render the report.
        """
        self._ensure_hosting_for_db_ops()
        name = self._hosting_db_full_name(name)
        if name not in {r['name'] for r in self.hosting_db_list()}:
            raise UserError(
                _("Database '%s' does not belong to this instance.") % name
            )
        mod_list = self._parse_upgrade_modules(modules)

        # The script runs inside the live container. It marks the
        # requested modules 'to upgrade' and triggers Odoo's in-process
        # registry rebuild (``button_immediate_upgrade``); the live
        # workers reload via the registry-signaling sequence right
        # after. We capture the module names BEFORE the call (the env is
        # reset during the rebuild) and ``os._exit(0)`` on success so a
        # noisy cursor teardown can't turn a good run into a non-zero
        # exit. Markers tell a real success from "done" text in a log.
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
        with self.docker_server_id._get_ssh_connection() as ssh:
            ec, sout, serr = self._docker_exec_python(
                ssh, script, env=script_env, timeout=1800,
            )
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
        # Validate the module list upfront so a bad name is a synchronous
        # error, not a ``failed`` record the customer has to discover.
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
        self.env['saas.job']._enqueue(
            op, '_run_upgrade_live', channel='dbop',
            lock_key='instance:%s' % self.id, idempotent=False, max_attempts=1)
        return op

    def hosting_db_restore_prepare_upload(self, name):
        """Create a placeholder backup record + a presigned PUT URL.

        Lets the customer upload their OWN local Odoo backup (.zip)
        straight to the bucket from the browser — the bytes never pass
        through Odoo, so no worker is held and there's no request
        timeout, at any size up to the bucket's single-PUT limit. The
        record is ephemeral (reaped within a couple of hours — the
        uploaded object is not retained). Returns ``(backup, url)``.
        """
        self._ensure_hosting_for_db_ops()
        # ``_hosting_db_full_name`` enforces the instance prefix + a valid
        # identifier — that's the ownership boundary.
        full = self._hosting_db_full_name(name)
        # Restore always creates a NEW database — never overwrite an
        # existing one (no accidental data loss). The customer must pick a
        # free name.
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
            # Placeholder until the browser finishes the PUT; flipped to
            # 'done' by hosting_db_restore_from_upload once we confirm
            # the object actually landed in the bucket.
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
        """Verify an uploaded object, then restore it into its db_name.

        Reuses the standard background restore (``action_restore_backup``
        -> ``_do_restore_backup``): download from the bucket to the
        docker host, drop + recreate the target DB, ``psql`` the dump
        in, restore the filestore. All on the host with generous
        timeouts, so it scales to large databases without tying up the
        portal. The uploaded object is reaped afterwards (ephemeral).
        """
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
        # Track it as a per-DB operation (like create/duplicate) and run
        # in the background — deliberately NOT via action_restore_backup,
        # which flips the WHOLE instance to 'provisioning'. Restoring one
        # database shouldn't make the instance look down: the container
        # keeps running, only the target DB is briefly replaced, and the
        # UI shows just that row as "Restoring…".
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
        self.env['saas.job']._enqueue(
            op, '_run_restore', args=(backup.id,), channel='dbop',
            lock_key='instance:%s' % self.id, idempotent=False, max_attempts=1)
        return op

    def hosting_db_reset_admin_password(self, name, new_password,
                                        login=None):
        """Reset an administrator's password on a customer database.

        Self-service for "I forgot my admin password". We don't have
        the customer's current password (that's the whole point), so
        we can't go through XML-RPC ``authenticate``. Instead we
        docker-exec a short Python script inside the customer's Odoo
        container — same path ``hosting_db_list`` uses — and let the
        ORM set the password via the inverse setter (so the proper
        pbkdf2 hashing pipeline runs, not raw column writes).

        Which user gets reset, in order:

        1. ``login`` — an exact login the customer typed. Use this
           when they created their own admin and know its login, or
           when several admins exist and they want a specific one.
        2. ``base.user_admin`` — the bootstrap admin, if it still
           exists and is active.
        3. The oldest active internal member of the
           Settings/Administration group (``base.group_system``).
           This is what covers "the customer DELETED the original
           admin and created their own" — we reset whoever currently
           holds admin rights, not a hardcoded uid.
        4. The oldest active internal user (last resort).

        Returns the login of the user whose password was reset, so the
        caller can show it (useful when the customer forgot which user
        is the admin).
        """
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

        # The script runs inside the Odoo container with ``os`` and
        # ``odoo.tools.config`` already imported by
        # ``_docker_exec_python``. Markers (BEGIN/END) so we can tell
        # success apart from a stdout that happens to contain "OK".
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
        with self.docker_server_id._get_ssh_connection() as ssh:
            exit_code, stdout, stderr = self._docker_exec_python(
                ssh, script, env=script_env, timeout=120,
            )
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
            # Strip the password from the env before logging in case
            # the helper echoed it — it never does, but defense in depth.
            raise UserError(_(
                "We couldn't reset the admin password for '%s' just now. "
                "Please try again, or contact support if the problem "
                "continues."
            ) % name)
        # Pull out the login that was reset so the caller can confirm it.
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
        """Drop a customer database at the PG level (reliable).

        Uses ``dropdb --force`` (PG 13+) on the db server via
        :meth:`_pg_drop_db`, which terminates any lingering
        connections — including the registry-build attempts a broken
        DB attracts — and drops atomically in one step. The previous
        XML-RPC ``db.drop`` path lost a race against the instance's
        own workers reconnecting to rebuild the registry, so a DB that
        had failed to load could never be deleted from the dashboard.

        The customer can only target DBs in the instance's prefix
        namespace — ``_hosting_db_full_name`` enforces that even if a
        crafted POST tried to drop e.g. ``postgres``.
        """
        self._ensure_hosting_for_db_ops()
        name = self._hosting_db_full_name(name)
        if name not in {r['name'] for r in self.hosting_db_list()}:
            raise UserError(
                _("Database '%s' does not belong to this instance.") % name
            )

        # Drop the PG database (force-terminates connections), then
        # remove its filestore. ``_pg_drop_db`` is ``--if-exists`` and
        # clears any ``datistemplate`` flag first, so it's safe even
        # on a half-built DB. Filestore cleanup is best-effort: a
        # dropped DB with a leftover filestore dir is harmless, just
        # wasted disk.
        self._pg_drop_db(name)
        self.env['saas.audit.log']._saas_audit(
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

    def hosting_db_backup(self, name, backup_format='zip'):
        """Create the instance's single on-demand backup of one database.

        Triggered from the Databases page. Policy (per customer
        request): an instance keeps AT MOST ONE on-demand backup at a
        time, and it's ephemeral — transient, reaped within an hour so
        nothing is retained on the bucket.

        So pressing "Download backup":
          * wipes every existing on-demand backup on this instance
            (any database) — bucket object + record — keeping only the
            new one;
          * creates an ``ephemeral`` backup of ``<sub>_<name>`` with a
            1-hour ``expires_at``, which ``_cron_cleanup_ephemeral_backups``
            reaps once it lapses (right after the download).

        Full-instance snapshots (``is_full_instance=True``) are left
        untouched. Reuses ``_run_portal_backup``, which honours the
        record's ``db_name`` and ``ephemeral`` flag.
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
        # Serialise against concurrent backup clicks on this instance
        # (same guard as the instance-level backup).
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

        # Single on-demand slot per instance: clear every existing
        # on-demand backup (any database) before making the new one so
        # only the latest survives. Snapshots are left alone.
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
        # Extension in the name makes the format obvious in the list and
        # in the downloaded filename (e.g. ``backup_acme_test_…​.dump``).
        backup = Backup.create({
            'instance_id': self.id,
            'db_name': full,
            'name': 'backup_%s_%s.%s' % (full, now_str, fmt),
            'state': 'running',
            'is_full_instance': False,
            'ephemeral': True,
            'format': fmt,
        })
        # Durable queue (ARCH-004 Phase 1).
        self.env['saas.job']._enqueue(
            backup, '_run_portal_backup',
            channel='backup', lock_key='instance:%s' % self.id,
            max_attempts=1,
        )
        return backup

    def _hosting_template_db_name(self):
        """Per-instance template DB name.

        Lives outside the customer's prefix namespace so:
        * ``hosting_db_list`` (which filters by ``<sub>_``) won't show it;
        * ``_DB_NAME_RE`` rejects names starting with ``_``, so a
          customer can't accidentally target it via the portal.
        """
        self.ensure_one()
        # Strip hyphens; PG identifiers are happier with underscores.
        safe = (self.subdomain or '').replace('-', '_').lower()
        return '__odoo_template_%s' % safe

    def _hosting_ensure_template_db(self):
        """Return a ready-to-clone per-instance template DB, building
        it if necessary. Self-healing and concurrency-safe.

        The template (``__odoo_template_<sub>``) is a fully-installed
        ``base`` database. It's built once via the slow ``odoo -i
        base`` path; every customer create after that is a near-instant
        ``CREATE DATABASE WITH TEMPLATE`` clone off it.

        Three states are handled so a create never dead-ends:

        * **Healthy** (exists + ``base`` installed) → make sure the
          ``datistemplate`` shield is set and return it.
        * **Half-built leftover** (exists but ``base`` NOT installed —
          a previous build was OOM-killed or interrupted) → drop it and
          rebuild. No manual cleanup, no "stuck retry".
        * **Missing** → build it.

        A per-instance in-process lock serialises the build so two
        concurrent first-creates can't both init, and so the
        drop-and-rebuild above can't ever hit a build that's actually
        in flight.
        """
        self.ensure_one()
        template = self._hosting_template_db_name()
        with _hosting_template_build_lock(self.id):
            # Happy path first: a healthy template already exists.
            if self._pg_db_initialized(template):
                # Ensure the shield flag is set — covers templates from
                # older code that flagged after init, or a flag that
                # got cleared. Idempotent and cheap.
                self._pg_mark_template(template, flag=True)
                return template

            # Not healthy. If a half-built shell is sitting there from a
            # failed attempt, clear it — the lock guarantees no other
            # build is using this name right now, so this is safe.
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

    def _hosting_core_addons_path(self, ssh):
        """Core (non-customer) addons path for the base-only template build.

        Reads the instance's ``odoo.conf`` and strips the customer's
        ``/mnt/extra-addons`` entries, leaving only the image's core Odoo
        addons (where ``base`` lives). This makes the one-time template
        build immune to a broken or version-mismatched customer module,
        which would otherwise abort ``odoo -i base`` and strand every DB
        create in "creating". Falls back to the standard image core paths
        if the conf can't be read.
        """
        self.ensure_one()
        default = '/opt/odoo/odoo/addons,/opt/odoo/addons'
        conf = '%s/config/odoo.conf' % self._get_instance_path()
        try:
            code, out, _err = ssh.execute(
                "grep -iE '^[[:space:]]*addons_path' %s" % shlex.quote(conf),
                timeout=30,
            )
        except Exception:
            return default
        if code != 0 or not out:
            return default
        line = out.strip().splitlines()[0]
        __, __, val = line.partition('=')
        parts = [p.strip() for p in val.split(',') if p.strip()]
        core = [p for p in parts if not p.startswith('/mnt/extra-addons')]
        return ','.join(core) if core else default

    def _hosting_build_template_db(self, template):
        """Build ``template`` from scratch: createdb → init → verify.

        Pauses the live container for the one-time init. Two reasons,
        both learned the hard way in production:

        * **Memory** — the init runs in a second ``docker compose run
          --rm`` Odoo container; run alongside the live one it doubles
          RAM on the host and the init gets OOM-killed mid-install
          (symptom: the install dies with ``SSL connection has been
          closed unexpectedly`` — the PG backend killed under memory
          pressure). Freeing the live container's RAM first gives the
          init room.
        * **Race** — with the container down, no worker can grab the
          half-built DB and cache a broken registry.

        This is a ONE-TIME ~60-90s blip, and only on the first DB
        create per instance. Every create after that is an instant
        clone with no pause.

        ``datistemplate=true`` is flagged up front so that (a) once the
        container is back up Odoo's ``list_dbs()`` keeps its workers /
        cron / db-selector off it, and (b) the later clone can run —
        ``CREATE DATABASE ... WITH TEMPLATE`` needs *zero* connections
        to the source, and the flag is what keeps Odoo from opening
        any.
        """
        self.ensure_one()
        self._append_log(
            "Bootstrapping per-instance template DB '%s' (one-time, "
            "~60-90s)..." % template
        )
        self._pg_ensure_db_with_grants(template)
        self._pg_mark_template(template, flag=True)

        instance_path = self._get_instance_path()
        init_exit = 1
        init_output = ''
        with self.docker_server_id._get_ssh_connection() as ssh:
            self._append_log(
                "Pausing instance for one-time template build "
                "(~60-90s, first DB only)..."
            )
            try:
                self._compute_driver(connection=ssh).destroy(self._compute_handle())
            except Exception:
                pass  # best-effort pause before the one-time template build
            # The template only needs ``base``, so build it with a
            # CORE-ONLY addons path (customer ``/mnt/extra-addons`` modules
            # stripped). ``odoo -i base`` scans every manifest in the path,
            # so a single broken / version-mismatched customer module (e.g.
            # an Odoo-18 ``version`` string in an Odoo-17 image) would
            # otherwise abort the init and leave every DB create stuck in
            # "creating". Customer modules still install into the real DB
            # later via the normal flow — the template clone is base-only.
            core_addons = self._hosting_core_addons_path(ssh)
            init_cmd = (
                'cd %s && docker compose run --rm -T odoo '
                'odoo -d %s '
                '-i base '
                '--addons-path=%s '
                '--without-demo=all '
                '--stop-after-init '
                '--no-http '
                '--workers=0 '
                '--log-level=info 2>&1'
            ) % (
                shlex.quote(instance_path),
                shlex.quote(template),
                shlex.quote(core_addons),
            )
            try:
                init_exit, stdout, stderr = ssh.execute(
                    init_cmd, timeout=1800,
                )
                init_output = (stdout or '') + (stderr or '')
            finally:
                # Always bring the instance back up, even if the init
                # raised — leaving the customer's container down is far
                # worse than a failed template build.
                self._append_log("Resuming instance...")
                try:
                    self._compute_driver(connection=ssh).start(self._compute_handle())
                except Exception as e:
                    self._append_log("Warning: failed to resume container: %s" % e)

        # Verify at the PG level (independent of the container being
        # fully back up). On any failure, drop the partial build + its
        # filestore so the NEXT create self-heals from a clean slate
        # rather than tripping over our debris.
        if init_exit != 0 or not self._pg_db_initialized(template):
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
                "instance (the one-time setup failed). The instance is "
                "back online; please try creating the database again.\n\n"
                "Last setup output:\n%s"
            ) % (init_output[-6000:] or '(no output captured)'))

        self._append_log("Template DB '%s' ready." % template)
        return template

    def _hosting_filestore_path(self, db_name):
        """Return the host-side path to a DB's Odoo filestore.

        On an object-storage host (``object_filestore_mount`` set) the filestore
        is bind-mounted from the JuiceFS mount, so it lives at
        ``<mount>/<partner>/<sub>/filestore/<db>``. Otherwise the compose volume
        maps ``./data/odoo`` → ``/var/lib/odoo``, so it's
        ``<instance_path>/data/odoo/filestore/<db>``. Used to copy / delete
        filestores without entering the container.
        """
        mount = self._get_filestore_mount()
        if mount:
            return '%s/%s' % (mount, db_name)
        return '%s/data/odoo/filestore/%s' % (
            self._get_instance_path(), db_name,
        )

    def _hosting_clone_filestore(self, source_db, target_db):
        """Clone the template's filestore directory to the new DB's path.

        On an object-storage host the filestore lives on JuiceFS, so we use
        ``juicefs clone`` — a metadata-only copy-on-write that is instant and
        consumes no extra object storage (no byte copy, dropping ``cp -a``).
        On a local-disk host we keep ``cp -a``. Either way we run sudo (the
        source is container-owned) and fix ownership so the running container
        can write. If the template never wrote a filestore, create an empty one.
        """
        self.ensure_one()
        src = self._hosting_filestore_path(source_db)
        dst = self._hosting_filestore_path(target_db)
        use_object_store = bool(self._get_filestore_mount())

        with self.docker_server_id._get_ssh_connection() as ssh:
            container_uid = self._get_container_uid(ssh)
            # CoW clone on JuiceFS (instant, no extra storage) vs byte copy.
            copy_step = (
                '  sudo juicefs clone %(src)s %(dst)s; '
                if use_object_store else
                '  sudo cp -a %(src)s %(dst)s; '
            )
            cmd = (
                # Idempotent: if dst already exists from a previous
                # half-run, blow it away first.
                'sudo rm -rf %(dst)s && '
                # Copy or create empty. The template's filestore may
                # legitimately not exist if no module wrote anything
                # to disk; cover that case so we don't error out.
                'if [ -d %(src)s ]; then '
                + copy_step +
                'else '
                '  sudo mkdir -p %(dst)s; '
                'fi && '
                'sudo chown -R %(uid)s:%(uid)s %(dst)s && '
                'sudo chmod -R 755 %(dst)s'
            ) % {
                'src': shlex.quote(src),
                'dst': shlex.quote(dst),
                'uid': container_uid,
            }
            exit_code, stdout, stderr = ssh.execute(cmd, timeout=600)
            if exit_code != 0:
                raise UserError(_(
                    "Failed to clone filestore:\n%s"
                ) % (stderr or stdout))

    def _hosting_drop_filestore(self, db_name):
        """Remove a DB's filestore directory. Best-effort.

        Called from rollback paths and from instance deletion. We
        guard the name regex one more time before passing to the
        shell — defense in depth even though callers already validate.
        """
        self.ensure_one()
        if not self._DB_IDENT_RE.match(db_name or ''):
            return
        path = self._hosting_filestore_path(db_name)
        with self.docker_server_id._get_ssh_connection() as ssh:
            ssh.execute(
                'sudo rm -rf %s' % shlex.quote(path), timeout=120,
            )

    def _hosting_patch_admin_creds(self, db_name, login, password,
                                   lang, country_code):
        """Set admin login / password / lang on a freshly-cloned DB.

        Goes through ``docker compose exec python3 -`` so Odoo's
        password hashing runs (we never see or store the plaintext
        in PG). The script writes directly via the ORM — short, no
        registry preload contention because the DB was just cloned
        and no worker has touched it yet.
        """
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
        with self.docker_server_id._get_ssh_connection() as ssh:
            exit_code, stdout, stderr = self._docker_exec_python(
                ssh, script, env=env, timeout=120,
            )
        if exit_code != 0 or 'OK' not in (stdout or ''):
            raise UserError(_(
                "Could not patch admin credentials:\n%s\n%s"
            ) % ((stdout or '')[-1000:], (stderr or '')[-500:]))

    def hosting_db_create_async(self, name, login, password,
                                lang='en_US', country_code=None):
        """Queue a database create and return the tracking record.

        The actual create runs in a background thread so the HTTP
        request returns within ~200 ms — far below nginx's
        ``proxy_read_timeout`` and without tying up a worker for the
        30-90 s the CLI init takes.
        """
        self._ensure_hosting_for_db_ops()
        # Validate upfront so the customer gets a synchronous error
        # for bad names instead of a "failed" record they have to
        # discover on refresh.
        full_name = self._hosting_db_full_name(name)
        if not password:
            raise UserError(_("Initial admin password is required."))
        existing = {r['name'] for r in self.hosting_db_list()}
        if full_name in existing:
            raise UserError(_("Database '%s' already exists.") % full_name)
        # Only ONE create may run on an instance at a time. The first
        # one builds the per-instance template (a ~60-90s, container-
        # pausing, single-flight operation) and a parallel create would
        # race it; just as importantly, this is the server-side guard
        # that stops a customer kicking off a second create from
        # another browser tab. Enforced here (not just in the UI) so it
        # holds no matter how the request arrives.
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
        # passes them in memory only. Create is non-idempotent (no auto-retry),
        # so it gains nothing from the durable queue anyway.
        run_in_background(
            op, '_run_create',
            method_args=(
                login, password, lang or 'en_US', country_code or None,
            ),
            thread_name='saas_db_create_%s' % full_name,
            heartbeat_field='last_heartbeat',
        )
        return op

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
        self.env['saas.job']._enqueue(
            op, '_run_duplicate', channel='dbop',
            lock_key='instance:%s' % self.id, idempotent=False, max_attempts=1)
        return op

    def hosting_db_drop_async(self, name):
        """Queue a database drop and return the tracking record.

        Drop is fast (a single ``DROP DATABASE``) but still goes async
        so the experience matches create / duplicate and the customer
        sees the same in-flight indicator.
        """
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
        self.env['saas.job']._enqueue(
            op, '_run_drop', channel='dbop',
            lock_key='instance:%s' % self.id, idempotent=False, max_attempts=1)
        return op

    def _DEPRECATED_hosting_db_duplicate_dockerexec(self, source, new_name):
        """[DEPRECATED] Pre-XML-RPC duplicate path via docker exec.

        Replaced by the XML-RPC ``hosting_db_duplicate`` above.
        """
        self._ensure_hosting_for_db_ops()
        # Both source and target live under the instance prefix.
        # _hosting_db_full_name strips a re-pasted prefix so the
        # customer can hand us either the bare name or the full one.
        source = self._hosting_db_full_name(source)
        new_name = self._hosting_db_full_name(new_name)
        existing = {r['name'] for r in self.hosting_db_list()}
        if source not in existing:
            raise UserError(
                _("Source database '%s' does not exist.") % source
            )
        if new_name in existing:
            raise UserError(
                _("Target database '%s' already exists.") % new_name
            )
        script = (
            "from odoo.service import db\n"
            "db.exp_duplicate_database(\n"
            "  os.environ['SAAS_DB_SRC'],\n"
            "  os.environ['SAAS_DB_DST'],\n"
            ")\n"
            "print('OK')\n"
        )
        env = {'SAAS_DB_SRC': source, 'SAAS_DB_DST': new_name}
        with self.docker_server_id._get_ssh_connection() as ssh:
            exit_code, stdout, stderr = self._docker_exec_python(
                ssh, script, env=env, timeout=600,
            )
        if exit_code != 0 or 'OK' not in stdout:
            raise UserError(_(
                "Database duplicate failed:\n%s\n%s"
            ) % (stdout[-1000:], stderr[-500:]))
        return new_name

    def _DEPRECATED_hosting_db_drop_dockerexec(self, name):
        """[DEPRECATED] Pre-XML-RPC drop path via docker exec.

        Replaced by the XML-RPC ``hosting_db_drop`` above.
        """
        self._ensure_hosting_for_db_ops()
        name = self._hosting_db_full_name(name)
        # Belt and braces: confirm the DB really is one of ours before
        # talking to PG. ``hosting_db_list`` already filters by prefix.
        if name not in {r['name'] for r in self.hosting_db_list()}:
            raise UserError(
                _("Database '%s' does not belong to this instance.") % name
            )
        script = (
            "from odoo.service import db\n"
            "db.exp_drop(os.environ['SAAS_DB_NAME'])\n"
            "print('OK')\n"
        )
        with self.docker_server_id._get_ssh_connection() as ssh:
            exit_code, stdout, stderr = self._docker_exec_python(
                ssh, script, env={'SAAS_DB_NAME': name}, timeout=120,
            )
        if exit_code != 0 or 'OK' not in stdout:
            raise UserError(_(
                "Database drop failed:\n%s\n%s"
            ) % (stdout[-1000:], stderr[-500:]))
        return name

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
            'pending_plan_id': self.pending_plan_id.id if self.pending_plan_id else False,
            'backup_running': bool(self.backup_ids.filtered(lambda b: b.state == 'running')),
            'restoration_pending': bool(self.restoration_invoice_id),
            'db_ops_running': bool(self._hosting_reconcile_db_ops()),
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

    def _has_overdue_invoices_past_grace(self):
        """Return True if this instance has invoices overdue past the grace period."""
        self.ensure_one()
        if self.is_trial or not self.partner_id:
            return False
        all_invoices = self._get_all_invoices()
        if not all_invoices:
            return False
        today = fields.Date.today()
        overdue = all_invoices.filtered(
            lambda m: m.move_type == 'out_invoice'
            and m.payment_state not in ('paid', 'in_payment', 'reversed')
            and m.state == 'posted'
            and m.invoice_date_due
            and m.invoice_date_due < today
        )
        if not overdue:
            return False
        grace_days = self._grace_period_days()
        oldest_due = min(overdue, key=lambda m: m.invoice_date_due).invoice_date_due
        return (today - oldest_due).days > grace_days

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

    def action_reactivate(self, new_plan_id, billing_period='monthly'):
        """Reactivate a cancelled instance with a new plan.

        Reuses the same record — resets state to draft, assigns the new
        plan, clears old infrastructure fields, then runs the billing /
        deploy flow.  The retained_backup_path is preserved so the admin
        can still restore data if the client requests it.
        """
        self.ensure_one()
        if self.state not in ('cancelled', 'cancelled_by_client'):
            raise UserError(_("Only cancelled instances can be reactivated."))

        # Retry any cleanup that failed during the original
        # cancellation BEFORE we clear the old FKs (we lose the
        # ability to reach the old resources once the FKs go).
        self._retry_pending_cleanup()

        new_plan = self.env['saas.plan'].browse(int(new_plan_id))
        if not new_plan.exists() or new_plan.is_trial_plan:
            raise UserError(_("Please select a valid paid plan."))

        if billing_period not in ('monthly', 'yearly'):
            billing_period = 'monthly'
        if billing_period == 'yearly' and not new_plan.yearly_price:
            billing_period = 'monthly'

        # Reset to draft with new plan — clear old infra but keep
        # history. ``cancellation_reason`` and the retained snapshot
        # rows are intentionally NOT cleared so the customer can
        # restore their data after re-enabling daily backups.
        # ``daily_backup_enabled`` IS cleared on purpose: a cancelled
        # subscription means the snapshot add-on is gone too, so the
        # customer must opt in (and pay) again before they can use
        # snapshots — including restoring from the one we retained.
        self.write({
            'state': 'draft',
            'plan_id': new_plan.id,
            'billing_period': billing_period,
            'is_trial': False,
            # Clear stale infrastructure (will be re-allocated on deploy)
            'docker_server_id': False,
            'db_server_id': False,
            'xmlrpc_port': False,
            'longpolling_port': False,
            'deploy_retry_count': 0,
            'is_overcommitted': False,
            # Clear stale billing refs (new SO will be created)
            'sale_order_id': False,
            'pending_plan_id': False,
            'pending_billing_period': False,
            'pending_change_invoice_id': False,
            'scheduled_plan_id': False,
            'scheduled_billing_period': False,
            'suspension_warning_sent': False,
            # Daily-backup subscription is reset — the customer must
            # re-enable it (and pay a fresh activation invoice) before
            # nightly snapshots resume OR the retained snapshot can
            # be restored. See ``action_restore_full_instance``'s gate.
            'daily_backup_enabled': False,
            'daily_backup_pending_invoice_id': False,
            'daily_backup_next_invoice_date': False,
            'daily_backup_last_invoice_date': False,
            # Saved card + auto-renew are tied to the previous
            # subscription. Reactivation is a fresh commitment; force
            # the customer to opt in again so the new subscription
            # never charges an old card without explicit consent
            # (also avoids PCI surprises if the customer changed banks
            # during the cancellation window).
            'payment_token_id': False,
            'auto_renew_subscription': True,
            'auto_renew_daily_backup': True,
            # retained_backup_path is intentionally NOT cleared
            # Reset restore banner so client sees the option again
            'restore_banner_dismissed': False,
            'restoration_invoice_id': False,
        })

        self._append_log(
            "Instance reactivated by client. New plan: %s (%s)."
            % (new_plan.name, billing_period)
        )
        self.message_post(body=_(
            "Instance reactivated. New plan: %s (%s)."
        ) % (new_plan.name, billing_period))

        # Run billing flow (creates SO + invoice)
        self.action_confirm_and_bill()
        return True

