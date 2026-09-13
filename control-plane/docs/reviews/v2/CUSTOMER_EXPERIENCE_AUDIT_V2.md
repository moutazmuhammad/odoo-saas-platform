# CUSTOMER EXPERIENCE AUDIT — V2 (Second Pass)

> Re-audit of the VELTNEX SPA assuming V1 UX findings fixed. Only **NEW** CX issues across real
> journeys (auth/recovery, billing transparency, destructive env ops, mobile/a11y). The
> automated pass re-raised "logs need search/filter" — **dropped** (V1 UX-013).
>
> Severity counts: **Critical 0 · High 4 · Medium 7 · Low 6** (17 findings)

---

## CX-001
- **Severity:** High
- **Component:** Password recovery
- **File Path:** `veltnex/src/pages/Login.tsx`
- **Line Numbers:** `:~108` ("Forgot?" → `/web/reset_password`)
- **Description:** The only password-recovery path hard-navigates out of the SPA to Odoo's
  backend `/web/reset_password` page — a jarring context switch with no explanation, different
  styling, and a dead-end back into the portal. There is no in-SPA reset flow.
- **User Impact:** Locked-out customers bounce to an unfamiliar Odoo screen; many will assume
  it's broken and contact support.
- **Business Impact:** Avoidable support volume; login friction increases churn at re-activation.
- **Recommended Fix:** Build an in-SPA reset flow (request email → tokenized reset form), or at
  minimum open the backend page in a new tab with a clear explanatory message.

---

## CX-002
- **Severity:** High
- **Component:** Session-expiry handling
- **File Path:** `veltnex/src/lib/api.ts`; `veltnex/src/pages/portal/InstanceDetail.tsx`
- **Line Numbers:** `api.ts:~54-56` (auth_required); `InstanceDetail.tsx:~76` (catch → "Instance not found")
- **Description:** When the session expires, several component-level `catch` blocks swallow the
  `auth_required` error and render misleading states like "Instance not found" instead of routing
  to login. The customer sees their instance "disappear" mid-session.
- **User Impact:** Confusion and false alarm (looks like data loss / a bug) rather than a clear
  "please sign in again".
- **Business Impact:** Trust damage and support tickets from a benign session timeout.
- **Recommended Fix:** Centralize `auth_required` handling so it always triggers the
  AuthContext redirect; never let components mask it as a not-found error.

---

## CX-003
- **Severity:** Medium
- **Component:** Registration — late duplicate detection
- **File Path:** `veltnex/src/pages/Register.tsx`
- **Line Numbers:** `:~117-138` (`handleAccount` → `registerStart`)
- **Description:** Email/phone uniqueness is only discovered after OTP send/verify, so a user
  registering with an existing email completes several steps before hitting the conflict and
  must restart.
- **User Impact:** Wasted effort at the first conversion moment; frustration.
- **Business Impact:** Signup-funnel drop-off.
- **Recommended Fix:** Check email/phone availability at account-step submit and show an inline
  error (with a link to login / forgot-password).

---

## CX-004
- **Severity:** Medium
- **Component:** Instance list — no at-risk/payment flag
- **File Path:** `veltnex/src/pages/portal/Instances.tsx`
- **Line Numbers:** `:~29-61` (status column)
- **Description:** The list shows lifecycle status badges but nothing indicating "unpaid invoice"
  / "renewal due" / "at risk of suspension". Customers can't see at a glance which instances need
  payment.
- **User Impact:** Missed payment deadlines → surprise suspensions.
- **Business Impact:** Involuntary churn and dunning load that a simple visual flag would prevent.
- **Recommended Fix:** Add a "payment due / past due" badge driven by the instance's unpaid-invoice
  state, linking straight to checkout.

---

## CX-005
- **Severity:** Medium
- **Component:** Billing transparency — next charge date
- **File Path:** `veltnex/src/components/BillingPanel.tsx`
- **Line Numbers:** `:~156-187` (wallet section)
- **Description:** Neither the billing panel nor instance detail surfaces the next billing /
  renewal date or amount; customers must infer it from invoice history.
- **User Impact:** No visibility into when/how much they'll next be charged; can't plan.
- **Business Impact:** "When am I charged?" tickets; perceived opacity vs competitors that show a
  clear next-invoice summary.
