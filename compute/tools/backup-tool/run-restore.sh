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

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

echo "[restore-tool] starting restore for instance ${INSTANCE_NAME}"

case "${SOURCE_TYPE:-PVC}" in
  PVC)
    BASE="/backups/${INSTANCE_NAME}"
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
    cp "${SRC}/db.dump" "${SRC}/filestore.tar.gz" "$WORKDIR/"
    ;;
  ObjectStorage)
    # Not yet exercised against a real bucket — mirrors run-backup.sh's own
    # ObjectStorage gap (no S3-compatible CLI in this image yet). Left
    # deliberately explicit (fail loud) rather than silently no-op, so a
    # misconfigured instance is never mistaken for a successful restore.
    echo "[restore-tool] ERROR: ObjectStorage source needs an S3-compatible" >&2
    echo "  CLI (aws/rclone) added to this image and endpoint/bucket wiring below" >&2
    echo "  this line — not implemented yet in this build." >&2
    exit 1
    ;;
  *)
    echo "[restore-tool] ERROR: unknown SOURCE_TYPE '${SOURCE_TYPE:-}'" >&2
    exit 1
    ;;
esac

echo "[restore-tool] restoring database ${DB_NAME}@${DB_HOST}:${DB_PORT}"
# --no-owner: the dump's original role (from wherever it was taken) has no
# reason to exist in this cluster's Postgres, and this Job's own DB_USER
# already owns the empty target database OdooInitJob-equivalent
# provisioning created — reassigning ownership to a nonexistent role would
# otherwise fail loudly (or worse, silently as a superuser).
pg_restore -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
  --no-owner --exit-on-error "${WORKDIR}/db.dump"

echo "[restore-tool] extracting filestore into ${ODOO_DATA_DIR}"
mkdir -p "$ODOO_DATA_DIR"
tar -C "$ODOO_DATA_DIR" -xzf "${WORKDIR}/filestore.tar.gz"

echo "[restore-tool] restore completed successfully"
