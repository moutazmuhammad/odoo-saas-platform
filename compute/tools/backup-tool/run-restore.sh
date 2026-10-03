#!/usr/bin/env bash
# Entrypoint for the one-time restore Job's container (see
# compute/operator/internal/resources/restore.go:OdooRestoreJob). Direct
# inverse of run-backup.sh: consumes the same db.dump + filestore.tar.gz +
# manifest.json triple that tool produces, restoring them into this
# instance's already-provisioned-but-empty database and filestore PVC
# before the web Deployment ever starts.
#
# Contract (env vars set by the operator):
#   INSTANCE_NAME, SOURCE_TYPE, SOURCE_BUCKET, SOURCE_PREFIX, BACKUP_ID,
#   ODOO_DATA_DIR, DB_HOST, DB_PORT, DB_USER, DB_NAME, PGPASSWORD
#   (ObjectStorage only) OBJECT_STORAGE_ENDPOINT/ACCESS_KEY/SECRET_KEY
set -euo pipefail
source /usr/local/lib/lib-objectstorage.sh

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

echo "[restore-tool] starting restore for instance ${INSTANCE_NAME}"

case "${SOURCE_TYPE:-PVC}" in
  PVC)
    # SOURCE_PREFIX, when set, names the directory the backup actually
    # lives under — which is the ORIGINAL instance's name (run-backup.sh
    # publishes to /backups/<that instance's INSTANCE_NAME>/<STAMP>), not
    # necessarily this restore Job's own INSTANCE_NAME. This is the
    # cross-instance onboarding case spec.restore exists for in the first
    # place (a customer's backup migrating onto a newly-created instance
    # with a different name) — falling back to INSTANCE_NAME only covers
    # the narrower same-name restore-in-place case.
    BASE="/backups/${SOURCE_PREFIX:-$INSTANCE_NAME}"
    if [ -n "${BACKUP_ID:-}" ]; then
      SRC="${BASE}/${BACKUP_ID}"
    else
      # Same STAMP format run-backup.sh uses (date -u +%Y%m%dT%H%M%SZ)
      # sorts lexicographically == chronologically, so the last entry is
      # the most recent run.
      SRC="$(find "$BASE" -mindepth 1 -maxdepth 1 -type d | sort | tail -n1)"
    fi
    if [ -z "$SRC" ] || [ ! -d "$SRC" ]; then
      echo "[restore-tool] ERROR: no backup found at ${BASE}${BACKUP_ID:+/$BACKUP_ID}" >&2
      exit 1
    fi
    echo "[restore-tool] restoring from ${SRC}"
    cp "${SRC}/db.dump" "${SRC}/filestore.tar.gz" "${SRC}/manifest.json" "$WORKDIR/"
    if [ -d "${SRC}/dbs" ]; then cp -r "${SRC}/dbs" "$WORKDIR/"; fi
    ;;
  ObjectStorage)
    # KEY_BASE must use the exact same fallback as run-backup.sh's
    # DESTINATION_PREFIX-or-INSTANCE_NAME, for the same cross-instance
    # onboarding reason documented on the PVC case above.
    KEY_BASE="${SOURCE_PREFIX:-$INSTANCE_NAME}"
    _rclone_configure_remote
    BASE_REMOTE="objstore:${SOURCE_BUCKET}/${KEY_BASE}"
    if [ -n "${BACKUP_ID:-}" ]; then
      RUN="${BACKUP_ID}"
    else
      RUN="$(rclone lsf "$BASE_REMOTE" --dirs-only | sed 's#/$##' | sort | tail -n1)"
    fi
    if [ -z "$RUN" ]; then
      echo "[restore-tool] ERROR: no backup found at s3://${SOURCE_BUCKET}/${KEY_BASE}${BACKUP_ID:+/$BACKUP_ID}" >&2
      exit 1
    fi
    SRC_REMOTE="${BASE_REMOTE}/${RUN}"
    echo "[restore-tool] restoring from s3://${SOURCE_BUCKET}/${KEY_BASE}/${RUN}"
    rclone copy "${SRC_REMOTE}/db.dump" "$WORKDIR/" --checksum
    rclone copy "${SRC_REMOTE}/filestore.tar.gz" "$WORKDIR/" --checksum
    rclone copy "${SRC_REMOTE}/manifest.json" "$WORKDIR/" --checksum
    if python3 -c 'import json, sys; sys.exit(not json.load(open(sys.argv[1])).get("extra_databases"))' "${WORKDIR}/manifest.json"; then
      rclone copy "${SRC_REMOTE}/dbs" "${WORKDIR}/dbs" --checksum
    fi
    if [ ! -f "${WORKDIR}/db.dump" ] || [ ! -f "${WORKDIR}/filestore.tar.gz" ]; then
      echo "[restore-tool] ERROR: backup artifacts not found at s3://${SOURCE_BUCKET}/${KEY_BASE}/${RUN}" >&2
      exit 1
    fi
    ;;
  *)
    echo "[restore-tool] ERROR: unknown SOURCE_TYPE '${SOURCE_TYPE:-}'" >&2
    exit 1
    ;;
