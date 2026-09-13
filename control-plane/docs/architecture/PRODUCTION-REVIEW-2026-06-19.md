# Production-Readiness Review — Flaws, Gaps & Go-Live Blockers

**Date:** 2026-06-19 · **Branch:** `architecture-evolution`
**Method:** four parallel deep-dive audits (security, billing/pricing, provisioning/data/reconciler, architecture docs) reading the actual code with file:line evidence, plus findings from this session's live end-to-end testing.
**Bottom line:** the engineering is genuinely advanced and most of the architecture exists as designed — but it is **not** production-ready. There are **5 Critical** issues that can cause data corruption, silent permanent-down, real money loss, or full auth bypass; a large set of High/Medium robustness and billing gaps; documentation that **over-claims completeness** (some "done/proven" primitives are stubs); and the entire business/legal/operational half of go-live (DR, GDPR, PCI, abuse, on-call, patching) is largely absent.

> Severity legend: **Critical** = corruption / money loss / auth bypass / silent outage. **High** = exploitable abuse, concurrency hazard, or customer-visible failure with no self-heal. **Medium** = data-integrity edge cases, scale ceilings. **Low** = hardening.

---

## 0. TL;DR — fix these before anything else

| # | Blocker | Why it's a blocker | Where |
|---|---------|--------------------|-------|
| **B1** | Registration OTP returned to the client (`debug_otp`) | Phone verification is fully bypassable → unlimited fake accounts → trial/resource abuse | `saas_website/controllers/api.py:182,194`; `controllers/registration.py:193` |
| **B2** | Trial gating keyed on the contact partner, not the commercial entity | A paying customer can farm **unlimited free trials**, even after paying | `saas_core/models/res_partner.py:153`; `saas_instance.py:1176,1244,1376` |
| **B3** | Double-deploy race (recovery cron re-triggers an in-flight deploy) | Duplicate containers / corrupted DB init / orphaned ports on any slow deploy | `saas_instance.py:_do_deploy` (4973-5195, no commit) vs `_cron_recover_stuck_provisioning` (3937-3948) |
| **B4** | Server allocation overcommits (no lock; capacity off by default `max=0`) | Concurrent orders pile onto one host past its limits → outage | `saas_server.py:398-427`, defaults `:87-104` |
| **B5** | Reconciler can't heal a **deleted** container (`not_found`→`start()`) | A removed container stays silently down forever | `saas_instance.py:8131-8137` |
| **B0** | Root SSH password was pasted in chat repeatedly; not confirmed rotated; compromised test box still the basis of all "proof" | Known breach entry vector still open | `PRODUCTION-READINESS.md` P0.2/P0.4, `SECURITY-INCIDENT-2026-06-17.md` |

Everything below expands these and adds the rest.

---

## 1. Security (CRITICAL/HIGH)

### SEC-1 (Critical) — Registration OTP returned to the client → verification bypass
`register_start`/`register_resend` return `{'debug_otp': otp.code}` (`api.py:182,194`, guarded only by a `# TODO: REMOVE before production` comment). The SMS path is also best-effort (logs when no provider configured). An attacker reads the OTP from the response and verifies any phone number — **phone verification is decorative**. This is also what made the whole E2E test scriptable this session, confirming it's live.
**Fix:** delete `debug_otp`/`debug_phone_otp` from all responses; gate any echo behind a server-side dev flag never reflected to the client.

### SEC-2 (High) — No rate limiting / brute-force protection anywhere
No throttle/lockout/captcha on `login`, `register/start|resend|verify`, `check-subdomain`. OTP `_verify` (`registration.py:106`) has **no attempt counter** over a 10⁶ space with a 10-min TTL → brute-forceable even without SEC-1. Login allows unbounded credential stuffing.
**Fix:** OTP attempt counter (invalidate after ~5); per-IP+identifier rate limits on register/login; consider captcha.

### SEC-3 (High) — `access_token` is an unscoped, full-control bearer token, broadly echoed
Every instance's `portal.mixin` `access_token` grants **owner-equivalent** authority (shell, SQL, DB drop/restore) and is serialized into dashboard/list/detail/environments payloads (`api.py:1604,1637`). A leak via referer/history/support hands an attacker destructive control. No rotation/revocation surface.
**Fix:** stop returning it in list/dashboard (session cookie already authorizes the SPA); restrict token paths to read-only; add rotation/revocation; keep it out of query strings.

