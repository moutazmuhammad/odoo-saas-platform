# Troubleshoot missing projects or denied actions

Check setup, account, scopes, direct grants, and group-derived access.

Reviewed: 2026-10-05

## Permission checks

1. Verify you are signed in with the email the owner granted access to, and complete password/phone setup.
2. Check whether an invitation is still pending, expired, or accepted by the matching account.
3. Confirm the project belongs to the intended customer and appears under Shared projects if you are a teammate.
4. Check the selected environment type. Staging roles do not authorize Production and may have no existing environment yet.
5. Compare the requested action with the role catalog. Backup creation is different from download; repository edits require project administration.
6. If access should have been revoked, check direct grants and every group. Removing one grant is insufficient if another grants the action.

## Why you may see not found

Unauthorized resource requests can return a not-found response to avoid disclosing another customer’s resources. That message does not always mean the project was physically deleted. Ask the owner to review your effective project and environment access. Billing and unrestricted Database Manager remain owner-only even with Project Administrator.

## Related guides

- [Fixed role reference](iam-roles.md)
- [Choose project and environment scopes](iam-scopes.md)
- [Invite an existing account](iam-invitations.md)
- [Complete your first teammate login](teammate-onboarding.md)
