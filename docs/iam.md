# Customer project access

Install `saas_iam` alongside the existing SaaS modules. The portal exposes **Project Access** at `/my/access` and on each hosting project's page. Existing customer owners retain their access automatically; teammates receive only explicitly assigned roles. Owner-created profiles require email and phone verification before assigned access activates.

There are no custom roles, folders, or organization hierarchy. A grant selects a teammate or team group, one or several projects belonging to the same customer, fixed roles, and production, staging, development, or all environments. Roles combine within their assigned scopes. Every role includes access to the basic project/environment details in its scope. A project with only staging access remains discoverable while production details and operations are restricted.

| Fixed role | Additional permissions |
| --- | --- |
| Project Viewer | Metrics and build history, without deployment logs |
| Logs Viewer | Application and deployment logs |
| Deployment Operator | Build, deploy, and upgrade changed Odoo modules |
| Instance Operator | Start, stop, and restart |
| Environment Creator | Create staging/development servers within reserved capacity |
| Environment Deleter | Delete staging/development servers, optionally removing their Git branch |
| Database Creator | Create databases |
| Database Backup Operator | Create backups |
| Database Backup Downloader | Download backups |
| Database Restore Operator | Restore databases and copy from authorized project environments |
| Database Deleter | Delete databases |
| Database Access Administrator | Reset database administrator passwords |
| SQL Operator | Run the existing SQL tool, subject to its query restrictions |
| Terminal Operator | Open the instance terminal |
| Project Access Administrator | Grant and remove access within the administrator's own permissions and scopes |
| Project Administrator | All project permissions, including settings and destructive instance operations |

Database roles include database listing. Backup roles include backup listing. Duplicating a database requires both Database Creator and Database Backup Downloader, because duplication exports the source data. Copying between environments requires restore permission on the target and backup download permission on the source. Both must belong to the same project.

Billing, payments, purchased capacity, and the unrestricted signed Database Manager are reserved for the customer owner and platform administrators. Teammates use the permission-checked portal database tools. Project Administrator does not grant billing access. Platform staff retain their existing global read access, and platform administrators retain management access.

## Teammate profiles

The customer owner selects **Add teammate** in Project Access and enters name, email, and a phone number in international format. Profiles appear immediately in the Teammates tab and in user/group selectors. New portal accounts receive a generated temporary password shown once to the owner. The owner shares the sign-in details privately. Existing accounts keep their password and contact data; the customer profile stores its own name and phone.

After signing in, the teammate must verify email and phone using account-bound, single-use, ten-minute codes. Newly created accounts also replace their temporary password with a password of at least twelve characters. Assigned direct/group permissions remain disabled until these steps complete, including direct backend API calls. Codes have attempt limits, send/verify endpoints are rate limited, and profile codes cannot be used for registration or password resets.

Verification requires working outgoing email and an Odoo SMS provider with sufficient credit. Delivery errors are shown to the teammate; this feature never returns verification codes in an API response or bypasses verification when delivery is unavailable.

The owner can delete a profile. This removes their customer's direct roles and group membership, revokes their invitations, and invalidates pending work/terminal access. It preserves the global login and any access/projects belonging to other customers. Recreating a removed profile requires verification again and does not restore deleted grants.

## Invitations and teams

Invitations expire after seven days and can be accepted only by an active account whose login matches the invited email. The database stores a hash of the invitation token. The owner can copy the invitation link; email delivery uses the existing Odoo outgoing-mail queue and requires working mail configuration. Removing all pending grants invalidates an invitation's access. An accepted invitation establishes the teammate relationship for subsequent grants and group membership.

Only the customer owner manages team groups and membership. Group removal deactivates the group and revokes its effective access. Access administrators can invite or assign roles to their projects but cannot grant permissions or environment scopes beyond their own. All project IDs are validated before any multi-project assignment is written.

## Enforcement and revocation

Authorization applies to JSON APIs, legacy portal forms, terminal and log endpoints, backup downloads, and public instance model operations. Shared project links provide limited read access; they cannot authorize database, log, terminal, or management actions. Customer teammates do not receive native ORM access to another customer's instances or backup storage URLs.

Removing a role or group membership closes terminals that are no longer authorized. Log streams recheck access at least every five seconds while yielding data. Pending durable jobs are cancelled when access disappears; queued build steps retain the original actor and check permission before execution. Operations already executing can finish. Backup links exposed in the portal go through an authorization-checking download endpoint. A storage URL already issued by a successful download remains valid until its existing expiry; revocation cannot retract downloaded data.

## Deployment

Back up the platform and control-plane database, stop the Odoo and job services, copy the new module and changed website files, and install `saas_iam` while upgrading only `saas_website`. For an existing installation, upgrade only `saas_iam` for teammate-profile schema changes; its `sms` dependency must be installed. Deploy the rebuilt website bundle and restart both services. This installs the IAM tables and job authorization fields; it does not upgrade customer Odoo modules, recreate customer instances, or assign teammate roles automatically.

The regression suite covers role combinations, environment scopes, customer isolation, delegation limits, single-use invitations, group membership, direct model calls, status/billing redaction, permitted creation/deletion/restarts, backup download revocation, and frontend grants across multiple projects.
