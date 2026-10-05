# Restore a database from a local backup

Upload an Odoo backup and restore into a new or existing database.

Reviewed: 2026-10-05

## Before you begin

Use a running hosting environment and **Database Restore Operator** access. Choose a backup compatible with the environment’s Odoo version and installed code. The upload dialog accepts supported Odoo backup archives; prefer an Odoo ZIP that includes the filestore for attachments. A raw database dump alone does not include attachments.

## Upload and restore

1. Choose Restore in Databases, select the backup file, and choose a new database name or replacement of an existing database.
2. In Production, replace the existing customer database or delete it before restoring another name. For replacement, type the exact full target name required by the dialog.
3. Keep the browser open for upload. The file goes directly to object storage and shows upload progress.
4. After upload completes, the platform validates the archive and starts the background restore. Follow the database operation until it completes or shows an error.
5. Open the restored database with its original application credentials. Reset its administrator password if required and permitted.

## Replacement and compatibility

> Warning: Replacement destroys the target’s current data. Download a fresh target backup before confirming. Archive validation cannot guarantee that every custom module, external integration, or major-version mismatch will work after restore.

## Related guides

- [Create and download a database backup](ondemand-backups.md)
- [Database Manager and administrator passwords](database-access.md)
- [Troubleshoot database and restore operations](database-troubleshooting.md)
