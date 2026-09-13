# MASTER EXECUTIVE REPORT — V2 (Second-Pass Due Diligence)

**Premise:** A fresh re-audit of the entire repository **assuming every V1 finding is already
fixed**, hunting only for NEW issues missed in the first pass. Focus areas (as requested):
**scalability, tenant isolation, billing correctness, provisioning reliability, customer
experience.**

**Method:** Parallel deep-dives followed by source verification. As in V1, automated claims
were re-checked against code; several over-stated "Critical/High" items were **disproven** and
are recorded in each report's "Cleared / Overstated" section rather than inflating the counts.

---

## Executive Summary

After assuming V1 remediation, **no new Critical-severity defects were found.** The V2 surface
is dominated by **second-order scaling/concurrency issues** (shared-proxy nginx races, capacity
cache races, lock-scope and sampler ceilings), **provisioning-lifecycle edge cases** (partial-
create subdomain reservation, stale-health overcommit, non-capacity-aware retry, silent
background-thread death), **billing precision/transparency mismatches** (portal vs backend
proration, discounted yearly floor, pre-tax wallet cap), and a **broad customer-experience gap
set** (no in-app password reset, session-expiry masking, destructive env-merge affordances,
billing transparency, mobile/a11y). The most consequential theme for the "thousands of tenants"
goal is **concurrency correctness on shared hosts/proxies and capacity accounting** — these are
the items that turn an isolated incident into a multi-tenant one.

Notably, an automated pass claimed five Critical/High **cross-tenant data leaks** (including
`github_token`). Verification showed customers are `base.group_portal` users with **no ACL** to
those models, so the API's `sudo()`+partner checks are the real gate — the leaks are **not
exploitable**; the residual is only a defense-in-depth gap (downgraded to Low).

### Scores (V2 — incremental risk only; assumes V1 fixed)

| Dimension | Score (/10) | Note |
|---|---:|---|
| Scalability | 5 | Concurrency races on shared proxy/capacity + sampler/lock ceilings cap multi-host scale. |
| Tenant Isolation | 7 | Current model is sound (ACL + partner-scoped rules); gaps are DiD + scoping-key inconsistency. |
| Billing Correctness | 6 | No new revenue-critical bug; precision/transparency mismatches + floor-policy ambiguity. |
| Provisioning Reliability | 5 | Lifecycle edge cases (partial create, overcommit, retry, silent thread death). |
| Customer Experience | 5 | Real journey gaps: recovery, billing transparency, destructive-action safety, mobile/a11y. |

> New confirmed findings: **Critical 0 · High 8 · Medium 20 · Low 13 = 41**, across five reports.

---

## Critical Issues
None new (after assuming V1 fixes and clearing the over-stated cross-tenant claims).

## High Priority Issues
- **SCALE-001** Concurrent `nginx reload` on a shared proxy has no per-proxy lock → routing
  disruption blast radius across co-located tenants.
- **SCALE-002** Port advisory lock held across nginx + certbot (60-120s) → per-host provisioning
  serializes during cert issuance.
- **SCALE-005** Server-capacity cache read/invalidate race → concurrent allocators overcommit a
  host beyond `max_*` limits.
- **PROV-001** Background DB-op thread can die unguarded → operation stuck `running` ~30 min,
  customer sees an indefinite spinner.
- **CX-001** No in-SPA password reset (dumps users to Odoo `/web/reset_password`).
- **CX-002** Session expiry masked as "Instance not found" instead of re-auth.
- **CX-008** Flagship drag-merge is effectively undiscoverable.
- **CX-009** Production merge has no danger styling / second confirmation → accidental live deploys.

## Medium Priority Issues
Scalability: nginx non-atomic writes (SCALE-003), metrics table growth/retention (SCALE-004),
port-exhaustion dead-end (SCALE-006), single-global sampler ceiling (SCALE-007), template-build
lock granularity (SCALE-008). Provisioning: partial-create subdomain reservation (PROV-002),
stale-health overcommit/no pending fallback (PROV-003), non-capacity-aware retry (PROV-004).
Billing: portal vs backend proration `-2` (BILL-V2-001), discounted yearly floor (BILL-V2-002),
pre-tax wallet cap (BILL-V2-003), pre-confirmation wallet lock (BILL-V2-004). Isolation:
partner/commercial/company scoping inconsistency breaking team access (ISO-002). CX: late
duplicate-account detection (CX-003), no at-risk/payment flag on list (CX-004), no next-charge
date (CX-005), restore-dialog wording (CX-007), env-delete confirmation tone (CX-010), misleading
backups empty state (CX-011), no account security controls/2FA (CX-012).

