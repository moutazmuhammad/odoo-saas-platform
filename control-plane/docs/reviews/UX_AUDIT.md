# UX & PRODUCT AUDIT — VELTNEX SaaS Portal

> Reviewed as a paying customer and as a UX/Product/QA director, benchmarked against Odoo.sh,
> Heroku, Render, Railway, Fly.io, DO App Platform. Scope: `veltnex/src` (React SPA) +
> portal JSON API. References give component + line where applicable.
>
> Severity counts (this file): **Critical 3 · High 7 · Medium 11 · Low 3** (24 findings)

---

## UX-001
- **Severity:** Critical
- **Component:** Registration (debug OTP on screen)
- **File Path:** `veltnex/src/pages/Register.tsx`
- **Line Numbers:** `:130, 224` (`setDebugOtp(... // TODO: REMOVE before production`); render ~`:352-359`
- **Description:** The signup screen renders the OTP code returned by the API (see SEC-001).
  Beyond the security hole, it is a glaring "unfinished product" signal in the first
  impression of every new customer.
- **User Impact:** Erodes trust at the most important moment (signup).
- **Business Impact:** Conversion damage + the security breach in SEC-001.
- **Recommended Fix:** Remove debug OTP UI and API field; deliver via SMS/email only.

---

## UX-002
- **Severity:** Critical (product gap)
- **Component:** Team / collaborator management
- **File Path:** none — feature absent across `veltnex/src/pages/portal/*`
- **Line Numbers:** n/a
- **Description:** No way to invite teammates, assign roles, or share instance access. Every
  competitor offers team membership with RBAC (owner/admin/developer/read-only). Access is
  effectively single-user.
- **User Impact:** Teams cannot collaborate; they resort to sharing one login (insecure) or
  cannot use the product.
- **Business Impact:** Blocks mid-market/enterprise sales; hard ceiling on TAM; churn as
  customers grow into teams.
- **Recommended Fix:** Build collaborators (invite flow + role matrix), backed by partner/role
  records and record rules; tie to pricing tiers.

---

## UX-003
- **Severity:** Critical (product gap)
- **Component:** Deploy rollback
- **File Path:** `veltnex/src/components/DeploymentHistory.tsx`; `veltnex/src/pages/portal/Code.tsx`
- **Line Numbers:** history renders builds; no rollback action
- **Description:** Deployment history is shown but there is no one-click rollback to a previous
  build and no auto-rollback on failed deploy. Competitors treat this as table stakes for
  production safety.
- **User Impact:** A bad deploy means manual Git revert + redeploy — minutes of downtime vs
  seconds; high stress for production users.
- **Business Impact:** Reliability perception damage; churn risk for the most valuable
  (production) customers.
- **Recommended Fix:** Add rollback per build + optional auto-rollback on health-check failure;
  back with an immutable build/artifact history (`saas.build`).

---

## UX-004
- **Severity:** High
- **Component:** Checkout redirect feedback
- **File Path:** `veltnex/src/pages/portal/Backups.tsx`, `InstanceDetail.tsx`
- **Line Numbers:** `Backups.tsx:~64`; `InstanceDetail.tsx:178-189`
- **Description:** Actions that return a checkout URL do `window.location.href = url` with no
  spinner or button-disable; there's a 1-2s dead period where the UI appears frozen.
- **User Impact:** Users re-click or abandon, causing duplicate orders / accidental charges.
- **Business Impact:** Support tickets, disputed charges, cart abandonment.
- **Recommended Fix:** Set a loading state, disable the button, show "Redirecting to
  checkout…" before navigation.

---

## UX-005
- **Severity:** High
- **Component:** Polling lifecycle leaks
- **File Path:** `veltnex/src/components/DeploymentHistory.tsx`; `veltnex/src/pages/portal/Backups.tsx`, `Databases.tsx`
- **Line Numbers:** `Backups.tsx:~85`; `Databases.tsx:~122`; DeploymentHistory poll
- **Description:** Several `setInterval` polls lack an `alive`/unmount guard, so they keep
  firing and call `setState` on unmounted components after navigation.
- **User Impact:** Console errors, wasted requests, sluggishness over a long session.
- **Business Impact:** Avoidable API load and degraded experience at scale.
- **Recommended Fix:** Standard `let alive = true` + cleanup clearing interval and guarding
  setState (the pattern is already used elsewhere — apply consistently).

---

## UX-006
- **Severity:** High
- **Component:** Polling backoff on errors
- **File Path:** `veltnex/src/context/InstancesContext.tsx`; `veltnex/src/pages/portal/InstanceDetail.tsx`
- **Line Numbers:** `InstancesContext.tsx:~87`; `InstanceDetail.tsx:97-108`
- **Description:** On poll failure (network/401/5xx) the SPA silently retries every ~4s with no
  backoff and no stop on `auth_required`, hammering a degraded or expired-session backend.
- **User Impact:** After session expiry the app neither recovers gracefully nor warns; just
  loops failing.
- **Business Impact:** Load amplification during incidents (see PERF-008).
- **Recommended Fix:** Exponential backoff + jitter; detect `auth_required` → redirect to
  login; pause when tab hidden.

