"""Full unification: every Services plan keeps a FIXED backup count.

Following the removal of the per-plan-size scaling, normalise EXISTING
Services plans to the same fixed retention as hosting
(``DEFAULT_MAX_BACKUPS``). Hosting plans are left untouched (their
retention is the fixed ``HOSTING_MAX_SNAPSHOTS`` constant and the
``max_backups`` field isn't consulted for them); trial plans are left at
0 (a constraint requires it).

Only the plan field is changed here — backups beyond the new limit are
pruned safely by the regular backup-cleanup cron on its next run, not by
this migration.
"""
import logging

from odoo.api import Environment, SUPERUSER_ID

from odoo.addons.saas_core.models.saas_instance_backup import DEFAULT_MAX_BACKUPS

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = Environment(cr, SUPERUSER_ID, {})
    changed = 0
    for plan in env['saas.plan'].search([('is_trial_plan', '=', False)]):
        if any(p.is_hosting for p in plan.saas_product_ids):
            continue  # hosting retention is the fixed HOSTING_MAX_SNAPSHOTS
        if plan.max_backups != DEFAULT_MAX_BACKUPS:
            plan.max_backups = DEFAULT_MAX_BACKUPS
            changed += 1
    _logger.info(
        "Unified %s Services plan(s) to fixed backup retention = %s",
        changed, DEFAULT_MAX_BACKUPS,
    )
