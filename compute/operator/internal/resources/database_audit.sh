#!/bin/sh
set -eu
export PGCONNECT_TIMEOUT=10
psql -X -q -v ON_ERROR_STOP=1 -d "$DB_NAME" <<'SQL'
DO $$ BEGIN
IF EXISTS (SELECT FROM pg_roles WHERE rolname = current_user AND
 (rolsuper OR rolcreaterole OR rolreplication OR rolbypassrls OR rolcreatedb))
 OR EXISTS (SELECT FROM pg_roles WHERE rolname <> current_user
 AND (rolsuper OR rolcreaterole OR rolreplication OR rolbypassrls OR rolcreatedb
      OR (rolname ~ '^pg_' AND rolname <> 'pg_database_owner'))
 AND pg_has_role(current_user, oid, 'MEMBER')) THEN
 RAISE EXCEPTION 'unsafe external/shared application role';
END IF;
END $$;
SQL