## Low Priority Issues
SCALE-009/010/011 (region-less global lock, unbatched reaper, in-process sampler de-dup);
PROV-005/006 (subdomain reuse, per-op SSH timeouts); BILL-V2-005/006 (advertised-vs-realized
discount, `0.0` yearly sentinel); ISO-001/003 (record-rule DiD gap, company-based staff scope);
CX-006/013/014/015/016/017 (invoice payment method, command palette, dialog touch dismiss, help
anchor on mobile, OTP cooldown visibility, env loading skeleton).

## Missing Features (still, beyond V1's list)
- Customer-facing **account security**: 2FA, active-session management, sign-in history (CX-012).
- **Self-serve password reset** inside the product (CX-001).
- **Billing transparency** surfaces: next charge date/amount, payment-method on invoice,
  at-risk indicators (CX-004/005/006).

## Business Risks
- **Scaling-correctness risk:** shared-proxy and capacity races mean growth itself increases the
  probability of multi-tenant incidents and silent overcommit.
- **Revenue precision/trust risk:** quote-vs-invoice proration mismatch and a discounted yearly
  floor create disputes and small, systematic leakage on floored plans.
- **Churn/data-loss risk:** misleading backups empty state, weak destructive-action affordances,
  and opaque session/billing states drive avoidable loss and support cost.
- **Team-adoption blocker:** partner-exact record-rule scoping prevents multi-user customers from
  sharing their own instances (compounds the V1 missing-teams gap).

---

## Recommended Roadmap (V2 increment)

### Immediate (1-2 Weeks)
- Per-proxy lock around nginx write/reload + atomic config rename (SCALE-001/003).
- Invalidate-and-read capacity strictly after the allocation lock (SCALE-005).
- Wrap background DB-op target in fail-fast try/except + heartbeat (PROV-001).
- In-SPA (or clearly-explained) password reset; centralize session-expiry → re-auth (CX-001/002).
- Danger styling + second confirm on production merge; typed confirm on env delete (CX-009/010).

### Short Term (1 Month)
- Narrow the deploy critical section: release port lock before nginx/certbot (SCALE-002).
- Capacity-aware pending-provision retries + email on "queued for capacity"; strict overcommit
  health + pending fallback (PROV-003/004).
- Unify proration to a single helper (remove `-2`); decide and codify the yearly-floor policy;
  size wallet from the confirmed, tax-aware invoice total (BILL-V2-001/002/003/004).
- Billing transparency UI: next charge date/amount, at-risk badges, payment method on invoice
  (CX-004/005/006); fix misleading backups empty state (CX-011).

### Medium Term (3 Months)
- Shard the live-metrics sampler by host (remove the global-lock ceiling); partition/retire the
  metrics table (SCALE-004/007).
- Fix template-build lock granularity and region-less global allocation lock (SCALE-008/009).
- Canonicalize the tenant boundary to `commercial_partner_id` across record rules and API; add
  isolation tests for multi-contact accounts (ISO-002); add partner-scoped record rules as DiD
  (ISO-001).
- Account security controls: 2FA, session management, activity log (CX-012).

### Long Term (6 Months)
- Graceful port-pool/capacity degradation (route/queue instead of hard-fail) and partial-create
  atomicity/subdomain reuse (SCALE-006, PROV-002/005).
- Mobile/a11y pass (dialog touch dismiss, help anchors, skeletons) and command-palette verbs
  (CX-013/014/015/017).

---

## Counting & Verification Notes
- Findings are numbered per area (`SCALE-/ISO-/BILL-V2-/PROV-/CX-###`). All five reports end with
  a **Cleared / Overstated** table documenting automated claims that verification disproved:
  the five "cross-tenant leak" claims, the trial concurrent-create race (V1-cleared FOR UPDATE),
  the storage-block "free month", repeated-upgrade double-credit, and the yearly-support
  "asymmetry" (by-design policy).
- Zero new Criticals is an honest, deliberate result of (a) assuming V1 remediation and (b) not
  inflating disproven items — not a lack of scrutiny.
