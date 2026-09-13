#!/usr/bin/env bash
# Phase 2.2.5 — egress-restricted sandbox network for the image build worker.
#
# Tenant image builds run untrusted customer code (requirements.txt pip install,
# module setup.py). This creates a dedicated docker network `saas-build` and
# DOCKER-USER firewall rules so a build container CAN reach public PyPI + DNS but
# CANNOT reach:
#   - cloud metadata (169.254.169.254) — would leak instance credentials/role
#   - RFC1918 internals (other tenants, PostgreSQL on 172.17.0.1, the control
#     plane, docker bridges)
# Builds use `DOCKER_BUILDKIT=0 docker build --network saas-build ...` so the
# untrusted RUN steps are confined to this network. (FROM pulls happen on the
# daemon, not this network, so the local registry still works.)
#
# Idempotent. Note: iptables rules are runtime-only — re-run after a reboot
# (or persist via netfilter-persistent).
set -euo pipefail

NET="${NET:-saas-build}"
SUBNET="${SUBNET:-172.31.255.0/24}"

log() { printf '\033[1;36m[build-sandbox]\033[0m %s\n' "$*"; }

# --- 1) dedicated build network with a known subnet --------------------------
if ! docker network inspect "$NET" >/dev/null 2>&1; then
  log "creating network $NET ($SUBNET)"
  docker network create --subnet "$SUBNET" "$NET" >/dev/null
else
  SUBNET="$(docker network inspect -f '{{(index .IPAM.Config 0).Subnet}}' "$NET")"
  log "network $NET already exists ($SUBNET)"
fi

# --- 2) egress firewall on DOCKER-USER (scoped to the build subnet) ----------
# Order matters (first match wins). We insert at the top in REVERSE priority so
# the final order is: allow DNS -> drop metadata -> drop RFC1918 -> (public ok).
add_top() { # idempotent insert at position 1
  iptables -C DOCKER-USER "$@" 2>/dev/null || iptables -I DOCKER-USER "$@"
}
# Inserted last in this list ends up highest. Build bottom-up:
add_top -s "$SUBNET" -d 10.0.0.0/8       -j DROP
add_top -s "$SUBNET" -d 172.16.0.0/12    -j DROP
add_top -s "$SUBNET" -d 192.168.0.0/16   -j DROP
add_top -s "$SUBNET" -d 169.254.0.0/16   -j DROP   # link-local incl. metadata
# DNS must sit ABOVE the RFC1918 drops so pip can resolve even via a private resolver
add_top -s "$SUBNET" -p tcp --dport 53   -j RETURN
add_top -s "$SUBNET" -p udp --dport 53   -j RETURN

# DOCKER-USER lives in FORWARD, so it only catches build->OTHER-container /
# build->internet routing. Traffic to the HOST's own IPs (e.g. PostgreSQL on the
# docker0 gateway 172.17.0.1) hits INPUT and would bypass the rules above —
# block those here. (DNS via docker's embedded resolver is unaffected; internet
# egress is FORWARD with a public dst, also unaffected.)
add_input() { iptables -C INPUT "$@" 2>/dev/null || iptables -I INPUT 1 "$@"; }
add_input -s "$SUBNET" -p tcp --dport 5432 -j DROP   # PostgreSQL on any host IP
add_input -s "$SUBNET" -d 172.17.0.1       -j DROP   # docker0 gateway = host internals
add_input -s "$SUBNET" -d 169.254.0.0/16   -j DROP   # metadata via host path

log "effective DOCKER-USER rules for $SUBNET:"
iptables -L DOCKER-USER -n --line-numbers | grep -E "$SUBNET|Chain" || true
log "INPUT (container->host) drops for $SUBNET:"
iptables -L INPUT -n --line-numbers | grep -E "$SUBNET" || true
log "OK — build sandbox ready (use: DOCKER_BUILDKIT=0 docker build --network $NET ...)"
