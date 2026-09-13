# BILLING CORRECTNESS AUDIT — V2 (Second Pass)

> Re-audit assuming V1 billing findings fixed. Only **NEW** billing/revenue defects, verified
> against the actual math. Several automated claims were **disproven** and are listed at the
> end (storage "free month", repeated-upgrade double-credit) so they aren't acted on.
>
> Severity counts: **Critical 0 · High 0 · Medium 4 · Low 2** (6 findings)

---

## BILL-V2-001
- **Severity:** Medium
- **Component:** Proration — portal display vs backend invoice mismatch
- **File Path:** `saas_website/controllers/portal.py` (display); `saas_core/models/saas_instance.py` (invoice)
- **Line Numbers:** `portal.py:570`, `portal.py:648` (`(next_invoice_date - today).days - 2`); backend `_proration_credit` `saas_instance.py:~9971` (no `-2`)
- **Description:** The portal computes the proration credit/remaining-value using
  `remaining_days = (next_invoice_date - today).days - 2`, while the authoritative backend
  `_proration_credit` uses `remaining_days = (next_invoice_date - today).days` with no
  deduction. The `-2` is undocumented and arbitrary, so the quote the customer sees differs from
  the invoice they are actually charged (customer is shown a smaller credit than the backend
  grants).
- **Business Impact:** Quote ≠ invoice undermines billing trust and generates "why is my charge
  different?" tickets; an unexplained magic constant in money math is an audit red flag even
  though it currently under-credits (customer-favorable) rather than overcharges.
- **Technical Impact:** Two divergent proration formulas for the same operation.
- **Recommended Fix:** Have the portal call the same `_proration_credit` helper as the backend
  (single source of truth); remove the `-2` or document and apply it in both places.

---

## BILL-V2-002
- **Severity:** Medium
- **Component:** Yearly minimum-monthly floor carries the yearly discount
- **File Path:** `saas_core/models/saas_pricing.py`
- **Line Numbers:** `:283` (`yearly = max(pre_minimum_yearly, minimum_monthly * 12 * (1 - discount))`)
- **Description:** The minimum-monthly revenue floor is multiplied by `(1 - discount)` when used
  as the yearly floor, so the effective annual floor is **below** `minimum_monthly * 12` (the
  floor the monthly path enforces). A code comment says this is intentional ("only the infra
  portion it floors carries the yearly discount"), but the effect is that the smallest,
  floor-priced plans yield less annual revenue than the monthly floor implies.
- **Business Impact:** Revenue leakage on the cheapest floored plans when billed yearly; the
  "floor" no longer guarantees a minimum annual take. Needs an explicit policy decision.
- **Technical Impact:** The floor semantics differ between monthly and yearly paths.
- **Recommended Fix:** If the floor is meant as a hard minimum, use `minimum_monthly * 12` (no
  discount) for the yearly floor; otherwise document the intended discounted-floor policy and
  reflect it in pricing tests.

---

## BILL-V2-003
- **Severity:** Medium
- **Component:** Wallet credit cap is pre-tax subtotal
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** `_order_lines_subtotal` `:2488-2495`; wallet cap `:2508-2512`
- **Description:** Wallet consumption is capped at `min(wallet.balance, subtotal)` where
  `subtotal` sums `price_unit * qty` **pre-tax**. If any billing product carries tax, the
  invoice's `amount_residual` is post-tax and exceeds the pre-tax subtotal the wallet was sized
  to, so wallet credit cannot fully settle a tax-bearing invoice and the residual/idempotency
  accounting can drift. (Whether this bites depends on tax config — flag and verify the tax
  setup for billing products.)
- **Business Impact:** Customers with prepaid balance still get charged the tax delta
  unexpectedly, or invoices don't fully reconcile against the wallet — both produce disputes
  and reconciliation work.
- **Technical Impact:** Wallet sizing uses a different (pre-tax) basis than the invoice total.
- **Recommended Fix:** Decide wallet semantics explicitly (tax-inclusive vs pre-tax credit) and
  size the consumption from the confirmed, tax-aware invoice total; document it in the UI.

---

## BILL-V2-004
- **Severity:** Medium
- **Component:** Wallet locked on pre-confirmation subtotal
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** `:2497-2522`
- **Description:** The wallet is `_lock()`ed and the consumption amount computed from the
  in-memory order-line subtotal **before** the sale order is confirmed and the invoice is
  generated. If Odoo's SO→invoice computation adjusts totals (taxes, pricelist rounding,
  rounding method), the locked amount no longer equals the invoice residual, risking
  over/under-consumption relative to the actual amount due.
- **Business Impact:** Edge-case mischarges and harder reconciliation on orders where invoice
  totals differ from the raw line subtotal.
- **Technical Impact:** Wallet decision is made against pre-confirmation data.
- **Recommended Fix:** Compute and apply wallet consumption from the confirmed invoice total
  (post-tax, post-rounding) within the same transaction, after invoice creation and before
  posting/payment.

---

## BILL-V2-005
- **Severity:** Low
- **Component:** Advertised yearly discount vs computed savings
- **File Path:** `saas_core/models/saas_pricing.py`
- **Line Numbers:** `:299-302` (`savings_percent` derived from actual yearly vs monthly)
- **Description:** `savings_percent` is recomputed from the actual monthly×12 vs yearly. When a
  minimum floor (BILL-V2-002) or a tier ceiling overrides the raw discount, the displayed
  percentage diverges from the configured/advertised yearly discount (e.g., "20% off" can render
  as "4%"). The code comment acknowledges this. It is correct arithmetic but a transparency
  mismatch with marketing copy.
- **Business Impact:** Customer confusion / distrust when the shown discount ≠ advertised
  discount; support tickets.
- **Technical Impact:** Two notions of "discount" (config vs realized) surface to the user
  without explanation.
- **Recommended Fix:** When floor/tier overrides the discount, show a clarifying note ("annual
  minimum applies") rather than a bare, lower percentage; keep advertised vs realized distinct.

---

## BILL-V2-006
- **Severity:** Low
- **Component:** Yearly-availability check treats `0.0` as "no yearly"
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** `:~9671-9672` (`if billing_period == 'yearly' and not new_plan.yearly_price: billing_period = 'monthly'`)
- **Description:** `not new_plan.yearly_price` is truthy for `0.0`, so a plan with a deliberately
  zero yearly price (e.g., a promo/free-year plan) is silently downgraded to monthly billing.
  Float fields default to `0.0`, so "unset" and "free" are indistinguishable.
- **Business Impact:** Promotional free-year configurations silently bill monthly instead;
  minor but surprising.
- **Technical Impact:** Sentinel ambiguity between "no yearly option" and "zero-priced yearly".
- **Recommended Fix:** Add an explicit `has_yearly` boolean (or nullable price) rather than
  inferring availability from a falsy `0.0`.

---

## Cleared / Overstated (verified)

| Automated claim | Verdict | Evidence |
|---|---|---|
| Buying a storage block at exact renewal gives a **free month** (`charge=0`) | **Cleared** | `saas_instance.py:1885-1886`: proration only applies inside `if 0 < left < total_days`; when `left==0` the charge stays `full`, not 0. |
| **Repeated upgrades** in one cycle re-credit the full remaining period each time | **Cleared** | `_proration_credit` recomputes `remaining_days` from **today** (`saas_instance.py:~9971`), so the window shrinks with each upgrade; no double-credit from cycle start. |
| Yearly **support** add-on asymmetry is a bug | **By design** | Flat add-ons billed ×12 with no yearly discount is the documented pricing policy (`saas_pricing.py:261-279`). |
