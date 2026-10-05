# Duplicate a database

Create an independent copy where capacity allows it.

Reviewed: 2026-10-05

## Requirements

Duplication is a hosting operation on a running environment. You need **Database Creator** and **Database Backup Downloader**, because the operation copies source data. The Production one-database rule normally prevents duplicating an existing Production database on the same instance; use a Staging or Development target through the copy workflow instead.

## Duplicate

1. Select the source database and choose Duplicate.
2. Enter a new valid name and verify enough storage is available.
3. Confirm and wait for the background operation to finish before opening the copy.

> Warning: The copied database has its own data after creation. Check scheduled tasks, email, payment providers, and external integrations before using it for testing; cloning is not a guarantee of neutralization.

## Related guides

- [Copy databases between environments](environment-copy.md)
- [Create and open a database](manage-databases.md)
- [Create and download a database backup](ondemand-backups.md)
