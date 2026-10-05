# Copy databases between environments

Copy selected databases into Staging or Development in the same project.

Reviewed: 2026-10-05

## Requirements

Source and target must be different running hosting instances in the same project. The target must be Staging or Development. You need **Database Backup Downloader** on the source and **Database Restore Operator** on the target. The database-copy action cannot target Production.

## Select and copy

1. Open the copy-data action, choose the source and target, then select the source databases.
2. Review the target names. The source prefix is replaced by the target environment prefix.
3. Explicitly confirm overwrite for each target database that already exists. Unconfirmed conflicts are skipped.
4. Start the copy and check progress, target databases, and errors. A batch can partially succeed.

## What is copied

The operation copies the selected database dump and its filestore. Other target databases are left alone. Copying data does not merge code, migrate a major Odoo version, or prove that test integrations are disabled. Check outgoing mail, jobs, payment providers, and external connections before testing with copied live data.

## Related guides

- [Create a Staging or Development environment](environment-create.md)
- [Restore a database from a local backup](database-restore.md)
- [Merge code between environments](environment-merge.md)
