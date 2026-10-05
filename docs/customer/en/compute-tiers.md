# Change compute tiers

Understand replica-based tiers and their billing behavior.

Reviewed: 2026-10-05

## What a compute tier changes

Compute tiers apply to the Kubernetes backend and define application replica counts and their price. The platform publishes the available names, such as Standard, HA, or Scale. A larger replica count is not a guarantee of a specific availability SLA or a replacement for sizing Workers, CPU, RAM, and storage correctly.

## Change a tier

From the instance overview, select an available tier. Moving to more replicas in a priced tier requires a prorated invoice to be paid first. A free, lower-replica, or lateral change can be queued without a charge and does not refund the current period. Wait for the infrastructure change before evaluating the new replica count.

## Related guides

- [Monitor resource usage](monitoring.md)
- [Decline optional charges and understand proration](optional-charges.md)
