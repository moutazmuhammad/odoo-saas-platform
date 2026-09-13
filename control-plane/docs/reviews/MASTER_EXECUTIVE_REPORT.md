# MASTER EXECUTIVE REPORT — Pre-Acquisition Technical Due Diligence

**Target:** Odoo 18 SaaS platform (Odoo.sh-style multi-tenant Odoo hosting).
**Codebase:** `saas_core` (Odoo module, ~34k LOC Python), `saas_website` (portal + JSON API),
`veltnex` (React/Vite SPA, ~14k LOC TS/TSX).
**Method:** Full-repository read with file:line verification. Automated first-pass findings
were re-verified against source; several "Critical" claims were **disproven and downgraded**
(documented in each report's "Cleared / Overstated" section) — the numbers below reflect only
findings confirmed in code.

---

## Executive Summary

The platform is a feature-rich, working Odoo.sh competitor with genuinely thoughtful pieces
(driver/dataservice seams, per-instance networks, advisory-locked provisioning, idempotent
wallet, real rate limiter, template-clone DB creation, restic+zip backup split). It is **not
yet production-ready for an enterprise-scale, multi-thousand-tenant deployment.** The dominant
risks are: a documented authentication backdoor shipping in code, plaintext storage of all
platform secrets, a 12,324-line god-model, a single control-plane DB that synchronously
orchestrates the whole fleet over SSH with no durable job queue, and the absence of CI/CD,
IaC, monitoring/alerting, and a disaster-recovery plan. Several major SaaS capabilities
(teams/RBAC, API keys, alerts, self-service domains/SSL, audit log, deploy rollback) are
missing relative to every named competitor.

A buyer should price in **6-12 months of de-risking** (security remediation, monolith
decomposition, control/data-plane split, operational tooling) before scaling.

### Scores

| Dimension | Score (/10) | Rationale |
|---|---:|---|
| **Overall Architecture** | 4 | Working but a 12k-line god-model, control-plane SPOF, leaky/partial driver abstraction, no IaC. |
| **SaaS Maturity** | 4 | Core lifecycle present; missing teams, API keys, alerts, domains/SSL self-service, audit log, rollback. |
| **Production Readiness** | 3 | No CI/CD, no monitoring/alerting, no DR plan, auth backdoor, plaintext secrets. |
| **Security** | 2 | Debug-OTP auth bypass, plaintext secrets/SSH keys in DB, root containers, coarse RBAC, no audit log. |
| **UX** | 5 | Solid SPA foundation; polling leaks/no backoff, missing states, weak destructive confirms, signup backdoor. |
| **Reliability** | 3 | No durable queue, partial-failure orphans, long lock holds, 85 broad excepts, no alerting, untested DR. |
| **Scalability** | 4 | Serial backups, unbounded crons, per-host deploy serialization, sequential metrics, missing indexes. |

> Aggregate confirmed findings (by file; note deliberate cross-references — see "Counting"):
> **Critical 10 · High 29 · Medium 41 · Low 14 = 94.**
> Distinct **root-cause** Criticals ≈ 8 (the auth backdoor and secrets issues are cross-listed
> across Security/UX/Business because they have security, UX, and revenue dimensions).

---

## Critical Issues

1. **Auth backdoor — `debug_otp` returned to client** (SEC-001 / UX-001 / BIZ-001) — signup
   OTP is fully bypassable; enables mass fake accounts + trial farming. **Fix before any
   production traffic.**
2. **Plaintext secrets in control DB** (SEC-002) — tenant DB/admin passwords, Git tokens, and
   **SSH private keys** stored unencrypted; one DB read compromises the whole platform.
3. **Tenant containers run as root on shared hosts** (SEC-003) — container breakout → host
   root → co-tenant breach.
4. **God-model `saas_instance.py` (12,324 lines)** (ARCH-001) — unmaintainable, regression-
   prone core.
5. **Control-plane DB is a fleet-wide SPOF** (ARCH-002) — one Odoo DB synchronously
   orchestrates every tenant over SSH; its downtime halts all provisioning/backup/restore.
6. **No disaster-recovery readiness** (ARCH-003) — no RTO/RPO, no tested restore, control DB
   is also the secret store.

## High Priority Issues

- No durable job queue; provisioning runs in HTTP-worker threads with silent failures
  (ARCH-004 / PERF-002).
- Partial/incomplete Kubernetes driver + leaky driver abstraction (ARCH-005).
- **No CI/CD** (ARCH-007) and **no IaC/GitOps** (ARCH-006).
- Partial-failure provisioning leaves orphaned DBs/containers/ports (ARCH-008).
- Long-held deploy/port advisory locks serialize per-host provisioning (ARCH-009 / PERF-011).
- Coarse timeouts / no retry+circuit-breaker on SSH (ARCH-010).
- **No monitoring/alerting/Sentry/Prometheus** (SEC-009).
- Tenant Postgres roles have `CREATEDB` on shared clusters; no per-tenant DB quotas (SEC-004 /
  ARCH-012).
- Plaintext credentials rendered onto hosts (SEC-006); 7-day presigned backup URLs (SEC-008);
  supply-chain exposure via tenant pip/Git into root containers (SEC-007).
- Serial backup cron can't close the window at scale (PERF-001); local-disk backup staging
  (PERF-005); unbounded crons (PERF-003); sequential metrics sampling (PERF-004).
- Billing: downgrade add-on reconciliation + effective-date (BIZ-002), trial-conversion
  proration (BIZ-003), dunning/auto-renew failure ladder (BIZ-004), free-resource economics
  (BIZ-005).
- Missing flagship product features: **teams/RBAC** (UX-002), **deploy rollback** (UX-003),
  **alerts** (UX-007), **custom domains/SSL** (UX-008), **audit log** (UX-009), **API keys**
  (UX-010).
- SPA polling leaks (UX-005) and no error backoff (UX-006 / PERF-008).

## Medium Priority Issues

Cron idempotency/partial commits (ARCH-011 / PERF-009 / BIZ-009); filestore isolation
(ARCH-013); template-DB automation (ARCH-014); recovery race (ARCH-015); webhook de-dup
(ARCH-016); Git clone TLS (ARCH-017); test-gating (ARCH-018); metrics scalability (ARCH-019);
missing indexes (PERF-007); N+1 billing aggregation (PERF-006); audit log/tamper-evidence
(SEC-010); webhook rate limiting (SEC-011); container-log authz (SEC-012); OTP brute-force
window + unhashed OTP (SEC-013); SSH command construction breadth (SEC-014); broad exceptions
(SEC-015); coarse RBAC (SEC-016); pricing-config falsy trap (BIZ-011); order-time pricing race
(UX-011 / BIZ-012); destructive-action confirmations (UX-012); logs search (UX-013); SQL
truncation (UX-014); backup progress (UX-015); optimistic UI (UX-016); empty states (UX-017);
trial eligibility messaging (UX-018); git webhook onboarding (UX-019); deploy throttle feedback
(UX-020); version-upgrade wizard (UX-021); refund caps (BIZ-006); credit expiry (BIZ-007);
suspension→deletion retention (BIZ-008); margin/cost visibility (BIZ-010).

## Low Priority Issues

Config coupling (ARCH-020); committed SPA bundles (ARCH-021); runbook documentation
(ARCH-022); backup add-on lifecycle billing (BIZ-013); reactivation semantics (BIZ-014);
region/residency default (BIZ-015); home-page 3D bundle (PERF-013); exception-masked latency
(PERF-014); SPA session model (SEC-018); CSRF posture review (SEC-019); restore-confirmation UX
(SEC-017); status page (UX-022); session-expiry warning (UX-023); copy feedback (UX-024).

---

## Missing Features (vs Odoo.sh / Heroku / Render / Railway / Fly.io / DO App Platform)

- **Team / collaborator management with RBAC** (owner/admin/developer/read-only).
- **Self-service API keys** with scopes + documented OpenAPI.
- **Configurable alerts** (resource thresholds, deploy/billing/suspension events).
- **Self-service custom domains + automated SSL** (advertised but not built).
- **Customer-facing audit log** (and internal immutable audit trail).
- **One-click deploy rollback** + auto-rollback on failed deploy.
- **In-place Odoo version upgrade wizard.**
- **Public status page + backed SLA.**
- **CI/CD pipeline, IaC, monitoring/observability stack, DR runbook** (operational platform).

## Business Risks

- **Revenue leakage / fraud:** auth backdoor enables trial farming; downgrade/trial proration
  gaps; free-resource economics without hard caps; order-time pricing race; pricing-config
  falsy trap.
- **Breach exposure:** plaintext secrets + root containers + shared hosts = a single bug can
  become an all-tenant breach and a reportable incident.
- **Compliance blockers:** no audit log, coarse RBAC, plaintext secrets, region/residency
  defaults → SOC 2 / ISO 27001 / GDPR gaps that stall enterprise deals.
- **Churn / reliability perception:** no rollback, no alerts, no status page, involuntary-churn
  risk from immature dunning, silent deploy failures.
- **Operational fragility:** control-plane SPOF, no DR, no monitoring → outages are likely,
  slow to detect, and slow to recover.
- **Acquisition cost:** material remediation + refactor effort must be priced into valuation.

---

## Recommended Roadmap

### Immediate (1-2 Weeks) — ship-blockers
- Remove `debug_otp` from API + SPA; enforce real OTP delivery (SEC-001/UX-001/BIZ-001).
- Encrypt/relocate secrets (DB/admin passwords, Git tokens, SSH private keys) to a secret
  manager; rotate all existing secrets (SEC-002/SEC-006).
- Stand up error monitoring (Sentry) + basic alerting on auth failures, cron failures, server
  health (SEC-009).
- Add a CI gate running the existing test suite + `tsc`/lint (ARCH-007).
- Harden destructive flows: typed confirmation on DB drop; checkout-redirect loading states
  (UX-012/UX-004); server-side price re-validation at order (BIZ-012).

### Short Term (1 Month)
- Non-root containers + capability drops + seccomp; remove tenant `CREATEDB`; per-tenant DB
  quotas (SEC-003/SEC-004).
- Introduce a durable job queue for provisioning/backups with retries, idempotency, and a
  client-pollable operation status (ARCH-004/PERF-002).
- Shorten advisory-lock critical sections; add SSH retries/timeouts/circuit breaker
  (ARCH-009/ARCH-010); idempotent partial-failure teardown (ARCH-008).
- Parallelize + window-bound backups and metrics; add missing indexes; batch unbounded crons
  (PERF-001/004/003/007).
- Immutable internal audit log; reduce presigned-URL TTL (SEC-010/SEC-008).
- Billing correctness: dunning state machine, downgrade add-on/effective-date, trial-conversion
  proration, invoice period-uniqueness guard (BIZ-002/003/004/009).

### Medium Term (3 Months)
- Begin god-model decomposition into bounded services/mixins behind the existing seams
  (ARCH-001).
- Split control plane vs data plane: host agents reconcile desired state; control-plane HA
  (ARCH-002).
- Build missing SaaS features: teams/RBAC, API keys, alerts, custom domains/SSL, deploy
  rollback, customer audit log (UX-002/003/007/008/009/010).
- Introduce IaC for hosts/networks/buckets + drift detection (ARCH-006); automated, versioned
  template-DB builds (ARCH-014).

### Long Term (6 Months)
- Complete or remove the Kubernetes path; make the driver fully backend-agnostic (ARCH-005).
- Define and test DR (RTO/RPO, geo-replicated backups, control-DB failover, restore drills)
  (ARCH-003).
- Per-tenant cost attribution feeding margin alerts and pricing (BIZ-010); residency-aware
  region selection (BIZ-015).
- SOC 2 / ISO 27001 readiness program (RBAC granularity, audit, secrets, retention policy).

---

## Counting & Verification Notes
- Findings are numbered per file (`SEC-/ARCH-/PERF-/UX-/BIZ-###`). Cross-cutting root causes
  (auth backdoor, plaintext secrets, pricing race) appear in more than one report under
  distinct IDs because they carry security **and** UX **and** revenue consequences; the
  aggregate count therefore exceeds the count of distinct root causes (~8 Critical roots).
- Each report ends with a "Cleared / Overstated" table listing automated-pass claims that were
  **verified false** (trial race, wallet double-spend, backup IDOR, shared network, per-instance
  shell authz, silent port collision) so the board does not act on false positives.
