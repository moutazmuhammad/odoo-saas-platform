"""Drop columns of saas.instance fields removed in the unused-field sweep.

Removed fields (nothing ever read them — write-only or compute-only):
  - backup_count        (stored compute; no view/logic consumer)
  - disk_usage_bytes    (written by usage refresh, never read)
  - db_size_bytes       (written by usage refresh, never read)

``daily_backup_pending_state`` and ``daily_backup_monthly_price`` were
non-stored (related / compute) so they have no columns to drop.
"""


def migrate(cr, version):
    for col in ('backup_count', 'disk_usage_bytes', 'db_size_bytes'):
        cr.execute(
            'ALTER TABLE saas_instance DROP COLUMN IF EXISTS "%s"' % col
        )
