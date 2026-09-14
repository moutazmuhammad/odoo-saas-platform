#!/usr/bin/env bash
# Entrypoint for the backup CronJob's container (see
# compute/operator/internal/resources/backup.go:BackupCronJob). Runs as a
# non-root, read-only-rootfs container: every write goes to /tmp (emptyDir)
# or /backups (the destination PVC) — never to the image's own filesystem.
#
# Contract (env vars set by the operator):
#   RETENTION, INSTANCE_NAME, DESTINATION_TYPE, DESTINATION_BUCKET,
#   DESTINATION_PREFIX, DB_HOST, DB_PORT, DB_USER, DB_NAME, PGPASSWORD
#   (ObjectStorage only) OBJECT_STORAGE_ENDPOINT/ACCESS_KEY/SECRET_KEY
#
# Produces one timestamped run containing db.dump (pg_dump custom format),
# filestore.tar.gz, and manifest.json tying them together — the same triple
# restore consumes, so a database dump can never be paired with a mismatched
# filestore snapshot (see docs/architecture.md, "Backups").
set -euo pipefail
source /usr/local/lib/lib-objectstorage.sh

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

echo "[backup-tool] starting run ${STAMP} for instance ${INSTANCE_NAME}"

echo "[backup-tool] dumping database ${DB_NAME}@${DB_HOST}:${DB_PORT}"
pg_dump -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
  --format=custom --file="${WORKDIR}/db.dump"

echo "[backup-tool] archiving filestore"
# Deliberately not `tar -C /filestore -czf ... .`: that stores /filestore's
# own "." directory entry (with its mtime/mode) in the archive, and
# run-restore.sh's non-root container has no permission to restore a
# directory entry's attributes onto a mount point it doesn't own — GNU tar
# treats that as a hard error and aborts the whole extraction even though
# every actual file underneath restores fine. Archiving each top-level
# entry individually (find -mindepth 1, NUL-delimited for filenames with
# spaces/newlines) instead keeps the archive's own top level "flat", so
# restore never needs to touch $ODOO_DATA_DIR's own attributes at all.
(cd /filestore && find . -mindepth 1 -print0 | \
  tar --null --no-recursion -czf "${WORKDIR}/filestore.tar.gz" -T -)

cat > "${WORKDIR}/manifest.json" <<EOF
{
  "instance": "${INSTANCE_NAME}",
  "timestamp": "${STAMP}",
  "db_dump": "db.dump",
  "filestore_archive": "filestore.tar.gz",
  "retention": ${RETENTION:-0}
}
EOF

case "${DESTINATION_TYPE:-PVC}" in
  PVC)
    DEST="/backups/${INSTANCE_NAME}/${STAMP}"
    echo "[backup-tool] publishing to ${DEST}"
    mkdir -p "$DEST"
    mv "${WORKDIR}/db.dump" "${WORKDIR}/filestore.tar.gz" "${WORKDIR}/manifest.json" "$DEST/"

    if [ "${RETENTION:-0}" -gt 0 ]; then
      BASE="/backups/${INSTANCE_NAME}"
      RUNS=$(find "$BASE" -mindepth 1 -maxdepth 1 -type d | sort)
      COUNT=$(echo "$RUNS" | grep -c . || true)
      if [ "$COUNT" -gt "$RETENTION" ]; then
        PRUNE_N=$((COUNT - RETENTION))
        echo "$RUNS" | head -n "$PRUNE_N" | while read -r old; do
          echo "[backup-tool] pruning old run $old (retention=${RETENTION})"
          rm -rf "$old"
        done
      fi
    fi
    ;;
  ObjectStorage)
    # KEY_BASE mirrors the PVC case's per-instance top-level directory:
    # DESTINATION_PREFIX, when set, lets several instances share one
    # bucket without colliding (see BackupDestinationSpec.Prefix's doc
    # comment); falling back to INSTANCE_NAME keeps the common case (one
    # instance, no explicit prefix) collision-free by default too. This
    # must stay the exact same fallback run-restore.sh's SOURCE_PREFIX
    # uses, since restore has to find what backup wrote.
    KEY_BASE="${DESTINATION_PREFIX:-$INSTANCE_NAME}"
    _rclone_configure_remote
    DEST="objstore:${DESTINATION_BUCKET}/${KEY_BASE}/${STAMP}"
    echo "[backup-tool] publishing to s3://${DESTINATION_BUCKET}/${KEY_BASE}/${STAMP}"
    rclone copy "$WORKDIR" "$DEST" --checksum

    if [ "${RETENTION:-0}" -gt 0 ]; then
      BASE_REMOTE="objstore:${DESTINATION_BUCKET}/${KEY_BASE}"
      RUNS=$(rclone lsf "$BASE_REMOTE" --dirs-only | sed 's#/$##' | sort)
      COUNT=$(printf '%s\n' "$RUNS" | grep -c . || true)
      if [ "$COUNT" -gt "$RETENTION" ]; then
        PRUNE_N=$((COUNT - RETENTION))
        printf '%s\n' "$RUNS" | head -n "$PRUNE_N" | while read -r old; do
          echo "[backup-tool] pruning old run $old (retention=${RETENTION})"
          rclone purge "${BASE_REMOTE}/${old}"
        done
      fi
    fi
    ;;
  *)
    echo "[backup-tool] ERROR: unknown DESTINATION_TYPE '${DESTINATION_TYPE:-}'" >&2
    exit 1
    ;;
esac

echo "[backup-tool] run ${STAMP} completed successfully"
