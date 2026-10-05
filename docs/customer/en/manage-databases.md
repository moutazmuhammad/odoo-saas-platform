# Create and open a database

Initialize your application and understand the Production database limit.

Reviewed: 2026-10-05

## Database limits

Production hosting allows one customer database. You can delete it and create another, or restore into it with replacement. Staging and Development do not use this one-database limit, but they share their environment’s storage allowance. Existing legacy extra databases are not proof that creating another is allowed.

## Create the database

1. Open a running hosting environment’s **Databases** tab with **Database Creator** access.
2. Choose Create database and enter a lowercase name, administrator login, and password. The portal currently initializes new databases in English; configure the application language inside Odoo afterward.
3. Review the full prefixed name and confirm. Wait until the create operation finishes and the database appears.
4. Use Open from the database row and sign in with its administrator credentials. A VELTNEX password does not automatically become the database password.

## Naming rules

Use at least 3 characters, start with a letter, and use lowercase letters, digits, underscores, or hyphens. Do not end with an underscore/hyphen or repeat either consecutively. The full database name, including the environment prefix, must fit PostgreSQL’s 63-character name limit. The prefix keeps databases associated with the correct environment.

## Related guides

- [Restore a database from a local backup](database-restore.md)
- [Database Manager and administrator passwords](database-access.md)
- [Delete and replace a database](database-delete.md)
