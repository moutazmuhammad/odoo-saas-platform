"""Retire the per-plan-size Services backup scaling.

Services backup retention is now FIXED (= hosting's DEFAULT_MAX_BACKUPS),
not scaled between a min and a max by plan size. Drop the two settings
(saas_master.custom_plan_min_backups / _max_backups) — they lived in a
noupdate block, so Odoo won't orphan-clean them — and their
ir.model.data anchors.

Existing custom plans keep their current ``max_backups`` value on
purpose: lowering it would prune customers' existing backups on the next
cleanup cron. New custom plans are stamped with the fixed value by the
configurator.
"""


def migrate(cr, version):
    cr.execute(
        "DELETE FROM ir_config_parameter WHERE key IN %s",
        (('saas_master.custom_plan_min_backups',
          'saas_master.custom_plan_max_backups'),),
    )
    cr.execute(
        "DELETE FROM ir_model_data WHERE module = 'saas_core' AND name IN %s",
        (('config_param_custom_plan_min_backups',
          'config_param_custom_plan_max_backups'),),
    )
