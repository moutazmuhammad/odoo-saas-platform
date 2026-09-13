"""18.0.48.0.0 — snapshot add-on follows the subscription period.

The daily-backup add-on no longer has a standalone monthly billing cycle.
It now follows the subscription's period (monthly plan → monthly, yearly
plan → yearly, 12x up-front) and its charge is merged into the plan's
renewal invoice. To make existing subscribers consistent:

  * Align every enabled instance's ``daily_backup_next_invoice_date`` to its
    plan renewal date (``next_invoice_date``) so the next backup charge rides
    the next renewal instead of a now-removed standalone monthly invoice.
  * Drop the obsolete ``merge_snapshot_into_renewal_invoice`` toggle — merging
    is now intrinsic, not optional.

Idempotent: re-running only re-aligns dates (already-aligned rows are
unchanged) and re-deletes an already-absent config row.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # 1) Align the backup cycle to the plan renewal for every enabled
    #    instance that has an active billing cycle. Only move rows that are
    #    not already aligned (keeps the migration idempotent + cheap).
    cr.execute("""
        UPDATE saas_instance
           SET daily_backup_next_invoice_date = next_invoice_date
         WHERE daily_backup_enabled = TRUE
           AND next_invoice_date IS NOT NULL
           AND (daily_backup_next_invoice_date IS DISTINCT FROM next_invoice_date)
    """)
    if cr.rowcount:
        _logger.info(
            "v48: aligned daily_backup_next_invoice_date to the plan renewal "
            "for %d instance(s).", cr.rowcount)

    # 2) Drop the obsolete merge toggle (merging is now always-on).
    cr.execute("""
        DELETE FROM ir_config_parameter
         WHERE key = 'saas_master.merge_snapshot_into_renewal_invoice'
    """)
