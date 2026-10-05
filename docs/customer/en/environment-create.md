# Create a Staging or Development environment

Use a reserved slot and an existing or new Git branch.

Reviewed: 2026-10-05

## Before you begin

- Use a paid hosting project with a repository connected to Production.
- Have an unused reserved slot of the required type and **Environment Creator** access for that type.
- Ensure the repository token can read code and perform any required branch operation.

## Create the environment

1. Open the project workspace and choose to create Staging or Development.
2. Enter the environment name. Development requires a name. Select an existing branch or specify a new branch; when omitted the branch uses the environment name.
3. Confirm creation and follow provisioning and the initial code build. The environment inherits the project repository and Odoo version.
4. Open Databases to create or restore data, or use the project’s database-copy workflow.

## Data and isolation

> Warning: Do not assume creation automatically clones Production data, loads demo data, or disables outgoing email, scheduled actions, payments, and integrations. Confirm the new environment’s actual databases and configure test-safe application settings before use.

Staging and Development are separate runtime and data environments. Their Databases tools are not subject to the single-customer-database Production limit, although storage capacity still applies.

## Related guides

- [Copy databases between environments](environment-copy.md)
- [Reserve and release environment slots](environment-slots.md)
- [Troubleshoot a project stuck in provisioning](provisioning-troubleshooting.md)
