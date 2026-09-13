"""Seed the daily-backup add-on price on existing installs.

The data file at ``data/ir_config_parameter.xml`` is ``noupdate=1``, so
adding the row there only helps fresh installs. Sites that upgrade from
an earlier version need a one-shot write here, otherwise customers
hitting Enable Daily Backups see the "pricing isn't configured" error
until the operator opens Settings and saves.

We only write when the parameter is missing or zero so a deliberate
operator override (e.g. a different monthly price) is preserved.
"""
import logging

_logger = logging.getLogger(__name__)

DEFAULT_PRICE = '5.0'
KEY = 'saas_master.hosting_daily_backup_price'


def migrate(cr, version):
    cr.execute(
        "SELECT value FROM ir_config_parameter WHERE key = %s",
        (KEY,),
    )
    row = cr.fetchone()
    current = (row[0] if row else '') or ''
    try:
        is_zero = float(current) <= 0.0
    except (TypeError, ValueError):
        is_zero = True

    if not row:
        cr.execute(
            "INSERT INTO ir_config_parameter (key, value, create_uid, "
            "create_date, write_uid, write_date) "
            "VALUES (%s, %s, 1, NOW() AT TIME ZONE 'UTC', 1, "
            "NOW() AT TIME ZONE 'UTC')",
            (KEY, DEFAULT_PRICE),
        )
        _logger.info("Seeded %s = %s (was missing)", KEY, DEFAULT_PRICE)
    elif is_zero:
        cr.execute(
            "UPDATE ir_config_parameter SET value = %s, "
            "write_date = NOW() AT TIME ZONE 'UTC' WHERE key = %s",
            (DEFAULT_PRICE, KEY),
        )
        _logger.info("Updated %s to %s (was %r)", KEY, DEFAULT_PRICE, current)
    else:
        _logger.info("%s already set to %r — leaving as is", KEY, current)
