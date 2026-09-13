# BUSINESS LOGIC & REVENUE AUDIT — Odoo 18 SaaS Platform

> Reviewed as CTO + CFO: subscription/billing/invoice/trial lifecycle, upgrade/downgrade,
> cancellation/reactivation, provisioning/suspension/deletion, and abuse/fraud vectors.
> File:line verified; overstated automated findings cleared at the end.
>
> Severity counts (this file): **Critical 1 · High 4 · Medium 7 · Low 3** (15 findings)

---

## BIZ-001
- **Severity:** Critical
- **Component:** Signup integrity → trial/abuse economics
- **File Path:** `saas_website/controllers/api.py`; `veltnex/src/pages/Register.tsx`
- **Line Numbers:** `api.py:233, 250`; `Register.tsx:130, 224`
- **Description:** Because the OTP is handed to the client (SEC-001/UX-001), the phone-identity
  gate is void. Attackers can mass-create verified accounts with disposable numbers, defeating
  the per-identity trial limit and any anti-abuse logic keyed on "verified" accounts.
- **Business Impact:** Unbounded free-trial farming = direct margin loss (free compute,
  storage, support) plus skewed funnel/metrics that mislead investors. Fraud and chargeback
  surface expands.
- **Technical Impact:** Every downstream control assuming verified humans is unreliable.
- **Recommended Fix:** Remove debug OTP; enforce real SMS/email verification; add velocity/
  fraud checks (disposable-number detection, IP/device throttles).

---

## BIZ-002
- **Severity:** High
- **Component:** Downgrade revenue & add-on reconciliation
- **File Path:** `saas_website/controllers/portal.py`
- **Line Numbers:** plan-change/proration `:640-650, 698-754`
- **Description:** Plan-change proration is computed for the base plan, but add-on lines (e.g.
  daily-backup) are not clearly re-validated/re-priced on downgrade, and the pending change is
  stored without an explicit effective-date field, so the renewal cron must infer timing.
- **Business Impact:** Revenue leakage (add-ons under-/over-charged after a downgrade) and
  billing disputes from mistimed plan changes — both erode trust and margin.
- **Technical Impact:** Downgrade is a multi-field state change without a single authoritative
  effective-date; reconciliation is implicit.
- **Recommended Fix:** Add `pending_plan_effective_date`; re-price all add-ons on plan change;
  unit-test prorated upgrade/downgrade invoices including add-ons.

---

## BIZ-003
- **Severity:** High
- **Component:** Trial-to-paid conversion proration
- **File Path:** `saas_website/controllers/main.py`
- **Line Numbers:** conversion path `~:1212-1227`
- **Description:** On converting a trial to paid before the trial ends, there is no clear
  credit for unused trial days; the customer is charged a full period. (The 25%×3 trial-credit
  policy from billing-overhaul v46 governs trial *discounting*, but the day-of-conversion
  proration is the gap here.)
- **Business Impact:** Either overcharging (churn/disputes) or, if handled inconsistently,
  under-charging — both are CFO-relevant and damage trust.
- **Technical Impact:** Conversion does not compute remaining-period credit deterministically.
- **Recommended Fix:** Define and test conversion proration explicitly; align with the v46
  policy.

---

## BIZ-004
- **Severity:** High
- **Component:** Auto-renew / dunning failure handling
- **File Path:** `saas_core/models/saas_instance.py`; `saas_core/data/saas_recurring_billing_cron.xml`
- **Line Numbers:** `_cron_check_overdue_invoices`, `_cron_retry_failed_payments`, `_cron_send_renewal_reminders` (cron defs `:5-96`)
- **Description:** Dunning/retry crons exist, but there is no surfaced state machine for the
  failure ladder (retry schedule → grace period → suspend → delete) with customer-visible
  status, and payment-retry depends on stored `payment.token` (saved card) whose failure modes
  (expired card, revoked token) need explicit handling.
- **Business Impact:** Either premature suspension (involuntary churn) or indefinite free
  service (revenue leakage) depending on edge-case handling; involuntary churn is a leading
  SaaS revenue killer.
- **Technical Impact:** Failure ladder is spread across crons without a single source of truth
  or customer-facing dunning status.
- **Recommended Fix:** Model an explicit dunning state machine with configurable retry
  schedule, grace, and notifications; expose status in the portal; handle token-invalid cases.

---

