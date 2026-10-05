# Delete an environment or its Git branch

Remove a non-production server and choose whether to keep its branch.

Reviewed: 2026-10-05

## Before deleting

> Warning: Deletion permanently removes the environment’s active databases, files, and runtime logs. Download any backups you need first. This workflow cannot delete Production; cancelling the project is a separate action.

You need **Environment Deleter** for the target environment type. Deleting a server frees its reserved slot for reuse but does not release or refund the slot itself.

## Delete safely

1. Select the Staging or Development environment and choose Delete.
2. Leave remote-branch deletion unchecked to keep the code branch. Select it only if the branch is no longer needed.
3. Type the exact environment name required by the confirmation dialog and confirm.
4. Wait for removal to finish. If branch deletion is rejected, inspect provider permissions, default/protected branch settings, and whether the branch still exists.

## Related guides

- [Reserve and release environment slots](environment-slots.md)
- [Cancel and reactivate a project](reactivate.md)
- [Create a Staging or Development environment](environment-create.md)