- **Recommended Fix:** Show `next_invoice_date` + projected amount + payment method in a billing
  summary card.

---

## CX-006
- **Severity:** Low
- **Component:** Invoice detail — payment method not shown
- **File Path:** `veltnex/src/pages/portal/InvoiceDetail.tsx`
- **Line Numbers:** `:~50-145`
- **Description:** The invoice view doesn't show which saved card will be / was charged, nor a
  shortcut to update it if it's expiring.
- **User Impact:** Customers can't verify the charge source or fix a stale card from where it
  matters.
- **Business Impact:** Higher failed-payment rate from un-updated cards.
- **Recommended Fix:** Display card brand/last-4/expiry on the invoice with an "update payment
  method" link.

---

## CX-007
- **Severity:** Medium
- **Component:** Restore dialog — ambiguous, risky wording
- **File Path:** `veltnex/src/pages/portal/Backups.tsx`
- **Line Numbers:** `:~205-280`
- **Description:** The restore confirmation conflates "restore to a past state" (destructive
  overwrite of current data) with "recover from backup". It mentions a pre-restore safety
  snapshot but doesn't make clear that everything created since the snapshot date is lost.
- **User Impact:** Customers may trigger a destructive overwrite without understanding the data
  loss, or avoid a needed restore out of fear.
- **Business Impact:** Accidental data loss incidents and support escalations.
- **Recommended Fix:** Reword to state explicitly: "REPLACES current data with the state from
  <date>; anything created since cannot be recovered," plus typed-name confirmation.

---

## CX-008
- **Severity:** High
- **Component:** Environments — drag-merge discoverability
- **File Path:** `veltnex/src/pages/portal/Environments.tsx`
- **Line Numbers:** `:~353-355` (tip text, conditionally shown)
- **Description:** The flagship "drag a branch onto another to merge & redeploy" interaction is
  conveyed only by a small conditional text tip, with no drag handles, affordances, or drop-zone
  highlighting. Most customers will never discover it.
- **User Impact:** A core differentiator goes unused; customers merge manually in Git.
- **Business Impact:** The Odoo.sh-style workflow that justifies the product is effectively
  invisible — lost value and stickiness.
- **Recommended Fix:** Add visible drag handles, hover/drop-zone styling, and an onboarding
  callout the first time multiple environments exist.

---

## CX-009
- **Severity:** High
- **Component:** Environments — production merge lacks danger affordance
- **File Path:** `veltnex/src/pages/portal/Environments.tsx`
- **Line Numbers:** `:~1069-1126` (merge dialog)
- **Description:** Merging into Production shows an amber banner but the confirm button is neutral
  ("Merge & redeploy") with no danger styling and no second confirmation. A distracted user can
  one-click deploy untested code to the live production instance.
- **User Impact:** Accidental production deploys → outages affecting the customer's own end-users.
- **Business Impact:** Reliability incidents and trust damage from a foot-gun on the highest-stakes
  action.
- **Recommended Fix:** Use a destructive (red) button for production targets and require an
  explicit second confirmation ("deploy to live Production").

---

## CX-010
- **Severity:** Medium
- **Component:** Environments — delete confirmation underplays data loss
- **File Path:** `veltnex/src/pages/portal/Environments.tsx`
- **Line Numbers:** `:~1287-1322` (delete dialog)
- **Description:** The delete dialog title ("Remove environment") and body read as benign, while
  the action permanently destroys the child environment's databases, files, and logs.
- **User Impact:** Accidental deletion of a working staging/dev environment and its data.
- **Business Impact:** Lost work, recovery tickets, churn risk.
- **Recommended Fix:** Title "Delete environment — cannot be undone"; spell out exactly what is
  destroyed; require typed confirmation for environments that hold data.

---

## CX-011
- **Severity:** Medium
- **Component:** Backups — misleading empty state
- **File Path:** `veltnex/src/pages/portal/Backups.tsx`
- **Line Numbers:** `:~148-154`
- **Description:** With no snapshots, the empty state says daily snapshots "run automatically …
  the first one will appear here," but this is only true if daily backups are enabled. With
  backups off, the reassuring message implies protection that doesn't exist.
