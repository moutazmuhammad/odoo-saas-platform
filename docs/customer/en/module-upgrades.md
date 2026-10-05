# Understand selective Odoo module upgrades

Distinguish code deployment, module upgrades, and major-version migration.

Reviewed: 2026-10-05

## How modules are selected

The build compares repository module upgrade inputs with the last successful baseline. Relevant inputs include manifest version/dependencies/loaded data, model fields and schema metadata, XML or CSV loaded by the manifest, and migration files. Ordinary Python method-body and static asset changes generally do not request a database upgrade by themselves.

Selected repository modules are passed by name; the pipeline does not request the blanket `all` upgrade. Without a previous baseline, all detected repository modules may be selected. Odoo skips uninstalled modules; deployment is not a replacement for installing a new application. Odoo dependencies and migration logic can still cause related database work.

## Request a manual upgrade

1. Back up the database and verify the module code is already deployed.
2. In Databases, choose the module-upgrade action for the required database.
3. Enter explicit technical module names such as `sale, stock_account`. Use **Deployment Operator** access and follow the operation report.

> Warning: Module upgrades can change schema and records. They are different from changing the Odoo major version and should be tested in Staging before Production.

## Related guides

- [Create a Staging or Development environment](environment-create.md)
- [Create and download a database backup](ondemand-backups.md)
- [Roll back a Deployment](deployment-rollback.md)
