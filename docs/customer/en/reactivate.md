# Cancel and reactivate a project

Understand cascading deletion, retained data, and recovery conditions.

Reviewed: 2026-10-05

## Before cancelling

Production subscription cancellation and reactivation are customer-owner billing actions. The current portal does not expose a general active-subscription cancellation button. Contact support as the owner to arrange cancellation; declining an optional unpaid invoice is a separate workflow and is not active subscription cancellation.

> Warning: Cancelling a Production project also tears down its Staging and Development servers. Export important databases first. Cancellation is different from Stop.

During teardown the platform attempts a fresh full-instance snapshot and removes older snapshots and on-demand backups. Only a successfully captured fresh snapshot is retained; if capture fails, there may be no retained recovery copy. Never assume the most recent old backup will survive cancellation.

## Reactivate and restore

If Reactivate is offered, select a paid plan and complete its checkout. Reactivation creates fresh infrastructure; restoring retained data is a separate recovery step and may require daily backups to be enabled and a restoration invoice to be settled. Review the price and snapshot availability shown before confirming. Retained snapshots are not a promise of indefinite storage.

## Related guides

- [Restore a full-instance snapshot](restore.md)
- [View and pay invoices](invoices.md)
- [Create and download a database backup](ondemand-backups.md)
