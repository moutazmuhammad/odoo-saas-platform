from datetime import timedelta

from odoo import http, fields, _
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from odoo.addons.saas_core.models.saas_instance import SUBDOMAIN_RE

# States that BLOCK a new subdomain claim. Cancelled instances are
# included on purpose: a cancelled instance still has its retained
# snapshot in cloud storage waiting for reactivation, and we don't
# want another customer (or even the same one creating a fresh
# instance instead of reactivating) to steal the subdomain it was
# previously bound to. The original owner can either reactivate the
# existing record or pick a brand-new subdomain.
_ACTIVE_STATES = (
    'draft', 'pending_payment', 'paid', 'pending_provision',
    'provisioning', 'running', 'stopped', 'suspended',
    'cancelled', 'cancelled_by_client',
)

# States that count toward a customer's max-instances QUOTA. Cancelled
# instances are excluded: they consume no infrastructure and — as the quota
# error message itself promises ("delete or cancel an existing one first") —
# cancelling must actually free a slot. (Cancelled instances stay in
# _ACTIVE_STATES for subdomain reservation / reactivation, which is a
# separate concern from the resource quota.)
_QUOTA_STATES = tuple(
    s for s in _ACTIVE_STATES if s not in ('cancelled', 'cancelled_by_client')
)


