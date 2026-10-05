# Fixed role reference

Compare all predefined roles and combinations.

Reviewed: 2026-10-05

## Role catalog

Every fixed role includes basic project and environment viewing within its scope. Additional actions are listed below. Role codes and permission identifiers are technical identifiers; the displayed role labels are localized.

| Role | Additional access |
| --- | --- |
| Project Viewer | View project details, metrics, and build metadata; no log output. |
| Logs Viewer | Read application logs and deployment output. |
| Deployment Operator | Deploy existing code configuration and upgrade named database modules; view build metadata and database list. |
| Instance Operator | Start, stop, and restart the environment. |
| Environment Creator | Create Staging/Development servers within reserved capacity. |
| Environment Deleter | Delete Staging/Development servers, optionally their Git branch. |
| Database Creator | List and create databases. Duplication also needs backup download. |
| Database Backup Operator | List databases/backups and create backups. |
| Database Backup Downloader | List databases/backups and download completed backups. |
| Database Restore Operator | List databases/backups and restore databases or snapshots. |
| Database Deleter | List and permanently delete databases. |
| Database Access Administrator | List databases and reset database administrator passwords. |
| SQL Operator | List databases and run the read-only SQL console. |
| Terminal Operator | Open the instance Shell. |
| Project Access Administrator | Manage access within their own permissions and environment scopes. |
| Project Administrator | All project permissions, including configuration and destructive actions, but not owner billing or unrestricted Database Manager. |

## Combine roles deliberately

Backup creation and download are separate permissions. Duplication needs Database Creator + Database Backup Downloader. Copying across environments needs backup download on the source and database restore on the target. A Logs Viewer does not gain deployment permission, and a Project Viewer does not gain log output. Reserve Project Administrator for people who need all project actions.

## Related guides

- [Grant and remove project access](iam-grants.md)
- [Choose project and environment scopes](iam-scopes.md)
- [Database Manager and administrator passwords](database-access.md)
