# TENANT ISOLATION AUDIT — V2 (Second Pass)

> Re-audit assuming V1 findings fixed. Focus: cross-tenant access via the authorization stack
> (ACL → record rules → API ownership checks). **Important calibration:** an automated pass
> claimed five "missing record rule" CRITICAL/HIGH cross-tenant leaks (incl. `github_token`).
> I verified the actual access model and **those are NOT customer-exploitable** — see the
> "Cleared / Overstated" section. The genuinely new issues are below.
>
> Severity counts: **Critical 0 · High 0 · Medium 1 · Low 2** (3 findings)

---

## Access model established (ground truth)

- Customers are **`base.group_portal`** users (`saas_website/controllers/registration.py:295-296`).
- Portal `ir.model.access` (`saas_website/security/ir.model.access.csv`) grants portal **read**
  only to: `saas.instance`, `saas.instance.backup`, `saas.instance.folder` (all RW for
  folder), plus public catalog models. Each has a **partner-scoped record rule** in
  `saas_website/security/saas_website_security.xml`.
- Portal users have **no ACL row** for `saas.instance.repo`, `saas.build`, `saas.wallet`,
  `saas.wallet.transaction`, `saas.wallet.lot`, `saas.payment.method`,
  `saas.instance.db.operation` — those are granted only to internal `group_saas_user`/
  `group_saas_manager` (`saas_core/security/ir.model.access.csv`). A portal RPC `read`/`search`
  on them is denied at the ACL layer regardless of record rules; the JSON API reaches them via
  `sudo()` with explicit `partner_id` ownership checks.

---

## ISO-001
- **Severity:** Low (defense-in-depth gap, **not** customer-exploitable)
- **Component:** Record-rule coverage for portal-adjacent models
- **File Path:** `saas_website/security/saas_website_security.xml`; `saas_core/security/ir.model.access.csv`
- **Line Numbers:** absence of portal `ir.rule` for repo/build/wallet*/payment_method/db_operation
- **Description:** `saas.instance.repo`, `saas.build`, `saas.wallet*`, `saas.payment.method`,
  and `saas.instance.db.operation` have no portal record rule. This is currently harmless
  because portal users have no ACL to those models, so the API's `sudo()` + `partner_id` checks
  are the sole gate. The risk is purely future-proofing: if anyone ever adds a portal ACL row
  (e.g., to expose wallet history directly) without simultaneously adding a partner-scoped
  record rule, it becomes an immediate cross-tenant read of billing/repo data.
- **Business Impact:** None today; a latent foot-gun that would turn a one-line ACL change into
  a cross-tenant data breach.
- **Technical Impact:** Authorization relies on every future controller using `sudo()` + manual
  checks, with no model-level backstop.
- **Recommended Fix:** Add partner-scoped record rules for these models now (read-only, portal
  group) so the model layer is safe-by-default even if ACLs change later.

---

## ISO-002
- **Severity:** Medium
- **Component:** Inconsistent ownership scoping (partner_id vs commercial_partner_id vs company_id)
- **File Path:** `saas_website/security/saas_website_security.xml`; `saas_core/security/saas_security.xml`; `saas_website/controllers/api.py`
- **Line Numbers:** portal rule `saas_website_security.xml:17-21` (`partner_id == user.partner_id.id`); core rules `saas_security.xml:26-88` (`company_id in company_ids`); API uses `commercial_partner_id` (e.g. trial/wallet/payment-method checks `api.py:1361,1657`)
- **Description:** Three different ownership keys are used across the stack. The portal record
  rule scopes instances by the **exact** `partner_id == user.partner_id.id`; the internal rules
  scope by **`company_id`**; several API checks use **`commercial_partner_id`**. Consequences:
  (a) a customer organization with multiple contacts/users — where instances are owned by the
  commercial (parent) partner or a sibling contact — will find child portal users **unable to
  see** those instances (fail-closed, but a real functional/UX defect for teams); (b) the
  mismatch makes reasoning about "who can see what" error-prone, and any future move to
  commercial-partner scoping in record rules must be done carefully to avoid the opposite
  (over-broad) failure.
- **Business Impact:** Multi-user customers cannot reliably share access to their own instances
  (blocks the team use-case and creates "my instance disappeared" tickets); inconsistent keys
  raise the odds of a future isolation regression.
- **Technical Impact:** No single canonical "account boundary" definition; record rules and API
  diverge.
- **Recommended Fix:** Pick one canonical boundary (recommend `commercial_partner_id`) and apply
  it uniformly in record rules and API checks; add tests asserting a child contact sees exactly
  the account's instances and nothing else.

---

## ISO-003
- **Severity:** Low
- **Component:** Internal-staff isolation is company-based, not partner-based
- **File Path:** `saas_core/security/saas_security.xml`
- **Line Numbers:** `:26-88` (`group_saas_user`/`group_saas_manager` rules use `company_id in company_ids`)
- **Description:** Internal access to instances/backups/repos is scoped by `company_id`. If the
  deployment is single-company (typical for a SaaS operator), every `group_saas_user` sees every
  tenant — which is intended for staff, but means there is **no partner-level compartmentation**
  for internal roles. Combined with the V1 finding that powerful actions sit behind one manager
  group, internal blast radius is broad.
- **Business Impact:** Any internal account compromise or over-broad staff grant exposes all
  tenants' operational data.
- **Technical Impact:** Company-based rules don't compartmentalize staff by responsibility.
- **Recommended Fix:** Introduce scoped internal roles (e.g., support limited to assigned
  tickets/accounts) if/when staff headcount grows; pairs with the V1 RBAC-granularity item.

---

## Cleared / Overstated (verified NOT exploitable)

| Automated claim | Verdict | Evidence |
|---|---|---|
| Portal users can read other tenants' **repos incl. `github_token`** (CRITICAL) | **Cleared** | No portal ACL row for `saas.instance.repo`; portal RPC read is denied at the ACL layer (`saas_website/security/ir.model.access.csv`). |
| Portal users can read other tenants' **wallet/transactions** | **Cleared** | No portal ACL for `saas.wallet*`; API uses `sudo()` + partner check. |
| Portal users can read other tenants' **builds** | **Cleared** | No portal ACL for `saas.build`. |
| Portal users can read other tenants' **payment methods** | **Cleared** | No portal ACL for `saas.payment.method`; API also checks `partner_id` (`api.py:1657`). |
| Portal users can read other tenants' **DB operations** | **Cleared** | No portal ACL for `saas.instance.db.operation`. |

The underlying observation (no record rules on these models) is real but is reclassified as a
**defense-in-depth gap (ISO-001, Low)**, not an active cross-tenant vulnerability.
