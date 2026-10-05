# Grant and remove project access

Assign fixed roles to people or groups across selected projects.

Reviewed: 2026-10-05

## Grant access

1. Open **Team & permissions**, globally or from the project. Use the customer/project filter to verify the workspace.
2. Choose **Grant access**, select an existing teammate or team group, and select the projects. Add the teammate first if they are not listed.
3. Select fixed roles and Production, Staging, Development, or all environments. Review destructive roles before saving.
4. Check the resulting role rows and ask the teammate to complete setup or accept an invitation if required.

## Remove access

Remove the applicable role grants and review group membership too. Effective access is the union of direct and group grants; removing one path does not remove another. Revocation blocks later authorization checks, closes terminals that no longer qualify, and cancels unauthorized queued work. Work already executing can finish. Downloaded data remains outside the platform’s control.

## Related guides

- [Manage team groups](iam-groups.md)
- [Add and manage teammates](teammate-profiles.md)
- [Troubleshoot missing projects or denied actions](iam-troubleshooting.md)
