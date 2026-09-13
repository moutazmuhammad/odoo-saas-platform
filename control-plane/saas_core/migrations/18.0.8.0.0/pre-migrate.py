"""Pre-migrate 18.0.8.0.0 — merge saas_container_physical_server,
saas_psql_physical_server, and saas_proxy_server into the unified
saas_server table.

DEFENSIVE: every operation is guarded so the script is a safe no-op on
DBs that never had the older tables (i.e. installs created on or after
18.0.8.0.0).
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

    # If none of the legacy tables exist, this DB never had the old
    # split-server schema — nothing to migrate. Bail out early.
    legacy_tables = (
        'saas_container_physical_server',
        'saas_psql_physical_server',
        'saas_proxy_server',
    )
    if not any(_table_exists(cr, t) for t in legacy_tables):
        _logger.info(
            "Pre-migration 18.0.8.0.0: no legacy server tables found, "
            "skipping unification (already on unified schema)."
        )
        return

    _logger.info(
        "Pre-migration 18.0.8.0.0: unifying server models into saas_server"
    )

    # ------------------------------------------------------------------
    # 1. Create the new saas_server table.
    # ------------------------------------------------------------------
    cr.execute("""
        CREATE TABLE IF NOT EXISTS saas_server (
            id SERIAL PRIMARY KEY,
            sequence INTEGER DEFAULT 10,
            name VARCHAR NOT NULL,
            is_docker_host BOOLEAN DEFAULT FALSE,
            is_db_server BOOLEAN DEFAULT FALSE,
            is_proxy_server BOOLEAN DEFAULT FALSE,
            ssh_key_pair_id INTEGER,
            ssh_user VARCHAR DEFAULT 'root',
            ssh_port INTEGER DEFAULT 22,
            ip_v4 VARCHAR,
            private_ip_v4 VARCHAR,
            ssh_connect_using VARCHAR DEFAULT 'public_ip',
            docker_base_path VARCHAR DEFAULT '/home/odoo',
            psql_port INTEGER DEFAULT 5432,
            create_uid INTEGER,
            create_date TIMESTAMP,
            write_uid INTEGER,
            write_date TIMESTAMP
        )
    """)

    # ------------------------------------------------------------------
    # 2. Migrate Docker servers.
    # ------------------------------------------------------------------
    if _table_exists(cr, 'saas_container_physical_server'):
        _logger.info("Migrating Docker servers...")
        # ON CONFLICT DO NOTHING in case rows with the same id already
        # exist (re-running the migration must be safe).
        cr.execute("""
            INSERT INTO saas_server (
                id, sequence, name, is_docker_host,
                ssh_key_pair_id, ssh_user, ssh_port,
                ip_v4, private_ip_v4, ssh_connect_using,
                docker_base_path,
                create_uid, create_date, write_uid, write_date
            )
            SELECT
                id, sequence, name, TRUE,
                ssh_key_pair_id, ssh_user, ssh_port,
                ip_v4, private_ip_v4, ssh_connect_using,
                docker_base_path,
                create_uid, create_date, write_uid, write_date
            FROM saas_container_physical_server
            ON CONFLICT (id) DO NOTHING
        """)

    # ------------------------------------------------------------------
    # 3. Migrate DB servers — merge with existing if same IP.
    # ------------------------------------------------------------------
    if _table_exists(cr, 'saas_psql_physical_server'):
        _logger.info("Migrating DB servers...")
        cr.execute("""
            UPDATE saas_server ss
            SET is_db_server = TRUE,
                psql_port = ps.psql_port
            FROM saas_psql_physical_server ps
            WHERE (
                (ss.ip_v4 IS NOT NULL AND ss.ip_v4 = ps.ip_v4)
                OR (ss.private_ip_v4 IS NOT NULL AND ss.private_ip_v4 = ps.private_ip_v4)
            )
        """)
        cr.execute("""
            INSERT INTO saas_server (
                sequence, name, is_db_server,
                ssh_key_pair_id, ssh_user, ssh_port,
                ip_v4, private_ip_v4, ssh_connect_using,
                psql_port,
                create_uid, create_date, write_uid, write_date
            )
            SELECT
                ps.sequence, ps.name, TRUE,
                ps.ssh_key_pair_id, ps.ssh_user, ps.ssh_port,
                ps.ip_v4, ps.private_ip_v4, ps.ssh_connect_using,
                ps.psql_port,
                ps.create_uid, ps.create_date, ps.write_uid, ps.write_date
            FROM saas_psql_physical_server ps
            WHERE NOT EXISTS (
                SELECT 1 FROM saas_server ss
                WHERE (
                    (ss.ip_v4 IS NOT NULL AND ss.ip_v4 = ps.ip_v4)
                    OR (ss.private_ip_v4 IS NOT NULL AND ss.private_ip_v4 = ps.private_ip_v4)
                )
            )
        """)

    # ------------------------------------------------------------------
    # 4. Migrate Proxy servers — merge with existing if same IP.
    # ------------------------------------------------------------------
    if _table_exists(cr, 'saas_proxy_server'):
        _logger.info("Migrating Proxy servers...")
        cr.execute("""
            UPDATE saas_server ss
            SET is_proxy_server = TRUE
            FROM saas_proxy_server pxs
            WHERE (
                (ss.ip_v4 IS NOT NULL AND ss.ip_v4 = pxs.ip_v4)
                OR (ss.private_ip_v4 IS NOT NULL AND ss.private_ip_v4 = pxs.private_ip_v4)
            )
        """)
        cr.execute("""
            INSERT INTO saas_server (
                sequence, name, is_proxy_server,
                ssh_key_pair_id, ssh_user, ssh_port,
                ip_v4, private_ip_v4, ssh_connect_using,
                create_uid, create_date, write_uid, write_date
            )
            SELECT
                pxs.sequence, pxs.name, TRUE,
                pxs.ssh_key_pair_id, pxs.ssh_user, pxs.ssh_port,
                pxs.ip_v4, pxs.private_ip_v4, pxs.ssh_connect_using,
                pxs.create_uid, pxs.create_date, pxs.write_uid, pxs.write_date
            FROM saas_proxy_server pxs
            WHERE NOT EXISTS (
                SELECT 1 FROM saas_server ss
                WHERE (
                    (ss.ip_v4 IS NOT NULL AND ss.ip_v4 = pxs.ip_v4)
                    OR (ss.private_ip_v4 IS NOT NULL AND ss.private_ip_v4 = pxs.private_ip_v4)
                )
            )
        """)

    # ------------------------------------------------------------------
    # 5. Fix sequence value for saas_server id.
    # ------------------------------------------------------------------
    cr.execute("""
        SELECT setval('saas_server_id_seq',
                       COALESCE((SELECT MAX(id) FROM saas_server), 0) + 1,
                       false)
    """)

    # ------------------------------------------------------------------
    # 6. Re-point db_server_id on saas_instance.
    # ------------------------------------------------------------------
    if _column_exists(cr, 'saas_instance', 'db_server_id') \
            and _table_exists(cr, 'saas_psql_physical_server'):
        _logger.info("Re-mapping db_server_id on saas_instance...")
        cr.execute("""
            UPDATE saas_instance si
            SET db_server_id = ss.id
            FROM saas_psql_physical_server ps
            JOIN saas_server ss ON (
                ss.is_db_server = TRUE
                AND (
                    (ss.ip_v4 IS NOT NULL AND ss.ip_v4 = ps.ip_v4)
                    OR (ss.private_ip_v4 IS NOT NULL AND ss.private_ip_v4 = ps.private_ip_v4)
                )
            )
            WHERE si.db_server_id = ps.id
        """)

    # ------------------------------------------------------------------
    # 7. Re-point proxy_server_id on saas_based_domain.
    # ------------------------------------------------------------------
    if _column_exists(cr, 'saas_based_domain', 'proxy_server_id') \
            and _table_exists(cr, 'saas_proxy_server'):
        _logger.info("Re-mapping proxy_server_id on saas_based_domain...")
        cr.execute("""
            UPDATE saas_based_domain bd
            SET proxy_server_id = ss.id
            FROM saas_proxy_server pxs
            JOIN saas_server ss ON (
                ss.is_proxy_server = TRUE
                AND (
                    (ss.ip_v4 IS NOT NULL AND ss.ip_v4 = pxs.ip_v4)
                    OR (ss.private_ip_v4 IS NOT NULL AND ss.private_ip_v4 = pxs.private_ip_v4)
                )
            )
            WHERE bd.proxy_server_id = pxs.id
        """)

    # ------------------------------------------------------------------
    # 8. Drop FK constraints that reference the old tables.
    # ------------------------------------------------------------------
    if _table_exists(cr, 'saas_docker_container'):
        cr.execute("""
            SELECT constraint_name FROM information_schema.table_constraints
            WHERE table_name = 'saas_docker_container'
              AND constraint_type = 'FOREIGN KEY'
              AND constraint_name LIKE %s
        """, ('%server_id%',))
        for row in cr.fetchall():
            cr.execute(
                'ALTER TABLE saas_docker_container '
                'DROP CONSTRAINT IF EXISTS "%s"' % row[0]
            )

    if _table_exists(cr, 'saas_instance'):
        for like in ('%docker_server_id%', '%db_server_id%'):
            cr.execute("""
                SELECT constraint_name FROM information_schema.table_constraints
                WHERE table_name = 'saas_instance'
                  AND constraint_type = 'FOREIGN KEY'
                  AND constraint_name LIKE %s
            """, (like,))
            for row in cr.fetchall():
                cr.execute(
                    'ALTER TABLE saas_instance '
                    'DROP CONSTRAINT IF EXISTS "%s"' % row[0]
                )

    if _table_exists(cr, 'saas_based_domain'):
        cr.execute("""
            SELECT constraint_name FROM information_schema.table_constraints
            WHERE table_name = 'saas_based_domain'
              AND constraint_type = 'FOREIGN KEY'
              AND constraint_name LIKE %s
        """, ('%proxy_server_id%',))
        for row in cr.fetchall():
            cr.execute(
                'ALTER TABLE saas_based_domain '
                'DROP CONSTRAINT IF EXISTS "%s"' % row[0]
            )

    _logger.info("Pre-migration 18.0.8.0.0: completed")
