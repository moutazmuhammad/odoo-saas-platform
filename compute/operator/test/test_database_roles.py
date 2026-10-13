"""P0.24 integration checks against disposable local PostgreSQL clusters.

Run: python3 -m unittest discover -s compute/operator/test -p test_database_roles.py -v
No Docker, Kubernetes credentials or existing databases are used.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

RESOURCES = Path(__file__).resolve().parents[1] / 'internal' / 'resources'
BIN = Path(subprocess.check_output(['pg_config', '--bindir'], text=True).strip())


@unittest.skipUnless((BIN / 'initdb').exists(), 'local PostgreSQL server binaries required')
class DatabaseRolesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='veltnex-p024-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = dict(os.environ, PGHOST=str(self.root), PGPORT='15439',
                        PGUSER='odoo', PGPASSWORD='legacy-test-password')
        pw = self.root / 'password'
        pw.write_text(self.env['PGPASSWORD'])
        pw.chmod(0o600)
        self.run_pg('initdb', '-D', str(self.root / 'data'), '-U', 'odoo',
                    '--auth=scram-sha-256', '--pwfile', str(pw))
        self.run_pg('pg_ctl', '-D', str(self.root / 'data'), '-l', str(self.root / 'log'),
                    '-o', f"-h '' -k {self.root} -p 15439", '-w', 'start')
        self.addCleanup(lambda: self.run_pg('pg_ctl', '-D', str(self.root / 'data'), '-m', 'immediate', '-w', 'stop'))

    def run_pg(self, command, *args, env=None, sql=None, check=True):
        result = subprocess.run([str(BIN / command), *args], env=env or self.env,
                                input=sql, text=True, capture_output=True)
        if check and result.returncode:
            log = (self.root / 'log').read_text() if (self.root / 'log').exists() else ''
            self.fail(f'{command} failed: {result.stderr}\n{result.stdout}\n{log}')
        return result

    def sql(self, sql, env=None, db='postgres', check=True):
        return self.run_pg('psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-d', db,
                           env=env, sql=sql, check=check)

    def migrate(self, legacy='odoo'):
        env = dict(self.env, DB_HOST=str(self.root), DB_PORT='15439', DB_NAME='tenant_main',
                   ADMIN_PASSWORD='admin-test-password', OWNER_PASSWORD='owner-test-password',
                   LEGACY_USER=legacy, LEGACY_PASSWORD=self.env['PGPASSWORD'])
        if legacy == 'veltnex_admin':
            env['LEGACY_PASSWORD'] = env['ADMIN_PASSWORD']
        result = subprocess.run(['sh', str(RESOURCES / 'database_roles.sh')],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.owner = dict(self.env, PGUSER='odoo_owner', PGPASSWORD='owner-test-password')
        self.admin = dict(self.env, PGUSER='veltnex_admin', PGPASSWORD='admin-test-password')

    def test_existing_data_privileges_restore_and_retry(self):
        self.sql('CREATE DATABASE tenant_main; CREATE DATABASE tenant_extra;')
        self.sql("""CREATE TABLE sample(id serial PRIMARY KEY, value text);
                    INSERT INTO sample(value) VALUES ('preserved');
                    CREATE VIEW sample_view AS SELECT * FROM sample;
                    CREATE MATERIALIZED VIEW sample_mv AS SELECT * FROM sample;
                    CREATE FUNCTION sample_function() RETURNS text LANGUAGE sql AS $$ SELECT 'ok'::text $$;""",
                 db='tenant_main')
        self.migrate()
        self.assertEqual(self.sql('SELECT value FROM sample', self.owner, 'tenant_main').stdout.strip(), 'preserved')
        self.assertEqual(self.sql("SELECT count(*) FROM pg_class WHERE relname IN ('sample', 'sample_id_seq', 'sample_view', 'sample_mv') AND relowner = 'odoo_owner'::regrole", self.admin, 'tenant_main').stdout.strip(), '4')
        self.assertEqual(self.sql("SELECT proowner::regrole FROM pg_proc WHERE proname = 'sample_function'", self.admin, 'tenant_main').stdout.strip(), 'odoo_owner')
        self.assertEqual(self.sql("SELECT rolsuper, rolcreaterole, rolreplication, rolbypassrls, rolcreatedb FROM pg_roles WHERE rolname=current_user", self.owner).stdout.strip(), 'f|f|f|f|t')
        for sql in ["COPY (SELECT 1) TO PROGRAM 'true'", "SELECT pg_read_file('/etc/passwd')",
                    "ALTER SYSTEM SET work_mem='8MB'", 'CREATE ROLE forbidden',
                    'SET ROLE veltnex_admin', 'SET ROLE pg_read_server_files', 'SELECT * FROM pg_authid']:
            with self.subTest(sql=sql):
                self.assertNotEqual(self.sql(sql, self.owner, 'tenant_main', check=False).returncode, 0)
        self.assertNotEqual(self.sql('SELECT 1', check=False).returncode, 0, 'legacy password still authenticates')
        # Odoo database-manager create/duplicate/drop on a dedicated server.
        self.sql('CREATE DATABASE tenant_copy TEMPLATE tenant_main; DROP DATABASE tenant_copy;', self.owner)
        self.sql('CREATE DATABASE tenant_new;', self.owner)
        self.assertEqual(self.sql("SELECT count(*) FROM pg_extension WHERE extname IN ('unaccent','pg_trgm')", self.owner, 'tenant_new').stdout.strip(), '2')
        # The dump and restore run with the limited application role.
        dump = self.root / 'database.dump'
        self.run_pg('pg_dump', '-Fc', '-f', str(dump), '-d', 'tenant_main', env=self.owner)
        self.sql('DROP DATABASE tenant_extra WITH (FORCE); CREATE DATABASE tenant_extra;', self.owner)
        self.run_pg('pg_restore', '--no-owner', '--role=odoo_owner', '--single-transaction', '--exit-on-error',
                    '-d', 'tenant_extra', str(dump), env=self.owner)
        self.assertEqual(self.sql('SELECT value FROM sample', self.owner, 'tenant_extra').stdout.strip(), 'preserved')
        # A retry after completion uses the new admin and retains data/passwords.
        self.migrate()
        self.assertEqual(self.sql('SELECT value FROM sample', self.owner, 'tenant_main').stdout.strip(), 'preserved')

    def test_fresh_database_and_external_audit(self):
        # Model a fresh official postgres image bootstrapped as veltnex_admin.
        self.sql("CREATE ROLE veltnex_admin SUPERUSER LOGIN PASSWORD 'admin-test-password';")
        self.migrate('veltnex_admin')
        self.sql('CREATE TABLE install_base(id serial PRIMARY KEY);', self.owner, 'tenant_main')
        self.sql('ALTER TABLE install_base ADD COLUMN module_upgrade text;', self.owner, 'tenant_main')
        audit_env = dict(self.owner, DB_NAME='tenant_main')
        script = ['sh', str(RESOURCES / 'database_audit.sh')]
        # CREATEDB is rejected for external/shared logins.
        self.assertNotEqual(subprocess.run(script, env=audit_env, capture_output=True).returncode, 0)
        self.sql('ALTER ROLE odoo_owner NOCREATEDB;', self.admin)
        self.assertEqual(subprocess.run(script, env=audit_env, capture_output=True).returncode, 0)
        self.sql('GRANT pg_read_server_files TO odoo_owner;', self.admin)
        self.assertNotEqual(subprocess.run(script, env=audit_env, capture_output=True).returncode, 0)
        self.sql('REVOKE pg_read_server_files FROM odoo_owner; GRANT veltnex_admin TO odoo_owner;', self.admin)
        self.assertNotEqual(subprocess.run(script, env=audit_env, capture_output=True).returncode, 0)


if __name__ == '__main__':
    unittest.main()