---

## UX-007
- **Severity:** High (product gap)
- **Component:** Alerts / notifications
- **File Path:** `veltnex/src/components/NotificationsBell.tsx` (ornamental); no alerts page
- **Line Numbers:** n/a
- **Description:** No configurable alerts (CPU/RAM/storage thresholds, deploy failure, billing
  events, suspension). The notifications bell is not a real alerting system. Customers must
  watch dashboards manually.
- **User Impact:** Customers discover outages, full disks, and billing problems too late.
- **Business Impact:** Higher churn and support cost; no premium-support/SLA upsell.
- **Recommended Fix:** Alert rules + channels (email/Slack/webhook) driven by the metrics
  pipeline; surface in a notifications center.

---

## UX-008
- **Severity:** High (product gap)
- **Component:** Custom domains / SSL self-service
- **File Path:** none — marketing promises it, no portal UI
- **Line Numbers:** Home page claims "Free SSL & custom domains"
- **Description:** No self-service domain page (CNAME validation, auto Let's Encrypt). The
  homepage advertises the capability but customers must contact support.
- **User Impact:** Cannot go live on their own domain without a support round-trip.
- **Business Impact:** Slower time-to-value; support load; promise/feature mismatch (a
  diligence concern).
- **Recommended Fix:** Domains page with CNAME instructions, validation polling, and
  automated cert issuance/renewal.

---

## UX-009
- **Severity:** High (product gap)
- **Component:** Audit log (customer-facing)
- **File Path:** none
- **Line Numbers:** n/a
- **Description:** Customers have no record of who on their account did what (scaled, deployed,
  restored, deleted). Enterprise buyers require this for SOC 2 / internal compliance.
- **User Impact:** No accountability within the customer's own team.
- **Business Impact:** Blocks enterprise adoption; pairs with the missing internal audit log
  (SEC-010).
- **Recommended Fix:** Per-instance/account audit log surfaced in the portal.

---

## UX-010
- **Severity:** High (product gap)
- **Component:** API keys / programmatic access
- **File Path:** none — only session-cookie JSON API exists
- **Line Numbers:** n/a
- **Description:** No self-service API tokens with scopes; customers cannot automate
  provisioning, deploys, or billing reads, nor integrate CI/CD.
- **User Impact:** Forces manual UI work; no automation.
- **Business Impact:** Loses developer-centric customers; no platform/automation upsell.
- **Recommended Fix:** API key management (create/revoke/scope) + documented OpenAPI.

---

## UX-011
- **Severity:** Medium
- **Component:** Pricing recompute race
- **File Path:** `veltnex/src/pages/Hosting.tsx`
- **Line Numbers:** `:225-239` (price effect lacks cancel flag)
- **Description:** Rapid region/config changes fire overlapping price calculations; a stale
  response can overwrite the current one because the pricing effect lacks the `cancelled`
  guard used in sibling effects.
- **User Impact:** Customer may see (and order at) the wrong price for the selected region.
- **Business Impact:** Mischarges / disputes; trust erosion.
- **Recommended Fix:** Add a `cancelled` flag / AbortController and ignore stale responses.

---

## UX-012
- **Severity:** Medium
- **Component:** Destructive-action confirmation (drop database)
- **File Path:** `veltnex/src/pages/portal/Databases.tsx`
- **Line Numbers:** drop button handler
- **Description:** Database drop lacks a strong typed confirmation (e.g. "type DROP <db>"),
  risking one-click loss of a production database.
- **User Impact:** Catastrophic accidental data loss.
- **Business Impact:** Severe customer incident; potential liability.
- **Recommended Fix:** Require typed confirmation + brief undo window; reuse the restore
  confirmation pattern.

---

## UX-013
- **Severity:** Medium
- **Component:** Logs viewer — no search/filter
- **File Path:** `veltnex/src/pages/portal/Logs.tsx`
- **Line Numbers:** renders ~600 recent lines, no filter
- **Description:** No search, level filter, or time filter over the log stream; finding an
  error in hundreds of lines is impractical.
- **User Impact:** Logs are near-useless for real debugging.
- **Business Impact:** Drives demand for external logging; lowers perceived platform value.
- **Recommended Fix:** Client-side search + level toggles; server-side query for older logs.

---

## UX-014
- **Severity:** Medium
- **Component:** SQL console — silent truncation
- **File Path:** `veltnex/src/pages/portal/SqlConsole.tsx`
- **Line Numbers:** `limit=1000`
- **Description:** Results over 1000 rows are cut off with no "results truncated" warning,
  implying the user has seen everything.
- **User Impact:** Wrong conclusions from incomplete data.
- **Business Impact:** Data-correctness perception risk.
- **Recommended Fix:** Show truncation banner; offer CSV export / pagination.

---

## UX-015
- **Severity:** Medium
- **Component:** Backup progress visibility
- **File Path:** `veltnex/src/pages/portal/Backups.tsx`
- **Line Numbers:** download polling `:155-186`
- **Description:** Long backups show only an indefinite "Preparing…" spinner with no
  stage/percentage; large instances may wait many minutes with no signal.
- **User Impact:** Users assume failure, retry, and create duplicate backups.
- **Business Impact:** Wasted resources; support contacts.
- **Recommended Fix:** Emit stage/progress events (dumping/uploading/%) and show a progress bar.

---

## UX-016
- **Severity:** Medium
- **Component:** Optimistic UI on instance actions
- **File Path:** `veltnex/src/pages/portal/InstanceDetail.tsx`
- **Line Numbers:** `:165-176`
- **Description:** Start/Stop wait for server response before reflecting state, so buttons feel
  unresponsive and users may double-click.
- **User Impact:** Perceived lag / uncertainty whether the click registered.
- **Business Impact:** Polish/perceived-quality gap vs competitors.
- **Recommended Fix:** Optimistically set transitional state, disable the control, revert on
  error.

---

## UX-017
- **Severity:** Medium
- **Component:** Empty states
- **File Path:** `veltnex/src/pages/portal/Instances.tsx` (and other lists)
- **Line Numbers:** DataTable usage without empty template
- **Description:** A new user with zero instances sees a blank table rather than a guided
  "Create your first instance" CTA. (An `EmptyState` component exists but isn't wired here.)
- **User Impact:** New users are unsure if the page failed to load.
- **Business Impact:** Onboarding/activation friction.
- **Recommended Fix:** Provide empty-state content with a primary CTA to `/hosting`.

---

## UX-018
- **Severity:** Medium
- **Component:** Trial eligibility messaging
- **File Path:** `veltnex/src/pages/Hosting.tsx`
- **Line Numbers:** trial CTA shown unconditionally
- **Description:** The backend correctly blocks a free trial once the partner owns a paid
  instance, but the UI still shows "Start free trial," so eligible-looking users hit a
  server-side rejection.
- **User Impact:** Confusing dead-end after clicking.
- **Business Impact:** Friction; perceived bugginess.
- **Recommended Fix:** Fetch eligibility and hide/disable the trial CTA with an explanatory
  tooltip.

---

## UX-019
- **Severity:** Medium
- **Component:** Git auto-deploy onboarding
- **File Path:** `veltnex/src/pages/portal/Code.tsx`
- **Line Numbers:** repo set, no webhook helper UI
- **Description:** Customers can attach a repo but webhook setup is manual with no
  "copy webhook URL" button or provider-specific instructions, so many never enable
  auto-deploy.
- **User Impact:** High-friction developer onboarding; feature underused.
- **Business Impact:** Low adoption of a flagship capability.
- **Recommended Fix:** Copy-URL button + step-by-step instructions; optional OAuth auto-setup.

---

## UX-020
- **Severity:** Medium
- **Component:** Deploy button throttling feedback
- **File Path:** `veltnex/src/pages/portal/Code.tsx`
- **Line Numbers:** deploy handler
- **Description:** Deploy can be clicked repeatedly; the backend rate-limits but the button
  doesn't disable, so users think it's broken.
- **User Impact:** Confusion; perceived broken control.
- **Business Impact:** Support noise.
- **Recommended Fix:** Disable + "Deploy queued" state for the cooldown window.

---

## UX-021
- **Severity:** Medium (product gap)
- **Component:** Odoo version upgrade wizard
- **File Path:** none — selectable at create, not upgradable in-portal
- **Line Numbers:** n/a
- **Description:** Customers pick an Odoo version at creation but have no self-service path to
  upgrade a running instance (a core Odoo.sh capability).
- **User Impact:** Stuck on old versions; manual migration.
- **Business Impact:** Feature-parity gap vs Odoo.sh.
- **Recommended Fix:** Upgrade wizard with compatibility checks and one-click execution.

---

## UX-022
- **Severity:** Low (product gap)
- **Component:** Status page / SLA transparency
- **File Path:** none — Home claims "99.99% uptime SLA"
- **Line Numbers:** n/a
- **Description:** No status page or SLA document; during incidents customers can't tell
  platform vs their config.
- **User Impact:** Panic and duplicate support contacts during outages.
- **Business Impact:** Support escalations; trust erosion; unbacked SLA claim.
- **Recommended Fix:** Public status page + linked SLA.

---

## UX-023
- **Severity:** Low
- **Component:** Session-expiry warning
- **File Path:** `veltnex/src/context/AuthContext.tsx`; `veltnex/src/lib/api.ts`
- **Line Numbers:** n/a
- **Description:** No warning before session expiry; users mid-form get bounced to login and
  lose work.
- **User Impact:** Lost work; frustration.
- **Business Impact:** Perceived unreliability.
- **Recommended Fix:** Pre-expiry warning + "stay signed in" refresh.

---

## UX-024
- **Severity:** Low
- **Component:** Copy-to-clipboard feedback
- **File Path:** `veltnex/src/pages/portal/Environments.tsx`
- **Line Numbers:** `setCopied(false)` after ~1500ms
- **Description:** Copy actions only swap an icon briefly; no explicit "Copied!" toast, so
  success is ambiguous.
- **User Impact:** Users unsure the copy worked; re-click.
- **Business Impact:** Minor friction.
- **Recommended Fix:** Show a brief toast confirmation.
