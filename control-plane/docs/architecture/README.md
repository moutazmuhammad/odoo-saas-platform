# Architecture Docs

This folder now holds only durable architecture/structure references. The
project's live plan, current-state summary, and phased roadmap all moved to
[`/ROADMAP.md`](../../../ROADMAP.md) at the repo root — start there.

| Doc | Purpose |
|---|---|
| `architecture-spec-v1.md` | The original target architecture (Control Plane layers, storage model, deploy/backup/restore/upgrade flows, and the six guiding principles). Still the conceptual reference; `ROADMAP.md` notes explicitly where the platform has since diverged from it. |
| `AS-BUILT.md` | As-built provisioning/deploy flow and the deploy-mechanism finding (hybrid base-image + mounted source), as deltas against the spec above. |

Every other document previously here (implementation plans, phase-status
trackers, the microservices migration plan, the driver-boundary call-site
catalog, incident reports, test-run snapshots) was a point-in-time
plan/audit/status artifact, not an architecture reference — their useful
content has been consolidated into `/ROADMAP.md` and the files themselves
removed. They remain in git history (`git log --diff-filter=D -- control-plane/docs/architecture/`)
if the original wording is ever needed.

The compute microservice's own architecture doc lives at
[`compute/docs/architecture.md`](../../../compute/docs/architecture.md)
(English) / `architecture.ar.md` (Arabic).
