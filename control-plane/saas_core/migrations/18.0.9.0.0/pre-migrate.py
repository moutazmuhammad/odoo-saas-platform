"""Migrate saas.plan → saas.product from Many2one to Many2many.

Creates the relay table and populates it from the old saas_product_id column
before the ORM drops it.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # Check if old Many2one column exists
    cr.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'saas_plan' AND column_name = 'saas_product_id'
    """)
    if not cr.fetchone():
        _logger.info("saas_plan.saas_product_id column not found — skipping.")
        return

    # Create the Many2many relay table
    cr.execute("""
        CREATE TABLE IF NOT EXISTS saas_plan_product_rel (
            plan_id    INTEGER NOT NULL REFERENCES saas_plan(id) ON DELETE CASCADE,
            product_id INTEGER NOT NULL REFERENCES saas_product(id) ON DELETE CASCADE,
            PRIMARY KEY (plan_id, product_id)
        )
    """)

    # Migrate existing Many2one data into the relay table
    cr.execute("""
        INSERT INTO saas_plan_product_rel (plan_id, product_id)
        SELECT id, saas_product_id
        FROM saas_plan
        WHERE saas_product_id IS NOT NULL
        ON CONFLICT DO NOTHING
    """)
    migrated = cr.rowcount
    _logger.info(
        "Migrated %d plan-product links from Many2one to Many2many.", migrated
    )

    # Drop the old column so the ORM doesn't complain
    cr.execute("ALTER TABLE saas_plan DROP COLUMN saas_product_id")
