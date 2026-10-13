"""Execute P0.24 SQL on PostgreSQL's standalone backend (no sockets).

This validates real SQL and permissions. Authentication, the shell transport,
pg_dump/pg_restore and HTTP continuity require test_database_roles.py/live CI.
"""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

RESOURCES = Path(__file__).resolve().parents[1] / 'internal' / 'resources'
BIN = Path(subprocess.check_output(['pg_config', '--bindir'], text=True).strip())


@unittest.skipUnless((BIN / 'initdb').exists(), 'local PostgreSQL server binaries required')
class OfflineDatabaseRolesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='veltnex-p024-offline-')
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / 'data'
        subprocess.run([str(BIN / 'initdb'), '-D', str(self.data), '-U', 'odoo', '--auth=trust'],
                       check=True, capture_output=True)
        self.blocks = re.findall(r"<<'SQL'[^\n]*\n(.*?)\nSQL", (RESOURCES / 'database_roles.sh').read_text(), re.S)
        self.assertEqual(len(self.blocks), 7)
        self.values = dict(admin_password='test-admin', owner_password='test-owner', legacy_user='odoo', database='tenant_main')

    def sql(self, statement, db='postgres', role=None, error=False):
        if role:
            statement = f'SET SESSION AUTHORIZATION "{role}";\n\n' + statement
        result = subprocess.run([str(BIN / 'postgres'), '--single', '-D', str(self.data), '-j', db],
                                input=statement + '\n\n', text=True, capture_output=True)
        failed = result.returncode != 0 or 'ERROR:' in result.stderr or 'FATAL:' in result.stderr
        if error:
            self.assertTrue(failed, result.stdout + result.stderr)
        else:
            self.assertFalse(failed, result.stdout + result.stderr)
        return result.stdout

    def block(self, index, db='postgres', role=None):
        sql = self.blocks[index]
        for key, value in self.values.items():
            sql = sql.replace(":'" + key + "'", "'" + value.replace("'", "''") + "'")
            sql = sql.replace(':"' + key + '"', '"' + value.replace('"', '""') + '"')
        # psql's gexec executes each query result as a SQL statement. Do the
        # same in PL/pgSQL, retaining the production SELECT and its predicates.
        sql = re.sub(r'(?ms)^SELECT\b(.*?)\\gexec',
                     lambda match: 'DO $execute$ DECLARE command text; BEGIN FOR command IN SELECT'
                     + match.group(1) + ' LOOP EXECUTE command; END LOOP; END $execute$;', sql)
        return self.sql(sql, db, role)

    def migrate(self):
        self.block(0)
        self.block(1, role='veltnex_admin')
        self.block(2, 'template1', 'veltnex_admin')
        self.block(3, role='veltnex_admin')
        self.block(4, 'tenant_main', 'veltnex_admin')
        self.block(5, role='veltnex_admin')
        self.block(6, 'tenant_main', 'odoo_owner')

    def test_migration_preserves_data_and_excludes_system_catalogs(self):
        self.sql('CREATE DATABASE tenant_main;')
        self.sql("""CREATE TABLE sample(id serial PRIMARY KEY, value text);
INSERT INTO sample(value) VALUES ('preserved');
CREATE VIEW sample_view AS SELECT * FROM sample;
CREATE MATERIALIZED VIEW sample_mv AS SELECT * FROM sample;
CREATE FUNCTION sample_function() RETURNS text LANGUAGE sql AS $$ SELECT 'ok'::text $$;
CREATE TYPE status AS ENUM ('ok', 'failed');
CREATE DOMAIN positive_number AS int CHECK (VALUE > 0);""", 'tenant_main')
        self.migrate()
        self.assertIn('preserved', self.sql('SELECT value FROM sample;', 'tenant_main', 'odoo_owner'))
        self.sql("""DO $$ BEGIN
IF EXISTS (SELECT FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='pg_catalog' AND c.relowner='odoo_owner'::regrole) THEN
 RAISE EXCEPTION 'system catalog ownership leaked'; END IF;
IF EXISTS (SELECT FROM pg_roles WHERE rolname='odoo' AND (rolsuper OR rolcanlogin)) THEN
 RAISE EXCEPTION 'legacy role still usable'; END IF;
IF EXISTS (SELECT FROM pg_class WHERE relname IN ('sample','sample_id_seq','sample_view','sample_mv')
 AND relowner <> 'odoo_owner'::regrole) THEN RAISE EXCEPTION 'wrong object ownership'; END IF;
END $$;""", 'tenant_main', 'veltnex_admin')
        for command in ["COPY (SELECT 1) TO PROGRAM 'true';", 'SELECT * FROM pg_authid;',
                        "SELECT pg_read_file('/etc/passwd');", "ALTER SYSTEM SET work_mem='8MB';",
                        'CREATE ROLE forbidden;', 'SET ROLE veltnex_admin;', 'SET ROLE odoo;']:
            with self.subTest(command=command):
                self.sql(command, 'tenant_main', 'odoo_owner', error=True)
        self.sql('ALTER TABLE sample ADD COLUMN module_upgrade text;', 'tenant_main', 'odoo_owner')
        self.sql('CREATE DATABASE tenant_duplicate TEMPLATE tenant_main;', role='odoo_owner')
        self.sql('DROP DATABASE tenant_duplicate;', role='odoo_owner')
        # Retrying the idempotent SQL with the persisted admin must succeed.
        self.block(1, role='veltnex_admin')
        self.block(4, 'tenant_main', 'veltnex_admin')
        self.block(5, role='veltnex_admin')
        self.assertIn('preserved', self.sql('SELECT value FROM sample;', 'tenant_main', 'odoo_owner'))

    def test_external_audit_rejects_direct_and_inherited_privileges(self):
        self.block(0)
        self.block(1, role='veltnex_admin')
        self.sql('CREATE DATABASE tenant_main OWNER odoo_owner;', role='veltnex_admin')
        audit = re.search(r"<<'SQL'\n(.*?)\nSQL", (RESOURCES / 'database_audit.sh').read_text(), re.S).group(1)
        self.sql(audit, 'tenant_main', 'odoo_owner', error=True)  # CREATEDB forbidden on shared databases.
        self.sql('ALTER ROLE odoo_owner NOCREATEDB;', role='veltnex_admin')
        self.sql(audit, 'tenant_main', 'odoo_owner')
        for role in ['pg_read_server_files', 'pg_write_server_files', 'pg_execute_server_program', 'pg_read_all_data', 'veltnex_admin']:
            with self.subTest(role=role):
                self.sql(f'GRANT {role} TO odoo_owner;', role='veltnex_admin')
                self.sql(audit, 'tenant_main', 'odoo_owner', error=True)
                self.sql(f'REVOKE {role} FROM odoo_owner;', role='veltnex_admin')


if __name__ == '__main__':
    unittest.main()
