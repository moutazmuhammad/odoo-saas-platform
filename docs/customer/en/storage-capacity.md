# Manage storage capacity and blocks

Respond to capacity warnings and safely release unused storage.

Reviewed: 2026-10-05

## Capacity states

The capacity checker warns at 80% usage and starts a grace period at 100%. The default storage grace period is 7 days and can be changed by the platform. Continued over-capacity usage after that period can pause the workspace. These checks use measured usage and periodic jobs, so displayed values are not an instantaneous hard quota.

## Expand or release capacity

1. Review the capacity message and current usage on the instance overview.
2. Upgrade the plan or buy the storage blocks offered by the interface. Block size and price are configured by the platform; paid additions activate after payment.
3. To release blocks, first remove enough data to satisfy the headroom check. Confirm the resulting capacity and recurring charge; no automatic refund is promised for released storage.

## Related guides

- [Choose Workers and storage](sizing.md)
- [Change a subscription plan](change-plan.md)
