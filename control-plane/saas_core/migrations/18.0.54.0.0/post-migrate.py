"""Cancellation cleanup flags collapse to one (18.0.54.0.0).

``pg_cleanup_pending`` / ``nginx_cleanup_pending`` (ssh_docker's separate
database / proxy teardown steps) are replaced by ``infra_cleanup_pending``:
on Kubernetes one CR delete removes the whole tenant. Carry any pending
flag over so the new hourly retry picks those instances up.
"""


def migrate(cr, version):
    cr.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'saas_instance' AND column_name = 'pg_cleanup_pending'
    """)
    if not cr.fetchone():
        return
    cr.execute("""
        UPDATE saas_instance SET infra_cleanup_pending = TRUE
        WHERE coalesce(pg_cleanup_pending, FALSE)
           OR coalesce(nginx_cleanup_pending, FALSE)
    """)
