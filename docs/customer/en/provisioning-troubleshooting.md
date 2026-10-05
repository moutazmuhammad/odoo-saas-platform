# Troubleshoot a project stuck in provisioning

Find the active operation and collect evidence before retrying.

Reviewed: 2026-10-05

## Check in this order

1. Confirm checkout is settled. Pending payment is not a completed launch.
2. Read the current state and operation. Provisioning can also represent restore, cancellation, or another background action.
3. For Initial deployment, inspect Deployment history if permitted and check whether an image build or rollout is active.
4. Refresh status after a reasonable interval rather than creating duplicate projects or repeating destructive requests.
5. Contact support with the project/environment, operation start time, invoice reference, build stage, and visible error if progress has stopped.

## Common causes

Capacity allocation, an unavailable region/Cluster, image retrieval, TLS setup, Git credentials, dependency builds, database readiness, or failed infrastructure checks can delay launch. Background retry and recovery do not make every error recoverable automatically. Do not send server passwords or tokens when requesting help.

## Related guides

- [Instance status reference](instance-status.md)
- [Troubleshoot slow or failed deployments](deployment-troubleshooting.md)
- [Choose a support plan and request help](support-plans.md)
