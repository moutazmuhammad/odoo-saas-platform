# Create and download a database backup

Export one database with the correct format and download permission.

Reviewed: 2026-10-05

## Choose a backup format

| Format | Contents |
| --- | --- |
| ZIP | Odoo database export with filestore attachments. Prefer it for moving the application with files. |
| Dump | PostgreSQL database dump without the filestore. Preserve attachment files separately if needed. |

## Back up and download

1. With **Database Backup Operator**, choose Backup for the database on a running hosting environment.
2. Select the available format and wait for preparation to complete.
3. Use **Database Backup Downloader** access to download the completed copy. Creation permission alone does not grant download.
4. Store the file securely and test restoration in an appropriate non-production environment.

## Links and retention

Downloads go through an authorization-checked portal link and can redirect to expiring storage access. Request a new download from the page if the link expires. On-demand exports and temporary restore/copy files have cleanup policies; do not use the portal as permanent backup storage. Removing access cannot retract an already downloaded file or immediately revoke a storage URL already issued.

## Related guides

- [Restore a database from a local backup](database-restore.md)
- [Fixed role reference](iam-roles.md)
- [Restore a full-instance snapshot](restore.md)
