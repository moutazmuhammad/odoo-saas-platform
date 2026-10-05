# Database Manager and administrator passwords

Open authorized management tools and separate platform access from Odoo access.

Reviewed: 2026-10-05

## Open Database Manager

The owner or a platform administrator can open **Database Manager** from the environment’s Databases page. The button generates authorized access to Odoo’s own manager without revealing the master password. Teammates, including Project Administrators, use the permission-checked portal database actions instead; no fixed teammate role grants unrestricted Database Manager access.

Use the control-panel button rather than bookmarking or sharing the manager link. The public root of a hosting site with a customer database serves the application; an empty site shows a no-database message. Database-management authorization is separate from visitors opening the customer’s website.

## Reset the database administrator password

1. With **Database Access Administrator** access, choose Reset password for the required database.
2. Set a new password that meets the dialog’s requirements and save it securely.
3. Use the administrator login displayed for the database and the new password to sign in to Odoo.

This changes the database administrator’s application password. It does not change your VELTNEX login, the PostgreSQL connection credential, or other Odoo users.

## Related guides

- [Fixed role reference](iam-roles.md)
- [Create and open a database](manage-databases.md)
- [Recover your VELTNEX password](password-recovery.md)
