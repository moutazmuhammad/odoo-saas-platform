from odoo import api, fields, models, _


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    saas_default_instance_starting_port = fields.Integer(
        string='Default Starting Port',
        config_parameter='saas_master.default_instance_starting_port',
        default=32000,
        help='First port number in the range used for auto-assigning HTTP and '
             'longpolling ports to new instances. Ports are allocated in pairs '
             '(HTTP, longpolling) starting from this value.',
    )

    # ========== Compute backend ==========
    # The backend a new instance provisions on. Kubernetes is the default —
    # Docker Compose is kept only as a simpler, optional alternative for an
    # operator who doesn't want to run a cluster. This is a platform-level
    # infrastructure choice, deliberately independent of the customer-facing
    # compute tiers (see saas.compute.tier — a Docker-Compose-backed
    # instance just never offers tier selection).
    saas_default_compute_driver = fields.Selection(
        [('kubernetes', 'Kubernetes'), ('ssh_docker', 'Docker Compose')],
        string='Default Compute Backend',
        config_parameter='saas_master.default_compute_driver',
        default='kubernetes',
        help='Backend new instances provision on when nothing more specific '
             'assigns one. Kubernetes is the modern default; Docker Compose '
             'remains available as a simpler alternative for an operator '
             'who has not set up a cluster. Existing instances are '
             'unaffected by changing this.',
    )

    # Compute tiers (Standard/HA/Scale/...) are managed as their own
    # records — SaaS > Configuration > Compute Tiers — not as settings
    # here, since each tier needs its own name/replicas/price and the set
    # of tiers is meant to grow without code changes (see
    # saas.compute.tier).

    # ========== Free Trial ==========
    saas_trial_days = fields.Integer(
        string='Free Trial Duration (Days)',
        config_parameter='saas_master.trial_days',
        default=14,
        help='Number of days for the free trial period. '
             'After expiry the instance is suspended until the client pays.',
    )

    # ========== Custom Plan Pricing, Hosting Plan Builder, Pricing
    # Engine cost floors/minimums/storage blocks, and the deprecated
    # extra-storage-per-GB field all live in
    # saas_billing/models/res_config_settings.py now — every field in
    # those sections is a rate, discount, floor, or minimum, i.e.
    # commercial/billing concerns. ==========

    # ========== Support ==========
    saas_support_email = fields.Char(
        string='Support Email',
        config_parameter='saas_master.support_email',
        help='Support email address shown to clients in email notifications '
             'and portal pages when they need to contact support.',
    )
    # The retained-snapshot restoration fee is COMPUTED, not configured:
    # months retained after cancellation × snapshot size (rounded up to
    # the next whole GB) × saas_master.snapshot_price_per_gb. See
    # saas.instance._get_retained_snapshot_fee().

    # ========== Website Sections ==========
    # Toggle visibility of the public Services / Hosting sections.
    # These intentionally don't use ``config_parameter=`` — that path
    # routes through Odoo's set_param() which unlinks the row on False,
    # and through a Boolean coercion that reads ``bool('False')`` (True!)
    # — so the toggle always springs back on. We read/write the
    # underlying ir.config_parameter rows by hand in get_values /
    # set_values below, storing the literal strings ``'True'`` and
    # ``'False'``. The templates check ``!= 'False'`` so an unset row
    # (fresh install) defaults to shown.
    saas_show_services_section = fields.Boolean(
        string='Show Services Section',
        default=True,
        help='Show the "Services" section (catalog and detail pages) on '
             'the public website. Turn off if you only want to sell '
             'Hosting at this stage. The data is preserved.',
    )
    saas_show_hosting_section = fields.Boolean(
        string='Show Hosting Section',
        default=True,
        help='Show the "Hosting" section (landing page and configurator) '
             'on the public website. Turn off to hide hosting offerings '
             'temporarily. The data is preserved.',
    )

    # ========== Rate Limiting ==========
    # NO config_parameter= here — deliberately. The framework persists
    # integer settings as ``repr(value) if value else False`` and
    # ``set_param(key, False)`` DELETES the parameter, so saving 0
    # ("unlimited", per the help text) silently reverted to the default
    # on reload. Handled manually in get_values/set_values instead,
    # like the Boolean settings below (same falsy-value trap).
    saas_max_instances_per_user = fields.Integer(
        string='Max Instances Per User',
        default=5,
        help='Maximum number of active instances a single customer can have. '
             '0 = unlimited.',
    )

    # ========== Dunning / Grace, storage-capacity grace, and bonus-credit
    # expiry all live in saas_billing/models/res_config_settings.py now —
    # all three gate billing/suspension timing, not provisioning. ==========

    # ========== Backup Storage ==========
    saas_backup_provider = fields.Selection([
        ('aws', 'AWS S3'),
        ('digitalocean', 'DigitalOcean Spaces'),
        ('hetzner', 'Hetzner Object Storage'),
    ], string='Backup Provider',
        config_parameter='saas_backup.provider',
    )
    saas_backup_bucket_name = fields.Char(
        string='Bucket Name',
        config_parameter='saas_backup.bucket_name',
    )
    saas_backup_region = fields.Char(
        string='Region',
        config_parameter='saas_backup.region',
        help='e.g. us-east-1, europe-west1, nyc3, fsn1',
    )
    saas_backup_access_key = fields.Char(
        string='Access Key',
        config_parameter='saas_backup.access_key',
    )
    saas_backup_secret_key = fields.Char(
        string='Secret Key',
        config_parameter='saas_backup.secret_key',
    )
    saas_backup_endpoint = fields.Char(
        string='Endpoint URL',
        config_parameter='saas_backup.endpoint',
        help='Custom S3-compatible endpoint. Not needed for DigitalOcean '
             'Spaces or Hetzner Object Storage (derived from the region). '
             'e.g. https://nyc3.digitaloceanspaces.com',
    )
    # Dedicated, broader-privilege credentials used ONLY by the "Allow
    # browser uploads" (PutBucketCORS) button — kept separate from the
    # least-privilege object key above so the backup/restore key never
    # needs bucket-admin rights.
    saas_backup_cors_access_key = fields.Char(
        string='CORS Admin Access Key',
        config_parameter='saas_backup.cors_access_key',
    )
    saas_backup_cors_secret_key = fields.Char(
        string='CORS Admin Secret Key',
        config_parameter='saas_backup.cors_secret_key',
    )

    # Snapshot storage uses the same bucket as backups — there's a
    # single Storage block in settings. ``saas.product._get_storage_config``
    # reads the same ``saas_backup.*`` parameters. Any leftover
    # ``saas_snapshot.*`` rows from an earlier configuration are ignored
    # (they're cleaned up by the migration in 18.0.14.0.0/post-migrate.py).

    def set_values(self):
        res = super().set_values()
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param(
            'saas_master.show_services_section',
            'True' if self.saas_show_services_section else 'False',
        )
        ICP.set_param(
            'saas_master.show_hosting_section',
            'True' if self.saas_show_hosting_section else 'False',
        )
        # Always write the string — '0' (unlimited) included. See the
        # field definition for why config_parameter= can't be used.
        ICP.set_param(
            'saas_master.max_instances_per_user',
            str(int(self.saas_max_instances_per_user or 0)),
        )
        # Grace-period/storage-grace/env-price-factor manual handling and
        # the plan re-pricing trigger moved to
        # saas_billing/models/res_config_settings.py's own set_values
        # override (they're all billing-rate concerns).
        return res

    @api.model
    def get_values(self):
        res = super().get_values()
        ICP = self.env['ir.config_parameter'].sudo()
        res['saas_show_services_section'] = ICP.get_param(
            'saas_master.show_services_section', 'True',
        ) != 'False'
        res['saas_show_hosting_section'] = ICP.get_param(
            'saas_master.show_hosting_section', 'True',
        ) != 'False'
        try:
            res['saas_max_instances_per_user'] = int(
                ICP.get_param('saas_master.max_instances_per_user', '5') or 0
            )
        except (TypeError, ValueError):
            res['saas_max_instances_per_user'] = 5
        return res

    def action_apply_bucket_cors(self):
        """Configure the storage bucket's CORS so customers' browsers can
        upload restore files straight to it (presigned PUT). Persists the
        current settings first, then applies the policy via the bucket
        API using the configured credentials — no cloud-console trip."""
        self.ensure_one()
        # Make sure freshly-typed provider/keys/bucket are saved before
        # we open a client against them.
        self.set_values()
        origins = self.env['saas.instance.backup'].apply_bucket_cors()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': _("Bucket ready for uploads"),
                'message': _(
                    "Browser uploads (restore from file) are now allowed "
                    "from: %s"
                ) % ', '.join(origins),
                'sticky': False,
            },
        }
