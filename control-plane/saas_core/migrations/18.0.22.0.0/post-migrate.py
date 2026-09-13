"""Grandfather existing daily-backup subscribers (P4).

P4 lets the daily-backup add-on switch to storage-based pricing. To avoid
a surprise jump on a LIVE recurring charge, every instance that already
has daily backups enabled gets its backup price locked at the current flat
amount until its next backup-invoice date. While locked,
``_get_daily_backup_price`` returns the flat settings price regardless of
the add-on's mode; new activations price with the current model
immediately.

Lock date = the instance's ``daily_backup_next_invoice_date`` when known,
else ~1 month out (so the customer keeps the old price for at least one
more cycle and is billed the new amount only from the following cycle).

Idempotent: only fills a NULL lock, so re-running won't extend an existing
lock or stomp an operator override.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    # Lock = next backup invoice date, or one month from today as a floor.
    cr.execute(
        """
        UPDATE saas_instance
           SET backup_price_locked_until =
               COALESCE(daily_backup_next_invoice_date,
                        (NOW() AT TIME ZONE 'UTC')::date + INTERVAL '1 month')
         WHERE daily_backup_enabled = TRUE
           AND backup_price_locked_until IS NULL
        """
    )
    _logger.info(
        "P4 grandfathering: locked backup price for %d existing daily-backup "
        "subscriber(s).", cr.rowcount,
    )
