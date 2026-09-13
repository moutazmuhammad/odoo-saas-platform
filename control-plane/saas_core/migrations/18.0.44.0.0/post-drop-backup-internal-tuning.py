"""Remove the retired backup internal-tuning layer.

The "Internal tuning (cost & storage churn)" settings block and its
machinery are gone:

- Budget factor (footprint ceiling = provisioned storage × factor) and
  the upgrade nudge it drove — fields ``backup_over_budget`` /
  ``backup_upgrade_recommended``, the ``_cron_check_backup_budgets``
  cron, and the ``saas_master.backup_budget_factor`` parameter.
- Prune interval gating — restic ``--prune`` now runs with every
  nightly ``forget``; the ``last_restic_prune`` field and the
  ``saas_master.restic_prune_interval_days`` parameter are gone.

The cron record is deleted here (its data record would otherwise
linger and crash calling the removed method); the parameters were
settings-only (no data-file anchors).
"""


def migrate(cr, version):
    for col in ('backup_over_budget', 'backup_upgrade_recommended',
                'last_restic_prune'):
        cr.execute(
            'ALTER TABLE saas_instance DROP COLUMN IF EXISTS "%s"' % col
        )
    cr.execute(
        "DELETE FROM ir_config_parameter WHERE key IN %s",
        (('saas_master.backup_budget_factor',
          'saas_master.restic_prune_interval_days'),),
    )
    cr.execute(
        "DELETE FROM ir_cron WHERE id IN ("
        "  SELECT res_id FROM ir_model_data"
        "   WHERE module = 'saas_core'"
        "     AND name = 'ir_cron_saas_backup_budget_check'"
        "     AND model = 'ir.cron')"
    )
    cr.execute(
        "DELETE FROM ir_model_data WHERE module = 'saas_core' "
        "AND name = 'ir_cron_saas_backup_budget_check'"
    )
