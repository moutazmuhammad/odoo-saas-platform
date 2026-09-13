# Production-Readiness Roadmap

**Date:** 2026-06-18 · **Branch:** `architecture-evolution`
**Question this answers:** what exactly must happen before we can confidently run this platform in production?
**Verdict today:** the engineering is production-grade (Phases 0–6 done, 104 unit tests green, every path
live-verified), but the system is **not yet ready to flip on for real tenants** — it was validated on a
disposable (and previously compromised) test box, not deployed to production, and the new infra is still
dev-grade as configured. The steps below close that gap.

**Legend:** 🤖 = the assistant can do it · 👤 = the user must do it · 🤝 = both.
**Access note:** the assistant works over SSH with the control-plane key. Anything on the **production**
box (`165.22.69.199`, db `veltnex`) assumes the same SSH access used on the test box; tasks needing the
DigitalOcean console, a credential, or a risk decision are 👤.

---

## P0 — Security & integrity (BLOCKERS — nothing ships until these are green)

### 0.1 Audit the production box for compromise — 🤖
The test box (`165.245.245.196`) was hit by the Outlaw botnet via exposed PostgreSQL + weak creds; prod may
share those exposures. See `SECURITY-INCIDENT-2026-06-17.md`.
**Done when:** the same IOC sweep (rogue `/var/tmp` binaries, crontab persistence, backdoor SSH keys,
outbound C2, listening ports) has run on `165.22.69.199` and reports clean — or findings are contained.

### 0.2 Rotate every credential that touched chat / is reused — 🤝
Root SSH password (pasted in chat repeatedly), reused DB/role passwords, object-store + registry keys.
👤 rotate what you control (DO root, Spaces). 🤖 update any the platform stores; switch prod SSH to key-only.
**Done when:** prod SSH has `PasswordAuthentication no`, and no credential in use was ever shown in plaintext.

### 0.3 Lock down PostgreSQL exposure on prod — 🤖
Bind PG to the docker bridge + `pg_hba` for `172.16/12` only — never `0.0.0.0`; firewall `:5432` from the
public internet. (See `project_pg_docker_bridge` — this can drift.)
**Done when:** `ss -tlnp | grep 5432` shows no public bind and an external connect is refused.