### SEC-4 (High) — SSRF via self-hosted Git provider URL
For a non-github/gitlab/bitbucket URL with a token, the provider base is built straight from the customer hostname (`saas_instance_repo.py:449-461`) and the server makes authenticated requests to it (branch listing, webhook register) with **no internal/link-local blocklist** → `169.254.169.254`, `10.x`, control-plane hosts reachable from the SaaS control plane via `/branches` and repo-change.
**Fix:** allowlist provider hosts; reject private/loopback/link-local resolved IPs (resolve+pin to defeat DNS rebinding); force `https`.

### SEC-5 (High) — Account enumeration
`register/start` (unauthenticated, pre-OTP) returns distinct "email already exists" vs "phone already registered" messages (`api.py:153-160`) → clean enumeration oracle for the whole customer base, unthrottled.
**Fix:** generic response; defer uniqueness conflict to post-OTP; throttle.

### SEC-Medium/Low (summary)
- **(M)** SQL "read-only" console can be flipped read-write within the **tenant's own** DB (a single `SET TRANSACTION READ ONLY` precedes attacker SQL) — limited to the customer's own isolated PG, so not a cross-tenant escape, but the advertised guarantee is false. `saas_instance.py:10418-10456`. Fix: genuinely read-only role / `default_transaction_read_only`.
- **(M)** Webhook routing secret travels in the URL path and is also the HMAC key — a path-secret leak compromises the signing key. `controllers/webhook.py:39-49`. Fix: separate routing id from signing secret.
- **(L)** Raw exception text reflected to clients (`environment_create`, storage add/release). Minimal password policy (8 chars here, 6 on reset). OTP stored plaintext. SSH host-key pinning is opt-in (AutoAdd fallback). **Customer-facing "container" wording** — fixed this session in the packages guard; audit remaining strings.

> **Confirmed sound:** SSH/Docker/k8s command building uses `shlex.quote`; DB names regex-validated; SQL/DB-ops travel base64 in env vars (not on the command line); secret fields gated behind `groups=` and never in customer serializers; webhook HMAC verified on the raw body with SHA-1 rejected + replay protection; per-tenant DB manager preserves master-password checks. Tenant **DB isolation** holds.

---

## 2. Provisioning / Data / Reconciler (CRITICAL/HIGH)

