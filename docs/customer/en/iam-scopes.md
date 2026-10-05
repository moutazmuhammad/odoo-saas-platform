# Choose project and environment scopes

Grant access to exactly the projects and environment types needed.

Reviewed: 2026-10-05

## How scopes apply

Select one or multiple customer projects and the environment types covered. **All environments** includes Production, Staging, and Development. A Production-only role cannot operate Staging, and a Staging-only role cannot operate Production. New environments of an authorized type in the assigned project fall within that type’s scope.

## An empty scope is still meaningful

You can grant Staging access before the owner creates Staging. The teammate can discover the project but sees no authorized environments until one exists. Do not add Production solely to make the page look populated. Choose Production only when live-environment access is intended.

## Delegation boundaries

A Project Access Administrator can grant only permissions and environment scopes already available to them. They cannot promote themselves or someone else beyond their own access. Owner-only team-profile, group, capacity, and billing management remains separate.

## Related guides

- [Navigate your project workspace](project-workspace.md)
- [Grant and remove project access](iam-grants.md)
- [Fixed role reference](iam-roles.md)
