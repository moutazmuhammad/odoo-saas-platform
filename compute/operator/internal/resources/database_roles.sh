#!/bin/sh
# Platform-only job. Never mount these credentials into an Odoo container.
set -eu
export PGCONNECT_TIMEOUT=10
export PGHOST="$DB_HOST" PGPORT="$DB_PORT" PGUSER=veltnex_admin PGPASSWORD="$ADMIN_PASSWORD"
if ! psql -X -qAt -d postgres -c 'SELECT 1' >/dev/null 2>&1; then
  # Existing volumes retain the old bootstrap login. Create the new admin
  # before revoking it; retries prefer the new login if creation succeeded.
  PGUSER="$LEGACY_USER" PGPASSWORD="$LEGACY_PASSWORD" psql -X -q -v ON_ERROR_STOP=1 \
    -v admin_password="$ADMIN_PASSWORD" -d postgres <<'SQL'
SET log_statement = 'none';
SELECT format('CREATE ROLE veltnex_admin LOGIN SUPERUSER PASSWORD %L', :'admin_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'veltnex_admin') \gexec
SQL
fi
psql -X -q -v ON_ERROR_STOP=1 -v owner_password="$OWNER_PASSWORD" -d postgres <<'SQL'
SET log_statement = 'none';
SELECT 'CREATE ROLE odoo_owner' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'odoo_owner') \gexec
ALTER ROLE odoo_owner LOGIN NOSUPERUSER CREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'owner_password';
-- Remove inherited privileges, including predefined file/execute roles.
SELECT format('REVOKE %I FROM odoo_owner', r.rolname)
FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid
WHERE m.member = 'odoo_owner'::regrole \gexec
SQL
# Extensions must be available to future CREATE DATABASE operations too.
psql -X -q -v ON_ERROR_STOP=1 -d template1 <<'SQL'
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
SQL
# Names are opaque platform identifiers; refuse line breaks before shell iteration.
psql -X -qAt -v ON_ERROR_STOP=1 -d postgres <<'SQL' >/dev/null
DO $$ BEGIN
IF EXISTS (SELECT FROM pg_database WHERE datname !~ '^[a-zA-Z0-9_.-]+$') THEN
  RAISE EXCEPTION 'unsupported database name; expected an opaque platform identifier';
END IF;
END $$;
SQL
databases=$(psql -X -qAt -v ON_ERROR_STOP=1 -d postgres -c "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres'")
printf '%s\n' "$databases" |
while IFS= read -r database; do
  [ -n "$database" ] || continue
  psql -X -q -v ON_ERROR_STOP=1 -v legacy_user="$LEGACY_USER" -v database="$database" -d "$database" <<'SQL'
BEGIN;
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
-- The old login is initdb's bootstrap role: it owns pg_catalog too.
-- Never REASSIGN OWNED wholesale, which would hand system catalogs to Odoo.
SELECT format('ALTER SCHEMA %I OWNER TO odoo_owner', nspname)
FROM pg_namespace WHERE nspowner = (SELECT oid FROM pg_roles WHERE rolname = :'legacy_user')
 AND nspname <> 'information_schema' AND nspname !~ '^pg_' \gexec
SELECT format('ALTER %s %I.%I OWNER TO odoo_owner',
 CASE c.relkind WHEN 'S' THEN 'SEQUENCE' WHEN 'v' THEN 'VIEW'
 WHEN 'm' THEN 'MATERIALIZED VIEW' WHEN 'f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END,
 n.nspname, c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relowner = (SELECT oid FROM pg_roles WHERE rolname = :'legacy_user')
 AND c.relkind IN ('r','p','S','v','m','f')
 AND n.nspname <> 'information_schema' AND n.nspname !~ '^pg_'
ORDER BY CASE WHEN c.relkind = 'S' THEN 1 ELSE 0 END \gexec
SELECT format('ALTER %s %I.%I(%s) OWNER TO odoo_owner',
 CASE p.prokind WHEN 'p' THEN 'PROCEDURE' WHEN 'a' THEN 'AGGREGATE' ELSE 'FUNCTION' END,
 n.nspname, p.proname, pg_get_function_identity_arguments(p.oid))
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.proowner = (SELECT oid FROM pg_roles WHERE rolname = :'legacy_user')
 AND n.nspname <> 'information_schema' AND n.nspname !~ '^pg_'
 AND NOT EXISTS (SELECT FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass
  AND d.objid = p.oid AND d.deptype = 'e') \gexec
SELECT format('ALTER %s %I.%I OWNER TO odoo_owner',
 CASE WHEN t.typtype = 'd' THEN 'DOMAIN' ELSE 'TYPE' END, n.nspname, t.typname)
FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
LEFT JOIN pg_class c ON c.oid = t.typrelid
WHERE t.typowner = (SELECT oid FROM pg_roles WHERE rolname = :'legacy_user')
 AND (t.typrelid = 0 OR c.relkind = 'c')
 AND NOT EXISTS (SELECT FROM pg_type base WHERE base.typarray = t.oid)
 AND n.nspname <> 'information_schema' AND n.nspname !~ '^pg_'
 AND NOT EXISTS (SELECT FROM pg_depend d WHERE d.classid = 'pg_type'::regclass
  AND d.objid = t.oid AND d.deptype = 'e') \gexec
ALTER DATABASE :"database" OWNER TO odoo_owner;
ALTER SCHEMA public OWNER TO odoo_owner;
COMMIT;
SQL
done
psql -X -q -v ON_ERROR_STOP=1 -v database="$DB_NAME" -v legacy_user="$LEGACY_USER" -d postgres <<'SQL'
SELECT format('CREATE DATABASE %I OWNER odoo_owner', :'database')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'database') \gexec
-- Keep system databases admin-owned.
ALTER DATABASE postgres OWNER TO veltnex_admin;
ALTER DATABASE template0 OWNER TO veltnex_admin;
ALTER DATABASE template1 OWNER TO veltnex_admin;
-- The initdb bootstrap role owns system catalogs even after NOSUPERUSER.
-- Revoke login AND terminate its sessions; never grant it to the application.
-- Existing instances require a maintenance window for this cutover.
SELECT format('REVOKE %I FROM %I', r.rolname, :'legacy_user')
FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid
JOIN pg_roles member ON member.oid = m.member
WHERE member.rolname = :'legacy_user' AND :'legacy_user' NOT IN ('veltnex_admin', 'odoo_owner') \gexec
SELECT format('ALTER ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD NULL', :'legacy_user')
WHERE :'legacy_user' NOT IN ('veltnex_admin', 'odoo_owner') \gexec
SELECT pg_terminate_backend(pid) FROM pg_stat_activity
WHERE usename = :'legacy_user' AND :'legacy_user' NOT IN ('veltnex_admin', 'odoo_owner')
 AND pid <> pg_backend_pid();
SQL
# Verify using the actual application login, including ownership and extensions.
PGUSER=odoo_owner PGPASSWORD="$OWNER_PASSWORD" psql -X -q -v ON_ERROR_STOP=1 -d "$DB_NAME" <<'SQL'
DO $$ BEGIN
IF EXISTS (SELECT FROM pg_roles WHERE rolname = current_user AND
 (rolsuper OR rolcreaterole OR rolreplication OR rolbypassrls)) THEN
 RAISE EXCEPTION 'unsafe application role';
END IF;
END $$;
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
SQL
