# Customer project access

Install `saas_iam` alongside the existing SaaS modules. The portal exposes **Team & permissions** at `/my/access` and on each hosting project's page. Existing customer owners retain their access automatically; teammates receive only explicitly assigned roles. New owner-created accounts must replace their temporary password and verify their mobile number on WhatsApp before assigned access activates.

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

The customer owner selects **Add teammate** in Team & permissions and enters a name and email; phone is optional when creating the profile, with an automatic dialing code from the selected country (defaulting to the owner’s country). A mobile number is required when the teammate completes setup. Profiles appear immediately in the Teammates tab and in user/group selectors. New portal accounts receive a generated temporary password shown once to the owner, who shares it privately with the teammate. Existing accounts keep their password and contact data.

At first login, a new teammate chooses their own password (at least twelve characters and different from the temporary password). The teammate then confirms or changes their mobile number and enters a WhatsApp verification code. Until both steps complete, the backend blocks project permissions and workspace APIs, including requests made outside the frontend. The password change retains the current session. Customer-owner registration remains unchanged.

In the Teammates tab, the owner can reset passwords for accounts created and used exclusively for their team. A reset generates a new temporary password shown once, invalidates existing sessions and pending authorized work, and requires another password change at the next login. The owner cannot reset independently created logins or accounts shared with another customer, or take over users with their own projects or billing records. Those accounts manage their own passwords. Accounts managed by another customer join additional projects through an invitation accepted by the teammate, preventing unrelated customers from interfering with the original owner’s account controls.

Deleting a teammate profile revokes the customer's direct roles, group membership and invitations, and invalidates terminal/pending-job access. Accounts owned exclusively by the team are disabled. Shared or independently owned accounts retain their login and other customers' access. Recreating a disabled team account generates a new temporary password and does not restore deleted roles.

The migration identifies accounts created by their own customer owner in the previous profile flow, so those users gain the same management controls. It does not adopt existing independent logins. Phone verification requires Meta WhatsApp Cloud API configuration.

Managed logins remain teammates after changing their password: they cannot buy or create independent projects, access customer billing, or create their own teammate accounts. Their project list and actions depend on explicit grants, including for any historical project accidentally linked to their partner. Creating an authorized staging/development environment within an existing project remains possible with the Environment Creator role and reserved capacity.

New teammate profiles and portal signups reject phones already used by another login account or teammate profile. Checks cover phone/mobile, inactive login accounts, formatting variants, and national formats with a known country; a transaction lock serializes claims for the same normalized number. Contacts without a login do not reserve registration identifiers. Registration creates a new contact after phone verification and never attaches its new login to a matching historical billing contact. Verification confirms teammate phone ownership; existing duplicate contact data is not rewritten. Frontend controls deny access when permission metadata is missing.

## WhatsApp verification

Configure **Settings → SaaS Manager → WhatsApp phone verification** with the Meta phone number ID, system user access token, approved authentication template name, its exact language, and the supported Graph API version from your Meta app. Use a Copy Code authentication template with a single code variable and a ten-minute expiration. The token and teammate challenge codes use encrypted fields. The same sender handles customer registration and teammate verification; phone codes are never returned in API responses, including when the old sign-up test setting is enabled. Provider credentials and template approval are required before delivery can be verified live.

Teammate codes expire after ten minutes, are bound to one signed-in user and pending phone number, allow five guesses, and are replaced on resend. Delivery and verification endpoints are rate limited. Failed delivery keeps access blocked. Phone uniqueness is checked again when confirming the code. Changing a verified phone or receiving another temporary password requires verification again. Account setup is enforced by the backend and IAM model guards as well as the frontend.

Provider documentation: [Meta Cloud API](https://www.postman.com/meta/whatsapp-business-platform/folder/lczy75a/templates).

## Invitations and teams

Invitations expire after seven days and can be accepted only by an active account whose login matches the invited email. The database stores a hash of the invitation token. The owner can copy the invitation link; email delivery uses the existing Odoo outgoing-mail queue and requires working mail configuration. Removing all pending grants invalidates an invitation's access. An accepted invitation establishes the teammate relationship for subsequent grants and group membership.

Only the customer owner manages team groups and membership. Group removal deactivates the group and revokes its effective access. Access administrators can invite or assign roles to their projects but cannot grant permissions or environment scopes beyond their own. All project IDs are validated before any multi-project assignment is written.

## Enforcement and revocation

Authorization applies to JSON APIs, legacy portal forms, terminal and log endpoints, backup downloads, and public instance model operations. Shared project links provide limited read access; they cannot authorize database, log, terminal, or management actions. Customer teammates do not receive native ORM access to another customer's instances or backup storage URLs.

Removing a role or group membership closes terminals that are no longer authorized. Log streams recheck access at least every five seconds while yielding data. Pending durable jobs are cancelled when access disappears; queued build steps retain the original actor and check permission before execution. Operations already executing can finish. Backup links exposed in the portal go through an authorization-checking download endpoint. A storage URL already issued by a successful download remains valid until its existing expiry; revocation cannot retract downloaded data.

## Deployment

Back up the platform and control-plane database, stop the Odoo and job services, copy the new module and changed website files, and install `saas_iam` while upgrading only `saas_website`. For an existing installation, upgrade only `saas_iam` for teammate-profile schema changes; Deploy the rebuilt website bundle and restart both services. This installs the IAM tables and job authorization fields; it does not upgrade customer Odoo modules, recreate customer instances, or assign teammate roles automatically.

The regression suite covers role combinations, environment scopes, customer isolation, delegation limits, single-use invitations, group membership, direct model calls, status/billing redaction, permitted creation/deletion/restarts, backup download revocation, and frontend grants across multiple projects.