class SaasWebsite(http.Controller):

    # ------------------------------------------------------------------
    #  Helpers
    # ------------------------------------------------------------------

    def _section_enabled(self, name):
        """Return True if the ``Services`` / ``Hosting`` section is enabled
        in settings. Default-on so a fresh install behaves like before.

        ``name`` is ``'services'`` or ``'hosting'``.
        """
        key = 'saas_master.show_%s_section' % name
        return request.env['ir.config_parameter'].sudo().get_param(
            key, 'True',
        ) != 'False'

    def _get_trial_info(self, hosting=False):
        """Return (trial_available, trial_days) for the current user.

        :param hosting: if True, check hosting trial; otherwise service trial.

        A trial is unavailable when (a) trial_days is 0, (b) the partner has
        already used their trial of this type, or (c) the partner already
        owns a paid (non-trial) instance of this type — once the customer
        has paid for a server, the free trial no longer applies.
        """
        trial_days = int(request.env['ir.config_parameter'].sudo().get_param(
            'saas_master.trial_days', '14',
        ))
        if trial_days <= 0:
            return False, trial_days
        if request.env.user._is_public():
            return True, trial_days
        partner = request.env.user.partner_id.sudo()
        if partner._saas_trial_used(hosting=hosting):
            return False, trial_days
        if partner._saas_has_paid_instance(hosting=hosting):
            return False, trial_days
        return True, trial_days

    # ==================================================================
    #  1. Services Catalog  –  /services
    # ==================================================================

    # /services and /services/<id> are served by the VELTNEX SPA — see
    # controllers/spa.py. (Ordering still POSTs to /services/order below.)

    # ==================================================================
    #  3. Configure Instance  –  /services/<id>/plans/<plan_id>/configure
    # ==================================================================

    @http.route('/services/<int:product_id>/plans/<int:plan_id>/configure',
                type='http', auth='public', website=True)
    def service_configure(self, product_id, plan_id, error=None, **kw):
        if not self._section_enabled('services'):
            return request.redirect('/')
        # Public users must register/login first
        if request.env.user._is_public():
            is_trial = kw.get('trial') == '1'
            params = 'product_id=%d&plan_id=%d' % (product_id, plan_id)
            if is_trial:
                params += '&is_trial=1'
            return request.redirect('/services/register?%s' % params)

        product = request.env['saas.product'].sudo().browse(product_id)
        plan = request.env['saas.plan'].sudo().browse(plan_id)
        if (not product.exists() or not product.is_published
                or not plan.exists()
                or product not in plan.saas_product_ids):
            return request.redirect('/services')

        is_trial = kw.get('trial') == '1'
        billing_period = kw.get('billing', 'monthly')
        if billing_period not in ('monthly', 'yearly'):
            billing_period = 'monthly'
        domains = request.env['saas.based.domain'].sudo().search([])

        return request.render('saas_website.service_configure_form', {
            'product': product,
            'plan': plan,
            'domains': domains,
            'error': error,
            'is_trial': is_trial,
            'billing_period': billing_period,
            'form_values': kw,
        })

    # ==================================================================
    #  4. Process Order  –  /services/order
    # ==================================================================

    @http.route('/services/order', type='http', auth='user', website=True,
                methods=['POST'], csrf=True)
    def service_order(self, **post):
        if not self._section_enabled('services'):
            return request.redirect('/')
        product_id = int(post.pop('product_id', 0))
        plan_id = int(post.pop('plan_id', 0))
        subdomain = (post.get('subdomain') or '').strip().lower()
        domain_id = int(post.get('domain_id', 0))
        billing_period = post.get('billing_period', 'monthly')
        if billing_period not in ('monthly', 'yearly'):
            billing_period = 'monthly'

        product = request.env['saas.product'].sudo().browse(product_id)
        plan = request.env['saas.plan'].sudo().browse(plan_id)
        if (not product.exists() or not product.is_published
                or not plan.exists()
                or product not in plan.saas_product_ids):
            return request.redirect('/services')

        # --- Trial / paid plan validation ---
        is_trial = post.get('is_trial') == '1'
        if is_trial and not plan.is_trial_plan:
            return self.service_configure(
                product_id, plan_id,
                error=_("This plan is not available for free trial."),
                **post,
            )
        if not is_trial and plan.is_trial_plan:
            return self.service_configure(
                product_id, plan_id,
                error=_("This plan is only available as a free trial."),
                **post,
            )

        # --- Subdomain validation ---
        if not subdomain or not SUBDOMAIN_RE.match(subdomain):
            return self.service_configure(
                product_id, plan_id,
                error=_("Invalid subdomain. Use only lowercase letters, digits, "
                        "and hyphens (max 63 chars, must start/end with alphanumeric)."),
                **post,
            )

        # Check uniqueness
        existing = request.env['saas.instance'].sudo().search([
            ('subdomain', '=', subdomain),
            ('domain_id', '=', domain_id),
            ('state', 'in', _ACTIVE_STATES),
        ], limit=1)
        if existing:
            return self.service_configure(
                product_id, plan_id,
                error=_("The subdomain '%s' is already taken. "
                        "Please choose another.") % subdomain,
                **post,
            )

        # --- Validate references ---
        domain = request.env['saas.based.domain'].sudo().browse(domain_id)
        version = product.odoo_version_id
        if not domain.exists() or not version:
            support_email = request.env['ir.config_parameter'].sudo().get_param(
                'saas_master.support_email', ''
            )
            support_msg = (
                _(" Please contact us at %s.") % support_email
                if support_email else _(" Please contact support.")
            )
            return self.service_configure(
                product_id, plan_id,
                error=_("Please select a valid domain.") if not domain.exists()
                       else _("No Odoo version configured for this service.") + support_msg,
                **post,
            )

        partner = request.env.user.partner_id

        # --- Rate limiting ---
        max_instances = int(request.env['ir.config_parameter'].sudo().get_param(
            'saas_master.max_instances_per_user', '5',
        ))
        if max_instances > 0:
            active_count = request.env['saas.instance'].sudo().search_count([
                ('partner_id', '=', partner.id),
                ('state', 'in', _QUOTA_STATES),
            ])
            if active_count >= max_instances:
                return self.service_configure(
                    product_id, plan_id,
                    error=_("You have reached the maximum number of instances (%d). "
                            "Please delete or cancel an existing one first.") % max_instances,
                    **post,
                )

        # --- Trial: one per client, and not after a paid instance ---
        if is_trial:
            partner_sudo = partner.sudo()
            if partner_sudo._saas_trial_used(hosting=False):
                return self.service_configure(
                    product_id, plan_id,
                    error=_("You have already used your free trial."),
                    **post,
                )
            if partner_sudo._saas_has_paid_instance(hosting=False):
                return self.service_configure(
                    product_id, plan_id,
                    error=_("You already have a paid service instance — "
                            "the free trial is no longer available."),
                    **post,
                )

        # --- Validate infrastructure ---
        docker_servers = request.env['saas.server'].sudo().search(
            [('is_docker_host', '=', True)], limit=1,
        )
        db_servers = request.env['saas.server'].sudo().search(
            [('is_db_server', '=', True)], limit=1,
        )
        if not docker_servers or not db_servers:
            support_email = request.env['ir.config_parameter'].sudo().get_param(
                'saas_master.support_email', ''
            )
            support_msg = (
                _("Please contact us at %s.") % support_email
                if support_email else _("Please contact support.")
            )
            return self.service_configure(
                product_id, plan_id,
                error=_("Service is temporarily unavailable. "
                        "No infrastructure servers are configured. ") + support_msg,
                **post,
            )

        # --- Create instance ---
        vals = {
            'subdomain': subdomain,
            'domain_id': domain.id,
            'partner_id': partner.id,
            'saas_product_id': product.id,
            'plan_id': plan.id,
            'odoo_version_id': version.id,
            'billing_period': billing_period,
        }
        if is_trial:
            vals['is_trial'] = True

        try:
            # Atomic (see hosting_order): a deferred constraint that fires on
            # flush during action_deploy() — e.g. one-trial-per-client — must
            # roll the instance INSERT back entirely, else a rejected trial
            # leaks an orphan draft instance (the caught error still lets the
            # request commit). cr.savepoint() undoes the INSERT on any failure.
            with request.env.cr.savepoint():
                instance = request.env['saas.instance'].sudo().create(vals)

                # Server allocation is handled by _allocate_servers() inside
                # action_deploy(), which enforces capacity limits
                # (max_instances, max_cpu_cores, max_ram_gb) and falls back
                # to overcommit servers only when explicitly allowed from
                # the backend.  Do NOT pre-assign servers here.

                if is_trial:
                    # Trial: skip billing, deploy immediately.
                    # The partner's trial flag (saas_trial_used) is set inside
                    # _do_deploy() only after the deployment actually succeeds,
                    # so that a failed deploy does not permanently lock the
                    # customer out of their trial.
                    instance.action_deploy()
                else:
                    # Paid: billing + auto-deploy flow
                    instance.action_confirm_and_bill()
        except (UserError, ValidationError) as e:
            # savepoint already rolled the INSERT back — no orphan to clean up.
            return self.service_configure(
                product_id, plan_id,
                error=str(e),
                **post,
            )

        if is_trial:
            return request.redirect('/my/instances/%s?access_token=%s' % (
                instance.id, instance.access_token,
            ))
        # Paid: redirect to checkout for immediate payment
        return request.redirect('/my/instances/%s/checkout' % instance.id)

    # ==================================================================
    #  5. Custom Plan Builder  –  pricing config & calculation
    # ==================================================================

    def _get_custom_plan_config(self):
        """Return custom plan builder configuration from system parameters.

        Per-unit rates are intentionally NOT included: pricing is computed
        server-side by saas.pricing.engine and only totals are exposed.
        This dict carries limits / discount / currency only."""
        ICP = request.env['ir.config_parameter'].sudo()
        return {
            'min_workers': int(ICP.get_param('saas_master.custom_plan_min_workers', '2')),
            'max_workers': int(ICP.get_param('saas_master.custom_plan_max_workers', '8')),
            'min_storage': int(ICP.get_param('saas_master.custom_plan_min_storage', '5')),
            'max_storage': int(ICP.get_param('saas_master.custom_plan_max_storage', '200')),
            'cpu_per_worker': float(ICP.get_param('saas_master.custom_plan_cpu_per_worker', '0.5')),
            'ram_per_worker': int(ICP.get_param('saas_master.custom_plan_ram_per_worker', '512')),
            'users_per_worker_min': int(ICP.get_param('saas_master.custom_plan_users_per_worker_min', '6')),
            'users_per_worker_max': int(ICP.get_param('saas_master.custom_plan_users_per_worker_max', '10')),
            'yearly_discount_pct': int(ICP.get_param('saas_master.custom_plan_yearly_discount_pct', '20')),
            'currency': request.env.company.currency_id.name or 'USD',
        }

    @http.route('/saas/custom-plan/calculate', type='json', auth='public', website=True)
    def custom_plan_calculate(self, workers=2, storage=5, region=None):
        """Real-time price calculation for the custom plan builder.

        ``region`` (id, optional) scales the price by that region's
        multiplier so a change-plan preview matches the region-aware plan
        that will actually be created/billed."""
        config = self._get_custom_plan_config()
        workers = max(config['min_workers'], min(int(workers), config['max_workers']))
        storage = max(config['min_storage'], min(int(storage), config['max_storage']))

        region_rec = self._resolve_region_id_strict(region)
        _q = request.env['saas.pricing.engine'].compute(
            'services', workers, storage, region=region_rec.id or None)

        min_users = workers * config['users_per_worker_min']
        max_users = workers * config['users_per_worker_max']

        return {
            'workers': workers,
            'storage': storage,
            'monthly_total': _q['monthly'],
            # Engine yearly: region-scaled AND discounted (infra only). The
            # client no longer derives yearly from monthly.
            'yearly_total': _q['yearly'],
            'min_users': min_users,
            'max_users': max_users,
            'currency': config['currency'],
        }

    @http.route('/services/<int:product_id>/custom/configure',
                type='http', auth='public', website=True)
    def service_custom_configure(self, product_id, workers=4, storage=20,
                                 billing='monthly', error=None, **kw):
        """Configure instance page for a custom plan."""
        if not self._section_enabled('services'):
            return request.redirect('/')
        if request.env.user._is_public():
            params = 'product_id=%d&custom=1&workers=%s&storage=%s&billing=%s' % (
                product_id, workers, storage, billing,
            )
            return request.redirect('/services/register?%s' % params)

        product = request.env['saas.product'].sudo().browse(product_id)
        if not product.exists() or not product.is_published:
            return request.redirect('/services')

        billing_period = billing if billing in ('monthly', 'yearly') else 'monthly'

        config = self._get_custom_plan_config()
        workers = max(config['min_workers'], min(int(workers), config['max_workers']))
        storage = max(config['min_storage'], min(int(storage), config['max_storage']))

        _q = request.env['saas.pricing.engine'].compute('services', workers, storage, billing_period)
        monthly_total = _q['monthly']

        discount = config['yearly_discount_pct'] / 100.0
        if billing_period == 'yearly':
            yearly_total = monthly_total * 12 * (1 - discount)
            display_total = yearly_total
            yearly_savings = (monthly_total * 12) - yearly_total
        else:
            yearly_total = monthly_total * 12
            display_total = monthly_total
            yearly_savings = 0

        domains = request.env['saas.based.domain'].sudo().search([])

        return request.render('saas_website.service_custom_configure_form', {
            'product': product,
            'domains': domains,
            'error': error,
            'workers': workers,
            'storage': storage,
            'monthly_total': monthly_total,
            'yearly_total': yearly_total,
            'display_total': display_total,
            'yearly_savings': yearly_savings,
            'billing_period': billing_period,
            'config': config,
            'form_values': kw,
        })

    @http.route('/services/custom-order', type='http', auth='user', website=True,
                methods=['POST'], csrf=True)
    def service_custom_order(self, **post):
        """Process a custom plan order."""
        if not self._section_enabled('services'):
            return request.redirect('/')
        product_id = int(post.pop('product_id', 0))
        workers = int(post.get('workers', 4))
        storage = int(post.get('storage', 20))
        subdomain = (post.get('subdomain') or '').strip().lower()
        domain_id = int(post.get('domain_id', 0))
        billing_period = post.get('billing_period', 'monthly')
        if billing_period not in ('monthly', 'yearly'):
            billing_period = 'monthly'

        product = request.env['saas.product'].sudo().browse(product_id)
        if not product.exists() or not product.is_published:
            return request.redirect('/services')

        config = self._get_custom_plan_config()
        workers = max(config['min_workers'], min(workers, config['max_workers']))
        storage = max(config['min_storage'], min(storage, config['max_storage']))

        # Subdomain validation
        if not subdomain or not SUBDOMAIN_RE.match(subdomain):
            return self.service_custom_configure(
                product_id, workers=workers, storage=storage,
                error=_("Invalid subdomain. Use only lowercase letters, digits, "
                        "and hyphens (max 63 chars, must start/end with alphanumeric)."),
                **post,
            )

        existing = request.env['saas.instance'].sudo().search([
            ('subdomain', '=', subdomain),
            ('domain_id', '=', domain_id),
            ('state', 'in', _ACTIVE_STATES),
        ], limit=1)
        if existing:
            return self.service_custom_configure(
                product_id, workers=workers, storage=storage,
                error=_("The subdomain '%s' is already taken.") % subdomain,
                **post,
            )

        domain = request.env['saas.based.domain'].sudo().browse(domain_id)
        version = product.odoo_version_id
        if not domain.exists() or not version:
            return self.service_custom_configure(
                product_id, workers=workers, storage=storage,
                error=_("Please select a valid domain.") if not domain.exists()
                       else _("No Odoo version configured for this service."),
                **post,
            )

        partner = request.env.user.partner_id

        # Rate limiting
        max_instances = int(request.env['ir.config_parameter'].sudo().get_param(
            'saas_master.max_instances_per_user', '5',
        ))
        if max_instances > 0:
            active_count = request.env['saas.instance'].sudo().search_count([
                ('partner_id', '=', partner.id),
                ('state', 'in', _QUOTA_STATES),
            ])
            if active_count >= max_instances:
                return self.service_custom_configure(
                    product_id, workers=workers, storage=storage,
                    error=_("You have reached the maximum number of instances (%d).") % max_instances,
                    **post,
                )

        # Validate infrastructure
        docker_servers = request.env['saas.server'].sudo().search(
            [('is_docker_host', '=', True)], limit=1,
        )
        db_servers = request.env['saas.server'].sudo().search(
            [('is_db_server', '=', True)], limit=1,
        )
        if not docker_servers or not db_servers:
            return self.service_custom_configure(
                product_id, workers=workers, storage=storage,
                error=_("Service is temporarily unavailable."),
                **post,
            )

        # Find or create a matching plan for this custom configuration
        # We look for a plan that matches exactly, or create a dynamic one
        plan = self._get_or_create_custom_plan(product, workers, storage, config)

        vals = {
            'subdomain': subdomain,
            'domain_id': domain.id,
            'partner_id': partner.id,
            'saas_product_id': product.id,
            'plan_id': plan.id,
            'odoo_version_id': version.id,
            'billing_period': billing_period,
        }
        try:
            # Atomic (see hosting_order): a deferred constraint firing on flush
            # during action_confirm_and_bill() must roll the INSERT back, else
            # a rejected order leaks an orphan draft instance.
            with request.env.cr.savepoint():
                instance = request.env['saas.instance'].sudo().create(vals)
                instance.action_confirm_and_bill()
        except (UserError, ValidationError) as e:
            return self.service_custom_configure(
                product_id, workers=workers, storage=storage,
                error=str(e),
                **post,
            )

        return request.redirect('/my/instances/%s/checkout' % instance.id)

    def _get_or_create_custom_plan(self, product, workers, storage, config, region=None):
        """Find or create a plan matching the custom configuration.

        ``region`` (optional) applies the region price multiplier; None =>
        x1.0 (default region / behaviour-neutral)."""
        Plan = request.env['saas.plan'].sudo()
        plan_name = 'Custom (%dW / %dGB)' % (workers, storage)

        # Single source of truth: saas.pricing.engine. Behaviour-neutral.
        _q = request.env['saas.pricing.engine'].compute(
            'services', workers, storage, region=region)
        monthly_price = _q['monthly']

        # Search for existing matching custom plan
        plan = Plan.search([
            ('is_custom', '=', True),
            ('workers', '=', workers),
            ('storage_limit', '=', float(storage)),
            ('price', '=', monthly_price),
            ('saas_product_ids', 'in', product.id),
        ], limit=1)

        if not plan:
            # One sizing formula everywhere (admin form onchange, custom
            # builder, trial) — see saas.plan._recommended_resources.
            res = Plan._recommended_resources('services', workers)
            yearly_price = _q['yearly']

            # Backup retention is FIXED platform-wide (no per-plan setting):
            # every Services instance keeps the last DEFAULT_MAX_BACKUPS
            # copies, mirroring hosting's fixed HOSTING_MAX_SNAPSHOTS.
            plan = Plan.create({
                'name': plan_name,
                'is_custom': True,
                'price': monthly_price,
                'yearly_price': yearly_price,
                'workers': workers,
                'storage_limit': float(storage),
                'cpu_limit': res['cpu_limit'],
                'ram_limit': res['ram_limit'],
                'saas_product_ids': [(4, product.id)],
                'sequence': 999,
            })

        return plan

    # ==================================================================
    #  Legacy: keep /plans as redirect to /services
    # ==================================================================

    @http.route('/plans', type='http', auth='public', website=True, sitemap=False)
    def plans_page_redirect(self, **kw):
        if not self._section_enabled('services'):
            return request.redirect('/')
        return request.redirect('/services', code=301)

    # ==================================================================
    #  Customer Documentation  –  /docs
    # ==================================================================
    # /docs is served by the VELTNEX SPA — see controllers/spa.py.

    # ==================================================================
    #  Cleanup: erase every Arabic-related database artifact
    # ==================================================================
    # The previous (now-removed) bilingual feature wrote a few things
    # into the database that module-upgrade alone can't undo:
    #   * Arabic linked to ``website.language_ids``.
    #   * ``ar_001`` JSONB entries on every saas_website view's
    #     ``arch_db``.
    #   * Website-builder copy-on-write copies of our views that
    #     captured the language switcher / Arabic text.
    # This endpoint reverses all three on demand. Hit it once after
    # upgrading and the deployment is back to English-only with no
    # leftover state.
    @http.route(
        '/saas/cleanup-arabic',
        type='http', auth='user', website=True,
        sitemap=False, multilang=False,
    )
    def saas_cleanup_arabic(self, **kwargs):
        if not request.env.user.has_group('base.group_user'):
            return request.make_response(
                'Forbidden — must be an internal user.',
                headers=[('Content-Type', 'text/plain; charset=utf-8')],
                status=403,
            )
        out = ['=== SAAS CLEANUP ARABIC ===']

        # --- 1. Unlink Arabic from every website ---
        Lang = request.env['res.lang'].sudo().with_context(active_test=False)
        ar = Lang.search([('code', '=', 'ar_001')], limit=1)
        if ar:
            websites = request.env['website'].sudo().search([])
            for w in websites:
                before = w.language_ids.mapped('code')
                if 'ar_001' in before:
                    try:
                        w.write({'language_ids': [(3, ar.id)]})
                        out.append("Unlinked Arabic from website %s." % w.name)
                    except Exception as e:
                        out.append("Could not unlink Arabic from %s: %s" % (w.name, e))
                else:
                    out.append("Website %s already free of Arabic." % w.name)

        # --- 2. Delete website-builder copy-on-write views that
        # captured the old layout. Any view with a non-False
        # ``website_id`` whose ``key`` starts with ``saas_website.``
        # is a frontend-builder cow'd copy of one of ours — safe to
        # remove because the module's master view is still there. -->
        View = request.env['ir.ui.view'].sudo()
        cow_views = View.search([
            ('key', '=like', 'saas_website.%'),
            ('website_id', '!=', False),
        ])
        if cow_views:
            out.append(
                "Found %d website-specific copy-on-write view(s) — deleting:"
                % len(cow_views)
            )
            for v in cow_views:
                out.append("  - %s (id=%s, website_id=%s)" % (v.key, v.id, v.website_id.id))
            try:
                cow_views.unlink()
                out.append("Deleted.")
            except Exception as e:
                out.append("Could not delete cow'd views: %s" % e)
        else:
            out.append("No website-specific cow'd copies of saas_website views.")

        # --- 3. Strip ``ar_001`` from every saas_website view's
        # JSONB arch_db. ---
        IrModelData = request.env['ir.model.data'].sudo()
        view_ids = IrModelData.search([
            ('module', '=', 'saas_website'),
            ('model', '=', 'ir.ui.view'),
        ]).mapped('res_id')
        if view_ids:
            request.env.cr.execute(
                """
                UPDATE ir_ui_view
                   SET arch_db = arch_db - 'ar_001'
                 WHERE id = ANY(%s)
                   AND arch_db ? 'ar_001'
                """,
                (view_ids,),
            )
            out.append(
                "Stripped ar_001 arch_db on %d view(s)."
                % request.env.cr.rowcount
            )

        # --- 4. Deactivate Arabic (best-effort) ---
        if ar and ar.active:
            try:
                ar.write({'active': False})
                out.append("Deactivated ar_001.")
            except Exception as e:
                out.append(
                    "Could not deactivate ar_001 (still referenced "
                    "somewhere): %s" % e
                )

        # --- 5. Bust caches + commit ---
        request.env.cr.commit()
        try:
            request.env.registry.clear_cache()
            out.append("Registry cache cleared.")
        except Exception as e:
            out.append("Could not clear cache: %s" % e)

        out.append("")
        out.append("Done. Hard-refresh your browser (Ctrl+Shift+R) to confirm.")
        return request.make_response(
            '\n'.join(out),
            headers=[('Content-Type', 'text/plain; charset=utf-8')],
        )

    # ==================================================================
    #  6. Hosting  –  /hosting
    # ==================================================================

    def _get_hosting_plan_config(self):
        """Return hosting plan builder configuration from system parameters.

        Per-unit rates are intentionally NOT included: pricing is computed
        server-side by saas.pricing.engine and only totals are exposed.
        This dict carries limits / discount / currency only."""
        ICP = request.env['ir.config_parameter'].sudo()
        return {
            'min_workers': int(ICP.get_param('saas_master.hosting_min_workers', '2')),
            'max_workers': int(ICP.get_param('saas_master.hosting_max_workers', '8')),
            'min_storage': int(ICP.get_param('saas_master.hosting_min_storage', '5')),
            'max_storage': int(ICP.get_param('saas_master.hosting_max_storage', '200')),
            'cpu_per_worker': float(ICP.get_param('saas_master.hosting_cpu_per_worker', '0.5')),
            'ram_per_worker': int(ICP.get_param('saas_master.hosting_ram_per_worker', '512')),
            # Sizing guidance shown next to the workers slider: recommended
            # users = workers × [min..max] (light → heavy usage), tuned in
            # Settings → "Users / worker: light → heavy".
            'users_per_worker_min': int(ICP.get_param(
                'saas_master.custom_plan_users_per_worker_min', '6')),
            'users_per_worker_max': int(ICP.get_param(
                'saas_master.custom_plan_users_per_worker_max', '10')),
            'yearly_discount_pct': int(ICP.get_param('saas_master.hosting_yearly_discount_pct', '20')),
            # Engine quote so this matches what the invoice will charge.
            # Usage-based pricing with nothing measured yet = the 1 GB
            # minimum, i.e. the "from" price.
            'daily_backup_price': request.env['saas.pricing.engine'].daily_backup_price(),
            'snapshot_price_per_gb': request.env['saas.pricing.engine'].snapshot_price_per_gb(),
            'currency': request.env.company.currency_id.name or 'USD',
        }

    def _get_or_create_hosting_product(self):
        """Get or auto-create the single hosting product record."""
        Product = request.env['saas.product'].sudo()
        product = Product.search([('is_hosting', '=', True)], limit=1)
        if not product:
            product = Product.create({
                'name': 'Cloud Hosting',
                'subtitle': 'Self-managed Odoo hosting',
                'icon': 'fa fa-server',
                'is_hosting': True,
                'is_published': True,
            })
        return product

    def _resolve_region(self, region_id=0):
        """Resolve the customer-picked region for the create flow.

        Explicit pick -> CHEAPEST available region -> None (no regions
        configured). The cheapest is the platform default so a buyer who
        doesn't pick still gets the lowest price. None keeps the funnel
        behaviour-neutral: the pricing engine uses x1.0 and allocation
        imposes no region constraint."""
        Region = request.env['saas.region'].sudo()
        region = None
        try:
            rid = int(region_id or 0)
        except (TypeError, ValueError):
            rid = 0
        if rid:
            region = Region.browse(rid)
            if not region.exists() or not region.active:
                region = None
        # Default to the RECOMMENDED region (not the cheapest) when the
        # customer didn't pick one.
        return region or Region._recommended_available()

    def _resolve_region_id_strict(self, region_id):
        """Resolve a region id to an ACTIVE region record WITHOUT defaulting.

        Unlike ``_resolve_region`` (the create funnel, which falls back to the
        platform default), a missing/invalid id here means x1.0 — used by the
        change-plan preview where the region must mirror the instance's stored
        ``region_id`` (a legacy instance with no region is priced at base, not
        at the default region's multiplier)."""
        Region = request.env['saas.region'].sudo()
        try:
            rid = int(region_id or 0)
        except (TypeError, ValueError):
            return Region.browse()
        if not rid:
            return Region.browse()
        r = Region.browse(rid)
        return r if (r.exists() and r.active) else Region.browse()

    def _region_domain_ok(self, domain, region):
        """Proxy co-location: a domain is usable in ``region`` when its
        reverse-proxy server sits in that region — or it has no proxy (the
        nginx config then lands on the region-matched Docker host, so the
        domain is region-neutral). A proxy with no region is treated as
        region-neutral too (behaviour-neutral with an unassigned fleet)."""
        if not region:
            return True
        proxy = domain.proxy_server_id
        if not proxy or not proxy.region_id:
            return True
        return proxy.region_id.id == region.id

    def _get_or_create_hosting_plan(self, product, workers, storage, config, region=None):
        """Find or create a plan matching the hosting configuration.

        ``region`` (optional) applies the region price multiplier; None =>
        x1.0 (default region / behaviour-neutral)."""
        Plan = request.env['saas.plan'].sudo()
        # Single source of truth: saas.pricing.engine. Behaviour-neutral.
        _q = request.env['saas.pricing.engine'].compute(
            'hosting', workers, storage, region=region)
        monthly_price = _q['monthly']
        plan_name = 'Hosting (%dW / %dGB)' % (workers, storage)

        plan = Plan.search([
            ('is_custom', '=', True),
            ('workers', '=', workers),
            ('storage_limit', '=', float(storage)),
            ('price', '=', monthly_price),
            ('saas_product_ids', 'in', product.id),
        ], limit=1)

        if not plan:
            # One sizing formula everywhere (admin form onchange, custom
            # builder, trial) — see saas.plan._recommended_resources.
            res = Plan._recommended_resources('hosting', workers)
            yearly_price = _q['yearly']

            plan = Plan.create({
                'name': plan_name,
                'is_custom': True,
                'price': monthly_price,
                'yearly_price': yearly_price,
                'workers': workers,
                'storage_limit': float(storage),
                'cpu_limit': res['cpu_limit'],
                'ram_limit': res['ram_limit'],
                # Hosting snapshot retention is fixed (HOSTING_MAX_SNAPSHOTS=7
                # in saas.instance.backup), so there is no per-plan backup
                # count to compute.
                'saas_product_ids': [(4, product.id)],
                'sequence': 999,
            })

        return plan

    def _get_or_create_hosting_trial_plan(self, product, config):
        """Get or create a trial plan for hosting."""
        Plan = request.env['saas.plan'].sudo()
        plan = Plan.search([
            ('is_trial_plan', '=', True),
            ('saas_product_ids', 'in', product.id),
        ], limit=1)

        if not plan:
            res = Plan._recommended_resources('hosting', config['min_workers'])
            plan = Plan.create({
                'name': 'Hosting Trial',
                'is_trial_plan': True,
                'is_custom': True,
                'price': 0,
                'yearly_price': 0,
                'workers': config['min_workers'],
                'storage_limit': float(config['min_storage']),
                'cpu_limit': res['cpu_limit'],
                'ram_limit': res['ram_limit'],
                'saas_product_ids': [(4, product.id)],
                'sequence': 0,
            })
        return plan

    # /hosting is served by the VELTNEX SPA — see controllers/spa.py.
    # /hosting/configure (below) stays Odoo QWeb: it's the purchase funnel.

    @http.route('/hosting/configure', type='http', auth='public', website=True)
    def hosting_configure(self, workers=0, storage=0, billing='monthly',
                          odoo_version_id='0', region_id='0', error=None, **kw):
        """Configure hosting instance: subdomain, repo, version."""
        if not self._section_enabled('hosting'):
            return request.redirect('/')
        if request.env.user._is_public():
            params = 'hosting=1&workers=%s&storage=%s&billing=%s&odoo_version_id=%s' % (
                workers, storage, billing, odoo_version_id,
            )
            if region_id and str(region_id) != '0':
                params += '&region_id=%s' % region_id
            if kw.get('support_code'):
                params += '&support_code=%s' % kw['support_code']
            if kw.get('is_trial') == '1':
                params += '&is_trial=1'
            # Use the generic (ungated) sign-up route — /services/register
            # is gated by the services section, so a hosting-only setup
            # would bounce the buyer to home.
            return request.redirect('/register?%s' % params)

        billing_period = billing if billing in ('monthly', 'yearly') else 'monthly'
        config = self._get_hosting_plan_config()
        workers = max(config['min_workers'], min(int(workers), config['max_workers']))
        storage = max(config['min_storage'], min(int(storage), config['max_storage']))

        # Backup add-on: a hidden checkbox on the form. Trials get it
        # free or not at all (per spec — paid feature only).
        daily_backup = (kw.get('daily_backup') == '1')

        # Region (S8c). Only offer regions that can actually host an
        # instance (proxy + docker + db in-region); empty regions are
        # hidden. The picker is shown only when there's a genuine choice
        # (>1 available region), so single-region / unconfigured fleets
        # behave exactly as before.
        Region = request.env['saas.region'].sudo()
        # Cheapest-first so the picker lists the lowest entry price at the
        # top (matters when there are many regions).
        regions = Region._available_regions().sorted(
            key=lambda r: (r.price_multiplier or 1.0, r.sequence, r.id))
        region = self._resolve_region(region_id)
        # If the resolved region has no capacity, don't pre-select it.
        if region and not region.has_capacity():
            region = regions[:1] or Region.browse()
        show_region_picker = len(regions) > 1

        # Support plan (P6): offer active plans; the picker is only shown
        # when there's a real choice (>1). Trials don't get paid support.
        Support = request.env['saas.support.plan'].sudo()
        support_plans = Support.search([('active', '=', True)], order='sequence, monthly_price, id')
        support_code = (kw.get('support_code') or '').strip() or None
        sel_support = support_plans.filtered(lambda s: s.code == support_code)[:1] \
            or support_plans.filtered(lambda s: s.is_default)[:1]
        support_code = sel_support.code if sel_support else None
        show_support_picker = len(support_plans) > 1

        _q = request.env['saas.pricing.engine'].compute(
            'hosting', workers, storage,
            region=region or None, support_code=support_code,
        )
        support_cost = _q['breakdown']['support_monthly']
        # Resource (plan) portion — the named-tier price or the linear
        # compute, shown as its own line so the breakdown is transparent.
        plan_cost = _q['breakdown']['resource_monthly']
        # Daily-backup add-on priced the SAME way it is billed (per GB of
        # used storage — see saas.pricing.engine.daily_backup_price /
        # saas.instance._get_daily_backup_price). A new instance has no
        # data yet, so the quote is the 1 GB minimum; renewals re-price
        # from actual usage. ``backup_unit_price`` is the per-month figure
        # used for the toggle; ``backup_cost`` is what's added to the total
        # only when the box is ticked. It is a MONTHLY fee — billed x12
        # on yearly with NO discount.
        backup_unit_price = request.env['saas.pricing.engine'].daily_backup_price()
        backup_cost = backup_unit_price if daily_backup else 0.0
        # Odoo.sh-style environments: per-server price for Staging/Development
        # (lowest spec, region-scaled), billed on the same period as the plan.
        env_server_price = request.env['saas.pricing.engine'].env_server_price(
            billing=billing_period, region=(region.id if region else None))
        # The yearly discount applies ONLY to the infra (plan) portion.
        # Support and the backup add-on are flat monthly fees billed x12 at
        # full price. ``_q`` already encodes "support flat, infra discounted"
        # in its monthly/yearly figures (computed without the backup add-on),
        # so we just add the backup on top.
        monthly_total = _q['monthly'] + backup_cost
        yearly_total = _q['yearly'] + backup_cost * 12
        # What the customer saves on yearly vs paying month-by-month. The
        # backup add-on cancels out (it's billed monthly x12 either way), so
        # the saving is entirely the infra yearly discount.
        yearly_savings = round(monthly_total * 12 - yearly_total, 2)
        yearly_savings_pct = (
            int(round(yearly_savings / (monthly_total * 12) * 100))
            if monthly_total else 0
        )

        versions = request.env['saas.odoo.version'].sudo().search(
            [('is_hosting_version', '=', True)], order='name desc',
        )
        domains = request.env['saas.based.domain'].sudo().search([])
        # Proxy co-location: when the customer can pick a region, only offer
        # domains whose reverse proxy lives in that region (S8c).
        if show_region_picker and region:
            domains = domains.filtered(lambda d: self._region_domain_ok(d, region))

        # ── Review page: all inputs were already collected in the SPA's
        # Step 3, so /hosting/configure is now a read-only summary + pay.
        # Resolve the chosen options for display + roll the env servers
        # into the period total.
        is_trial = kw.get('is_trial') == '1'
        sel_domain = (domains.filtered(
            lambda d: d.id == int(kw.get('domain_id') or 0))[:1]
            or domains[:1])
        sel_version = (versions.filtered(
            lambda v: v.id == int(odoo_version_id or 0))[:1]
            or versions[:1])
        try:
            staging_count = max(0, int(kw.get('staging_count', 0) or 0))
            dev_count = max(0, int(kw.get('dev_count', 0) or 0))
        except (TypeError, ValueError):
            staging_count = dev_count = 0
        env_count = 0 if is_trial else (staging_count + dev_count)
        env_total = env_server_price * env_count
        period = 'yearly' if billing_period == 'yearly' else 'monthly'
        period_total = (
            (yearly_total if period == 'yearly' else monthly_total)
            + env_total)
        return request.render('saas_website.hosting_review_form', {
            'sel_domain': sel_domain,
            'sel_version': sel_version,
            'sel_support': sel_support,
            'staging_count': staging_count,
            'dev_count': dev_count,
            'env_count': env_count,
            'env_total': env_total,
            'period': period,
            'period_total': period_total,
            'is_trial': is_trial,
            'domains': domains,
            'versions': versions,
            'regions': regions,
            'selected_region_id': region.id if region else 0,
            'show_region_picker': show_region_picker,
            'error': error,
            'workers': workers,
            'storage': storage,
            'backup_cost': backup_cost,
            'env_server_price': env_server_price,
            'plan_cost': plan_cost,
            'daily_backup': daily_backup,
            'support_plans': support_plans,
            'selected_support_code': support_code or '',
            'support_cost': support_cost,
            'monthly_total': monthly_total,
            'yearly_total': yearly_total,
            'yearly_savings': yearly_savings,
            'yearly_savings_pct': yearly_savings_pct,
            'billing_period': billing_period,
            'config': config,
            'odoo_version_id': int(odoo_version_id or 0),
            'selected_region': region,
            'form_values': kw,
        })

    @http.route('/hosting/configure/quote', type='json', auth='public', website=True)
    def hosting_configure_quote(self, workers=2, storage=5, region_id=0,
                                support_code=None, daily_backup=False):
        """Region/support/backup-aware price breakdown for the hosting
        checkout — lets the page update the total live instead of doing a
        full reload when the customer changes the region or support plan.

        Returns the same numbers the GET renders, plus the region-filtered
        domain list (changing region changes which domains are co-located
        and therefore selectable)."""
        Region = request.env['saas.region'].sudo()
        engine = request.env['saas.pricing.engine']
        config = self._get_hosting_plan_config()
        workers = max(config['min_workers'], min(int(workers or 0), config['max_workers']))
        storage = max(config['min_storage'], min(int(storage or 0), config['max_storage']))

        region = self._resolve_region(region_id)
        support = request.env['saas.support.plan'].sudo().search([
            ('code', '=', (support_code or '').strip()),
            ('active', '=', True),
        ], limit=1)
        support_resolved = support.code if support else None
        daily = str(daily_backup) in ('1', 'true', 'True', 'on') or daily_backup is True

        _q = engine.compute(
            'hosting', workers, storage,
            region=region.id or None, support_code=support_resolved,
        )
        support_cost = _q['breakdown']['support_monthly']
        plan_cost = _q['breakdown']['resource_monthly']
        backup_unit_price = engine.daily_backup_price()
        backup_cost = backup_unit_price if daily else 0.0
        monthly_total = _q['monthly'] + backup_cost
        yearly_total = _q['yearly'] + backup_cost * 12
        yearly_savings = round(monthly_total * 12 - yearly_total, 2)
        yearly_savings_pct = (
            int(round(yearly_savings / (monthly_total * 12) * 100))
            if monthly_total else 0
        )

        # Region-filtered domains (proxy co-location) — same rule as the GET.
        domains = request.env['saas.based.domain'].sudo().search([])
        show_region_picker = len(Region._available_regions()) > 1
        if show_region_picker and region:
            domains = domains.filtered(lambda d: self._region_domain_ok(d, region))

        return {
            'plan_cost': round(plan_cost, 2),
            'support_cost': round(support_cost, 2),
            'backup_unit_price': round(backup_unit_price, 2),
            'backup_cost': round(backup_cost, 2),
            # Totals WITHOUT the backup add-on, so the client's backup toggle
            # can add it on top exactly like the initial server render.
            'base_monthly': round(_q['monthly'], 2),
            'base_yearly': round(_q['yearly'], 2),
            'monthly_total': round(monthly_total, 2),
            'yearly_total': round(yearly_total, 2),
            'yearly_savings': yearly_savings,
            'yearly_savings_pct': yearly_savings_pct,
            'currency': config['currency'],
            'domains': [{'id': d.id, 'name': d.name} for d in domains],
        }

    @http.route('/hosting/order', type='http', auth='user', website=True,
                methods=['POST'], csrf=True)
    def hosting_order(self, **post):
        """Process a hosting order."""
        if not self._section_enabled('hosting'):
            return request.redirect('/')
        workers = int(post.get('workers', 4))
        storage = int(post.get('storage', 20))
        subdomain = (post.get('subdomain') or '').strip().lower()
        project_name = (post.get('project_name') or '').strip()
        domain_id = int(post.get('domain_id', 0))
        billing_period = post.get('billing_period', 'monthly')
        odoo_version_id = int(post.get('odoo_version_id', 0))
        repo_url = (post.get('repo_url') or '').strip()
        repo_branch = (post.get('repo_branch') or 'main').strip()
        git_token = (post.get('git_token') or '').strip()
        pip_packages = (post.get('pip_packages') or '').strip()
        is_trial = post.get('is_trial') == '1'
        # Odoo.sh-style environments chosen at checkout: extra Staging /
        # Development servers (each billed at the lowest-spec env price).
        try:
            staging_count = max(0, int(post.get('staging_count', 0) or 0))
        except (TypeError, ValueError):
            staging_count = 0
        try:
            dev_count = max(0, int(post.get('dev_count', 0) or 0))
        except (TypeError, ValueError):
            dev_count = 0

        if billing_period not in ('monthly', 'yearly'):
            billing_period = 'monthly'

        # Region (S8c): fixed at creation. Resolves to the default when
        # not supplied -> behaviour-neutral for single-region fleets.
        region = self._resolve_region(post.get('region_id', 0))

        config = self._get_hosting_plan_config()
        workers = max(config['min_workers'], min(workers, config['max_workers']))
        storage = max(config['min_storage'], min(storage, config['max_storage']))

        err_redirect = '/hosting/configure?workers=%d&storage=%d&billing=%s&odoo_version_id=%d&region_id=%d&error=%%s' % (
            workers, storage, billing_period, odoo_version_id,
            region.id if region else 0,
        )

        # Subdomain validation
        if not subdomain or not SUBDOMAIN_RE.match(subdomain):
            return request.redirect(err_redirect % 'Invalid+subdomain')

        existing = request.env['saas.instance'].sudo().search([
            ('subdomain', '=', subdomain),
            ('domain_id', '=', domain_id),
            ('state', 'in', _ACTIVE_STATES),
        ], limit=1)
        if existing:
            return request.redirect(err_redirect % ('Subdomain+already+taken'))

        # Version validation
        version = request.env['saas.odoo.version'].sudo().browse(odoo_version_id)
        if not version.exists() or not version.is_hosting_version:
            return request.redirect(err_redirect % ('Please+select+an+Odoo+version'))
        if not version.docker_image or not version.docker_image_tag:
            return request.redirect(err_redirect % ('Selected+Odoo+version+is+not+properly+configured.+Please+contact+support.'))

        domain = request.env['saas.based.domain'].sudo().browse(domain_id)
        if not domain.exists():
            return request.redirect(err_redirect % ('Please+select+a+domain'))
        # Proxy co-location: the domain's reverse proxy must be in the
        # chosen region (nginx + odoo + db all co-located).
        if not self._region_domain_ok(domain, region):
            return request.redirect(err_redirect % (
                'Selected+domain+is+not+available+in+this+region'))

        partner = request.env.user.partner_id

        # Rate limiting
        max_instances = int(request.env['ir.config_parameter'].sudo().get_param(
            'saas_master.max_instances_per_user', '5',
        ))
        if max_instances > 0:
            active_count = request.env['saas.instance'].sudo().search_count([
                ('partner_id', '=', partner.id),
                ('state', 'in', _QUOTA_STATES),
            ])
            if active_count >= max_instances:
                return request.redirect(err_redirect % ('Maximum+instances+reached'))

        # Infrastructure validation (region-aware): the chosen region must
        # have capacity — a proxy + Docker host + DB server in-region
        # (co-location). Empty regions can't be ordered. Defends against a
        # POSTed region_id that the picker would never have offered.
        if region and not region.has_capacity():
            return request.redirect(err_redirect % (
                'Service+temporarily+unavailable+in+the+selected+region'))
        if not region:
            # No region configured at all: fall back to the legacy global
            # capacity check (any docker host + any db server).
            Server = request.env['saas.server'].sudo()
            if not Server.search([('is_docker_host', '=', True)], limit=1) or \
               not Server.search([('is_db_server', '=', True)], limit=1):
                return request.redirect(err_redirect % (
                    'Service+temporarily+unavailable'))

        # Validate repo URL format (if provided)
        if repo_url:
            if not (repo_url.startswith('https://') or repo_url.startswith('git@')):
                return request.redirect(err_redirect % 'Repository+URL+must+start+with+https://+or+git@')
            if not git_token and ('github.com' in repo_url or 'gitlab.com' in repo_url or 'bitbucket.org' in repo_url):
                # Private repos need a token; public might work without one
                pass  # Allow it — clone will fail with clear error if repo is private

        # Git is OPTIONAL at checkout. Buying Staging/Development capacity only
        # reserves paid slots — the customer connects a repository later, when
        # they actually create an environment from the workspace. So we no
        # longer block checkout on a missing repo.

        # Hosting trial: one per client, and not after a paid hosting instance
        if is_trial:
            partner_sudo = partner.sudo()
            if partner_sudo._saas_trial_used(hosting=True):
                return request.redirect(err_redirect % ('You+have+already+used+your+free+hosting+trial'))
            if partner_sudo._saas_has_paid_instance(hosting=True):
                return request.redirect(err_redirect % (
                    'You+already+have+a+paid+hosting+instance+-+the+free+trial+is+no+longer+available'
                ))

        # Get or create hosting product and plan
        product = self._get_or_create_hosting_product()

        # For trial, use a trial plan
        if is_trial:
            plan = self._get_or_create_hosting_trial_plan(product, config)
        else:
            plan = self._get_or_create_hosting_plan(
                product, workers, storage, config, region=region)

        vals = {
            'subdomain': subdomain,
            'project_name': project_name or subdomain,
            'domain_id': domain.id,
            'partner_id': partner.id,
            'saas_product_id': product.id,
            'plan_id': plan.id,
            'odoo_version_id': version.id,
            'billing_period': billing_period,
            'pip_packages': pip_packages or False,
            # Paid daily-backup add-on. Trials don't get it.
            'daily_backup_enabled': (
                not is_trial and post.get('daily_backup') == '1'
            ),
        }
        # Pin the region at creation (fixed for the instance's life);
        # drives co-located server allocation.
        if region:
            vals['region_id'] = region.id
        # Staging/Development servers chosen at checkout are spawned once
        # the initial invoice is paid (trials get only Production).
        if not is_trial and (staging_count or dev_count):
            vals['pending_staging_count'] = staging_count
            vals['pending_dev_count'] = dev_count
        # Support plan (P6): a paid add-on chosen at checkout. Trials
        # don't get paid support. Falls back to the default plan.
        if not is_trial:
            Support = request.env['saas.support.plan'].sudo()
            sup = Support.search([
                ('code', '=', (post.get('support_code') or '').strip()),
                ('active', '=', True),
            ], limit=1)
            if sup:
                vals['support_plan_id'] = sup.id
        if is_trial:
            vals['is_trial'] = True

        try:
            # Atomic: a deferred constraint (e.g. the one-trial-per-client
            # ConstraintError that fires on flush during action_deploy) must
            # roll back the instance INSERT entirely — otherwise a rejected
            # trial leaks an orphan draft instance (subdomain/port/creds and
            # all) because the request still commits after the caught error.
            # A cr.savepoint() rolls the INSERT back cleanly on ANY failure,
            # so there is nothing to unlink afterwards.
            with request.env.cr.savepoint():
                instance = request.env['saas.instance'].sudo().create(vals)

                # Create the customer's repository record (if provided)
                if repo_url:
                    request.env['saas.instance.repo'].sudo().create({
                        'instance_id': instance.id,
                        'repo_url': repo_url,
                        'branch': repo_branch,
                        'github_token': git_token or False,
                        'webhook_enabled': bool(git_token),
                    })

                if is_trial:
                    # Trial: skip billing, deploy immediately.
                    instance.action_deploy()
                else:
                    instance.action_confirm_and_bill()
        except (UserError, ValidationError) as e:
            # savepoint already rolled the INSERT back — no orphan to clean up.
            return request.redirect(err_redirect % str(e).replace(' ', '+'))

        if is_trial:
            return request.redirect('/my/instances/%s?access_token=%s' % (
                instance.id, instance.access_token,
            ))
        return request.redirect('/my/instances/%s/checkout' % instance.id)

    @http.route('/saas/hosting-plan/calculate', type='json', auth='public', website=True)
    def hosting_plan_calculate(self, workers=2, storage=5, region=None):
        """Real-time price calculation for the hosting plan builder.

        ``region`` (id, optional) scales the price by that region's
        multiplier so a change-plan preview matches the region-aware plan
        that will actually be created/billed."""
        config = self._get_hosting_plan_config()
        workers = max(config['min_workers'], min(int(workers), config['max_workers']))
        storage = max(config['min_storage'], min(int(storage), config['max_storage']))

        region_rec = self._resolve_region_id_strict(region)
        _q = request.env['saas.pricing.engine'].compute(
            'hosting', workers, storage, region=region_rec.id or None)

        return {
            'workers': workers,
            'storage': storage,
            'monthly_total': _q['monthly'],
            # Engine yearly: region-scaled AND discounted. The client no
            # longer derives yearly from monthly.
            'yearly_total': _q['yearly'],
            'currency': config['currency'],
        }

    # ==================================================================
    #  Subdomain Availability Check (unchanged)
    # ==================================================================

    @http.route('/saas/check-subdomain', type='json', auth='public', website=True)
    def check_subdomain(self, subdomain='', domain_id=0):
        subdomain = (subdomain or '').strip().lower()
        if not subdomain:
            return {'available': False, 'message': ''}

        # Throttle so the availability oracle can't be scraped to enumerate
        # every taken subdomain (and, when signed in, owned cancelled ones).
        # Keyed by client IP, honouring the nginx reverse proxy.
        req = request.httprequest
        xff = req.headers.get('X-Forwarded-For', '')
        client_ip = (xff.split(',')[0].strip() if xff
                     else (req.remote_addr or '-'))
        allowed, _retry = request.env['saas.rate.limit'].sudo()._hit(
            'check_subdomain', client_ip, 40, 60)
        if not allowed:
            return {'available': False,
                    'message': _("Too many checks — please slow down and try "
                                 "again in a minute.")}

        if not SUBDOMAIN_RE.match(subdomain):
            return {
                'available': False,
                'message': _("Invalid format. Use lowercase letters, digits, and hyphens."),
            }

        existing = request.env['saas.instance'].sudo().search([
            ('subdomain', '=', subdomain),
            ('domain_id', '=', int(domain_id or 0)),
            ('state', 'in', _ACTIVE_STATES),
        ], limit=1)

        if existing:
            # If the existing record is the current user's own
            # cancelled instance, give them the clearer "reactivate
            # instead" message — otherwise just say it's taken.
            current_partner = request.env.user.partner_id
            if (existing.state in ('cancelled', 'cancelled_by_client')
                    and current_partner
                    and existing.partner_id == current_partner):
                return {
                    'available': False,
                    'message': _(
                        "'%s' is your own cancelled instance — "
                        "reactivate it from My Instances instead of "
                        "creating a new one."
                    ) % subdomain,
                }
            return {
                'available': False,
                'message': _("'%s' is already taken.") % subdomain,
            }

        return {
            'available': True,
            'message': _("'%s' is available!") % subdomain,
        }
