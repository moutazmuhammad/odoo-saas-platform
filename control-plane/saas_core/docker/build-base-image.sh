#!/usr/bin/env bash
# Phase 2.2.2 — build + push the immutable Odoo BASE image for one version.
#
# Bakes /opt/odoo-source/<ver> into an image FROM odoo-light:<ver>, tags it
# <registry>/odoo-base:<ver>, and pushes to the registry from registry.env.
# Idempotent (overwrites the tag).
#
# Usage (on the build host, as root):
#   ODOO_VERSION=18.0 bash build-base-image.sh
set -euo pipefail

ODOO_VERSION="${ODOO_VERSION:-18.0}"
SOURCE_DIR="${SOURCE_DIR:-/opt/odoo-source/$ODOO_VERSION}"
CREDS_FILE="/etc/saas/registry.env"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

log() { printf '\033[1;35m[base-image]\033[0m %s\n' "$*"; }

[ -f "$CREDS_FILE" ] || { echo "missing $CREDS_FILE — run provision-registry.sh first"; exit 1; }
# shellcheck disable=SC1090
. "$CREDS_FILE"
[ -d "$SOURCE_DIR/odoo" ] || { echo "Odoo source not found at $SOURCE_DIR (expected $SOURCE_DIR/odoo)"; exit 1; }
docker image inspect "odoo-light:$ODOO_VERSION" >/dev/null 2>&1 || {
  echo "odoo-light:$ODOO_VERSION not found — build it first (build-all.sh)"; exit 1; }

IMAGE="$REGISTRY_HOST/odoo-base:$ODOO_VERSION"
log "building $IMAGE (baking source from $SOURCE_DIR)"
docker build -f "$SCRIPT_DIR/Dockerfile.odoo-base" \
  --build-arg BASE_TAG="$ODOO_VERSION" \
  -t "$IMAGE" "$SOURCE_DIR"

echo "$REGISTRY_PASSWORD" | docker login "$REGISTRY_HOST" -u "$REGISTRY_USER" --password-stdin >/dev/null
log "pushing $IMAGE"
docker push "$IMAGE"
DIGEST="$(docker inspect --format '{{index .RepoDigests 0}}' "$IMAGE" 2>/dev/null || true)"
log "OK — pushed $IMAGE  (digest: ${DIGEST:-n/a})"
