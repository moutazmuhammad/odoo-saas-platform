# Merge code between environments

Promote a source Git branch into a target branch without copying data.

Reviewed: 2026-10-05

## Before merging

Choose two environments in the same project and connected repository. You need project-view access to the source and **Deployment Operator** access to the target. Check both branch names and test the source code before merging into Production. The repository credential must permit the provider’s merge operation.

## Merge and verify

1. Use the workspace merge action or drag the source environment to the target where the interface offers it.
2. Review the direction: source changes go into the target branch. Confirm the merge.
3. If the provider reports a successful merge and the target is running or stopped, the platform requests a target redeployment. Follow its Deployment history.
4. Resolve Git conflicts or protected-branch restrictions in your Git provider, then retry when appropriate.

## Code is separate from data

> Warning: Merging does not copy or replace databases. A Deployment rollback also does not reverse database migrations. Back up live data before changes that alter schema or loaded Odoo data.

## Related guides

- [Connect a Git repository](custom-code.md)
- [Understand selective Odoo module upgrades](module-upgrades.md)
- [Build, redeploy, and inspect Deployment history](deployment-history.md)
