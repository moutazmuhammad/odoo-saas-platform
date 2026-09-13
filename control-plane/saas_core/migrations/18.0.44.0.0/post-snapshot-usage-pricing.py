"""Switch snapshot pricing to usage-based; retire the legacy model.

The snapshot add-on is now billed by the storage actually consumed
(rounded up to the next whole GB × ``saas_master.snapshot_price_per_gb``,
default $0.40/GB, 1 GB minimum). The old %-of-plan / flat model is
REMOVED, along with the grandfathering lock that referenced it:

1. Re-point the ``daily_snapshots`` saas.addon (noupdate) at the new
   per-GB param — only when still on the legacy param, so an operator's
   customisation survives.
2. Delete the legacy config parameters and the flat price's data anchor
   (it lived in a noupdate block — Odoo won't orphan-clean it).
3. Drop the ``backup_price_locked_until`` column: the locks written by
   the 18.0.22 migration capped at one backup-invoice cycle and have
   long expired; the flat price they locked to no longer exists.
"""


def migrate(cr, version):
    cr.execute(
        """UPDATE saas_addon
              SET price_config_param = 'saas_master.snapshot_price_per_gb',
                  description = 'Daily deduplicated full-instance snapshots '
                                '(restic), 7-day retention. Billed monthly by '
                                'used storage (rounded up to the next whole '
                                'GB, 1 GB minimum).'
            WHERE code = 'daily_snapshots'
              AND price_config_param = 'saas_master.hosting_daily_backup_price'
        """
    )
    cr.execute(
        "DELETE FROM ir_config_parameter WHERE key IN %s",
        (('saas_master.hosting_daily_backup_price',
          'saas_master.backup_price_pct',
          'saas_master.backup_price_min'),),
    )
    cr.execute(
        "DELETE FROM ir_model_data WHERE module = 'saas_core' "
        "AND name = 'config_param_hosting_daily_backup_price'"
    )
    cr.execute(
        'ALTER TABLE saas_instance '
        'DROP COLUMN IF EXISTS backup_price_locked_until'
    )
