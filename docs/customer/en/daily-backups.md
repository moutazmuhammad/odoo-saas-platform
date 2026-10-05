# Enable automatic daily snapshots

Understand coverage, monthly billing, retention, and payment pauses.

Reviewed: 2026-10-05

## Coverage and retention

Daily snapshots are an optional paid add-on for eligible non-trial instances. Hosting snapshots capture full-instance data and recovery metadata, including databases and files. They are different from a one-off backup of one database. The default policy keeps 7 recent daily snapshots; actual capture times and available copies depend on successful scheduled runs and platform settings.

## Enable and monitor

1. As the owner, open **Snapshots** and choose to enable daily snapshots.
2. Review the price and checkout. The hosting charge is based on used data and is re-evaluated on monthly backup renewal, separately from the main subscription cycle.
3. After payment and activation, check the latest successful snapshot date. The first copy appears after its scheduled run completes.

## Payment and availability

An unpaid backup invoice pauses daily capture after its backup grace period (currently 3 days after the due date), without treating that optional charge as a main-subscription suspension. Capture resumes after settlement. Existing copies remain subject to retention. Backup access and restore require a running instance and the appropriate roles.

## Related guides

- [Restore a full-instance snapshot](restore.md)
- [Create and download a database backup](ondemand-backups.md)
- [Manage payment methods and automatic renewal](auto-renew.md)
