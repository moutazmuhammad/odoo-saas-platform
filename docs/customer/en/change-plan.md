# Change a subscription plan

Understand payment-gated upgrades and scheduled downgrades.

Reviewed: 2026-10-05

## Before you begin

Plan and purchased-capacity changes are owner billing actions. A Project Administrator teammate does not gain these permissions. Review the current plan, usage, billing period, and any pending invoice or scheduled change.

## How changes apply

| Change | Behavior |
| --- | --- |
| Upgrade | An invoice is created; the paid change applies after settlement. Unused prepaid value is accounted for in the quote and wallet/credit flow. |
| Downgrade | Scheduled for the cycle boundary. Storage usage must be below 75% of the target plan limit, leaving at least 25% free; insufficient or unavailable usage measurements can block the request. |
| Cancel scheduled change | Removes the pending downgrade before it takes effect. |

## Request a change

1. Open the instance and choose **Change plan** or **Upgrade plan** for a trial.
2. Select the plan and monthly or yearly billing, then review the quote.
3. Complete checkout if payment is required, or confirm the scheduled downgrade.
4. Check the active or scheduled plan in the overview. Resource changes can take time to reconcile.

## Related guides

- [Monthly and yearly billing](billing-options.md)
- [Decline optional charges and understand proration](optional-charges.md)
- [Manage storage capacity and blocks](storage-capacity.md)
