"""18.0.47.0.0 — v47 billing refactor, POST phase.

  * Re-materialise each pre-captured wallet balance as ONE customer_funded
    lot (never expires) so no customer money is lost in the schema change.
  * Drop the removed promotion / overage columns from saas_instance.
  * Seed the storage capacity grace period (configurable) if unset.

Idempotent and zero-loss: re-running only acts on the temp table, which is
dropped at the end.
"""
import logging

_logger = logging.getLogger(__name__)

# Columns removed in v47 (promotions + the old per-threshold notice marker).
_DROPPED_INSTANCE_COLS = (
    'trial_promo_pct',
    'trial_promo_cycles_remaining',
    'trial_promo_total_saved',
    'storage_notice_level',
)


def migrate(cr, version):
    if not version:
        return

    from odoo import api, SUPERUSER_ID
    env = api.Environment(cr, SUPERUSER_ID, {})

    # 1) Rebuild wallet balances as customer_funded lots.
    cr.execute("""
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'saas_wallet_migrate_v47'
    """)
    if cr.fetchone():
        cr.execute("SELECT wallet_id, balance FROM saas_wallet_migrate_v47 "
                   "WHERE balance > 0")
        for wallet_id, balance in cr.fetchall():
            wallet = env['saas.wallet'].browse(wallet_id)
            if not wallet.exists():
                continue
            wallet._credit(
                float(balance), origin='migration',
                reason='Balance migrated to v47 (your money — never expires)')
            _logger.info("v47: migrated wallet %s balance %.2f to a "
                         "customer_funded lot.", wallet_id, balance)
        cr.execute("DROP TABLE IF EXISTS saas_wallet_migrate_v47")

    # 2) Drop removed promo / overage columns (orphans are harmless, but the
    #    business asked for full removal).
    for col in _DROPPED_INSTANCE_COLS:
        cr.execute(
            "ALTER TABLE saas_instance DROP COLUMN IF EXISTS %s" % col)

    # 3) Seed the storage capacity grace period if the operator never set one.
    ICP = env['ir.config_parameter'].sudo()
    if not ICP.get_param('saas_master.storage_grace_days'):
        ICP.set_param('saas_master.storage_grace_days', '7')
        _logger.info("v47: seeded storage_grace_days=7.")
