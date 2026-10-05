# Instance status reference

Interpret payment, provisioning, running, and failure states.

Reviewed: 2026-10-05

## Status meanings

| State | Meaning and next action |
| --- | --- |
| Pending payment | Checkout is incomplete. Settle the invoice to activate the order. |
| Paid / Pending provision | Payment is recorded; allocation or infrastructure readiness is pending. |
| Provisioning | A background lifecycle operation is running. Read the operation details before retrying. |
| Running | The application is running. A hosting customer database may still need to be created. |
| Stopped | The application is stopped; capacity and subscription still exist. |
| Suspended | A restriction such as unpaid renewal or capacity has paused operation. |
| Failed | An operation failed. Inspect the error and deployment history. |
| Cancelled | Infrastructure has been removed or cancellation has completed. |

## Asynchronous operations

Creation, deployments, restore, and deletion can continue after their request returns. A browser refresh is not a completion signal. The platform serializes conflicting work and has retry and stuck-operation recovery, but network, capacity, Git, or storage failures can still require support.

## Related guides

- [Troubleshoot a project stuck in provisioning](provisioning-troubleshooting.md)
- [Troubleshoot slow or failed deployments](deployment-troubleshooting.md)
