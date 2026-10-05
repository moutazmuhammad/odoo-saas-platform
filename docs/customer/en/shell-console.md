# Use the browser Shell

Open a scoped terminal and understand session and persistence limits.

Reviewed: 2026-10-05

## Access and boundary

Shell is a hosting-only tool requiring a running environment and **Terminal Operator**. The customer terminal opens the instance’s dedicated Shell Container, not the Kubernetes host or a platform administrator terminal. It does not expose the application’s master/database credentials.

## Open and close a session

1. Select the correct environment and open Shell. Wait if the access Container is still being prepared.
2. Run diagnostic commands supported by the available tools and your environment. Do not assume root access or application database credentials.
3. Close the terminal when finished. Reconnect if the session expires or the instance is redeployed.

## Session and persistence

Idle terminals are cleaned up after approximately 10 minutes. Replaced runtime Containers do not preserve arbitrary local edits or package installations. Keep permanent application code and Python dependencies in Git and the build configuration. Treat commands as real actions in the scoped environment, and avoid sharing terminal output that contains private data.

## Related guides

- [Manage Python dependencies](python-dependencies.md)
- [Fixed role reference](iam-roles.md)
- [View and download application logs](application-logs.md)
