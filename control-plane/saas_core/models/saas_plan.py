from odoo import api, fields, models, _
from odoo.exceptions import UserError


class SaasPlan(models.Model):
    _name = 'saas.plan'
    _description = 'SaaS Plan'
    _order = 'sequence, name'

    sequence = fields.Integer(default=10)
    name = fields.Char(string='Plan Name', required=True)
    saas_product_ids = fields.Many2many(
        'saas.product',
        'saas_plan_product_rel',
        'plan_id',
        'product_id',
        string='Services',
        help='Services this plan is available for. '
             'Leave empty to make the plan available for all services.',
    )
    is_trial_plan = fields.Boolean(
        string='Trial Plan',
        default=False,
        help='If checked, this plan is available for free trials only '
             'and will not generate invoices.',
    )
    is_custom = fields.Boolean(
        string='Custom Plan',
        default=False,
        help='Auto-generated plans from the custom plan builder. '
             'These are hidden from the public pricing page.',
    )

    # ========== Public Tier (configurable named plans) ==========
    is_public_tier = fields.Boolean(
        string='Public Tier',
        default=False,
        help='Show this plan as a named tier card (e.g. Starter / Pro / '
             'Business) on the public pricing & configure pages. '
             'Custom (slider-built) plans leave this off.',
    )
    is_recommended = fields.Boolean(
        string='Recommended Tier',
        default=False,
        help='Highlight this tier as the recommended / default choice on '
             'the pricing cards.',
    )
    badge = fields.Char(
        string='Tier Badge',
        help='Optional short badge shown on the tier card '
             '(e.g. "Most popular", "Best value").',
    )

    # ========== Pricing ==========
    # price/yearly_price/manual_price/discount_amount/yearly_discount_pct
    # and the auto-price formula (_auto_price_vals/_sync_auto_price) live
    # in saas_billing/models/saas_plan.py now — every @api.constrains/
    # @api.onchange/@api.depends touching them would otherwise be
    # resolved against this model before saas_billing has contributed
    # them (see the billing/pricing architecture plan's "Critical
    # finding" / "Second finding").
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        default=lambda self: self.env.company.currency_id,
    )

    # ========== Resource Limits ==========
    cpu_limit = fields.Float(
        string='CPU Limit',
        default=1.0,
        help='CPU limit for the Docker container (e.g. 0.5 = half a core, 2.0 = two cores).',
    )
    ram_limit = fields.Char(
        string='RAM Limit',
        default='1g',
        help='RAM limit for the Docker container (e.g. 512m, 1g, 2g).',
    )
    workers = fields.Integer(
        string='Odoo Workers',
        default=2,
        help='Number of Odoo worker processes. '
             'Set to 0 for development/testing (threaded mode). '
             'Recommended: 1 per 2 CPU cores.',
    )
    storage_limit = fields.Float(
        string='Storage Limit (GB)',
        default=5.0,
        help='Included total storage (container disk + database) in GB. '
             'Usage above this is NOT suspended (A2) — it keeps running and '
             'is billed in storage blocks on the next renewal. The customer '
             'is notified at 80/90/100%% of the limit. Also used for '
             'downgrade eligibility (blocked if current usage >= 75%% of '
             'the target plan limit).',
    )
    instance_count = fields.Integer(
        string='Instances',
        compute='_compute_instance_count',
    )

    # Grace period before suspension is a single platform-wide Setting
    # (Settings → 'Grace Period (Days)'), no longer per-plan. See
    # saas.instance._grace_period_days().

    # ========== Worker-driven resource sizing ==========
    @api.model
    def _recommended_resources(self, kind, workers):
        """Recommended resource allocation for ``workers`` (the platform's
        equivalent of Odoo's deployment sizing guidance): CPU and RAM.
        Driven by the per-worker Settings so the operator tunes ONE place
        and every surface (admin plan form, custom builder, trial plan)
        agrees.

        RAM only collapses to the 'Ng' form when it's an exact number of
        GB — '%dg' % (1536 // 1024) would silently strip 512 MB from every
        odd worker count.
        """
        icp = self.env['ir.config_parameter'].sudo()
        prefix = 'hosting' if kind == 'hosting' else 'custom_plan'
        try:
            cpu_per = float(icp.get_param(
                'saas_master.%s_cpu_per_worker' % prefix, '0.5') or 0.5)
            ram_per = int(icp.get_param(
                'saas_master.%s_ram_per_worker' % prefix, '512') or 512)
        except (TypeError, ValueError):
            cpu_per, ram_per = 0.5, 512
        workers = max(int(workers or 0), 0)
        ram_mb = workers * ram_per
        if ram_mb and ram_mb % 1024 == 0:
            ram_limit = '%dg' % (ram_mb // 1024)
        else:
            ram_limit = '%dm' % ram_mb if ram_mb else ''
        return {
            'cpu_limit': max(1.0, workers * cpu_per) if workers else 0.0,
            'ram_limit': ram_limit,
        }

    # ========== The managed package ==========
    # Requests are this share of each limit (the rest is burst headroom);
    # floors keep tiny limits schedulable.
    _REQUEST_RATIO = 0.25
    _MIN_CPU_REQUEST_M = 100
    _MIN_MEM_REQUEST_MI = 128
    # Defaults when the plan has no CPU/RAM set (match the driver's).
    _DEFAULT_ODOO_CPU_M = 1000
    _DEFAULT_ODOO_MEM_MI = 2048
    # Quota headroom on top of the package: one rolling-update surge pod
    # (counted separately) plus one-off Jobs (init/update/restore/backup;
    # the restore Job's limits are the largest: 2 CPU / 2Gi).
    _JOB_HEADROOM_CPU_M = 2000
    _JOB_HEADROOM_MEM_MI = 2048
    _SHELL_CPU_LIMIT_M = 500
    _SHELL_MEM_LIMIT_MI = 256

    @api.model
    def _recommended_db_resources(self, workers):
        """PostgreSQL CPU (millicores) and RAM (MiB) limits for ``workers``:
        per worker like Odoo, half of Odoo's share by default
        (Settings > SaaS Manager > Database per worker), with floors."""
        icp = self.env['ir.config_parameter'].sudo()
        try:
            cpu_per = float(icp.get_param('saas_master.db_cpu_per_worker', '0.25') or 0.25)
            ram_per = int(icp.get_param('saas_master.db_ram_per_worker', '256') or 256)
            cpu_min = float(icp.get_param('saas_master.db_cpu_min', '0.25') or 0.25)
            ram_min = int(icp.get_param('saas_master.db_ram_min', '512') or 512)
        except (TypeError, ValueError):
            cpu_per, ram_per, cpu_min, ram_min = 0.25, 256, 0.25, 512
        workers = max(int(workers or 0), 0)
        return (int(round(max(cpu_min, workers * cpu_per) * 1000)),
                max(ram_min, workers * ram_per))

    def _request_of(self, limit, floor):
        return max(floor, int(limit * self._REQUEST_RATIO))

    def _package(self, storage_gb=0.0):
        """Everything one instance on this plan reserves on the cluster:
        One Odoo pod with separate web and smaller cron containers, the
        PostgreSQL pod, the volumes and the namespace quota. The single
        source for deploy, resize, capacity, cost and customer metrics.

        CPU in millicores, memory in MiB, storage in GiB."""
        self.ensure_one()
        odoo_cpu = int(round((self.cpu_limit or 0.0) * 1000)) or self._DEFAULT_ODOO_CPU_M
        odoo_mem = int(self.env['saas.instance']._parse_ram_string(self.ram_limit)
                       // (1024 ** 2)) or self._DEFAULT_ODOO_MEM_MI
        db_cpu, db_mem = self._recommended_db_resources(self.workers)
        # Cron has independent limits in a sidecar, not a second pod.
        cron_cpu = max(1, min(odoo_cpu // 4, 500))
        cron_mem = max(1, min(odoo_mem // 4, 512))
        replicas = 1
        odoo_pods = 1
        storage = max(1, int(round(storage_gb or self.storage_limit or 0)) or 1)
        pkg = {
            'workers': self.workers or 0,
            'replicas': replicas,
            'odoo_pods': odoo_pods,
            'odoo_cpu_m': odoo_cpu, 'odoo_cpu_req_m': self._request_of(odoo_cpu, self._MIN_CPU_REQUEST_M),
            'odoo_mem_mi': odoo_mem, 'odoo_mem_req_mi': self._request_of(odoo_mem, self._MIN_MEM_REQUEST_MI),
            'db_cpu_m': db_cpu, 'db_cpu_req_m': self._request_of(db_cpu, self._MIN_CPU_REQUEST_M),
            'db_mem_mi': db_mem, 'db_mem_req_mi': self._request_of(db_mem, self._MIN_MEM_REQUEST_MI),
            'cron_cpu_m': cron_cpu, 'cron_cpu_req_m': min(cron_cpu, self._request_of(cron_cpu, 10)),
            'cron_mem_mi': cron_mem, 'cron_mem_req_mi': min(cron_mem, self._request_of(cron_mem, 32)),
            'storage_gb': storage,
        }
        # What the customer pays for (and sees as 100%): every Odoo pod
        # plus the database.
        pkg['total_cpu_m'] = odoo_cpu * odoo_pods + cron_cpu + db_cpu
        pkg['total_mem_mi'] = odoo_mem * odoo_pods + cron_mem + db_mem
        pkg['total_cpu_req_m'] = pkg['odoo_cpu_req_m'] * odoo_pods + pkg['cron_cpu_req_m'] + pkg['db_cpu_req_m']
        pkg['total_mem_req_mi'] = pkg['odoo_mem_req_mi'] * odoo_pods + pkg['cron_mem_req_mi'] + pkg['db_mem_req_mi']
        # Namespace ceiling: the package, one surge Odoo pod, the shell
        # sidecars and Job headroom. Nothing in the tenant can exceed it.
        web_pods = replicas + 1
        pkg['quota'] = {
            'limits.cpu': '%dm' % (pkg['total_cpu_m'] + odoo_cpu + cron_cpu
                                   + self._SHELL_CPU_LIMIT_M * web_pods + self._JOB_HEADROOM_CPU_M),
            'limits.memory': '%dMi' % (pkg['total_mem_mi'] + odoo_mem + cron_mem
                                       + self._SHELL_MEM_LIMIT_MI * web_pods + self._JOB_HEADROOM_MEM_MI),
            'requests.cpu': '%dm' % (pkg['total_cpu_req_m'] + pkg['odoo_cpu_req_m'] + pkg['cron_cpu_req_m']
                                     + 10 * web_pods + 250),
            'requests.memory': '%dMi' % (pkg['total_mem_req_mi'] + pkg['odoo_mem_req_mi'] + pkg['cron_mem_req_mi']
                                         + 32 * web_pods + 512),
            # New Managed tenants share one PVC for the whole allowance.
            'requests.storage': '%dGi' % (storage + 1),
            'pods': '20',
        }
        return pkg

    @api.onchange('workers', 'saas_product_ids')
    def _onchange_workers_resources(self):
        """Auto-fill CPU / RAM from the worker count so every plan delivers
        the same per-worker experience — like Odoo's own sizing
        recommendation. Pre-fill only: the operator can still override any
        value after typing the worker count."""
        for rec in self:
            if rec.is_trial_plan or not rec.workers:
                continue
            vals = rec._recommended_resources(rec._kind(), rec.workers)
            rec.cpu_limit = vals['cpu_limit']
            rec.ram_limit = vals['ram_limit']

    @api.onchange('is_trial_plan')
    def _onchange_is_trial_plan(self):
        """Reset paid-plan settings when toggling Trial Plan in the form."""
        for rec in self:
            if rec.is_trial_plan:
                rec.price = 0.0
                rec.yearly_price = 0.0

    # ========== Automatic resource-based pricing (named tiers) ==========
    def _kind(self):
        """'hosting' or 'services' — selects the rate set for this plan."""
        self.ensure_one()
        return 'hosting' if any(
            p.is_hosting for p in self.saas_product_ids
        ) else 'services'

    # _check_trial_plan_zero, _check_price_floor, _auto_price_vals,
    # _onchange_auto_price, _onchange_manual_price, _sync_auto_price,
    # _check_yearly_not_above_monthly_annual, _compute_yearly_discount_pct,
    # create(), write(), and get_price_for_period all live in
    # saas_billing/models/saas_plan.py now — every one of them is a
    # commercial/pricing concern, and several have @api.constrains/
    # @api.onchange/@api.depends decorators that reference price/
    # yearly_price/manual_price/discount_amount, which can't be resolved
    # here at saas_core's own load time (see the plan doc's "Critical
    # finding" / "Second finding").

    def _compute_instance_count(self):
        data = self.env['saas.instance']._read_group(
            [('plan_id', 'in', self.ids)],
            ['plan_id'],
            ['__count'],
        )
        counts = {plan.id: count for plan, count in data}
        for rec in self:
            rec.instance_count = counts.get(rec.id, 0)

    def unlink(self):
        # Block deletion if any active instances use this plan
        active_states = (
            'draft', 'pending_payment', 'paid', 'pending_provision',
            'provisioning', 'running', 'stopped', 'suspended',
        )
        for rec in self:
            count = self.env['saas.instance'].search_count([
                ('plan_id', '=', rec.id),
                ('state', 'in', active_states),
            ])
            if count:
                raise UserError(
                    _("Cannot delete plan '%s': %d active instance(s) are "
                      "still using it. Cancel or reassign them first.")
                    % (rec.name, count)
                )
        return super().unlink()

