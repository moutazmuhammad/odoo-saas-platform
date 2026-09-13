"""Drop the now-redundant saas_plan.max_backups column.

Backup retention is fixed platform-wide, not a per-plan setting:

- Hosting keeps the last ``HOSTING_MAX_SNAPSHOTS`` snapshots; the plan
  field was never consulted for hosting.
- Services keeps the last ``DEFAULT_MAX_BACKUPS`` copies (every Services
  plan was already normalised to that value in 18.0.25.0.0).

The only signal the field still carried was "0 = backups disabled" for
trial plans, which is derivable from ``is_trial_plan`` directly. All
consumers (cleanup cron, auto-rotate on create) now use the fixed
``DEFAULT_MAX_BACKUPS`` constant, so the column is dead data.

The column was added in 18.0.4.0.0; it has no data-file anchor.
"""


def migrate(cr, version):
    cr.execute(
        'ALTER TABLE saas_plan DROP COLUMN IF EXISTS max_backups'
    )