## BIZ-005
- **Severity:** High
- **Component:** Free reserve-only / environment provisioning economics
- **File Path:** `saas_core/models/saas_instance.py`; `saas_website/controllers/portal.py`
- **Line Numbers:** environments/reservation logic (recent commits: dev/stage reserve-only, capped & free)
- **Description:** Per recent commits, dev/stage environment slots are reserve-only and some
  creation is "capped & free." Free provisioning paths (and trial environments) consume real
  compute/storage; without strict caps + abuse monitoring, these are a direct cost leak,
  especially given BIZ-001 (cheap account creation).
- **Business Impact:** Aggregate free-tier compute can quietly dominate infra cost at scale;
  hard to detect without per-account cost attribution.
- **Technical Impact:** No per-account/aggregate cost ceiling or anomaly alerting on free
  resources.
- **Recommended Fix:** Enforce hard caps per account and globally; meter and alert on free
  resource consumption; require verified payment method before free env provisioning.

---

## BIZ-006
- **Severity:** Medium
- **Component:** Refund caps vs original charge
- **File Path:** `saas_core/models/saas_wallet.py`
- **Line Numbers:** `_refund_move` `:244-280`
- **Description:** Refunds restore consumed credit to original lots with idempotency + a
  `max_amount` cap (good). The residual risk is when an invoice is edited/reduced after
  payment: the refund should be bounded by the *current* residual, and that linkage should be
  asserted, not assumed.
- **Business Impact:** Potential over-refund (wallet inflation beyond paid value) in edited-
  invoice edge cases.
- **Technical Impact:** Refund bound depends on caller passing correct `max_amount`.
- **Recommended Fix:** Derive the refund ceiling server-side from the move's current paid
  residual; add a test for post-payment invoice edits.

---

## BIZ-007
- **Severity:** Medium
- **Component:** Wallet credit expiry
- **File Path:** `saas_core/models/saas_wallet.py`; `saas_core/data/saas_recurring_billing_cron.xml`
- **Line Numbers:** `_cron_expire_credits` (cron `:80-84`)
- **Description:** Credits expire via cron; expiry boundary conditions (consume-vs-expire on
  the same day, timezone of `expiry_date`) need explicit, tested semantics so customers aren't
  surprised by lost credit and revenue recognition stays correct.
- **Business Impact:** Customer disputes over "disappeared" credit; revenue-recognition
  accuracy.
- **Technical Impact:** Boundary/timezone handling is implicit.
- **Recommended Fix:** Define expiry semantics precisely (inclusive/exclusive, TZ), notify
  before expiry, and test boundaries.

---

## BIZ-008
- **Severity:** Medium
- **Component:** Suspension → deletion data-retention policy
- **File Path:** `saas_core/models/saas_instance.py`; `saas_core/data/saas_trial_expiry_cron.xml`, `saas_storage_check_cron.xml`
- **Line Numbers:** suspension/trial-expiry crons
- **Description:** The lifecycle from non-payment/trial-end → suspend → delete needs a clearly
  defined, customer-communicated retention window and a recoverable grace state. If deletion is
  too eager, customers lose data (legal/CX risk); too lax, costs leak.
- **Business Impact:** Data-loss complaints and potential liability vs cost leakage; both
  material.
- **Technical Impact:** Retention windows and recoverability are not clearly centralized.
- **Recommended Fix:** Define retention/grace policy, keep a recoverable suspended state with a
  countdown, notify before deletion, and retain a final backup for a defined period.

---

## BIZ-009
- **Severity:** Medium
- **Component:** Invoice generation idempotency under cron concurrency
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** `_cron_generate_recurring_invoices`; commits in loops `:4458, 4504`
- **Description:** With per-iteration commits and unbounded sweeps (PERF), a cron crash/re-run
  must not double-issue renewal invoices; idempotency must be guaranteed by a "already invoiced
  for period" guard, not by run-completion.
- **Business Impact:** Double-invoicing risk = chargebacks and trust damage; or skipped
  invoicing = revenue loss.
- **Technical Impact:** Idempotency depends on precise period-guard logic.
- **Recommended Fix:** Enforce a unique (instance, billing-period) invoice guard; make
  generation idempotent and re-runnable.

---

## BIZ-010
- **Severity:** Medium
- **Component:** Margin/cost visibility
- **File Path:** `saas_core/data/saas_margin_alert_cron.xml`; `saas_core/tests/test_margin.py`
- **Line Numbers:** margin alert cron
- **Description:** A margin-alert mechanism exists, but with no real cost telemetry
  (SEC-009/ARCH) tying actual infra spend to per-tenant revenue, margin alerts may be based on
  modeled rather than measured cost, masking unprofitable tenants.
