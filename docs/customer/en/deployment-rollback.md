# Roll back a Deployment

Return to a previous runtime image while treating database changes separately.

Reviewed: 2026-10-05

## What rollback does

The backend supports rollback to a previously successful immutable runtime image when an eligible image is retained. The current customer portal does not expose a rollback button; contact support to assess an eligible image. It does not undo schema changes, module migrations, or data written since the earlier deployment.

## Plan a safe rollback

1. Identify the previous successful commit/image and read the failure that triggered rollback.
2. Confirm the older code can run against the current database. If it cannot, plan a compatible database restore with support.
3. Request rollback through support and agree on the target image and recovery plan. Monitor the resulting rollout and application behavior.

## Related guides

- [Restore a full-instance snapshot](restore.md)
- [Understand selective Odoo module upgrades](module-upgrades.md)
- [Build, redeploy, and inspect Deployment history](deployment-history.md)
