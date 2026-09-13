import math

from dateutil.relativedelta import relativedelta

from odoo import api, models

# ----------------------------------------------------------------------
# A5 — ONE billing methodology for the whole platform.
#
# A billing period is an exact number of CALENDAR months: monthly = 1,
# yearly = 12. All recurrence math uses ``relativedelta`` (calendar
# months/years), and all "how many months between two dates" math uses
# ``months_between`` below. The platform NEVER approximates a month as
# "days / 30" anywhere (that drifts ~6 days/year and made fixed fees
# inconsistent across flows).
# ----------------------------------------------------------------------
PERIOD_MONTHS = {'monthly': 1, 'yearly': 12}

# ----------------------------------------------------------------------
# Pricing engine — the SINGLE source of truth for every price the
# platform quotes (slider preview, configure, checkout, plan creation,
# upgrade/downgrade). See docs/pricing-system-execution-plan.md.
#
# STEP S1 (this file): the engine exists and reproduces the CURRENT
# linear price EXACTLY. It is additive — no caller is repointed yet
# (that is S2). Future capabilities (cost floor, region multiplier,
# add-ons, named tiers) are wired in as no-ops here so that, with their
# config defaults, compute() returns numbers identical to today.
#
# Behaviour-neutral guarantees in S1:
#   * floor rate defaults to 0  -> max(base, 0) == base
#   * region defaults to x1.0   -> no region scaling (no region model yet)
#   * addon total defaults to 0 -> no add-on model referenced yet
# ----------------------------------------------------------------------


