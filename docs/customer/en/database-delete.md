# Delete and replace a database

Understand confirmation, irreversible data loss, and creating another database.

Reviewed: 2026-10-05

## Before deleting

> Warning: Deleting removes the selected database and its associated application files. This cannot be undone by cancelling the dialog afterward. Download and test a usable backup first; deletion is not a backup operation.

## Delete

1. Use **Database Deleter** access on a running hosting environment.
2. Choose Delete for the database and type its exact full name to confirm.
3. Wait for the operation to complete. In Production, you can then create a replacement database or restore another one within the one-database limit.

## What stays in place

Deleting a database does not cancel the instance, free a reserved environment slot, remove its Git branch, or stop subscription billing. An empty hosting site displays the no-database page until another customer database is created or restored.

## Related guides

- [Create and download a database backup](ondemand-backups.md)
- [Restore a database from a local backup](database-restore.md)
- [Cancel and reactivate a project](reactivate.md)
