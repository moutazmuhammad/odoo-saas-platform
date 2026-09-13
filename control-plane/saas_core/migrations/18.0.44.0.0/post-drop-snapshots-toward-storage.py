"""Remove the "count snapshots toward storage allowance" opt-in.

Snapshots are now billed per GB of their own deduplicated footprint
(usage-based add-on pricing), so counting any part of that footprint
against the plan's storage allowance would always double-charge the
customer — the opt-in (previously default OFF) no longer makes sense in
any configuration. ``total_storage_bytes`` is unconditionally
disk + database now.

The parameter was settings-only (no data-file anchor).
"""


def migrate(cr, version):
    cr.execute(
        "DELETE FROM ir_config_parameter "
        "WHERE key = 'saas_master.snapshots_count_toward_storage'"
    )