### 0.4 Rebuild the compromised test box — 👤 → 🤖
👤 destroy + recreate the droplet (it's disposable). 🤖 re-provision from the idempotent `provision-*.sh`
scripts and re-run the suite — this also proves the scripts are a clean reference.
**Done when:** a fresh box reaches the same green state with no manual steps.

### 0.5 Verify backup → restore on production — 🤖
Re-prove the full restic→DO Spaces backup + restore round-trip on a prod tenant (or a staging clone), with
an integrity marker.
**Done when:** a prod tenant is backed up, a marker planted, restored, and the marker is gone + data intact
+ HTTP 200.

---

## P1 — Required for go-live (prod-grade infra + safe rollout)

### 1.1 Production object storage (DO Spaces, not MinIO) — 🤝
👤 create a Spaces bucket + keys. 🤖 point JuiceFS at Spaces (`--storage s3`) via
`provision-object-storage.sh`, mount, re-verify read/write + object-backing.
**Done when:** a tenant's attachment round-trips Odoo → JuiceFS → **Spaces** and survives a container
destroy/recreate.

### 1.2 Production registry (TLS or DO Container Registry) — 🤝
👤 pick provider + creds (DOCR is simplest). 🤖 point build/deploy at it; verify push/pull by digest.
**Done when:** a tenant image builds + deploys by digest from the prod registry.

### 1.3 Make all host config reboot-survivable — 🤖
The iptables rules (PG firewall, build-sandbox egress) are runtime-only; systemd units exist but need a
reboot test.
**Done when:** after a reboot of the test box, PG firewall + build-sandbox rules + JuiceFS mount +
registry/MinIO all come back automatically (persisted via `netfilter-persistent` + enabled units).

### 1.4 Staging environment on/near production — 🤝
👤 provision a staging server if you want true isolation (or use the Environments staging tier). 🤖 deploy
the `architecture-evolution` branch to staging and run the full smoke + Selenium UI pass.
**Done when:** staging runs the new branch end-to-end (provision, DB create, backup, object filestore,
immutable deploy) with zero errors.

### 1.5 Canary rollout to one real tenant (flagged) — 🤝
👤 pick one low-risk real tenant + accept the risk. 🤖 migrate it to object filestore + immutable image
behind the per-server flags, then watch it for several days.
**Done when:** the canary serves normally for the agreed soak period with no regressions; rollback path
exercised once.

### 1.6 Rollback runbook + go/no-go criteria — 🤖
Written, step-by-step: revert a tenant (immutable→legacy, object→local filestore), restore from backup,
who/what triggers a halt.
**Done when:** a second person could execute a rollback from the doc alone.

### 1.7 Load / concurrency test — 🤖
k6/Locust at expected peak (logins, page loads, a DB-create) against staging; capture p95 latency +
resource headroom; validate the live-metrics sampler + reconciler under load.
**Done when:** the system holds target concurrency within an agreed latency budget, and per-tenant
cost/margin still computes.

### 1.8 Baseline monitoring + alerting — 🤖
At minimum: per-tenant uptime/health (reconciler + an external HTTP check), disk/RAM headroom alerts, the
unprofitable-tenant cron, and backup-success alerting.
**Done when:** a killed container, a full disk, and a failed backup each produce an alert you actually
receive.

---

## P2 — Hardening (parallel with / shortly after launch)

- **2.1 Fully-rootless build worker** — 🤖 move tenant builds off the host docker daemon (rootless
  buildkit), on top of the egress firewall already in place.
- **2.2 Time-series observability (Grafana + VictoriaMetrics)** — 🤖 historical CPU/RAM/DB-size/margin
  trends on top of the Odoo-native dashboard (Phase 4.2.x).
- **2.3 CI pipeline** — 🤝 run the 104-test suite on every push; 👤 grants CI access, 🤖 wires it.
- **2.4 Fold provisioning-recovery crons into the reconciler** — 🤖 one idempotent recovery path (Phase 3.2.4).
- **2.5 Public-surface security review** — 🤖 rate-limiting/abuse on `/saas/api`, ensure the Odoo database
  manager isn't publicly reachable per tenant, tenant-isolation review.
- **2.6 Secrets out of the DB** — 🤝 move stored tokens/keys toward a secret manager or encrypted-at-rest.
- **2.7 DR drill + retention policy** — 🤝 periodic restore-test cadence; data-retention/GDPR decisions are
  the user's.

---

## Suggested sequence
P0 in order (0.1 → 0.5) → P1.1–P1.3 (prod infra solid) → P1.4 staging → P1.6 runbook + P1.7 load test →
P1.5 canary soak → P1.8 alerting live → graduate tenants in waves → P2 hardening alongside.

## The gate — when the platform is "production-ready"
All **P0 green**, all **P1 green**, and specifically:
1. Prod box audited-clean + key-only SSH + PG never public; all chat-exposed creds rotated.
2. New infra runs on **DO Spaces + a real registry**, and **survives a reboot**.
3. A **real canary tenant** has soaked on object-filestore + immutable image with rollback proven.
4. **Backup → restore** verified on prod, with a written runbook.
5. **Load test** passed at target concurrency; **alerts** fire on container death, disk-full, and backup
   failure.

At that point the platform isn't just well-architected — it's *operationally* trustworthy.

---

## Progress log (update as steps complete)

| Date | Step | Owner | Status | Notes |
|------|------|-------|--------|-------|
| 2026-06-18 | — | — | 📝 | Roadmap written. Phases 0–6 of the architecture complete + tested on the (disposable) test box; nothing deployed to production yet. |
