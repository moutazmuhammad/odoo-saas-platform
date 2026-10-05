# Troubleshoot slow or failed deployments

Separate queue delays, dependency failures, module errors, and rollout failures.

Reviewed: 2026-10-05

## Diagnose by stage

| Symptom | What to check |
| --- | --- |
| Queued for a long time | Another serialized operation or limited build capacity. Ask support if no progress is visible. |
| Repository fetch failure | Clone URL, branch existence, token access, and provider connectivity. |
| Package build failure | requirements.txt, version compatibility, and the first relevant installation error. |
| Module upgrade failure | The selected modules, missing dependencies, migration output, and Staging reproduction. |
| Rollout not ready | Application startup logs, image compatibility, database access, and resource pressure. |
| Git push not deploying | Provider webhook delivery, matching branch, repository integration, and existing build history. |

## Why duration varies

The platform uses cached dependency layers, changed-module selection, and background build polling. Cold caches, new Python packages, image transfer, queue occupancy, and database migrations can still take longer. Avoid repeated redeployments of the same failing commit. Fix the identified cause and redeploy once; use rollback/recovery guidance if live traffic is affected.

## Real-time notification connection errors

A websocket or longpolling error in Odoo logs usually requires checking the platform’s version-specific routing and evented application service. Odoo 16 and newer use /websocket; Odoo 15 and older use /longpolling. Share the Odoo version, environment, and exact error with support. Do not change your application database or disable access controls to work around a routing failure.

## Related guides

- [Build, redeploy, and inspect Deployment history](deployment-history.md)
- [Manage Python dependencies](python-dependencies.md)
- [Understand selective Odoo module upgrades](module-upgrades.md)
- [Roll back a Deployment](deployment-rollback.md)
