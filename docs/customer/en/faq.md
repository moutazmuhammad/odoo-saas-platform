# Common customer questions

Quick answers with links to the complete operating guides.

Reviewed: 2026-10-05

## Can a teammate also own projects?

Yes, after completing required account setup. Their own projects and projects shared by another owner are separated in the project list. Ownership of one project does not give owner powers over shared projects. See [Team and permissions](iam-overview.md).

## Can I replace the Production database?

Yes. Restore into the existing database with replacement, or back it up and delete it before creating another. Keep one customer database. See [Restore a database](database-restore.md).

## Does deleting Staging stop its capacity charge?

Deleting frees the server’s slot for reuse. Release the unused reserved slot to lower the recurring capacity charge and receive eligible unused-time wallet credit. See [Environment slots](environment-slots.md).

## Does merge move live data?

No. Merge changes Git code. Copy databases explicitly into Staging or Development; use restore for a planned database replacement. See [Merge code](environment-merge.md) and [Copy databases](environment-copy.md).

## Does stopping an instance or disabling auto-renew cancel billing?

No. Stop affects runtime; disabling auto-renew affects automatic charging. Invoices and reserved capacity can continue until the relevant subscription or capacity is cancelled/released. See [Automatic renewal](auto-renew.md).

## Does every code change upgrade all modules?

No. The build selects repository modules whose upgrade inputs changed. Runtime-only edits generally redeploy without a module upgrade. First builds without a baseline can select all detected repository modules. See [Selective module upgrades](module-upgrades.md).

## Related guides

- [Limits and availability reference](limits.md)
- [Troubleshoot missing projects or denied actions](iam-troubleshooting.md)
- [Choose a support plan and request help](support-plans.md)
