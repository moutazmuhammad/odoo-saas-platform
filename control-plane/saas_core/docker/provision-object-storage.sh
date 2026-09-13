#!/usr/bin/env bash
# Provision the Phase-2 object-storage filestore substrate on a docker host:
#   MinIO (S3-compatible object store, localhost-only) + JuiceFS (POSIX layer
#   with metadata in PostgreSQL, data in MinIO, local NVMe cache).
#
# Idempotent: safe to re-run (e.g. after a droplet rebuild). Each step checks
# for existing state before acting. Credentials are generated once and stored
# in CREDS_FILE (chmod 600); subsequent runs reuse them.
#
# Usage (on the host, as root):  bash provision-object-storage.sh
# Override via env: BUCKET, JFS_NAME, MOUNT, CACHE_DIR, CACHE_SIZE_MB, PG_HOST.
#
# Dev/test only. In prod the MinIO endpoint becomes R2/B2 and creds come from a
# secret manager — the JuiceFS format/mount steps are identical.
set -euo pipefail

BUCKET="${BUCKET:-saas-filestore}"
JFS_NAME="${JFS_NAME:-saasfs}"
MOUNT="${MOUNT:-/mnt/jfs}"
CACHE_DIR="${CACHE_DIR:-/var/jfsCache}"
CACHE_SIZE_MB="${CACHE_SIZE_MB:-2048}"
PG_HOST="${PG_HOST:-127.0.0.1}"
PG_PORT="${PG_PORT:-5432}"
JFS_META_DB="${JFS_META_DB:-juicefs_meta}"
JFS_META_USER="${JFS_META_USER:-juicefs}"
MINIO_ENDPOINT="http://127.0.0.1:9000"
CREDS_FILE="/etc/saas/object-storage.env"

log() { printf '\033[1;34m[obj-store]\033[0m %s\n' "$*"; }

# --- 0) credentials (generate once, reuse after) -----------------------------
mkdir -p /etc/saas
if [ ! -f "$CREDS_FILE" ]; then
  log "generating credentials -> $CREDS_FILE"
  cat > "$CREDS_FILE" <<EOF
MINIO_ROOT_USER=saasminio
MINIO_ROOT_PASSWORD=$(openssl rand -hex 24)
JFS_META_PASSWORD=$(openssl rand -hex 24)
EOF
  chmod 600 "$CREDS_FILE"
fi
# shellcheck disable=SC1090
. "$CREDS_FILE"

# --- 1) MinIO container (localhost-only) -------------------------------------
if ! docker ps -a --format '{{.Names}}' | grep -qx minio; then
  log "starting MinIO container (bound to 127.0.0.1 only)"
  docker volume create minio_data >/dev/null
  docker run -d --name minio --restart unless-stopped \
    -p 127.0.0.1:9000:9000 -p 127.0.0.1:9001:9001 \
    -e MINIO_ROOT_USER="$MINIO_ROOT_USER" \
    -e MINIO_ROOT_PASSWORD="$MINIO_ROOT_PASSWORD" \
    -v minio_data:/data \
    quay.io/minio/minio server /data --console-address ":9001" >/dev/null
else
  docker start minio >/dev/null 2>&1 || true
  log "MinIO container already present"
fi

# wait for MinIO health
for i in $(seq 1 30); do
  if curl -fsS -o /dev/null "http://127.0.0.1:9000/minio/health/ready" 2>/dev/null; then break; fi
  sleep 1
done

# --- 2) bucket (via mc one-shot container) -----------------------------------
log "ensuring bucket '$BUCKET'"
docker run --rm --network host --entrypoint /bin/sh quay.io/minio/mc -c "
  mc alias set local $MINIO_ENDPOINT '$MINIO_ROOT_USER' '$MINIO_ROOT_PASSWORD' >/dev/null &&
  mc mb --ignore-existing local/$BUCKET >/dev/null &&
  mc ls local/ "

# --- 3) JuiceFS client -------------------------------------------------------
if ! command -v juicefs >/dev/null 2>&1; then
  log "installing JuiceFS client"
  curl -fsSL https://d.juicefs.com/install | sh -
fi
juicefs version

# --- 4) PostgreSQL metadata role + db ----------------------------------------
log "ensuring PG metadata role + db ($JFS_META_USER / $JFS_META_DB)"
sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='$JFS_META_USER') THEN
    CREATE ROLE $JFS_META_USER LOGIN PASSWORD '$JFS_META_PASSWORD';
  ELSE
    ALTER ROLE $JFS_META_USER LOGIN PASSWORD '$JFS_META_PASSWORD';
  END IF;
END \$\$;
SQL
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$JFS_META_DB'" | grep -q 1; then
  sudo -u postgres createdb -O "$JFS_META_USER" "$JFS_META_DB"
fi
# pg_hba: allow the metadata role over TCP loopback (scram)
HBA="$(sudo -u postgres psql -tAc 'SHOW hba_file')"
if ! grep -qE "^host\s+$JFS_META_DB\s+$JFS_META_USER\s+127.0.0.1/32" "$HBA"; then
  echo "host    $JFS_META_DB    $JFS_META_USER    127.0.0.1/32    scram-sha-256" >> "$HBA"
  systemctl reload postgresql
fi

META_URL="postgres://${JFS_META_USER}:${JFS_META_PASSWORD}@${PG_HOST}:${PG_PORT}/${JFS_META_DB}"

# --- 5) format the JuiceFS volume (once) -------------------------------------
if ! juicefs status "$META_URL" >/dev/null 2>&1; then
  log "formatting JuiceFS volume '$JFS_NAME'"
  juicefs format \
    --storage minio \
    --bucket "$MINIO_ENDPOINT/$BUCKET" \
    --access-key "$MINIO_ROOT_USER" \
    --secret-key "$MINIO_ROOT_PASSWORD" \
    "$META_URL" "$JFS_NAME"
else
  log "JuiceFS volume already formatted"
fi

# --- 6) mount via systemd (survives reboot) ----------------------------------
mkdir -p "$MOUNT" "$CACHE_DIR"
UNIT=/etc/systemd/system/jfs-mount.service
if [ ! -f "$UNIT" ]; then
  log "installing systemd mount unit"
  cat > "$UNIT" <<EOF
[Unit]
Description=JuiceFS mount ($JFS_NAME) for SaaS filestore
After=network-online.target docker.service postgresql.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$CREDS_FILE
ExecStart=/usr/local/bin/juicefs mount --no-syslog \\
  --cache-dir $CACHE_DIR --cache-size $CACHE_SIZE_MB \\
  postgres://${JFS_META_USER}:\${JFS_META_PASSWORD}@${PG_HOST}:${PG_PORT}/${JFS_META_DB} $MOUNT
ExecStop=/bin/umount $MOUNT
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
fi
systemctl enable --now jfs-mount.service
for i in $(seq 1 20); do mountpoint -q "$MOUNT" && break; sleep 1; done

# --- 7) verify read/write ----------------------------------------------------
log "verifying read/write through $MOUNT"
TS="$(date +%s)"
echo "saas-filestore-ok-$TS" > "$MOUNT/.provision_check"
test "$(cat "$MOUNT/.provision_check")" = "saas-filestore-ok-$TS"
rm -f "$MOUNT/.provision_check"
juicefs status "$META_URL" 2>/dev/null | grep -E '"Name"|"Storage"|"Bucket"' || true
log "OK — MinIO + JuiceFS provisioned and writable at $MOUNT"