esac

# Validate every expected dump before changing any extra database. The manifest's
# instance field is the source identity; SOURCE_PREFIX is a storage path.
python3 - "$WORKDIR" "$INSTANCE_NAME" <<'PY_MANIFEST'
import json, os, pathlib, re, sys
workdir = pathlib.Path(sys.argv[1])
manifest = json.loads((workdir / 'manifest.json').read_text())
source = manifest.get('database_prefix') or (manifest.get('instance', '') + '_')
target_prefix = os.environ.get('DATABASE_PREFIX') or (sys.argv[2] + '_')
names = manifest.get('extra_databases', [])
if not isinstance(names, list):
    raise ValueError('Invalid extra_databases in backup manifest')
pairs = []
for name in names:
    if not isinstance(name, str) or not re.fullmatch(r'[_a-z][a-z0-9_-]{0,62}', name):
        raise ValueError('Invalid database name in backup manifest')
    if not (workdir / 'dbs' / (name + '.dump')).is_file():
        raise ValueError('Missing database dump: ' + name)
    target = target_prefix + name[len(source):] if source and name.startswith(source) else name
    if not re.fullmatch(r'[_a-z][a-z0-9_-]{0,62}', target):
        raise ValueError('Invalid mapped database name: ' + target)
    pairs.append((name, target))
(workdir / 'db-pairs').write_text(''.join(a + '\t' + b + '\n' for a, b in pairs))
PY_MANIFEST
echo "[restore-tool] restoring database ${DB_NAME}@${DB_HOST}:${DB_PORT}"
# --no-owner: the dump's original role (from wherever it was taken) has no
# reason to exist in this cluster's Postgres, and this Job's own DB_USER
# already owns the empty target database OdooInitJob-equivalent
# provisioning created — reassigning ownership to a nonexistent role would
# otherwise fail loudly (or worse, silently as a superuser).
#
# --single-transaction: this Job retries in place on failure (BackoffLimit,
# restartPolicy: OnFailure — see OdooRestoreJob's doc comment), and a
# *partial* restore is worse than no restore: it leaves the target
# database non-empty, so a retry's CREATE TABLE statements collide with
# whatever the failed attempt already created ("relation already exists"),
# permanently wedging every subsequent retry against a half-populated
# database that a plain retry can never get past on its own. Wrapping the
# whole restore in one transaction makes a failure roll back completely,
# so every retry starts from the same clean empty database the first
# attempt did (caught live: a restore that failed partway through — for
# an unrelated, transient reason — poisoned the database for every retry
# after it until this was added).
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres \
  -c "DROP DATABASE IF EXISTS \"${DB_NAME}\" WITH (FORCE)"
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres \
  -c "CREATE DATABASE \"${DB_NAME}\""
pg_restore -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
  --no-owner --single-transaction --exit-on-error "${WORKDIR}/db.dump"

while IFS=$'\t' read -r NAME TARGET; do
  echo "[restore-tool] restoring extra database ${NAME} into ${TARGET}@${DB_HOST}:${DB_PORT}"
  psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres \
    -c "DROP DATABASE IF EXISTS \"${TARGET}\" WITH (FORCE)"
  psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres \
    -c "CREATE DATABASE \"${TARGET}\""
  pg_restore -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$TARGET" \
    --no-owner --single-transaction --exit-on-error "${WORKDIR}/dbs/${NAME}.dump"
done < "${WORKDIR}/db-pairs"

echo "[restore-tool] extracting filestore into ${ODOO_DATA_DIR}"
mkdir -p "$ODOO_DATA_DIR"
tar -C "$ODOO_DATA_DIR" -xzf "${WORKDIR}/filestore.tar.gz"


# Filestore directories must follow the same cross-instance mapping as the DBs.
while IFS=$'\t' read -r NAME TARGET; do
  if [ "$NAME" != "$TARGET" ] && [ -d "${ODOO_DATA_DIR}/filestore/${NAME}" ]; then
    mv "${ODOO_DATA_DIR}/filestore/${NAME}" "${ODOO_DATA_DIR}/filestore/${TARGET}"
  fi
done < "${WORKDIR}/db-pairs"

echo "[restore-tool] restore completed successfully"