- **Business Impact:** Unprofitable accounts (heavy free/cheap tenants) go undetected; pricing
  decisions rest on estimates.
- **Technical Impact:** No measured per-tenant cost basis.
- **Recommended Fix:** Attribute real infra cost per tenant (compute/storage/backup/egress) and
  feed margin alerts from measured data.

---

## BIZ-011
- **Severity:** Medium
- **Component:** Pricing-config robustness
- **File Path:** `saas_core/models/saas_pricing.py`, `res_config_settings.py`
- **Line Numbers:** pricing engine; config-param falsy trap (project memory)
- **Description:** Pricing depends on config settings subject to the documented Boolean/Integer
  `config_parameter` falsy-value trap (a `0`/`False` can delete the row → default returns),
  which can silently change effective pricing (e.g. a discount reverting to default).
- **Business Impact:** Silent mispricing affects every new order — direct revenue impact.
- **Technical Impact:** Fragile config semantics for pricing inputs.
- **Recommended Fix:** Handle pricing settings via explicit get/set (not `config_parameter`)
  and add startup validation + tests asserting effective prices.

---

## BIZ-012
- **Severity:** Medium
- **Component:** Pricing recompute race (order-time)
- **File Path:** `veltnex/src/pages/Hosting.tsx`
- **Line Numbers:** `:225-239`
- **Description:** The client-side pricing race (UX-011) can present a price inconsistent with
  the selected configuration; if the displayed price is trusted at checkout without server
  re-validation, customers can be charged the wrong amount.
- **Business Impact:** Mischarges / disputes; potential under-charging if a cheaper stale price
  is submitted.
- **Technical Impact:** Client-displayed price must never be authoritative.
- **Recommended Fix:** Always recompute and validate price server-side at order creation;
  reject client-supplied prices.

---

## BIZ-013
- **Severity:** Low
- **Component:** Backup add-on billing on lifecycle changes
- **File Path:** `saas_core/data/saas_recurring_billing_cron.xml`
- **Line Numbers:** `_cron_renew_daily_backup_addons` (cron `:92-96`)
- **Description:** Daily-backup add-on renewal runs as its own cron; its interaction with
  suspension/downgrade (should it bill while suspended? after downgrade?) needs explicit rules
  to avoid charging for a service not rendered.
- **Business Impact:** Small but real disputes over add-on charges during inactive periods.
- **Technical Impact:** Add-on billing not clearly coupled to instance state.
- **Recommended Fix:** Gate add-on billing on active instance state; prorate on changes.

---

## BIZ-014
- **Severity:** Low
- **Component:** Reactivation path
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** lifecycle/state transitions
- **Description:** Reactivation after suspension/cancellation should deterministically restore
  the correct plan, pricing, and (if within retention) data; if reactivation re-prices at
  current rates without honoring the prior contract, customers may be surprised.
- **Business Impact:** CX friction on win-back; potential pricing inconsistencies.
- **Technical Impact:** Reactivation pricing/data-restore rules not clearly centralized.
- **Recommended Fix:** Define reactivation semantics (pricing + data window) and test them.

---

## BIZ-015
- **Severity:** Low
- **Component:** Region default & cheapest-region policy
- **File Path:** `saas_core/models/saas_region.py`, `saas_pricing.py`
- **Line Numbers:** region default logic
- **Description:** Policy sets cheapest/recommended region as default (intentional per project
  memory). Worth ensuring the default never silently routes a customer to a region with
  different data-residency implications than they expect (compliance-relevant for enterprise).
- **Business Impact:** Data-residency mismatch can block enterprise/regulated customers.
- **Technical Impact:** Default region selection has compliance side effects.
- **Recommended Fix:** Make region choice explicit at checkout with residency notes; don't
  silently default regulated workloads.

---

## Cleared / Overstated (verified)

| Claim | Verdict | Evidence |
|---|---|---|
| Concurrent trial creation mints multiple free trials | **Cleared** | `SELECT ... FOR UPDATE` on commercial partner inside `create()` (`saas_instance.py:1269-1278`) serializes; `_saas_has_paid_instance` also gates. |
| Wallet double-spend via concurrent debit | **Cleared** | `_consume` is `_lock()`-guarded and idempotent per move (`saas_wallet.py:188-218`). |
| Renewal double-invoice race | **Open but lower than claimed** | No proof of a missing guard found in read; tracked as BIZ-009 (verify period-uniqueness guard) rather than a confirmed defect. |
