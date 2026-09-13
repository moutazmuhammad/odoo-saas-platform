"""Retire the fixed restoration fee + retention surcharge settings.

Both are replaced by ONE computed charge
(``saas.instance._get_retained_snapshot_fee``): months the retained
snapshot sat in cloud storage after the instance was deleted × its size
rounded UP to the next whole GB × ``saas_master.snapshot_price_per_gb``.

- ``saas_master.data_restoration_fee`` (fixed restore fee) — gone; the
  restoration invoice now defaults to the computed amount.
- ``saas_master.hosting_snapshot_retention_surcharge`` and the
  ``pending_retention_surcharge`` flag — gone; retention storage cost is
  inside the computed fee instead of a separate line on the first
  daily-backup activation invoice.

Both parameters were settings-only (no data-file anchors).
"""


def migrate(cr, version):
    cr.execute(
        'ALTER TABLE saas_instance '
        'DROP COLUMN IF EXISTS pending_retention_surcharge'
    )
    cr.execute(
        "DELETE FROM ir_config_parameter WHERE key IN %s",
        (('saas_master.data_restoration_fee',
          'saas_master.hosting_snapshot_retention_surcharge'),),
    )