### PROV-1 (Critical) — Double-deploy race
`_do_deploy` runs as **one uncommitted transaction** (no `commit`/`flush` in 4973-5195), so `write_date` is frozen for the whole 5–15 min op. `_cron_recover_stuck_provisioning` (3937-3948) calls anything with `write_date < now-15min` "stuck", reverts it to `pending_provision`, and `_cron_retry_pending_provision` spawns a **second `_do_deploy` on the same host/ports/DB while the first still runs** → duplicate containers, corrupted DB init, IntegrityErrors. *(This session's manual deploys hit exactly this thread/commit fragility.)*
**Fix:** heartbeat via a side-cursor each phase (key recovery off that), or a per-instance PG advisory lock the recovery cron probes — the port allocator already does this at 3623.

### PROV-2 (Critical) — Server allocation overcommit
`_allocate_docker_server` (`saas_server.py:398-427`) does search→filter→`min` with **no lock**; the chosen instance isn't flipped to `provisioning` until later in the same uncommitted txn, so two concurrent orders pick the **same** least-loaded host and both pass. Capacity caps default to `0 = unlimited` (`:87-104`), so **overcommit is the default path**.
**Fix:** region-scoped advisory lock around allocate→state-set + direct SQL re-count inside the lock; set non-zero capacity defaults or warn on `max=0`.

### PROV-3 (Critical) — Reconciler can't heal a deleted container
`reconcile()` lumps `not_found` with `exited`/`dead` and calls `driver.start()` (`8131-8137`). A genuinely removed container fails `start` every cycle → **silently down forever**. This is the failure mode the reconciler most needs to cover.
**Fix:** on `not_found`, branch to recreate (`driver.create(spec)` / redeploy entrypoint).

### PROV-High
- **H1** Orphaned **`paid`** instances never deploy — recovery crons only scan `provisioning`/`pending_provision`. If `action_deploy` crashes before flipping to `provisioning`, the customer paid and gets nothing, no alert. *(Session E2E showed trials land in `pending_provision` and rely on the 15-min cron.)* Fix: sweep `paid` older than N min.
- **H2** **No `FOR UPDATE` on** stop/restart/redeploy/suspend/cancel/delete (restore/backup/plan-change *do* lock — inconsistent). Two clicks → concurrent `dropdb --force` / double prune / duplicate teardown. Fix: copy the restore lock pattern; make `_do_delete_instance` idempotent/resumable.
- **H3** `_cron_reconcile` has **no overlap guard** and runs the whole batch in one uncommitted txn — a slow run overlaps the next; a crash loses all recorded `actual_state`. Fix: `pg_try_advisory_xact_lock` + commit per server.
- **H4** Crash-loop detection uses Docker's **cumulative lifetime** `RestartCount` (only ever grows) → a healthy instance that restarted 5× over weeks gets parked `stopped` on its next restart; `restarting`/k8s `Pending` trips it with no count. Fix: restarts **within a window** / require persistence across ≥2 cycles.
- **H5** Webhook auto-deploy lock is an **in-process** `threading.Lock` dict — two workers (or webhook vs portal redeploy) race `git pull`+recreate. Fix: PG advisory lock keyed on instance id.

### PROV-Medium
- **M1 (live-metrics SerializationFailure — seen all session)**: 5 unprotected writers to the same `saas.instance` row (sampler, record cron, watch-write, **per-worker seed thread**, usage refresh); the advisory lock only serializes sampler-vs-sampler. The whole tick commits with **no savepoint**, so one contended row **rolls back every watched instance's sample**. Fix: remove the redundant seed-thread writer; wrap each write in `with cr.savepoint()`; add a small retry.
- **M2** Restore `createdb` paths miss the **PG15 public-schema grant** (`_pg_grant_public_schema` applied on provision but **not** after restore createdb at 7288/7377) → `permission denied for schema public` mid-restore on strict PG15.
- **M3** Restore is **destructive with no rollback**, and the docstring **claims a pre-restore safety snapshot that isn't taken** (`6498-6500` vs `6816`); FS is wiped (6993) before DB restore — repo unreachable mid-restore = data gone.
- **M4** Recovery cron reverts state without reconciling reality (container left running while record says `failed`). **M5** Single PG primary per host = structural scale ceiling. **M6** No cron reaps `saas.instance.backup` rows stuck in `running`.

### PROV-Low
restart/`Pending`/`paused`/`created` unhandled in reconcile; 30-min template-init timeout pauses a paying instance; orphaned `/tmp` zips on SSH failure; `restic forget --prune` failure swallowed + no prune cron (bucket grows unbounded); in-process template-build lock; port exhaustion hard-fails with no host fallback / no reclaim cron; `_parse_ram_to_gb` mis-parses bare bytes/`k` to ~0 GB (silently defeats RAM cap).

---

## 3. Billing / Pricing / Wallet (CRITICAL/HIGH)

### BILL-1 (Critical) — Unlimited free trials via partner scope
Trial state reads/writes the raw `partner_id` (any child contact), while the **wallet** correctly uses `commercial_partner_id`. `_saas_has_paid_instance` searches `('partner_id','=',self.id)` (`res_partner.py:153`); the uniqueness constraint and create-gate have the same blind spot. A company creates child contacts → a fresh trial each, forever, even after paying.
**Fix:** resolve to `commercial_partner_id` / `child_of` everywhere trial state is read/written (the single most load-bearing line is `res_partner.py:153`).

### BILL-High
- **H2 (Stranded wallet credit on cancel)**: for a deployed instance, `action_cancel` defers refund to `_do_delete_instance`, which calls `_void_unpaid_invoices_refund_credit()` **after** all SSH/Docker teardown (`6153`). If teardown raises first (SSH unreachable — common when cancelling), the reserved credit is never refunded and the invoice stays open. Fix: refund **before** queueing teardown (it's idempotent).
- **H3 (Perpetual phantom slot charge)**: deleting an extra-slot Staging/Dev env credits the current cycle but **never decrements** the parent's `staging_slots`/`dev_slots` (`action_delete_environment:2325` vs `_environment_order_lines:2032`) → parent keeps paying for a slot with no server, forever.

### BILL-Medium
- **M2** Verify `_storage_block_order_line` applies the same `period_months` factor the initial catch-up used (`:1827` vs `:1863`) — a mismatch over/under-charges yearly blocks each renewal.
- **M3** `_charge` currency guard only checks the provider **journal** currency; if `journal_id` is unset the check is skipped → multi-currency auto-renew can charge the wrong currency silently (`saas_payment.py:257`). Fix: validate `invoice.currency_id ∈ provider.available_currency_ids`, fail closed.
- **M4** Bonus (expiring) credit can resurface as never-expiring **customer_funded** via upgrade-surplus grants (`_grant_wallet_credit` defaults `CLASS_CUSTOMER`) — no real-money cash-out today, but it launders bonus→balance. Fix: re-credit unused value in the **original** class/expiry.
- **M5** Renewal auto-charge isn't idempotent against a manual payment landing mid-flight (no row lock before `_send_payment_request`) → possible double charge. **M-prorate** `_proration_credit` is purely day-based (the engine forbids day math) and can credit a **full** cycle on day 0; clamp `remaining_days`.

> **Confirmed correct (intentional policy):** yearly-discount-infra-only, children excluded from billing crons + cost added to parent exactly once, two-class wallet ledger with idempotent consume/refund (refund preserves class/expiry — bonus can't be laundered via refund), same-period upgrade doesn't reset the cycle, reversal/chargeback→suspend, renewal advances `next_invoice_date` before posting. The `config_parameter` falsy-trap is correctly avoided platform-wide.

---

## 4. Documentation flaws & illogical claims

The architecture **spec** is coherent and Phases 0–4 are genuinely code-backed. The doc problems are **over-claiming completeness**:

- **DOC-A2 (most important)** — Phase 1 is **not** fully routed through `ComputeDriver`. The zero-downtime green/blue customer redeploy (`_do_redeploy`) issues raw `docker compose down/up` at `saas_instance.py:5533,5553` — not introspection, **not** through the driver — directly contradicting "every lifecycle call site routes through the driver" and `DRIVER-BOUNDARY.md`'s "`grep docker` returns nothing." A `KubernetesDriver` would **not** be "just a new file"; the main deploy path is still hardcoded to docker-compose.
- **DOC-B1 (dangerous)** — `neutralize` is documented as a working restore/clone step but **raises `NotImplementedError`** (`dataservice/service.py:59`). A non-prod clone that should be neutralized would crash or **silently run un-neutralized → restored copy sends real customer email / fires crons / hits payment providers.**
- **DOC-B2** — `DataService.materialize` can only restore onto the snapshot's **own** instance; there is **no `clone` primitive**. "clone = snapshot + materialize(new identity)" — which Phases 6 (K8s tenant clone) and 7 (clone-upgrade-promote) and DR depend on — **does not exist**.
- **DOC-B3** — **PITR / continuous WAL archiving** (a core spec durability invariant) is **absent** (`grep wal_archive|pitr|archive_command|wal-g|pgbackrest` → nothing). Real RPO is **~24h**, contradicting the spec's "continuous WAL" promise. Not even on the P0/P1/P2 gap list.
- **DOC-B4** — Spec's content-addressed (CAS) filestore was replaced by JuiceFS CoW; reasonable, but no doc reconciles spec-vs-reality.
- **DOC-A1/A3/A4/A5 (inconsistencies)** — stale god-model counts ("248 methods / 11,334 lines" vs actual ~284 / 12,115); test counts inconsistent (docs say 104; tree has **113**; PHASE-1-STATUS says both 66 and 70); "Phases 0–6 done / every path live-verified" overstated (Phase 5 = 1 seam of ~13 steps; Phase 6 = unit-tested command strings, never run on a cluster); **branch contradiction** — `IMPLEMENTATION-PLAN`/`SESSION_NOTES` say live branch is `newhosting` and the deploy runbook does `reset --hard origin/newhosting`, but all work is on `architecture-evolution` → **following the runbook deploys the wrong/stale branch.**
- **DOC-C (weak reasoning)** — "real-infra proven" rests on a **single disposable box that was running a cryptominer during the test window** (benchmarks/resource baselines are tainted and feed the margin & scale math); "verified" mostly means run-once on an idle/compromised box; Phase 6 "proven" = asserting a kubectl string; Phase 3 "rolls back" but the deploy-rollback path isn't built.

---

## 5. Missing for production — business / legal / operational

`PRODUCTION-READINESS.md` is solid on infra hygiene (audit box, rotate creds, lock PG, rebuild box, backup/restore) but misses most of the *non-infra* half of go-live:

- **DR / RPO / RTO** — no targets, no cross-region replication; given no PITR, **RPO is silently 24h**. Should be P0/P1, not the passing P2.7 mention.
- **GDPR / data retention / right-to-erasure / DPA** — hand-waved as "user's decision." For the **Frankfurt (EU)** region this is a **legal go-live blocker**. Backups must expire on tenant delete.
- **Payment / PCI / tax** — wallet + multiple providers exist, but nothing on PCI scope, provider compliance, chargeback/fraud, or invoice/tax legal correctness.
- **ToS / AUP / abuse handling** — build workers **execute untrusted customer code**, and the test box was **already used to host a botnet spreader**. No acceptable-use policy, abuse reporting, or takedown path.
- **Email deliverability** — SPF/DKIM/DMARC, shared-IP reputation, bounce handling (worse given the neutralize stub, DOC-B1).
- **Patch / CVE management** — for base images, OS, JuiceFS/MinIO/registry. The incident itself was an unpatched-exposure problem.
- **On-call / incident response / status page / SLA** — alerting (P1.8) is not the same as a human rotation, escalation, customer status page, or SLA/credits.
- **Audit logging** — who did what in the control plane (admin actions); spec mentions log retention, readiness doc has none.
- **Backup hardening** — restore **drills as a cadence** (not one-time), plus **encryption + immutability** (ransomware-resistant), not just success-alerting.
- **CI before launch** — running the 113-test suite on every push should **precede** go-live (currently P2.3, "after launch").
- **Re-prioritize:** rootless build worker (untrusted code, P2.1) and per-tenant DB-manager exposure (P2.5) are arguably **P0 security**, not post-launch hardening.

---

## 6. Recommended go-live gate (ordered)

**Phase A — security & integrity (blockers):**
1. Remove `debug_otp` (SEC-1); add auth/OTP/registration rate-limiting (SEC-2).
2. Fix trial partner scope (BILL-1).
3. Rotate the root SSH password + all chat-exposed creds; lock PG to the docker bridge + firewall; **rebuild the compromised box** and re-prove from the idempotent scripts; treat any image built on it (e.g. `odoo-base:18.0`) as untrusted (B0 + supply-chain).
4. SSRF allowlist (SEC-4); stop echoing `access_token` + scope it (SEC-3); enumeration fix (SEC-5).
5. Make iptables PG-firewall + build-egress rules **reboot-survivable**.

**Phase B — correctness & resilience (blockers):**
6. Double-deploy race (PROV-1) + allocation overcommit/capacity defaults (PROV-2) + reconcile deleted-container (PROV-3).
7. Locking on all lifecycle actions + reconcile overlap guard (PROV-H2/H3); crash-loop window (PROV-H4); orphaned-`paid` sweep (PROV-H1); webhook PG lock (PROV-H5).
8. Stranded-credit-on-cancel (BILL-H2) + phantom-slot charge (BILL-H3); verify yearly-block + multi-currency math (BILL-M2/M3).
9. Live-metrics serialization fix (PROV-M1); PG15 restore grant (PROV-M2); destructive-restore safety snapshot or honest docstring (PROV-M3).
10. Implement (or explicitly descope, in docs) `neutralize` + cross-target `materialize`/`clone` (DOC-B1/B2) before relying on clone/upgrade/DR.

**Phase C — production-grade infra & rollout (P1):**
DO Spaces + real registry; staging env; load/concurrency test; canary soak with proven rollback; baseline monitoring/alerting; **CI gating before launch**.

**Phase D — business/legal/ops (parallel, but some are go-live blockers):**
GDPR/retention + DR/RPO/RTO + PITR decision; ToS/AUP/abuse; PCI/tax; email deliverability; on-call/status page/SLA; audit logging; patch cadence; backup encryption/immutability + restore drills.

**Phase E — doc hygiene:** correct the branch/deploy runbook (A5), reconcile test/line counts (A1/A3), downgrade overstated "done/proven" claims (A2/A4/C), and add the missing P0/P1 items (DR, GDPR, PCI, abuse, PITR) to `PRODUCTION-READINESS.md`.

---

## 7. Net assessment

Strong, real engineering with clean seams and a coherent target — but **over-claimed as nearly done.** The platform should be treated as **late-stage development, not pre-production**: it needs a security pass (auth bypass + abuse + SSRF), a concurrency/robustness pass (deploy race, overcommit, locking, reconcile gaps), three real billing fixes (trial scope, stranded credit, phantom slot), honest treatment of the stubbed clone/neutralize/PITR primitives the upper phases depend on, and the entire business/legal/operational layer (DR, GDPR, PCI, abuse, on-call) that isn't started. None of these is a redesign; they are a focused, prioritized hardening program on top of a sound foundation.

*All findings carry file:line evidence in the four source audits; nothing in the codebase was modified to produce this report.*
