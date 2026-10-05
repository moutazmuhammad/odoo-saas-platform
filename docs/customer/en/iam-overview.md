# Team and permissions overview

Understand ownership, principals, grants, and least-privilege access.

Reviewed: 2026-10-05

## Access model

A grant combines a teammate or team group, one or several projects belonging to the same customer, one or several fixed roles, and Production, Staging, Development, or all environments. Roles combine within their assigned scopes. Customers cannot create custom roles; there are no folder or organization hierarchies to configure.

## Owner versus teammate

The customer owner retains project control and billing. Teammates receive explicit project permissions; no project role gives access to the owner’s invoices, wallet, purchases, or unrestricted Database Manager. A teammate can own independent projects after completing account setup, while shared projects remain governed by their assigned roles. Permissions in VELTNEX do not create Odoo application users or replace Odoo access rights.

## Example

For a release teammate, assign Project Viewer + Logs Viewer + Deployment Operator to selected projects in Staging and Development. This enables inspecting and deploying those environments without deploying Production, accessing Shell, deleting databases, or managing billing.

## Related guides

- [Fixed role reference](iam-roles.md)
- [Choose project and environment scopes](iam-scopes.md)
- [Grant and remove project access](iam-grants.md)
