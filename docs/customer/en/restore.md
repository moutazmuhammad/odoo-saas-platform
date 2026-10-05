# Restore a full-instance snapshot

Replace current data with a previous snapshot and verify recovery.

Reviewed: 2026-10-05

## Before you begin

Use a running instance, an available completed snapshot, and **Database Restore Operator** access. Review the snapshot’s date and environment. Snapshot restoration is an instance-wide operation; use local database restore when only one database should be replaced.

## Restore and verify

1. Open Snapshots and choose Restore on the desired copy.
2. Read the replacement warning and type the exact name requested in the confirmation dialog.
3. Start restore and wait while the instance enters provisioning. The platform attempts a fresh pre-restore safety snapshot first.
4. After the instance returns to running, check database availability, attachments, application login, and critical workflows.

## Recovery consequences

> Warning: The chosen snapshot replaces current databases, files, and captured configuration. Later changes are lost unless preserved elsewhere. A safety snapshot is not a substitute for a backup you have already downloaded and verified. Coordinate downtime and integration replay with your team.

## Related guides

- [Enable automatic daily snapshots](daily-backups.md)
- [Restore a database from a local backup](database-restore.md)
- [Troubleshoot database and restore operations](database-troubleshooting.md)
