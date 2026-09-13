"""18.0.47.0.0 — v47 billing refactor, PRE phase.

The wallet ledger schema changes from a flat signed-amount ledger to a
LOT-based model: ``saas.wallet.transaction`` gains a required ``kind``
column and new ``lot_id`` / ``credit_class`` columns, and balances now
derive from ``saas.wallet.lot`` rather than from the transaction sum.

To avoid (a) a NOT NULL failure when the new required ``kind`` column is
added to existing rows and (b) silent loss of customer balances, we:

  1. snapshot each wallet's CURRENT balance (sum of legacy transactions)
     into a temp table, then
  2. clear the legacy ledger rows so the new columns add cleanly.

The post-migration re-materialises each captured balance as a single
``customer_funded`` lot (the customer's own money — never expires), so no
balance is lost and nothing is double-counted.
"""


def migrate(cr, version):
    if not version:
        return  # fresh install — nothing to migrate

    # Only act if the legacy wallet table exists.
    cr.execute("""
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'saas_wallet_transaction'
    """)
    if not cr.fetchone():
        return

    # 1) Snapshot current balances (legacy balance = sum of amounts).
    cr.execute("DROP TABLE IF EXISTS saas_wallet_migrate_v47")
    cr.execute("""
        CREATE TABLE saas_wallet_migrate_v47 AS
        SELECT wallet_id, ROUND(SUM(amount)::numeric, 2) AS balance
        FROM saas_wallet_transaction
        WHERE wallet_id IS NOT NULL
        GROUP BY wallet_id
    """)

    # 2) Clear legacy ledger rows so the new required `kind` column and the
    #    new (move_id, lot_id, kind) unique index add without conflict.
    cr.execute("DELETE FROM saas_wallet_transaction")
