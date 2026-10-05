# Manage Python dependencies

Declare reproducible package requirements in your repository.

Reviewed: 2026-10-05

## Use requirements.txt

Python dependencies come from your repository’s `requirements.txt`, not a manually entered package list in the control panel. Commit the file and deploy the affected branch. Pin compatible versions for repeatable builds. The build uses validation and cached dependency layers; a package change can take longer than a code-only rebuild.

```text
phonenumbers==9.0.40
# Add only dependencies needed by your modules.
```

## Resolve dependency failures

1. Read the failed build output to identify the package, version, or validation error.
2. Check compatibility with the selected Odoo and Python runtime, and confirm the package is available from the supported package source.
3. Correct and commit requirements.txt, then push the branch or request redeployment.

Do not use the browser terminal as a persistent package-installation method. Runtime Containers are replaced by deployments; durable dependencies belong in the image build. A requirements file does not provision arbitrary operating-system packages.

## Related guides

- [Connect a Git repository](custom-code.md)
- [Troubleshoot slow or failed deployments](deployment-troubleshooting.md)
