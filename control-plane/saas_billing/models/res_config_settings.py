from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # ========== Custom Plan Pricing ==========
    saas_worker_price = fields.Float(
        string='Price per Worker',
        config_parameter='saas_master.worker_price',
        default=0.0,
        help='Monthly price per Odoo worker for custom plan configurations. '
             'Used in the custom plan builder on the pricing page.',
    )
    saas_storage_price_per_gb = fields.Float(
        string='Price per GB (Custom Plan)',
        config_parameter='saas_master.storage_price_per_gb',
        default=0.0,
        help='Monthly price per GB of storage for custom plan configurations. '
             'Used in the custom plan builder on the pricing page.',
    )
    saas_custom_plan_min_workers = fields.Integer(
        string='Min Workers (Custom)',
        config_parameter='saas_master.custom_plan_min_workers',
        default=2,
        help='Minimum number of workers selectable in the custom plan builder.',
    )
    saas_custom_plan_max_workers = fields.Integer(
        string='Max Workers (Custom)',
        config_parameter='saas_master.custom_plan_max_workers',
        default=8,
        help='Maximum number of workers selectable in the custom plan builder.',
    )
    saas_custom_plan_min_storage = fields.Integer(
        string='Min Storage GB (Custom)',
        config_parameter='saas_master.custom_plan_min_storage',
        default=5,
        help='Minimum storage (GB) selectable in the custom plan builder.',
    )
    saas_custom_plan_max_storage = fields.Integer(
        string='Max Storage GB (Custom)',
        config_parameter='saas_master.custom_plan_max_storage',
        default=200,
        help='Maximum storage (GB) selectable in the custom plan builder.',
    )

    # --- Resource allocation per worker ---
    saas_custom_plan_cpu_per_worker = fields.Float(
        string='CPU per Worker',
        config_parameter='saas_master.custom_plan_cpu_per_worker',
        default=0.5,
        help='vCPU allocated per worker in custom plans (e.g. 0.5 = half a core per worker).',
    )
    saas_custom_plan_ram_per_worker = fields.Integer(
        string='RAM per Worker (MB)',
        config_parameter='saas_master.custom_plan_ram_per_worker',
        default=512,
        help='RAM in MB allocated per worker in custom plans (e.g. 512 = 512MB per worker).',
    )
    saas_custom_plan_users_per_worker_min = fields.Integer(
        string='Min Users per Worker',
        config_parameter='saas_master.custom_plan_users_per_worker_min',
        default=6,
        help='Minimum concurrent users each worker can handle (light usage). '
             'Used in the recommendation display.',
    )
    saas_custom_plan_users_per_worker_max = fields.Integer(
        string='Max Users per Worker',
        config_parameter='saas_master.custom_plan_users_per_worker_max',
        default=10,
        help='Maximum concurrent users each worker can handle (heavy usage). '
             'Used in the recommendation display.',
    )
    saas_custom_plan_yearly_discount_pct = fields.Integer(
        string='Yearly Discount %',
        config_parameter='saas_master.custom_plan_yearly_discount_pct',
        default=20,
        help='Percentage discount applied when yearly billing is selected for custom plans.',
    )

    # ========== Hosting Plan Builder ==========
    saas_hosting_worker_price = fields.Float(
        string='Hosting: Price per Worker',
        config_parameter='saas_master.hosting_worker_price',
        default=10.0,
        help='Monthly price per worker for self-managed hosting plans.',
    )
    saas_hosting_storage_price_per_gb = fields.Float(
        string='Hosting: Price per GB',
        config_parameter='saas_master.hosting_storage_price_per_gb',
        default=0.3,
        help='Monthly price per GB of storage for self-managed hosting plans.',
    )
    saas_hosting_min_workers = fields.Integer(
        string='Hosting: Min Workers',
        config_parameter='saas_master.hosting_min_workers',
        default=2,
    )
    saas_hosting_max_workers = fields.Integer(
        string='Hosting: Max Workers',
        config_parameter='saas_master.hosting_max_workers',
        default=8,
    )
    saas_hosting_min_storage = fields.Integer(
        string='Hosting: Min Storage GB',
        config_parameter='saas_master.hosting_min_storage',
        default=5,
    )
    saas_hosting_max_storage = fields.Integer(
        string='Hosting: Max Storage GB',
        config_parameter='saas_master.hosting_max_storage',
        default=200,
    )
    saas_hosting_cpu_per_worker = fields.Float(
        string='Hosting: CPU per Worker',
        config_parameter='saas_master.hosting_cpu_per_worker',
        default=0.5,
    )
    saas_hosting_ram_per_worker = fields.Integer(
        string='Hosting: RAM per Worker (MB)',
        config_parameter='saas_master.hosting_ram_per_worker',
        default=512,
    )
    saas_hosting_yearly_discount_pct = fields.Integer(
        string='Hosting: Yearly Discount %',
        config_parameter='saas_master.hosting_yearly_discount_pct',
        default=20,
    )
    # Odoo.sh-style environments: Staging/Development servers are priced at
    # the lowest hosting spec × this factor. NO config_parameter= here —
    # 0.0 ("free non-prod servers") is a meaningful value and set_param(key,
    # 0.0→False) would delete the row, springing the default back (falsy-value
    # trap). Handled manually in get_values/set_values, like the settings below.
    saas_env_price_factor = fields.Float(
        string='Environment Price Factor',
        default=1.0,
        help='Multiplier on the per-server price of Staging/Development '
             'environments (lowest hosting spec). 1.0 = full price; 0.7 = 30%% '
             'cheaper than a same-size production server; 0 = free.',
    )
    saas_snapshot_price_per_gb = fields.Float(
        string='Snapshot Price per GB (monthly)',
        config_parameter='saas_master.snapshot_price_per_gb',
        default=0.40,
        help='Usage-based snapshot pricing: the storage actually consumed '
             'by the instance\'s snapshots is rounded UP to the next whole '
             'GB and charged at this monthly rate (1 GB minimum). '
             'Re-evaluated on every monthly renewal. Retention is fixed at '
             '7 days per database.',
    )
    # Hosting snapshot retention is fixed (HOSTING_MAX_SNAPSHOTS=7 in
    # saas.instance.backup); there is no per-plan hosting backup count, so
    # no hosting_min_backups / hosting_max_backups settings here.

    # ========== Extra Storage Pricing (DEPRECATED — A2) ==========
    # Per-GB overage was removed in favour of blocks-only billing. This
    # field is kept ONLY so existing config rows don't error on upgrade; it
    # is no longer read by the pricing engine. Configure the storage block
    # size + price below instead.
    saas_extra_storage_price_per_gb = fields.Float(
        string='Extra Storage Price per GB (deprecated)',
        config_parameter='saas_master.extra_storage_price_per_gb',
        default=0.0,
        help='DEPRECATED: per-GB overage was replaced by blocks-only '
             'billing (see Storage Expansion Block below). This value is '
             'ignored by the pricing engine and kept only for backward '
             'compatibility.',
    )

    # ========== Pricing Engine: cost floor & storage blocks ==========
    # The pricing engine (saas.pricing.engine) charges custom configs as
    # max(rate_formula, floor). Floor rates are cost-derived: they protect
    # margin and block "cheap workers + huge storage" abuse. Defaults are 0
    # => no floor, so behaviour is unchanged until you set them.
    saas_hosting_worker_floor = fields.Float(
        string='Hosting: Worker Cost Floor',
        config_parameter='saas_master.hosting_worker_floor',
        default=0.0,
        help='Minimum monthly cost per worker on hosting custom configs '
             '(cost-derived floor). The engine charges max(rate, floor). '
             '0 = no floor.',
    )
    saas_hosting_storage_floor = fields.Float(
        string='Hosting: Storage Cost Floor (per GB)',
        config_parameter='saas_master.hosting_storage_floor',
        default=0.0,
        help='Minimum monthly cost per GB on hosting custom configs. 0 = no floor.',
    )
    saas_worker_floor = fields.Float(
        string='Services: Worker Cost Floor',
        config_parameter='saas_master.worker_floor',
        default=0.0,
        help='Minimum monthly cost per worker on services custom configs. 0 = no floor.',
    )
    saas_storage_floor = fields.Float(
        string='Services: Storage Cost Floor (per GB)',
        config_parameter='saas_master.storage_floor',
        default=0.0,
        help='Minimum monthly cost per GB on services custom configs. 0 = no floor.',
    )
    saas_hosting_minimum_monthly = fields.Float(
        string='Hosting: Minimum Monthly Charge',
        config_parameter='saas_master.hosting_minimum_monthly',
        default=0.0,
        help='Floor on the FINAL monthly total for hosting plans. A tiny '
             'config still bills at least this much, so it covers fixed '
             'business costs (payment fees, support, monitoring, CAC) that '
             'don\'t scale down. The customer just sees this as the price '
             '— no surcharge. 0 = no minimum.',
    )
    saas_minimum_monthly = fields.Float(
        string='Services: Minimum Monthly Charge',
        config_parameter='saas_master.minimum_monthly',
        default=0.0,
        help='Floor on the final monthly total for services custom plans. '
             '0 = no minimum.',
    )
    saas_storage_block_gb = fields.Integer(
        string='Storage Expansion Block (GB)',
        config_parameter='saas_master.storage_block_gb',
        default=10,
        help='Size of one storage-expansion block (GB). Storage above the '
             'plan allowance is billed in whole blocks of this size — e.g. '
             'with a 10 GB block and a 20 GB plan, 21 GB → 1 block, '
             '31 GB → 2 blocks. This is the ONLY overage mechanism (A2); '
             'the service is never suspended for going over.',
    )
    saas_storage_block_price = fields.Float(
        string='Storage Expansion Block Price (monthly)',
        config_parameter='saas_master.storage_block_price',
        default=0.0,
        help='Monthly price for one storage-expansion block. Overage block '
             'COUNTS are always shown to the customer; they are only '
             'charged once this price is set (0 = blocks shown but not '
             'billed).',
    )

    # ========== Dunning / Grace ==========
    # Single platform-wide grace period (no longer per-plan). NO
    # config_parameter= here for the same falsy-value reason as
    # saas_env_price_factor above: 0 ("no grace — suspend the day after
    # the due date") is a meaningful value, and set_param(key, False)
    # would delete the row. Handled manually in get_values/set_values.
    saas_grace_period_days = fields.Integer(
        string='Grace Period (Days)',
        default=7,
        help='Days after an invoice due date before the instance is '
             'automatically suspended for non-payment. Applies to all '
             'plans. 0 = suspend the day after the due date.',
    )
    # v47 storage capacity grace + bonus-credit expiry. storage_grace_days
    # uses manual get/set (0 = pause as soon as full, a meaningful value —
    # falsy-trap, see grace_period_days). system_credit_expiry_months uses
    # config_parameter (0 is never valid; min 1 enforced in the wallet).
    saas_storage_grace_days = fields.Integer(
        string='Storage Capacity Grace (Days)',
        default=7,
        help='Days a workspace can stay at full capacity before it is '
             'paused (reversible). The customer is guided to upgrade or add '
             'storage throughout. 0 = pause as soon as capacity is reached.',
    )
    saas_system_credit_expiry_months = fields.Integer(
        string='Bonus Credit Expiry (Months)',
        config_parameter='saas_master.system_credit_expiry_months',
        default=12,
        help='How long system-issued BONUS wallet credit stays valid. The '
             'customer\'s own money (customer-funded credit) NEVER expires.',
    )

    # ========== Margin visibility (display-only, no formula change) ==========
    # (price - floor) / price for each of the four rates above, so an
    # operator can see "we charge $10/worker, floor is $4, that's 60%
    # margin" without doing the subtraction by hand. Purely informational:
    # reads the same fields the pricing engine already uses.
    margin_hosting_worker_pct = fields.Float(
        string='Hosting Worker Margin %', compute='_compute_margin_pcts')
    margin_hosting_storage_pct = fields.Float(
        string='Hosting Storage Margin %', compute='_compute_margin_pcts')
    margin_services_worker_pct = fields.Float(
        string='Services Worker Margin %', compute='_compute_margin_pcts')
    margin_services_storage_pct = fields.Float(
        string='Services Storage Margin %', compute='_compute_margin_pcts')

    @api.depends('saas_hosting_worker_price', 'saas_hosting_worker_floor',
                 'saas_hosting_storage_price_per_gb', 'saas_hosting_storage_floor',
                 'saas_worker_price', 'saas_worker_floor',
                 'saas_storage_price_per_gb', 'saas_storage_floor')
    def _compute_margin_pcts(self):
        def margin(price, floor):
            return 100.0 * (price - floor) / price if price else 0.0
        for rec in self:
            rec.margin_hosting_worker_pct = margin(
                rec.saas_hosting_worker_price, rec.saas_hosting_worker_floor)
            rec.margin_hosting_storage_pct = margin(
                rec.saas_hosting_storage_price_per_gb, rec.saas_hosting_storage_floor)
            rec.margin_services_worker_pct = margin(
                rec.saas_worker_price, rec.saas_worker_floor)
            rec.margin_services_storage_pct = margin(
                rec.saas_storage_price_per_gb, rec.saas_storage_floor)

    def set_values(self):
        res = super().set_values()
        ICP = self.env['ir.config_parameter'].sudo()
        # Always write the string — '0' (no grace) included. See the field
        # definition for why config_parameter= can't be used.
        ICP.set_param(
            'saas_master.grace_period_days',
            str(int(self.saas_grace_period_days or 0)),
        )
        ICP.set_param(
            'saas_master.storage_grace_days',
            str(int(self.saas_storage_grace_days or 0)),
        )
        # Always write the string — '0.0' (free env servers) included. See the
        # field definition for why config_parameter= can't be used.
        ICP.set_param(
            'saas_master.env_price_factor',
            str(float(self.saas_env_price_factor or 0.0)),
        )
        # Re-derive every named tier's stored price from the (possibly
        # just-changed) per-worker / per-GB rates, so editing the rates
        # here reprices the published packages immediately — without this
        # each plan had to be re-saved by hand to pick up new rates.
        # Custom/trial plans are untouched (_sync_auto_price skips them).
        self.env['saas.plan'].sudo().search([
            ('is_public_tier', '=', True),
            ('is_trial_plan', '=', False),
        ])._sync_auto_price()
        return res

    @api.model
    def get_values(self):
        res = super().get_values()
        ICP = self.env['ir.config_parameter'].sudo()
        try:
            res['saas_grace_period_days'] = int(
                ICP.get_param('saas_master.grace_period_days', '7') or 0
            )
        except (TypeError, ValueError):
            res['saas_grace_period_days'] = 7
        try:
            res['saas_storage_grace_days'] = int(
                ICP.get_param('saas_master.storage_grace_days', '7') or 0
            )
        except (TypeError, ValueError):
            res['saas_storage_grace_days'] = 7
        try:
            res['saas_env_price_factor'] = float(
                ICP.get_param('saas_master.env_price_factor', '1.0')
            )
        except (TypeError, ValueError):
            res['saas_env_price_factor'] = 1.0
        return res
