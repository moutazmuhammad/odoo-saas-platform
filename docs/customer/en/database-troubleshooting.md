# Troubleshoot database and restore operations

Resolve capacity, naming, archive, and application compatibility failures.

Reviewed: 2026-10-05

## Common symptoms

| Symptom | Next step |
| --- | --- |
| No database yet | Create or restore a customer database from Databases. Running infrastructure alone does not initialize your business data. |
| Production allows one database | Replace the existing database during restore, or back it up and delete it before creating a different one. |
| Database name rejected | Check lowercase naming, the required prefix, separators, and total length. |
| Upload fails | Check connection and the archive. Request a fresh upload through the dialog after an expired link; contact support for storage/CORS errors. |
| Restore completes but Odoo fails | Check Odoo major-version compatibility and deployed custom modules; use the operation report and startup logs. |
| Attachments are missing | Confirm the backup contained the filestore, not only a raw database dump. |
| An operation remains running | Do not start conflicting operations. Note the operation/database and ask support to inspect its worker. |

## Information to provide

Provide the target environment, full database name, operation type, archive format and approximate size, Odoo version, time, and visible error. Do not attach a full customer backup to a public ticket. Agree on a secure channel if support needs the archive.

## Related guides

- [Restore a database from a local backup](database-restore.md)
- [Create and open a database](manage-databases.md)
- [Create and download a database backup](ondemand-backups.md)
