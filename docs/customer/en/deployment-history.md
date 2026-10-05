# Build, redeploy, and inspect Deployment history

Follow build stages, triggers, outcomes, and retention.

Reviewed: 2026-10-05

## Deployment workflow

Deployments can originate from initial launch, a Git push webhook, manual redeployment, a merge, or rollback. Automatic pushes require a working provider webhook and the matching repository branch. Check provider webhook delivery if a push does not appear. Manual redeployment requires **Deployment Operator** access.

| Stage | What is happening |
| --- | --- |
| Queued | Waiting for a build slot or serialized work to finish. |
| Building image | Fetching code, validating dependencies, and preparing the runtime image. |
| Upgrading modules & rolling out | Applying needed Odoo upgrades and checking the new application runtime. |
| Success / Failed | The final result; inspect details before another change. |

## Inspect a build

Select the environment and open its Deployment history. Review the trigger, branch, commit, author, time, stage, and result. **Project Viewer** can view build metadata. **Logs Viewer** is required to read deployment output. The current image continues serving during image preparation where possible, but rollout and database changes can still affect users.

## History retention

History is bounded, not permanent. Defaults are 30 days and 50 completed records per environment, configurable by the platform. Active work, current/previous successful images, and upgrade baselines can be protected beyond ordinary cleanup. Download important error output before it is removed; build-history retention is separate from backups and registry-image retention.

## Related guides

- [Understand selective Odoo module upgrades](module-upgrades.md)
- [Roll back a Deployment](deployment-rollback.md)
- [Troubleshoot slow or failed deployments](deployment-troubleshooting.md)
