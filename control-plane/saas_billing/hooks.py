import logging

_logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Step A of the billing/pricing architecture migration (see the
# architecture proposal / plan doc): `saas.payment.*`, `saas.wallet.*`,
# `saas.support.plan`, `saas.addon`, and `saas.pricing` (the engine) move
# from `saas_core` to this new addon. Their tables/data are UNTOUCHED —
# only which addon owns their `ir_model_data` rows changes, and this must
# happen BEFORE this addon's own manifest data loads (hence
# `pre_init_hook`, not `post_init_hook`).
#
# Scope note: this hook re-parents the MODEL-level xmlids (ir.model,
# ir.model.fields, ir.model.access, views/actions/menus/data/crons) for
# the five wholesale-relocated models above. It does NOT re-parent the
# field-level xmlids for the fields added onto `saas.instance`/
# `saas.plan` via `_inherit` in this same step (e.g.
# `field_saas_instance__support_plan_id`) — Odoo auto-creates those as
# NEW xmlids owned by `saas_billing` on a fresh install, which is exactly
# right for a database that has never had `saas_core` installed before.
# For `saas_dev` (the one persistent dev DB that HAS run the pre-cutover
# schema), the field ownership mismatch this leaves behind is not worth
# solving field-by-field: there is no real customer data anywhere yet
# (confirmed — see project memory), so `saas_dev` should be DROPPED AND
# RECREATED from scratch after this migration lands, not upgraded
# in-place. `saas_test` is already dropped and recreated on every
# `devctl.sh test` run, so it never hits this at all.
# ----------------------------------------------------------------------

# CONTAINS-style LIKE patterns matching every xmlid genuinely scoped to
# one of the five relocated models: Odoo prefixes a model's own
# ir.model/ir.model.fields/ir.model.access xmlids with 'model_'/'field_'/
# 'access_' before the underscored model name (e.g.
# 'field_saas_wallet__balance'), while views/actions/data records use the
# bare underscored name as their own prefix (e.g. 'saas_wallet_action') —
# a %contains% pattern on the model's underscored name catches all of
# these forms in one go. Safe here: none of these substrings appear
# anywhere else in this codebase's model names.
_LIKE_PATTERNS = (
    '%saas_wallet%',            # saas.wallet + .lot + .transaction
    '%saas_payment%',           # provider.config + method + attempt + gateway
    '%saas_support_plan%',      # model/fields/views/data
    '%saas_addon%',             # model/fields/views/data
    '%saas_pricing%',           # saas.pricing.engine (AbstractModel, no table)
)

# Hand-named menu items + the wallet-expiry cron don't share a clean
# per-model prefix with the rest of the platform's menus (they all start
# with the same `saas_master_menu_` prefix a LIKE pattern can't safely
# narrow) — enumerated explicitly instead.
_EXPLICIT_NAMES = (
    'saas_master_menu_billing',
    'saas_master_menu_wallets',
    'saas_master_menu_payment_methods',
    'saas_master_menu_payment_attempts',
    'saas_master_menu_margins',
    'saas_master_menu_payment_routing',
    'saas_master_menu_addons',
    'saas_master_menu_support_plans',
    'ir_cron_saas_wallet_expire_credits',
    'mail_template_saas_wallet_bonus_expiring',
)


def pre_init_hook(env):
    cr = env.cr
    cr.execute("""
        SELECT id, model, name FROM ir_model_data
        WHERE module = 'saas_core'
          AND (name ILIKE ANY(%s) OR name = ANY(%s))
    """, (list(_LIKE_PATTERNS), list(_EXPLICIT_NAMES)))
    rows = cr.fetchall()
    if not rows:
        _logger.info(
            "saas_billing pre_init_hook: nothing to re-parent from "
            "saas_core (fresh database with no prior saas_core install, "
            "or already re-parented).")
        return
    ids = [r[0] for r in rows]
    cr.execute(
        "UPDATE ir_model_data SET module = 'saas_billing' WHERE id = ANY(%s)",
        (ids,))
    _logger.info(
        "saas_billing pre_init_hook: re-parented %d ir_model_data row(s) "
        "from saas_core (%s).", len(ids),
        ', '.join(sorted({'%s:%s' % (m, n) for _i, m, n in rows[:5]})) + (
            ', ...' if len(rows) > 5 else ''))

