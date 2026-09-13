"""Pre-migrate 18.0.4.0.0 — convert port fields to integer and add
``max_backups`` to ``saas_plan``.

DEFENSIVE: only runs the type-conversion if the column is currently a
character-type. On a DB created at the new schema it's already integer
and the conversion is a no-op.
"""
import logging

_logger = logging.getLogger(__name__)


def _column_type(cr, table, column):
    cr.execute(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (table, column),
    )
    row = cr.fetchone()
    return row[0] if row else None


def migrate(cr, version):
    if not version:
        return

    _logger.info("saas_core: pre-migrate 18.0.4.0.0")

    for col in ('xmlrpc_port', 'longpolling_port'):
        col_type = _column_type(cr, 'saas_instance', col)
        if col_type is None:
            # Column doesn't exist yet — the ORM will create it as integer.
            continue
        if col_type in ('integer', 'bigint', 'smallint'):
            # Already migrated.
            continue
        # Char type — clean up garbage values and convert.
        _logger.info(
            "Converting saas_instance.%s from %s to integer", col, col_type,
        )
        cr.execute(
            "UPDATE saas_instance SET %s = NULL "
            "WHERE %s IS NOT NULL AND %s !~ '^[0-9]+$'" % (col, col, col)
        )
        cr.execute(
            "ALTER TABLE saas_instance "
            "ALTER COLUMN %s TYPE integer USING %s::integer" % (col, col)
        )

    # Add max_backups column to saas_plan with default value (idempotent).
    cr.execute(
        "ALTER TABLE saas_plan "
        "ADD COLUMN IF NOT EXISTS max_backups integer DEFAULT 7"
    )

    _logger.info("saas_core: pre-migrate 18.0.4.0.0 complete")