- **User Impact:** Customers believe they're protected and skip enabling backups — then suffer
  unrecoverable data loss.
- **Business Impact:** Data-loss incidents directly attributable to a misleading UI claim.
- **Recommended Fix:** Branch the empty state on `daily_backup_enabled`; when off, prompt the
  customer to enable it and explain there is no automatic protection yet.

---

## CX-012
- **Severity:** Medium
- **Component:** Settings — no account security controls
- **File Path:** `veltnex/src/pages/portal/Settings.tsx`
- **Line Numbers:** `:~50-150`
- **Description:** Settings lacks 2FA enrollment, an active-sessions list with "sign out
  everywhere," and any sign-in/activity history. Customers running production infrastructure have
  no way to harden or audit their own account.
- **User Impact:** No self-serve account protection or compromise detection.
- **Business Impact:** Account-takeover risk for high-value tenants; blocks security-conscious /
  enterprise customers (pairs with the V1 audit-log gap).
- **Recommended Fix:** Add 2FA + backup codes, active-session management, and a login/activity
  history (backend support required).

---

## CX-013
- **Severity:** Low
- **Component:** Command palette — missing high-intent actions
- **File Path:** `veltnex/src/components/CommandPalette.tsx`
- **Line Numbers:** `:~52-77`
- **Description:** ⌘K navigates to projects but omits high-value verbs like "Pay now (unpaid
  invoice)", "Create hosting instance", "Go to pending invoices".
- **User Impact:** Power users can't act quickly; fall back to clicking through the UI.
- **Business Impact:** Slower paths to the actions that most affect revenue (payment, create).
- **Recommended Fix:** Add state-aware actions (pay, create, view pending invoices) to the palette.

---

## CX-014
- **Severity:** Low
- **Component:** Dialog — accidental backdrop dismiss on touch
- **File Path:** `veltnex/src/components/ui/dialog.tsx`
- **Line Numbers:** `:~37-73` (`onClick={onClose}` on backdrop)
- **Description:** The backdrop closes on any click; on touch devices a scroll/swipe over content
  can register as a backdrop tap and dismiss the dialog mid-task.
- **User Impact:** Mobile users accidentally close confirm/restore/merge dialogs and must restart.
- **Business Impact:** Mobile friction on exactly the high-stakes dialogs.
- **Recommended Fix:** Only close when the click target is the backdrop element itself (guard via
  a data attribute / `e.target === e.currentTarget`).

---

## CX-015
- **Severity:** Low
- **Component:** Help deep-links on mobile
- **File Path:** `veltnex/src/pages/Help.tsx`
- **Line Numbers:** `:~36-51` (`scrollIntoView({ block: "start" })`)
- **Description:** Hash deep-links scroll the target to the top, where it hides under the sticky
  header on narrow viewports, so the section title isn't visible.
- **User Impact:** In-app help links from tooltips land on a seemingly wrong/blank spot on mobile.
- **Business Impact:** Reduced self-serve help effectiveness → more tickets.
- **Recommended Fix:** Use `block: "center"` (or apply a scroll-margin-top equal to header height).

---

## CX-016
- **Severity:** Low
- **Component:** OTP resend — low-visibility cooldown
- **File Path:** `veltnex/src/pages/Register.tsx`
- **Line Numbers:** `:~272, 395-409`
- **Description:** The resend cooldown is shown as muted gray text; users miss it and repeatedly
  tap the disabled button, thinking it's broken.
- **User Impact:** Confusion during OTP entry, especially on mobile.
- **Business Impact:** Minor signup friction / support questions.
- **Recommended Fix:** Make the countdown prominent ("Resend in 45s") with a clear disabled
  affordance.

---

## CX-017
- **Severity:** Low
- **Component:** Environments — no loading skeleton
- **File Path:** `veltnex/src/pages/portal/Environments.tsx`
- **Line Numbers:** `:~255-261` (full-page spinner "Loading project…")
- **Description:** The page blocks behind a single full-page spinner for 2-3s instead of showing
  structural skeletons, making it feel slow.
- **User Impact:** Perceived sluggishness; users may refresh or assume a hang.
- **Business Impact:** Polish/perceived-performance gap on a flagship page.
- **Recommended Fix:** Render skeletons for the sidebar, tabs, and deployment history while data
  loads.