class SaasPricingEngine(models.AbstractModel):
    _name = 'saas.pricing.engine'
    _description = 'SaaS Pricing Engine (single source of truth)'

    # ------------------------------------------------------------------
    # Config loading — same keys/defaults the controllers use today, so
    # the engine output matches the existing inline formulas exactly.
    # ``kind`` selects the rate set: 'hosting' or 'services'.
    # ------------------------------------------------------------------
    def _rate_config(self, kind):
        icp = self.env['ir.config_parameter'].sudo()
        get = icp.get_param
        if kind == 'hosting':
            p = 'saas_master.hosting_'
            cfg = {
                'worker_price': float(get('saas_master.hosting_worker_price', '10.0')),
                'storage_price_per_gb': float(get('saas_master.hosting_storage_price_per_gb', '0.3')),
                'min_workers': int(get('saas_master.hosting_min_workers', '2')),
                'max_workers': int(get('saas_master.hosting_max_workers', '8')),
                'min_storage': int(get('saas_master.hosting_min_storage', '5')),
                'max_storage': int(get('saas_master.hosting_max_storage', '200')),
                'yearly_discount_pct': int(get('saas_master.hosting_yearly_discount_pct', '20')),
                # Cost floor (S3 will surface these in Settings). Default 0
                # => no-op in S1, so prices are unchanged.
                'worker_floor': float(get('saas_master.hosting_worker_floor', '0')),
                'storage_floor': float(get('saas_master.hosting_storage_floor', '0')),
                # Minimum monthly charge (P1). Floor on the FINAL monthly
                # total so a tiny config still covers fixed business costs
                # (payment fees, support, CAC). Default 0 => no-op.
                'minimum_monthly': float(get('saas_master.hosting_minimum_monthly', '0')),
            }
        else:  # 'services' (custom plan builder)
            cfg = {
                'worker_price': float(get('saas_master.worker_price', '15.0')),
                'storage_price_per_gb': float(get('saas_master.storage_price_per_gb', '0.5')),
                'min_workers': int(get('saas_master.custom_plan_min_workers', '2')),
                'max_workers': int(get('saas_master.custom_plan_max_workers', '8')),
                'min_storage': int(get('saas_master.custom_plan_min_storage', '5')),
                'max_storage': int(get('saas_master.custom_plan_max_storage', '200')),
                'yearly_discount_pct': int(get('saas_master.custom_plan_yearly_discount_pct', '20')),
                'worker_floor': float(get('saas_master.worker_floor', '0')),
                'storage_floor': float(get('saas_master.storage_floor', '0')),
                'minimum_monthly': float(get('saas_master.minimum_monthly', '0')),
            }
        cfg['currency'] = self.env.company.currency_id.name or 'USD'
        return cfg

    # ------------------------------------------------------------------
    # No-op hooks for future steps. Implemented here so compute() reads
    # cleanly; they intentionally do nothing in S1.
    # ------------------------------------------------------------------
    def _region_multiplier(self, region):
        """Per-region price multiplier for the compute+storage portion.
        ``region`` may be a saas.region record, an id, or falsy. Unknown /
        falsy -> 1.0 (behaviour-neutral; legacy instances have no region)."""
        if not region:
            return 1.0
        try:
            if isinstance(region, int):
                region = self.env['saas.region'].sudo().browse(region)
            if region and region.exists() and region.price_multiplier:
                return region.price_multiplier
        except Exception:
            return 1.0
        return 1.0

    def _cost_floor(self, cfg, workers, storage):
        """S3/S4: cost-derived price floor. Default rates 0 -> 0.0."""
        return (workers * cfg['worker_floor']) + (storage * cfg['storage_floor'])

    # NOTE: the old "custom price can't undercut a tier" floor
    # (saas_master.custom_min_is_nearest_tier) was removed: under the
    # UNIFIED pricing model it is mathematically a no-op. A named tier's
    # price is its own linear rate minus a discount (never above the
    # linear rate), and the linear rate is monotonic in resources — so a
    # custom config that contains a tier always prices at or above that
    # tier's published price. Undercutting is structurally impossible;
    # the always-on _tier_ceiling is what keeps tiers and customs
    # consistent.

    def _exact_public_tier(self, kind, workers, storage):
        """The published public tier whose resources EXACTLY match this
        config, if any.

        When a customer's config matches a named tier, that tier's
        advertised price IS the price — so the pricing card, the
        configurator, and the invoice all quote the same number (one
        source of truth). Any non-matching (custom slider) config falls
        back to the linear rate. Returns an empty recordset when nothing
        matches.
        """
        tiers = self.env['saas.plan'].sudo().search([
            ('is_public_tier', '=', True),
            ('workers', '=', workers),
            ('storage_limit', '=', float(storage)),
        ])
        for t in tiers:
            t_kind = 'hosting' if any(
                p.is_hosting for p in t.saas_product_ids
            ) else 'services'
            if t_kind == kind:
                return t
        return self.env['saas.plan'].browse()

    def _tier_ceiling(self, kind, workers, storage):
        """Price CEILING for a custom config: the cheapest published tier
        that gives the SAME or MORE resources (workers >= W AND storage >=
        S). A customer must never pay more for a custom build than for a
        bigger named plan — paying more for less is nonsensical.

        Returns ``{'monthly': x|None, 'yearly': y|None}`` (None => no tier
        covers this config, so no cap). MONOTONIC: a bigger config is
        covered by fewer tiers, so the ceiling only rises or disappears, so
        capping by it keeps the slider monotonic. Always on (purely
        customer-fair)."""
        tiers = self.env['saas.plan'].sudo().search([
            ('is_public_tier', '=', True),
            ('workers', '>=', workers),
            ('storage_limit', '>=', float(storage)),
        ])
        monthly = None
        yearly = None
        for t in tiers:
            t_kind = 'hosting' if any(
                p.is_hosting for p in t.saas_product_ids
            ) else 'services'
            if t_kind != kind or not t.price:
                continue
            if monthly is None or t.price < monthly:
                monthly = t.price
            # Only tiers that actually offer yearly bound the yearly price.
            if t.yearly_price and (yearly is None or t.yearly_price < yearly):
                yearly = t.yearly_price
        return {'monthly': monthly, 'yearly': yearly}

    def _addons_total(self, kind, addon_codes, storage_gb=0):
        """Sum effective monthly prices of the given add-on codes that
        apply to ``kind``. ``storage_gb`` lets storage-aware add-ons (P4)
        scale; flat ones ignore it. Empty/none -> 0.0 (behaviour-neutral)."""
        if not addon_codes:
            return 0.0
        return self.env['saas.addon']._sum_prices(kind, addon_codes, storage_gb)

    @staticmethod
    def _clamp(value, lo, hi):
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = lo
        return max(lo, min(value, hi))

    # ------------------------------------------------------------------
    # THE single price computation. Everything must call this.
    # ------------------------------------------------------------------
    @api.model
    def compute(self, kind, workers, storage, billing='monthly',
                addon_codes=(), region=None, support_code=None):
        """Return one fully-resolved price quote.

        Args:
            kind: 'hosting' or 'services' (selects the rate set).
            workers, storage: requested config (clamped to plan limits).
            billing: 'monthly' or 'yearly'.
            addon_codes: iterable of add-on codes (S5; ignored in S1).
            region: region record/id (S7; multiplier is 1.0 in S1).
            support_code: optional saas.support.plan code (P3). A flat
                monthly fee added after infra/region; 0 if None/unknown.

        Returns a dict (superset of the current ``_price`` shape so
        callers can be repointed in S2 without changing their output):
            workers, storage, billing, monthly, yearly, total,
            monthly_equivalent, yearly_savings, savings_percent,
            currency, region_factor, floored, limits, breakdown.
        """
        cfg = self._rate_config(kind)
        workers = self._clamp(workers, cfg['min_workers'], cfg['max_workers'])
        storage = self._clamp(storage, cfg['min_storage'], cfg['max_storage'])

        base = (workers * cfg['worker_price']) + (storage * cfg['storage_price_per_gb'])
        # UNIFIED pricing — ONE formula for tiers and custom builds:
        #   * Resources are priced by the linear rate (``base``), which is
        #     monotonic: more workers/storage always costs more.
        #   * A named tier is the SAME linear rate minus an optional fixed
        #     discount (see ``saas.plan.discount_amount``) — that is the only
        #     knob for making a published plan a "good deal".
        #   * A custom build is CAPPED by the cheapest tier that covers it
        #     (same or more resources): a customer never pays more for a
        #     custom build than for a bigger named plan. So buying a
        #     tier-sized custom config charges exactly the tier price, and the
        #     card / configurator / invoice all agree.
        # No exact-match override (it spiked the price up at tier sizes and
        # broke monotonicity); the ceiling does the job, monotonically.
        resource_base = base
        floor = self._cost_floor(cfg, workers, storage)
        region_factor = self._region_multiplier(region)

        # Cost floor first (never below cost), then the tier CEILING (never
        # above a covering named plan). Both are monotonic in resources, so
        # the result stays monotonic.
        ceiling = self._tier_ceiling(kind, workers, storage)
        resource_pre = max(resource_base, floor)
        if ceiling['monthly'] is not None:
            resource_pre = min(resource_pre, ceiling['monthly'])
        floored = floor > resource_base
        # Region scaling, then re-apply the cost floor AFTER it: a region
        # ``price_multiplier`` below 1.0 must never scale a config below the
        # real infra cost. With the default floor of 0 this is a no-op
        # (``max(x, 0) == x``), so existing prices are unchanged.
        resource_monthly = max(resource_pre * region_factor, floor)
        addons_monthly = self._addons_total(kind, addon_codes, storage)
        # Support plan (P3): flat monthly fee, NOT scaled by region, added
        # after infra. 0 when no plan / the free default / unknown code.
        support_monthly = self.env['saas.support.plan']._price_for_code(support_code)
        pre_minimum = resource_monthly + addons_monthly + support_monthly

        # Minimum monthly charge (P1): the final total never drops below
        # the configured floor — small configs still cover fixed business
        # costs. Applied AFTER add-ons (so add-ons count toward the
        # minimum, never on top of it). Default 0 => no-op.
        minimum_monthly = cfg.get('minimum_monthly', 0.0) or 0.0
        monthly = max(pre_minimum, minimum_monthly)
        minimum_applied = minimum_monthly > pre_minimum

        discount = cfg['yearly_discount_pct'] / 100.0
        # The yearly discount applies ONLY to the infrastructure (compute +
        # storage) portion. Support plans and the daily-backup / other
        # add-ons are FLAT MONTHLY fees billed 12x at their full monthly
        # price and are NEVER discounted on yearly billing — so the quote
        # reconciles exactly with what is invoiced: support is billed
        # qty=12 @ the monthly price (see ``_support_order_line``) and the
        # snapshot add-on is billed monthly at full price by its own cron.
        # Linear yearly = discounted linear monthly, then capped by the
        # covering tier's yearly price (the same ceiling logic as monthly).
        resource_yearly = max(resource_base, floor) * 12 * (1 - discount)
        if ceiling['yearly'] is not None:
            resource_yearly = min(resource_yearly, ceiling['yearly'])
        # Same region-then-floor guard as the monthly path: a sub-1.0 region
        # multiplier can never pull the yearly infra price below the (yearly-
        # equivalent) cost floor. No-op when the floor is 0.
        floor_yearly = floor * 12 * (1 - discount)
        resource_yearly = max(resource_yearly * region_factor, floor_yearly)
        # Flat extras: full monthly price x12, NO yearly discount.
        other_yearly = (addons_monthly + support_monthly) * 12
        pre_minimum_yearly = resource_yearly + other_yearly
        # The minimum-monthly floor still guards the yearly total; only the
        # infra portion it floors carries the yearly discount.
        yearly = max(pre_minimum_yearly, minimum_monthly * 12 * (1 - discount))
        yearly_savings = (monthly * 12) - yearly
        is_yearly = billing == 'yearly'

        return {
            'workers': workers,
            'storage': storage,
            'billing': 'yearly' if is_yearly else 'monthly',
            'monthly': round(monthly, 2),
            'yearly': round(yearly, 2),
            'total': round(yearly if is_yearly else monthly, 2),
            'monthly_equivalent': round(yearly / 12 if is_yearly else monthly, 2),
            'yearly_savings': round(yearly_savings, 2),
            # Derived from the ACTUAL monthly vs yearly (a named tier can
            # carry its own yearly_price, so the real saving may differ
            # from the global discount). Falls back to the config pct.
            'savings_percent': (
                int(round(yearly_savings / (monthly * 12) * 100))
                if monthly else int(cfg['yearly_discount_pct'])
            ),
            'currency': cfg['currency'],
            'region_factor': region_factor,
            'floored': floored,
            'minimum_applied': minimum_applied,
            'limits': {
                'workers': {'min': cfg['min_workers'], 'max': cfg['max_workers']},
                'storage': {'min': cfg['min_storage'], 'max': cfg['max_storage']},
            },
            'breakdown': {
                'base': round(base, 2),
                # Cheapest covering-tier ceiling applied to this config (0 =
                # no tier covers it, so the linear rate stands).
                'tier_ceiling': round(ceiling['monthly'], 2) if ceiling['monthly'] is not None else 0.0,
                'resource_base': round(resource_base, 2),
                # The only floor left is the cost floor (the tier floor was
                # removed — a no-op under unified pricing); both keys kept
                # for breakdown consumers.
                'floor': round(floor, 2),
                'cost_floor': round(floor, 2),
                'resource_monthly': round(resource_monthly, 2),
                'addons_monthly': round(addons_monthly, 2),
                # P3: flat support-plan fee (not region-scaled).
                'support_monthly': round(support_monthly, 2),
                # P1: the total before the minimum-monthly floor, and the
                # minimum that was enforced (0 = off). monthly == max of
                # these two.
                'pre_minimum': round(pre_minimum, 2),
                'minimum_monthly': round(minimum_monthly, 2),
                # Per-resource split (pre-floor, pre-region) — kept so the
                # legacy display routes can show the same breakdown they
                # do today. Frontend will stop surfacing these in S8.
                'workers_cost': round(workers * cfg['worker_price'], 2),
                'storage_cost': round(storage * cfg['storage_price_per_gb'], 2),
            },
        }

    # ------------------------------------------------------------------
    # A5 — billing-period helpers (single methodology, no days/30).
    # ------------------------------------------------------------------
    @staticmethod
    def period_months(period):
        """Number of CALENDAR months in a billing period."""
        return PERIOD_MONTHS.get(period, 1)

    @staticmethod
    def period_delta(period):
        """``relativedelta`` to advance one billing period."""
        return (relativedelta(years=1) if period == 'yearly'
                else relativedelta(months=1))

    @staticmethod
    def months_between(start, end):
        """Whole calendar months elapsed between two dates, rounded UP so a
        started month counts as one (minimum 1 when end > start). Replaces
        every ``days / 30`` approximation in the codebase."""
        if not start or not end or end <= start:
            return 0
        months = (end.year - start.year) * 12 + (end.month - start.month)
        # A partial month past the day-of-month anchor counts as one more.
        if end.day > start.day:
            months += 1
        return max(1, months)

    @api.model
    def storage_block_config(self):
        """The platform's storage-expansion block (size GB, monthly price)."""
        icp = self.env['ir.config_parameter'].sudo()
        try:
            block_gb = int(icp.get_param('saas_master.storage_block_gb', '0') or 0)
        except (TypeError, ValueError):
            block_gb = 0
        try:
            block_price = float(
                icp.get_param('saas_master.storage_block_price', '0') or 0)
        except (TypeError, ValueError):
            block_price = 0.0
        return block_gb, block_price

    # v47: ``storage_overage`` was REMOVED. There is no usage-based / per-GB
    # overage billing anywhere. Extra storage is sold ONLY as purchased
    # blocks (a deliberate recurring add-on) or via a plan upgrade — see
    # ``saas.instance.action_purchase_storage_block`` and the capacity state
    # machine (``_evaluate_capacity``).

    @api.model
    def snapshot_price_per_gb(self):
        """Per-GB monthly rate for the usage-based snapshot add-on
        (``saas_master.snapshot_price_per_gb``, default $0.40/GB).
        0 makes the add-on free / unpurchasable (price 0 hides it)."""
        icp = self.env['ir.config_parameter'].sudo()
        try:
            return float(icp.get_param(
                'saas_master.snapshot_price_per_gb', '0.40') or 0)
        except (TypeError, ValueError):
            return 0.0

    @api.model
    def daily_backup_price(self, used_bytes=None):
        """Monthly price of the daily-backup add-on.

        SINGLE source of truth for the snapshot add-on price so the checkout
        quote, the portal, and the recurring invoice all agree.

        Usage-based: the space actually consumed (``used_bytes``) is
        rounded UP to the next whole GB and charged at the per-GB rate
        (``saas_master.snapshot_price_per_gb``, default $0.40/GB), with a
        1 GB minimum so the add-on never prices at $0. ``used_bytes=None``
        (configure-time quote — nothing measured yet) quotes the 1 GB
        minimum, i.e. the "from" price.

        This is a MONTHLY fee — never discounted on yearly billing.
        """
        per_gb = self.snapshot_price_per_gb()
        if per_gb <= 0:
            return 0.0
        billable_gb = max(1, math.ceil((used_bytes or 0) / (1024 ** 3)))
        return round(billable_gb * per_gb, 2)

    @api.model
    def env_price_factor(self):
        """Multiplier applied to the per-server price of Staging/Development
        environments (``saas_master.env_price_factor``, default 1.0). Lets the
        platform make non-production servers cheaper (e.g. 0.7) without code.
        Clamped to [0, 1e6]; 0 makes env servers free."""
        icp = self.env['ir.config_parameter'].sudo()
        raw = icp.get_param('saas_master.env_price_factor', '1.0')
        try:
            factor = float(raw)
        except (TypeError, ValueError):
            factor = 1.0
        return max(0.0, min(factor, 1e6))

    @api.model
    def env_server_price(self, billing='monthly', region=None):
        """Per-server price of a Staging/Development environment: the engine's
        hosting price at the LOWEST spec (min workers/storage), region-scaled,
        times ``env_price_factor``. Single source of truth so the configurator,
        the one-click create flow, and the recurring renewal line all agree."""
        cfg = self._rate_config('hosting')
        quote = self.compute(
            'hosting', cfg['min_workers'], cfg['min_storage'],
            billing=billing, region=region,
        )
        return round(quote['total'] * self.env_price_factor(), 2)

    @api.model
    def monthly_price(self, kind, workers, storage, region=None):
        """Convenience: just the monthly figure used when stamping a
        ``saas.plan.price`` at custom-plan creation (S2 will use this)."""
        return self.compute(
            kind, workers, storage, billing='monthly', region=region,
        )['monthly']
