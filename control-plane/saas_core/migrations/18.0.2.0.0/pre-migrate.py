"""Pre-migrate 18.0.2.0.0 — clean up old saas.odoo.module / saas.odoo.product
references and rename columns that were given clearer field names in 18.0.2.0.0.

DEFENSIVE: every operation is guarded by an existence check so the script
is a safe no-op on databases that never had the older schema (which is
the case for any DB created on or after 18.0.2.0.0).
"""
import logging

_logger = logging.getLogger(__name__)


def _table_exists(cr, table):
    cr.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
        (table,),
    )
    return bool(cr.fetchone())


def _column_exists(cr, table, column):
    cr.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (table, column),
    )
    return bool(cr.fetchone())


def migrate(cr, version):
    if not version:
        return

    _logger.info(
        "Pre-migration 18.0.2.0.0: cleaning old references and renaming columns"
    )

    # ------------------------------------------------------------------
    # 1. Null out product_id and module_id in instance module lines.
    #    These pointed to saas.odoo.product and saas.odoo.module respectively,
    #    but are now redefined as Many2one to product.product. The model
    #    itself was eventually removed; if the table is gone there's
    #    nothing to update.
    # ------------------------------------------------------------------
    if _table_exists(cr, 'saas_instance_module_line'):
        if _column_exists(cr, 'saas_instance_module_line', 'product_id'):
            cr.execute(
                "UPDATE saas_instance_module_line SET product_id = NULL"
            )
        if _column_exists(cr, 'saas_instance_module_line', 'module_id'):
            cr.execute(
                "UPDATE saas_instance_module_line SET module_id = NULL"
            )

        # Drop old FK constraints (recreated by the ORM with the new types).
        cr.execute(
            "ALTER TABLE saas_instance_module_line "
            "DROP CONSTRAINT IF EXISTS saas_instance_module_line_product_id_fkey"
        )
        cr.execute(
            "ALTER TABLE saas_instance_module_line "
            "DROP CONSTRAINT IF EXISTS saas_instance_module_line_module_id_fkey"
        )

    # ------------------------------------------------------------------
    # 2. Drop the old many2many rel table for installed modules.
    # ------------------------------------------------------------------
    cr.execute("DROP TABLE IF EXISTS saas_instance_installed_module_rel")

    # ------------------------------------------------------------------
    # 3. Rename columns on saas_instance for clearer field names.
    #    Each rename is conditional: it only fires if the OLD column
    #    exists AND the NEW column does not (so re-running the migration
    #    is safe).
    # ------------------------------------------------------------------
    column_renames = [
        ('based_domain_id', 'domain_id'),
        ('container_physical_server_id', 'docker_server_id'),
        ('psql_physical_server_id', 'db_server_id'),
        ('admin_passwd', 'admin_password'),
    ]
    if _table_exists(cr, 'saas_instance'):
        for old_col, new_col in column_renames:
            if _column_exists(cr, 'saas_instance', old_col) and \
                    not _column_exists(cr, 'saas_instance', new_col):
                _logger.info(
                    "Renaming saas_instance.%s -> %s", old_col, new_col,
                )
                cr.execute(
                    'ALTER TABLE saas_instance '
                    'RENAME COLUMN "%s" TO "%s"' % (old_col, new_col)
                )

        # ------------------------------------------------------------------
        # 4. Drop SQL constraints that reference old column names.
        # ------------------------------------------------------------------
        cr.execute(
            "SELECT constraint_name FROM information_schema.table_constraints "
            "WHERE table_name = 'saas_instance' "
            "  AND constraint_name LIKE %s",
            ('%container_physical_server%',),
        )
        for row in cr.fetchall():
            cr.execute(
                'ALTER TABLE saas_instance '
                'DROP CONSTRAINT IF EXISTS "%s"' % row[0]
            )

    _logger.info("Pre-migration 18.0.2.0.0: completed")
