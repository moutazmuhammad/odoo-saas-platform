"""Remove legacy ``saas_snapshot.*`` system parameters.

As of 18.0.14.0.0, snapshots and daily backups share a single bucket
configuration. The product snapshot helper (`saas.product._get_storage_config`)
reads from ``saas_backup.*`` directly, so the duplicate snapshot rows
are now dead weight. Drop them to keep the settings table tidy and
avoid future confusion about which set of values is canonical.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute(
        "DELETE FROM ir_config_parameter "
        "WHERE key LIKE 'saas_snapshot.%' RETURNING key"
    )
    rows = cr.fetchall()
    if rows:
        _logger.info(
            "Removed %d legacy saas_snapshot.* config parameters: %s",
            len(rows), ', '.join(r[0] for r in rows),
        )
