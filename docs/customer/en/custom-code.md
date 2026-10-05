# Connect a Git repository

Configure project code, private credentials, and branch inheritance.

Reviewed: 2026-10-05

## Before you begin

This is a hosting feature. Repository configuration requires **Project Administrator** or owner access and a running or stopped instance. A Deployment Operator can deploy existing configuration but cannot change the project repository settings. GitHub, GitLab, Bitbucket, and supported Gitea integrations have different provider permissions and API behavior.

## Connect and deploy

1. On Production, open **Code & packages** and enter the Git clone URL and branch.
2. For a private repository, enter a token with access to the repository. Allow branch and webhook operations if you will use those workflows.
3. Choose **Connect & deploy** or **Save & redeploy**. Wait for the build and rollout result.
4. On subsequent edits, leaving the token blank preserves the stored token. Never paste credentials into a public issue or log excerpt.

## Environment inheritance and disconnecting

Child environments inherit the repository and credential from the project, with their own selected branch. Manage shared connection settings on Production. Disconnecting removes custom code from the runtime image; it does not uninstall modules from your databases. Installed modules can still require that code, so plan and test disconnection carefully.

## Related guides

- [Manage Python dependencies](python-dependencies.md)
- [Build, redeploy, and inspect Deployment history](deployment-history.md)
- [Create a Staging or Development environment](environment-create.md)
