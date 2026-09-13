#!/usr/bin/env bash
# Provision the Phase-2.2 container registry on a build/docker host:
#   a self-hosted Docker Distribution (registry:2), localhost-bound, basic-auth.
#
# Idempotent: safe to re-run (after a droplet rebuild). Credentials are generated
# once and stored in CREDS_FILE (chmod 600).
#
# Why localhost-only: this box has a history of exposure (see SECURITY-INCIDENT);
# the registry is reached by the local build worker + docker daemon over
# 127.0.0.1, never the public internet. Docker treats 127.0.0.0/8 registries as
# "insecure" (plain HTTP) by default, so no daemon TLS config is needed.
#
# Prod swaps this for DigitalOcean Container Registry (or a TLS'd self-hosted
# Distribution); the build pipeline's docker login/push/pull is identical, only
# REGISTRY_HOST + creds change.
#
# Usage (on the host, as root):  bash provision-registry.sh
set -euo pipefail

REGISTRY_HOST="${REGISTRY_HOST:-127.0.0.1:5000}"
REGISTRY_PORT="${REGISTRY_PORT:-5000}"
CREDS_FILE="/etc/saas/registry.env"
AUTH_DIR="/etc/saas/registry-auth"

log() { printf '\033[1;35m[registry]\033[0m %s\n' "$*"; }

mkdir -p /etc/saas "$AUTH_DIR"

# --- 0) credentials (generate once) ------------------------------------------
if [ ! -f "$CREDS_FILE" ]; then
  log "generating registry credentials -> $CREDS_FILE"
  cat > "$CREDS_FILE" <<EOF
REGISTRY_HOST=$REGISTRY_HOST
REGISTRY_USER=saasreg
REGISTRY_PASSWORD=$(openssl rand -hex 24)
EOF
  chmod 600 "$CREDS_FILE"
fi
# shellcheck disable=SC1090
. "$CREDS_FILE"

# --- 1) htpasswd (bcrypt) for registry basic auth ----------------------------
if [ ! -f "$AUTH_DIR/htpasswd" ]; then
  log "writing htpasswd (bcrypt)"
  docker run --rm --entrypoint htpasswd httpd:2 -Bbn "$REGISTRY_USER" "$REGISTRY_PASSWORD" \
    > "$AUTH_DIR/htpasswd"
  chmod 600 "$AUTH_DIR/htpasswd"
fi

# --- 2) registry:2 container (localhost-bound, persistent volume) ------------
if ! docker ps -a --format '{{.Names}}' | grep -qx registry; then
  log "starting registry:2 (bound to 127.0.0.1:$REGISTRY_PORT)"
  docker volume create registry_data >/dev/null
  docker run -d --name registry --restart unless-stopped \
    -p 127.0.0.1:"$REGISTRY_PORT":5000 \
    -v registry_data:/var/lib/registry \
    -v "$AUTH_DIR":/auth:ro \
    -e REGISTRY_AUTH=htpasswd \
    -e "REGISTRY_AUTH_HTPASSWD_REALM=saas-registry" \
    -e REGISTRY_AUTH_HTPASSWD_PATH=/auth/htpasswd \
    -e REGISTRY_STORAGE_DELETE_ENABLED=true \
    registry:2 >/dev/null
else
  docker start registry >/dev/null 2>&1 || true
  log "registry container already present"
fi

# wait for it to answer
for i in $(seq 1 30); do
  if curl -fsS -o /dev/null -u "$REGISTRY_USER:$REGISTRY_PASSWORD" \
       "http://$REGISTRY_HOST/v2/" 2>/dev/null; then break; fi
  sleep 1
done

# --- 3) docker login + push/pull smoke test ----------------------------------
log "verifying login + round-trip"
echo "$REGISTRY_PASSWORD" | docker login "$REGISTRY_HOST" -u "$REGISTRY_USER" --password-stdin >/dev/null
# tiny round-trip using an image already on the host
SMOKE_SRC="$(docker images --format '{{.Repository}}:{{.Tag}}' | grep -v '<none>' | head -1)"
if [ -n "$SMOKE_SRC" ]; then
  docker tag "$SMOKE_SRC" "$REGISTRY_HOST/smoketest:probe"
  docker push "$REGISTRY_HOST/smoketest:probe" >/dev/null
  docker rmi "$REGISTRY_HOST/smoketest:probe" >/dev/null 2>&1 || true
  docker pull "$REGISTRY_HOST/smoketest:probe" >/dev/null
  log "push/pull round-trip OK ($SMOKE_SRC)"
fi
log "catalog: $(curl -fsS -u "$REGISTRY_USER:$REGISTRY_PASSWORD" "http://$REGISTRY_HOST/v2/_catalog")"
log "OK — registry ready at $REGISTRY_HOST (creds in $CREDS_FILE)"
