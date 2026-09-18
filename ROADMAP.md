# Odoo SaaS Platform — Production Roadmap

**Status: living document. Last synchronized with the codebase: 2026-09-15.**

This is the **single source of truth** for where this platform stands and where
it's going. It replaces `docs/PRODUCTION-READINESS-PLAN.md` and
`control-plane/docs/architecture/MICROSERVICES-PLAN.md` (both consolidated into
this document and removed, along with every other one-off audit/plan/status
report previously scattered across the repo — see "Document hygiene" below).
Pure architecture/structure references survive separately (component READMEs,
`compute/docs/architecture.md`, `control-plane/docs/architecture/architecture-spec-v1.md`)
and are linked from the relevant sections instead of being duplicated here.

**Vision**: an Odoo.sh-equivalent hosting platform — self-service instance
provisioning, git-based deploys, branch environments, backups/restore,
plan-based billing, and (the gap this revision exists to close) **real,
customer-facing infrastructure metrics and logs**, on a compute layer that
scales past a handful of hand-managed hosts without a rewrite.

## How to read this document

Every status claim below is tagged with how it was established, because a
recurring, named failure mode in this project's own history (see
`PRODUCTION-REVIEW-2026-06-19` in git history) was documentation confidently
claiming things were "done" that weren't, or citing stale numbers. Tags used:

- **[Live-verified]** — actually run against a real cluster/host/DB, not mocked.
- **[Test-verified]** — covered by passing automated tests, not run against real infra.
- **[Code-confirmed]** — read directly in the current source during this
  revision's audit; no test coverage confirmed either way.
- **[Documented, unverified]** — an earlier doc claimed this; not re-checked here.
- **[Known gap]** — confirmed absent.

Don't trust a claim's freshness by which file it's in — trust the tag, and
re-verify anything load-bearing before building on it if it's been a while
since 2026-09-15.

---

## 1. Executive summary

The platform is a working, revenue-capable Odoo hosting SaaS. Every customer
instance today runs on the **legacy path**: a fleet of manually-provisioned
Docker hosts, controlled over SSH, with an Odoo control plane (`saas_core`)
acting as the orchestrator, billing engine, and portal API backend. This path
is functional and has had a full security-hardening pass this cycle (see §3.6).

In parallel, a **Kubernetes compute layer** has been built from scratch this
year: a real operator (`compute/operator`), a `ComputeDriver` abstraction in
the control plane that can target either backend, and a live, working
microk8s cluster this repo has been developed and tested against. Real
provisioning now calls `create()` for a brand-new instance — Kubernetes is
the platform default (`saas_master.default_compute_driver`), Docker
Compose an available alternative — and an existing tenant can be migrated
across via a manual, admin-invoked tool. **Zero real paying customers have
actually used either path in production yet** — every live verification
so far has been against this repo's own dev/test fixtures on a local
microk8s cluster, never a real customer's data or domain. This is the
single most important fact to carry forward: *the new compute layer is
code-complete and live-verified against test infrastructure, not yet
proven against real production traffic.*

The platform has **no customer-facing observability** — no Prometheus, no
Grafana, no Loki, anywhere. What exists is a homegrown Odoo-DB metrics
sampler with a documented scaling ceiling, and raw SSH-based log tailing.
Building real metrics/logs is genuinely greenfield work, and this document's
biggest addition over its predecessors is a concrete design for it (§4.3),
sequenced to land on top of the Kubernetes cutover rather than retrofitted
onto the legacy fleet.

**Top 3 priorities, in order:**

1. **Finish the Kubernetes cutover (§5, Phase 2)** — the compute layer already
   exists; provisioning real tenants on it is the highest-leverage unfinished
   work, and almost everything else (real per-pod metrics, real autoscaling,
   real multi-region) is far cheaper to build on Kubernetes than on the
   legacy SSH/Docker fleet.
2. **Ship Prometheus + Grafana + Loki (§5, Phase 3)** — this is the
   customer-facing capability gap this document was commissioned to close,
   and it depends on Phase 2 being far enough along to have real pods to
   scrape.
3. **Close the scalability correctness bugs already found and documented**
   (§3.7, SCALE-001/002/005/007) — these are concurrency bugs on shared
   infra (nginx config races, capacity-allocation races, a single
   cluster-wide metrics-sampler lock) that turn an isolated incident into a
   multi-tenant one, independent of which compute backend is in use.

---

## 2. Repository layout

```text
.
├── control-plane/   Odoo 18 control plane: saas_core (models/business logic)
│                    + saas_website (public/portal API + QWeb templates that
│                    host the SPA). Owns tenant/billing records, auth, the
│                    Postgres-backed job queue, and the public
│                    /saas/api/v1/* JSON API.
│                    See control-plane/docs/architecture/.
│
├── frontend/        veltnex: the React/Vite/TypeScript SPA served by
│                    control-plane/saas_website. Talks only to
│                    /saas/api/v1/* — no direct infrastructure awareness.
│
├── compute/         The Kubernetes operator (OdooInstance CRD + controller):
│                    provisions/deletes/backs up/restores tenant instances.
│                    The Control Plane will talk to it exclusively through
│                    the Kubernetes API, never SSH, once cutover (§5 Phase 2)
│                    is complete. See compute/docs/architecture.md (English)
│                    / architecture.ar.md (Arabic).
│
├── ROADMAP.md       This file — the single source of truth.
│
└── .github/workflows/ci.yml   spa · odoo-tests · csrf-lint · secret-scan ·
                                compute · image-scan
```

### Document hygiene (what changed in this revision)

Every audit/plan/status/incident-report markdown file previously scattered
across `docs/`, `control-plane/docs/architecture/`, `control-plane/docs/reviews/`
(and its `v2/` subfolder), and `control-plane/SESSION_NOTES.md` has been
**deleted** — their useful, still-relevant content is consolidated into this
document (mainly §3 and §7). They remain available in git history
(`git log --diff-filter=D -- <path>`) if the original wording or a specific
audit's full finding list is ever needed. What's **kept** as standalone
architecture/structure documentation, and why:

| Kept | Why |
|---|---|
| `compute/docs/architecture.md` (+ `.ar.md`) | Current, accurate, 893-line design doc for the compute microservice — genuinely a reference doc, not a status report. |
| `compute/README.md` (+ `.ar.md`) | Component quick-start/usage. |
| `compute/operator/test/e2e/README.md` | Describes the e2e test suite's own structure. |
| `control-plane/docs/architecture/architecture-spec-v1.md` | Original north-star design (Control Plane layers, storage model, six guiding principles) — still the conceptual reference; superseded only where this roadmap explicitly says so. |
| `control-plane/docs/architecture/AS-BUILT.md` | As-built vs. spec deltas — a structural reference, not a status snapshot. |
| `control-plane/docs/architecture/README.md` | Index for the (now much smaller) architecture docs folder — updated to reflect this cleanup. |
| `control-plane/docs/LOCAL-TESTING.md`, `LOCAL-RUNTIME-SYSTEMD.md` | How the local dev environment is actually structured and run — practical structure docs, not status reports. |
| `frontend/veltnex/README.md`, `*/docs-img/README.md` | Component/asset-folder documentation. |

Two legacy-Docker runbooks (`control-plane/saas_core/docker/README.md`,
`.../SERVER-SETUP.md`) were **removed** as pure operational runbooks (package
lists, step-numbered shell commands) with no architectural content — except
one structural fact worth keeping: the legacy path shares one **read-only
Odoo source tree per version** under `/opt/odoo-source/<version>/`, mounted
read-only into every tenant container on that host (image family
`odoo-light:<version>`), which is how it keeps per-instance disk usage near
zero. Anyone standing up a new legacy host needs that runbook's content
reconstructed from git history or written fresh as part of Phase 6 (legacy
decommission planning).

---

## 3. Current state by subsystem

### 3.1 Control plane (`control-plane/saas_core`, `saas_website`)

**[Code-confirmed]** 31 models, ~12,600-line `saas_instance.py` (the "god
model" — see §3.9), Postgres-backed durable job queue (`saas_job.py`,
deliberately **not** Redis/Celery — see ADR-1 in §8), webhook-driven git
deploys, plan-based billing with proration and a wallet system, a
region model with per-region price multipliers, an internal ops-alert
webhook (Slack/Discord, `saas_alert.py`), and an append-only audit log
(`saas_audit_log.py`, wired into deploy/redeploy/restore/scale paths).

**[Code-confirmed]** A `ComputeDriver` abstraction (`saas_core/drivers/`)
with two implementations — `SshDockerDriver` (legacy, carries 100% of
production traffic today) and `KubernetesDriver` (new, live-verified against
a real cluster, carries 0% of production traffic). **Phase 2.3** of the
former `MICROSERVICES-PLAN.md` — routing every call site in `saas_instance.py`
that talks to a tenant's container through this driver interface instead of
raw SSH/`docker compose` strings — is **complete**: `_do_deploy_locked`'s
DB-init, `_do_redeploy`'s blue/green zero-downtime mechanism,
`hosting_db_upgrade_module`, and `_hosting_build_template_db` are all
routed and covered by characterization tests written against real
pre-change behavior before each swap. Four remaining raw-`docker` call
sites were deliberately left unrouted as out of `ComputeDriver`'s
per-instance-lifecycle scope (a tenant image build/push pipeline, an
image-UID probe, a throwaway pip-install validation container, and a
by-container-name health-check poll shared by canonical and blue/green
containers) — revisit only if Phase 2's Kubernetes cutover work finds a
concrete need.

**[Code-confirmed]** `DataService` (`saas_core/dataservice/`) implements the
two intended primitives partially: `snapshot()` (wraps restic backup) works;
**`materialize()` across a different target and `neutralize()` are still
`NotImplementedError`** — cross-instance clone/promote and secure
decommission are not built. Object storage (JuiceFS+MinIO for dev, S3-compatible
for prod) is wired into the backup/restore path and **[Live-verified]** for
the Kubernetes path this year (a real backup → cross-instance-restore round
trip against both PVC and ObjectStorage backends).

**[Live-verified]** `DataService.migrate_to_kubernetes(instance, target_server)`
— Phase 2's first work item, "build the migration path" — bridges the two
previously-disconnected backup systems above (restic-based legacy vs. the
operator's `pg_dump`+tar convention). It dumps a live `ssh_docker` tenant's
real DB+filestore straight from its container into the exact object-storage
layout `compute/operator`'s restore Job expects (new helpers on
`SaasInstanceBackup`: `_stream_pg_dump_for_k8s_restore`,
`_stream_filestore_tar_for_k8s_restore`, `_write_k8s_restore_manifest`,
`dump_for_k8s_migration`), creates a new parallel `saas.instance` +
`OdooInstance` CR with `spec.restore.source` pointing at it
(`KubernetesDriver._build_odoo_instance` now translates a
`spec.env['restore']` block onto `RestoreSourceSpec`), provisions the
namespace's restore-credentials Secret (the operator creates none itself),
polls the `RestoreReady` condition and then the instance's actual health
(the web Deployment rollout after restore takes a little longer than
restore itself), and verifies real data made it across (a `res.users`
row-count comparison between source and restored instance, not just
pod-health) before returning the new instance — verified but never cut
over. On any failure the new CR/namespace/instance are torn down and the
source is never touched. Unit-tested (41 tests,
`tests/test_k8s_migration_dump.py` + additions to `test_kubernetes_driver.py`/
`test_dataservice.py`), full `saas_core`+`saas_website` suite green (548
tests).

**Live-verified this session** against this machine's real microk8s cluster
+ a local MinIO stand-in (a real `ssh_docker`-mode tenant — a genuine
running Odoo 18 container with real Postgres data and filestore — migrated
end-to-end onto a fresh `OdooInstance`, reaching CR phase `Ready` with a
real Ingress route, `res.users` row counts matching, and the restored
instance's `/web/login` returning HTTP 200 over that route). Three real
bugs were found and fixed by this run (all now covered by regression
tests):
1. `_wait_for_restore_ready` treated `status=False` alone as terminal —
   `reconcileRestore` legitimately reports `status=False` with
   `reason=RestoreRunning`/`RestorePending` while the restore Job is still
   in progress; only `reason=RestoreFailed` is terminal.
2. `_ensure_restore_secret` could 403 writing into a namespace still
   mid-termination from an immediately-prior attempt reusing the same
   (deterministic) namespace name — now waits out `Terminating` before
   proceeding.
3. The post-restore health check was a single `driver.health()` call;
   the web Deployment's rollout after a successful restore visibly takes
   longer than the restore itself, so this is now `_wait_until_healthy`,
   a proper poll.

One real, **environment-specific finding, not a code bug**: the tenant
container's own `pg_dump` version must not exceed the restore tool image's
`pg_restore` version (`compute/tools/backup-tool`, currently pinned to
PostgreSQL 16) — `pg_dump`'s custom archive format is forward- but not
backward-compatible, and a newer `pg_dump` (e.g. a stock Docker Hub
`odoo:18.0` image, which bundles PostgreSQL 18 client tools) produces an
archive the restore tool's older `pg_restore` cannot read
(`unsupported version (1.16) in file header`). This platform's own
production tenant images should be checked against the restore tool's
pinned Postgres version as part of Phase 2's rollout — not yet audited.

Two things intentionally NOT fixed, left as accepted local-dev-only
friction (real production infra doesn't have either problem): this
bare-metal microk8s has no LoadBalancer provider, so a fresh Ingress
never gets a real address without manually patching the ingress
controller's Service status (stood in for what a real cloud LB/MetalLB
provides automatically); and the `saas.instance` DB write for the target
record is not crash-safe against a hard process kill mid-migration (a
Python exception is handled correctly — verified repeatedly, every
failure this session cleaned up its CR/namespace/DB row — but a raw
process kill mid-flight, e.g. an Odoo worker recycle, leaves the real K8s
resources orphaned with no DB row tracking them, since the DB write and
the K8s API calls aren't in the same transaction). The second one is worth
a real design decision before Phase 2 goes further (incremental commits
per step? a pre-created "in-progress" row committed before touching
Kubernetes?) — flagged, not solved, here.

**[Test-verified, partially live-verified]** `saas.instance
.action_cutover_to_kubernetes()` / `_do_cutover_to_kubernetes` — Phase 2's
cutover mechanism. A manual, per-tenant, health-gated, reversible action
that flips a legacy instance's live nginx vhost to an already-migrated
(`migration_state='verified'`) Kubernetes instance — deliberately the
"canary" primitive at MVP scope (one tenant at a time, by a human/ops
action); automating it into a scheduled/percentage rollout is left as a
later, separate step.

Two real design problems surfaced while building this, neither obvious
from the migration primitive alone:
1. `KubernetesDriver.endpoint()` used to return the tenant's own public
   domain (from the CR's `status.url`) — not a connectable address, since
   that hostname's DNS still points at the legacy proxy (connecting to it
   would loop back, not reach the cluster). It now returns a new
   manually-configured, per-region value, `saas.region.ingress_host`/
   `ingress_port` — treated the same way `kubeconfig` already is, since
   which Service/controller actually fronts a cluster (the operator
   supports both plain `Ingress` and Gateway API, chosen at the operator
   level) isn't safely auto-discoverable from the driver.
2. `migrate_to_kubernetes` gives the target a *different* subdomain
   (`<source>-k8s`) than the source, so the target's own `OdooInstance`
   Ingress rule routes on that internal hostname, not the tenant's real
   domain — nginx must send it as an explicit upstream `Host` header (a
   small, additive, opt-in template change: `upstream_host`, unset for
   every existing ssh_docker vhost). This also ruled out "promote target,
   retire source" as the cutover model: `saas.instance.subdomain` is under
   a strict, unconditional unique index, and there's no verified way to
   rename a live CR's domain post-creation. Instead, the source instance
   stays the tenant's permanent billing/domain identity forever (its own
   container is left running, untouched, as the live rollback target —
   reclaiming it is Phase 6's job); a new self-referencing
   `active_instance_id` field is the only persistent state added, set
   once a cutover succeeds.

Covered by 7 new tests (`test_cutover_to_kubernetes.py`) plus 3 rewritten
`KubernetesDriver.endpoint()` tests; full `saas_core`+`saas_website` suite
green (556 tests). Live-verified against this machine's real microk8s
cluster: the region's real Traefik LB IP, hit directly with the target
CR's own hostname as an explicit `Host` header, correctly reaches the real
pod (`HTTP 200` on `/web/login`; a wrong `Host` header correctly `404`s,
proving it really routes on Host and isn't just always succeeding) — the
one genuinely new, previously-unverified assumption this step relies on.
**Not** live-verified: an actual end-to-end nginx flip (this machine's
lightweight sshd-over-Docker stand-in for the legacy fleet has no
nginx/systemd installed — installing a full VM-like target for this ssh
stand-in was judged disproportionate to this step); that mechanism itself
is the pre-existing, already-in-production blue/green atomic-vhost
machinery, exercised only through the (real, unmocked) Jinja template
render in the new tests, not a live host.

**[Live-verified]** `_do_deploy_locked` now has a real Kubernetes branch —
a brand-new instance can be provisioned directly on Kubernetes, no
migration involved. Fixed three real bugs found while making this work,
none of which were obvious from the driver abstraction alone:
1. `saas.server._probe_reachable()` was a raw TCP/SSH connect — a
   `compute_driver='kubernetes'` server has no `ip_v4`/SSH, so it was
   **permanently reported unreachable** and could never be allocated. Now
   branches: `ssh_docker` keeps the TCP probe, `kubernetes` makes one
   short-timeout K8s API call (`list_namespace`).
2. Allocation (`_allocate_docker_server`/`_allocate_overcommit_server`)
   never filtered by `compute_driver` at all. Now takes an optional
   `compute_driver` param (backward-compatible — `None` keeps the old
   driver-blind behavior for other callers/tests); `_allocate_servers`
   always passes the platform default (`saas_master
   .default_compute_driver`, see below), **falling back to driver-blind
   allocation when literally no server of that type exists** — an
   operator with only Docker Compose hosts must not have every deploy
   fail over an unmet preference.
3. `_validate_deploy_fields`/`_allocate_servers`' DB-server step both
   unconditionally required SSH keys and a separate `db_server_id` —
   meaningless for Kubernetes (auth is via the region's kubeconfig; the
   database lives in-cluster via CloudNativePG). Both now skip entirely
   for a `kubernetes`-backed instance.

The fresh-create path itself (`_do_deploy_locked_kubernetes`) needs none
of the legacy SSH/mkdir/`_provision_postgresql`/manual `-i base` init
sequence — confirmed live that `compute/operator`'s `reconcileInitJob`
already runs `-i base --without-demo=all` and gates the web Deployment on
it, so a bare `driver.create()` with no `restore` key is a complete,
self-initializing tenant. **Live-verified end-to-end** against this
machine's real microk8s: `driver.create()` with no restore key reached CR
phase `Ready` and served a real `HTTP 200` on `/web/login` — the one
genuinely new, previously-unexercised code path this step relies on
(every prior live run always went through `migrate_to_kubernetes`'s
`restore` key).

**[Live-verified]** The customer-facing compute is a **multi-tier model**
(`saas.compute.tier` — Standard=1 replica/free, HA=2/priced, Scale=4/
priced, extensible by adding records, no code change), not a binary HA
flag. `saas.instance.compute_tier_id` points at the instance's current
tier; changing it patches the Kubernetes CR's replica count
(`KubernetesDriver.scale()`, a `spec.replicas` patch). An upgrade (more
replicas) is gated behind a prorated activation invoice, same shape as
the Daily Backup add-on; a downgrade or a free tier applies immediately
via a background job, no invoice. **Deliberately decoupled from which
backend an instance runs on**: a platform-level setting
(`saas_master.default_compute_driver`, Settings > SaaS > Compute Backend,
default `kubernetes`) decides Docker Compose vs. Kubernetes for new
instances; tiers are only ever offered on an instance already on
Kubernetes.

Three things confirmed live, none obvious from the CRD/code alone:
1. `spec.replicas > 1` requires the filestore's PVC to be `ReadWriteMany`
   — on THIS single-node microk8s cluster (`microk8s-hostpath` provisioner,
   otherwise RWO-only), an RWX PVC actually bound and a genuine 2-replica
   instance reached `Ready` and served real traffic when created fresh
   with the tier already selected.
2. The operator already solves the "Odoo cron workers are singletons"
   problem a naive multi-replica Deployment would hit: cron runs in its
   own dedicated single-replica Deployment, separate from the scalable web
   replicas (confirmed by inspecting the live pod list: `odoo-cron-*`
   alongside 2× `odoo-*` web pods) — nothing extra needed on the control-
   plane side for this.
3. **A real race condition, caught only by testing the full flow against
   a live cluster, not by unit tests**: scaling an *already-running*
   instance whose filestore was provisioned RWO (e.g. the earlier
   migration-verification instance, created before compute tiers existed)
   correctly goes `Degraded` — the operator's own message is exact:
   `"spec.replicas=2 requires spec.storage.filestore.accessMode=
   ReadWriteMany; ReadWriteOnce (the default) only safely supports a
   single replica"`. But `driver.scale()` patching `spec.replicas` does
   **not** synchronously update `status.phase` — a health check in the
   same instant can still read the pre-patch `Ready` phase before the
   operator's reconciler has run, so `_do_scale_compute_tier`'s post-scale
   health gate could exit successfully on that stale reading and commit
   `compute_tier_id` to a tier that was actually rejected. Fixed with a
   short settle delay before the first health check; re-verified live
   against the same broken-storage instance afterward — it now correctly
   detects the `Degraded` condition and rolls back the replica count
   without committing the tier change.

**[Code-confirmed]** Compute tiers have a real customer-facing surface,
not just a backend model — mirrors the existing Daily Backup add-on's UI
shape: a tier-picker card on the instance's own portal page
(`frontend/veltnex/src/pages/portal/InstanceDetail.tsx`, hidden entirely
on a Docker-Compose-backed instance, showing every active tier with its
replica count/price/description and Upgrade/Downgrade buttons), a JSON
API route (`/saas/api/v1/instances/<id>/compute-tier/change`,
`saas_website/controllers/api.py`), a QWeb checkout page for upgrades
(`portal_compute_tier_checkout`), and a help-topic entry. An admin manages
the tier catalog at SaaS > Configuration > Compute Tiers
(`saas.compute.tier`, seeded with Standard/HA/Scale,
`data/saas_compute_tier_data.xml`) — adding a new tier (e.g. an 8-replica
one later) is a data record, not a code change. Full backend suite 589
tests, frontend suite 19 tests, both green.

**Non-goals, explicitly not built this pass** (all confirmed with the
person requesting this feature, after several rounds of narrowing scope):
automated horizontal autoscaling beyond the fixed replica tiers — the
CRD's own `spec.autoscaling` is explicitly inert in this API version
("reserved for future use... validated but rejected", per its own Go
doc-comment) and would need real operator/Go work, not an Odoo-side flag;
reverse migration (Kubernetes → Docker Compose) when a tier is
downgraded; code-redeploy (`_do_redeploy`) for a Kubernetes-backed
instance; automating the existing cutover primitive into a cohort/
percentage rollout (it remains a manual, admin-invoked tool for moving an
*existing* tenant between backends, entirely independent of compute tiers
above); the multi-region seam and CNPG live-validation bullets below; and
the legacy decommission plan (Phase 6).

**[Known gap]** No PITR/WAL archiving anywhere — no `archive_command`,
`wal-g`, or `pgbackrest` in the codebase. Real recovery-point objective is
bounded by backup-cron frequency (daily), not continuous, regardless of what
any older doc claimed.

**[Code-confirmed]** Today only **two Odoo addons exist in this entire
platform**: `saas_core` (everything — provisioning, billing, security,
observability infra, all 31 models) and `saas_website` (portal/API, depends
on `saas_core`). §4.6 evaluates whether that single-addon shape is still
the right one and proposes a concrete redesign; it is a **proposal, not yet
started** — nothing in this section changes until Phase 10 (§5) picks it up.

### 3.2 Frontend (`frontend/veltnex`)

React/Vite/TypeScript SPA, talks only to `/saas/api/v1/*`. Deliberately has
zero infrastructure awareness, which is exactly why it was unaffected by the
compute-layer work to date. Idle-timeout auto-logout shipped this cycle
(`useIdleLogout.ts`, SEC-018).

### 3.3 Compute layer (`compute/operator`) — Kubernetes

**[Live-verified]** A single `controller-runtime` controller reconciling one
CRD, `OdooInstance` (cluster-scoped), via pure desired-state builders +
Server-Side Apply — no first-time-vs-update branching, fully idempotent by
construction. Reconciler responsibilities, in order: namespace/ServiceAccount/
ResourceQuota/LimitRange → database (pluggable: `Managed` single-StatefulSet,
`CloudNativePG` for real HA/PITR — **not live-tested against a real CNPG
install**, or `External`) → filestore PVC → admin Secret → `odoo.conf`
ConfigMap → gated init/restore Job → Service + web Deployment (+ a separate
single-replica cron Deployment when `replicas > 1`, to avoid double-firing
cron) → PodDisruptionBudget → NetworkPolicy/TLS/HTTPRoute-or-Ingress →
backup CronJob → status/conditions.

**[Code-confirmed]** Multi-tenancy is namespace-per-tenant with
`pod-security.kubernetes.io/enforce: restricted`, default-deny
NetworkPolicy (cross-tenant traffic impossible by construction), and
least-privilege RBAC (no wildcards). **Caveat**: deleting a tenant's
namespace deletes its PVCs, so object-storage backup destinations (not
PVC-based ones) are required for backup data to survive tenant deletion.

**[Known gap]** `spec.autoscaling{enabled,minReplicas,maxReplicas,
targetCPUUtilizationPercentage}` exists on the CRD but is explicitly
**rejected** by the controller today (`AutoscalingNotImplemented` validation
error) — no HPA object is ever created. This is reserved API surface, not a
working feature.

**[Known gap]** No log aggregation of any kind — application logs stay on
the pod's own stdout, deliberately never proxied into the controller's own
logs, and nothing ships them anywhere durable or searchable.

**[Code-confirmed]** The operator exposes its own Prometheus metrics
(`odoo_instance_reconciles_total`, `_reconcile_errors_total{reason}`,
`_reconcile_duration_seconds`, `odoo_instances_by_phase{phase}`) via the
standard controller-runtime `/metrics` endpoint, with an optional
(disabled-by-default) `ServiceMonitor` Helm template. **This describes the
controller's own health, not any tenant's application** — it is real,
working Prometheus wiring, but it is platform-team observability, not the
customer-facing capability this roadmap's Phase 3 builds. It is, however,
the right precedent to extend (see §4.3).

### 3.4 Legacy Docker/SSH path

Still carries **100% of production traffic**. Shares one read-only Odoo
source tree per version across all tenants on a host (near-zero
per-instance disk). Has a region model (`saas.region`) with per-region price
multipliers and host co-location enforcement, so multi-region *pricing and
placement* already exist here even though the Kubernetes path doesn't yet
have a multi-region story (§5 Phase 2 notes this explicitly).

### 3.5 CI/CD

`.github/workflows/ci.yml`: `spa` (typecheck+build), `odoo-tests` (full
Odoo suite, currently 533 tests, 0 failures as of the last run this cycle),
`csrf-lint` (AST-based static check that any `csrf=False` route is safe),
`secret-scan` (gitleaks), `compute` (operator build+vet+test), `image-scan`
(trivy against operator + backup-tool images, `--ignore-unfixed`). **[Known
gap]** No IaC/GitOps — no Terraform/Ansible/Pulumi anywhere in the repo;
infra is still hand-provisioned.

### 3.6 Security posture

A full pass through the previously-tracked SEC-00x findings closed the
easy/high-value ones this cycle. Status as of 2026-09-15:

| ID | Finding | Status |
|---|---|---|
| SEC-001 | Debug OTP leaked to client | **Fixed** |
| SEC-002 | Plaintext secrets (DB/admin passwords, git tokens, SSH keys) | **Fixed** (`EncryptedChar`/`EncryptedBinary`) |
| SEC-003 | Tenant containers ran as root | **Improved** — image ends `USER odoo`; capability-drop/seccomp posture **not separately verified** |
| SEC-004 | Tenant PG role has `CREATEDB` | **Open by decision** — needed by the template-clone flow; scoped as a documented tradeoff (two forward paths recorded), not silently left broken |
| SEC-005 | Host SSH terminal gated by an over-broad role | **Fixed** (`group_saas_host_shell` split out, grandfather migration for existing managers) |
| SEC-006 | `chmod 777` + cleartext `db_password` in `odoo.conf` on host | **Partially fixed** — permissions tightened to 700 across 9 call sites; the config file itself still renders the password in cleartext on the host filesystem |
| SEC-007 | No image scanning / supply-chain hygiene | **Fixed** (trivy CI job; found and fixed a real stale-CVE binary) |
| SEC-008 | 7-day presigned backup download URLs | **Fixed** (15 minutes) |
| SEC-009 | No telemetry/Prometheus/Sentry/alerting | **Partially addressed** (`saas_alert.py` internal ops webhook exists) — **the customer-facing half is the headline gap this roadmap's Phase 3 exists to close** |
| SEC-010 | No append-only audit log for deploy/redeploy/restore/scale | **Fixed** |
| SEC-011 | No webhook rate limiting | **Open** — HMAC signature verification is the real auth gate today, so this is a resource-abuse risk, not an auth bypass |
| SEC-012 | Container log-stream endpoint authorization | **Unverified** — flag for a dedicated pass |
| SEC-013 | OTP brute-force resistance | **Improved** (6 attempts / 600s); hashed-storage/3-min-validity recommendations not confirmed |
| SEC-014 | SSH command construction / injection risk | **Fixed** — dedicated audit found no exploitable injection; fixed 2 latent env-var-quoting footguns |
| SEC-015 | ~85 broad `except Exception:` blocks in the god model | **Open**, structural — tracked with §3.9 |
| SEC-016 | Coarse RBAC (one `group_saas_manager` for everything) | **Partially addressed** (host-shell split out); no support/billing/infra/read-only tier separation yet |
| SEC-017 | Restore-confirmation UX | **Not addressed** |
| SEC-018 | No SPA idle-timeout | **Fixed** (`useIdleLogout`, 30 min + 1 min warning) |
| SEC-019 | CSRF-route safety not enforced | **Fixed** (AST lint added to CI; caught and fixed a real gap in a prior audit's own claim) |

### 3.7 Scalability — known bottlenecks (the direct input to Phase 4)

These are concurrency-correctness bugs on shared infrastructure, found by a
dedicated scalability audit and re-surfaced here because they are exactly
the class of bug that turns one tenant's incident into every tenant's
incident:

- **SCALE-001/002/003**: the shared nginx reverse proxy has no per-proxy
  lock around vhost-write + reload; a deploy's port-lock scope wrongly
  spans the nginx-reload + certbot step (60-120s held); config writes are
  non-atomic.
- **SCALE-004**: the homegrown `saas.instance.metric` table has no
  partitioning and prunes via a single raw `DELETE` — at fleet scale this is
  tens of millions of rows with no size observability. **This is precisely
  the problem a real TSDB (Prometheus) replaces** — see §4.3.
- **SCALE-005**: server-capacity cache read/invalidate has a race that lets
  concurrent allocators overcommit a host past its configured CPU/RAM caps.
- **SCALE-006**: port-pool exhaustion is a hard failure with no fallback
  host or queueing.
- **SCALE-007**: the live-metrics sampler holds **one global advisory lock,
  cluster-wide** — a hard, non-shardable throughput ceiling; the audit's own
  recommendation was "shard by host, or push metrics to a TSDB from host
  agents," i.e. this finding independently points at the same Prometheus
  direction this roadmap's Phase 3 takes.
- **SCALE-008**: the template-DB build lock is keyed per-instance instead of
  per-version/region, and is in-process (doesn't coordinate across
  workers/hosts).
- **SCALE-009/010/011** (lower severity): region-less deployments collapse
  onto one global allocation lock; the DB-op reaper does unbatched per-row
  writes; sampler dedup state is in-process and lost on worker restart.

### 3.8 Provisioning reliability

- **PROV-001**: the background DB-operation thread has no fail-fast
  try/except — a crashed thread leaves the op record `running` for up to 30
  minutes with no heartbeat-based fast failure (customer sees an indefinite
  spinner).
- **PROV-002**: a partial create leaves the subdomain permanently reserved
  (an unconditional unique index), with no rollback/rename-on-failure path.
- **PROV-003**: the overcommit fallback trusts stale cached health with no
  graceful `pending_provision` degradation on probe failure.
- **PROV-004**: pending-provision retry is time-based, not capacity-aware —
  up to 24h before a hard failure, with no customer notification.
- **PROV-005/006** (lower severity): subdomains aren't reclaimed after
  cancellation; SSH timeouts use one global constant for both trivial and
  heavy operations.
- The container-crash reconciler **[Code-confirmed]** correctly
  distinguishes `not_found` (auto-recreate, then escalate) from a
  crash-loop, closing an earlier-identified gap.

### 3.9 Tenant isolation — the strongest area, one real gap

Ground truth: customers are `base.group_portal`; the portal ACL only grants
read on `saas.instance`/`backup`/`folder` — everything else has **no portal
ACL row at all**, so the API's own `sudo()` + `partner_id` ownership checks
are the real (and sufficient) gate. An automated pass's "critical
cross-tenant leak" claims were checked and disproven. The one real,
medium-severity issue: **three different ownership keys are used
inconsistently** (`partner_id` exact-match in portal rules vs. `company_id`
internally vs. `commercial_partner_id` in API checks) — this fails *closed*
(safe) but means **multi-contact customer organizations cannot share access
to their own instances today**, which blocks any future "teams" feature
(§3.11) until unified.

### 3.10 Billing

No revenue-critical bugs found. Known precision/transparency issues: a
portal-display-vs-backend proration mismatch of about 2 days; an ambiguous
discounted-yearly floor policy; wallet cap computed pre-tax; wallet-lock
timing. None are exploits.

### 3.11 Missing SaaS-standard features (parity gap vs. Odoo.sh/Heroku/Render/Railway/Fly.io)

**[Code-confirmed absent]**: customer-facing teams/collaborator RBAC,
self-service API keys, configurable customer-facing alerts, self-service
custom domains + automatic SSL (today's `saas.based.domain` is
platform-level base-domain config, not a customer feature), a
customer-facing audit log/activity feed, deploy rollback, an in-place
version-upgrade wizard, and a public status page.

### 3.12 UX gaps

No in-SPA password reset (drops to a raw Odoo page); a session-expiry state
mis-rendered as "instance not found" instead of a re-auth prompt; the
drag-to-merge flagship feature is undiscoverable; production merges have no
danger-styling or second confirmation step (real accidental-deploy risk); no
billing transparency (next charge date, at-risk badges, payment method on
invoice); a misleading empty-backups state; no account security surface
(2FA, session management, sign-in history); mobile/accessibility gaps.

### 3.13 Business/legal/operational gaps

Not a code problem, but a genuine go-live blocker list, essentially
untouched: DR readiness with defined RTO/RPO targets and drill cadence;
GDPR/data-retention/right-to-erasure handling; PCI/tax compliance; ToS/AUP
and an abuse-handling process; email deliverability (SPF/DKIM/DMARC); a
patch/CVE response cadence; on-call rotation, a public status page, and any
SLA commitment; backup encryption/immutability with periodic restore
drills (a real, not tabletop, restore drill).

### 3.14 The god model

`saas_instance.py` is 12,619 lines today (57% of `saas_core`'s 22,043 total
model-layer lines) — larger than when first flagged, not smaller, despite
this cycle's routing work. Every prior planning document agreed it should
be decomposed behind tests, peeled one concern at a time, never big-bang.
That has still not happened. It is not urgent in the sense of blocking a
specific feature, but it is the single largest ongoing tax on every other
change to this codebase and should get opportunistic, continuous attention
(§5, Phase 10) rather than a dedicated big-bang phase. **§4.6 turns this
from a vague "decompose it eventually" note into an actual target
architecture** — a concrete proposal for which Odoo addons should exist,
who owns what, and a migration order — written in response to an explicit
request to evaluate `saas_core`'s modular structure from first principles,
not assume today's single-addon shape is correct.

---

## 4. Target architecture

### 4.1 Compute layer end state

Kubernetes becomes the only compute backend; the legacy SSH/Docker fleet is
decommissioned after every tenant is migrated (§5 Phase 2, §5 Phase 6). The
`ComputeDriver` abstraction already makes this a swap, not a rewrite, at the
control-plane call-site level — the remaining work is provisioning-flow
integration (actually calling `create()`/`destroy()` for real tenants),
migration tooling (move an existing tenant's data onto a fresh
`OdooInstance`), and the operational confidence to flip the default.

### 4.2 Multi-region

The legacy path already prices and places by region (`saas.region`,
per-region multiplier, forced co-location of a tenant's servers). The
Kubernetes path has no multi-region story yet — the operator manages one
cluster. Per the "seam now, infra on demand" principle carried forward from
this project's earliest architecture doc (see ADR in §8): **do not** stand
up a second cluster or build cross-cluster federation speculatively. Do
make sure the `saas.region` → target-cluster mapping is a clean seam (a
region record should be able to name which cluster/kubeconfig serves it —
`saas.region.kubeconfig` already exists as an `EncryptedChar` field for
exactly this) so that adding a second cluster later is additive, not a
redesign.

### 4.3 Observability: Prometheus + Grafana + Loki

This is the platform capability this document exists to plan. There is
**no partial implementation to build on** beyond the operator's own
internal metrics and its `ServiceMonitor` template — which does, however,
establish the right integration pattern (Prometheus Operator CRDs) to
extend. Design:

**Metrics (Prometheus).**
- Deploy `kube-prometheus-stack` (Prometheus Operator + Prometheus + node-exporter
  + kube-state-metrics + Alertmanager) as a platform-level, not per-tenant,
  install — one Prometheus per cluster, not one per tenant.
- Every tenant pod already runs with explicit `resources.requests/limits`
  (the CRD requires it, no defaults) — cAdvisor/kube-state-metrics
  already gives per-pod CPU/RAM/network/disk-IO series labeled by
  namespace out of the box once the stack is installed. This alone
  replaces the homegrown `saas.instance.metric` sampler's CPU/RAM/storage
  columns (§3.7 SCALE-004/007) with a real TSDB, no custom exporter needed
  for infra-level metrics.
- For **Odoo-application-level** metrics (request latency, worker
  saturation, cron queue depth, DB connection pool usage) add a small
  Prometheus exporter to the Odoo image (an `/metrics` endpoint, e.g. via
  `prometheus_client` in a WSGI middleware, or a sidecar scraping Odoo's
  existing `/web/health` and internal counters) and a per-tenant
  `PodMonitor` (namespace-scoped, created by the operator alongside every
  `OdooInstance` — same reconcile-and-apply pattern already used for every
  other owned resource).
- **Tenant label discipline is mandatory from the first metric emitted**:
  every series must carry a `tenant` (or `namespace`, since it's already
  1:1 with tenant) label. This was principle P6 in the platform's original
  architecture spec and is exactly what makes the isolation model in the
  next bullet possible.

**Logs (Loki).**
- Deploy Loki + a log-shipping agent (Grafana Alloy or Promtail) as a
  DaemonSet, shipping every pod's stdout, labeled by namespace/tenant,
  same as metrics.
- This replaces the SSH-based `docker logs` live-tail feature (and its
  open SEC-012 authorization question) with a real, retained, searchable
  log store — but only for tenants on the Kubernetes path; the legacy
  path's log-stream endpoint stays as-is until cutover completes.

**Tenant isolation for metrics/logs — the hard part.**
Raw Prometheus/Loki/Grafana APIs must **never** be exposed directly to a
customer — PromQL/LogQL have no native per-tenant row-level security.
Two viable approaches, in order of recommendation:
1. **A thin, tenant-scoping reverse proxy inside the control plane** (or a
   small dedicated service): the portal SPA calls
   `/saas/api/v1/instances/<id>/metrics`, the control plane resolves that
   to the caller's own tenant/namespace (reusing the exact ownership check
   already used for every other portal endpoint), and forwards a
   label-constrained PromQL/LogQL query to Prometheus/Loki server-side.
   The customer never talks to Prometheus/Loki directly. This is the
   safer default and reuses existing auth code.
2. **Grafana with per-tenant provisioned dashboards + a data-source proxy
   that injects the tenant label filter**, embedded in the portal via a
   scoped, short-lived signed URL. More native "real Grafana dashboard"
   feel, but a materially larger security surface (Grafana's own
   authn/authz, dashboard-JSON injection risk, session handling) — treat
   as a Phase 3b enhancement once (1) is live and trusted, not the
   starting point.
- Either way, build and adversarially test the isolation boundary
  (attempt cross-tenant label injection, query-string tampering, and
  namespace enumeration) **before** exposing it to any real customer —
  this is a new, high-consequence attack surface the platform has never
  had before.

**Retention.** Short local retention in Prometheus (e.g. 15 days) is
sufficient for the "how's my instance doing right now / this week"
customer use case this closes; do not add a long-term remote-write tier
(Thanos/Mimir/VictoriaMetrics) until real demand or a compliance
requirement calls for it (postpone-aggressively principle again). Loki
retention should carry a per-tenant volume budget from day one — this
platform already has one incident-shaped precedent (the old metrics
sampler) of an unbounded per-tenant table becoming a fleet-wide problem;
don't repeat it with logs.

**Alerting.** Alertmanager for platform-team alerts (already has a home —
extend the existing `saas_alert.py` webhook as one more receiver, don't
build a second notification system). Customer-facing configurable alerts
(§3.11) are a distinct, later feature — don't conflate "we have
Alertmanager" with "customers can set their own alert thresholds."

**Sequencing dependency**: this phase needs real tenant pods to scrape,
which means it is materially cheaper and more valuable *after* Phase 2's
Kubernetes cutover has moved meaningful tenant volume over, not built
against the legacy Docker fleet (which would need its own
cAdvisor/node-exporter equivalent bolted on, then thrown away at cutover).

### 4.4 Autoscaling

Turn the CRD's already-reserved `spec.autoscaling` fields into a real HPA,
sourcing custom metrics from the Prometheus stack above via the
`prometheus-adapter` (or the Kubernetes custom-metrics API). This is
naturally sequenced after §4.3, since it needs the same metrics pipeline.

### 4.5 Data layer target

Complete `DataService.materialize()` for cross-instance targets (the real
"clone" primitive several features depend on) and `neutralize()` (secure
decommission). Add PITR/WAL archiving so recovery-point objective is
measured in minutes, not "since last daily backup." Add backup
encryption-at-rest verification and a recurring (quarterly, at minimum)
real restore drill — not a tabletop exercise — feeding into the DR/RPO/RTO
targets called out in §3.13.

### 4.6 `saas_core` module boundaries — a domain-driven redesign

This section answers a direct question: **should `saas_core` be split into
multiple Odoo addons along domain lines (e.g. a dedicated Billing module,
a Subscriptions module), or is today's single-addon shape actually
correct?** The answer below is not "reorganize the files" — it's a
specific target architecture, a specific migration mechanism, and an
explicit accounting of what should and shouldn't be split, including one
case (Billing vs. Subscriptions) where the conclusion is "not yet, and
here's exactly why."

#### 4.6.1 The one insight that shapes everything else

Odoo's `_inherit` mechanism lets a **different addon** contribute fields
and methods to an **existing model** without creating a new table — Odoo
merges every addon's `_inherit` classes for a given `_name` into one
Python class and one Postgres table at registry build time. This means
decomposing the god model does **not** require splitting `saas.instance`
into multiple tables/models (a genuinely risky, high-effort schema
migration on live customer data, and the kind of over-engineering this
evaluation was explicitly asked to avoid). It means moving **code** —
whole chunks of methods and their supporting fields — into a different
addon's Python files, while `saas.instance` itself stays one table, one
`_name`, one aggregate root. The redesign below is a "thin core, fat
inherited behavior" pattern: `saas_core` keeps a deliberately thin
`saas.instance` (identity, ownership, state, physical placement) and every
other addon *extends* it with only the fields/methods its own domain owns.

**This is a modular monolith, not microservices** — worth stating
explicitly since the rest of this roadmap (§4.1) is simultaneously moving
the *compute* layer to a genuinely separate service reached over the
Kubernetes API. The `saas_core` split proposed here is a different kind of
boundary: every addon below still shares one Python process, one Postgres
database, and one ORM registry. Modules "communicate" by calling public
methods directly on the same in-memory recordset — there is no queue, no
network hop, no serialization between them, and there shouldn't be one.
Introducing service boundaries *inside* the control plane, on top of the
real service boundary already being built for compute (§4.1), would be
the over-engineering this evaluation was asked to avoid. The payoff of
splitting into addons is entirely about **code organization, ownership,
and blast radius** (smaller, independently reviewable diffs; a
Python-import-level guarantee that billing code can't reach into
provisioning internals it doesn't depend on; addon-scoped tests/security
groups/data files) — not runtime isolation, which this design doesn't
attempt and doesn't need.

#### 4.6.2 Current shape (input to this design, gathered this revision)

**[Code-confirmed]** `saas_core` today: 31 models, 22,043 model-layer
lines. `saas_instance.py` alone is 12,619 lines / ~284 methods, breaking
down roughly as: deploy/redeploy/scale lifecycle (~3,500 lines, the
largest single cluster), billing/invoicing/plan-change/proration/renewal
(~2,800 lines, split across **two non-contiguous clusters** in the file),
backup/restore (~1,600 lines here plus all 2,122 lines of the already-
separate `saas_instance_backup.py`), health/reconciliation/metrics
(~1,400 lines), hosting self-service DB operations (~1,950 **contiguous**
lines — already the best-bounded section in the file), git-based deploy
hooks (~10 methods, bulk of the logic already in the separate 1,661-line
`saas_instance_repo.py`), compute-driver/SSH plumbing (~600 lines), and a
genuinely cross-cutting remainder (ORM overrides, a portal status
aggregator, secret re-encryption) that touches nearly every domain at once
and is exactly why this file resists a clean split today.

Only two addons exist: `saas_core` (depends on `base, mail, sale, account,
portal, phone_validation, payment, account_payment`) and `saas_website`
(depends on `saas_core, website, portal, payment, account_payment`).
Several of `saas_core`'s current Odoo-core dependencies — `sale, account,
payment, account_payment` — exist **only** because of billing code
(invoice/payment handling); core identity and provisioning need none of
them. That's a concrete, measurable sign the current boundary is wrong:
`saas_core`'s dependency footprint is wider than its actual core
responsibility.

**The existing addon boundary is already leaky** — important negative
finding, not assumed away: `saas_website` doesn't only call
`saas.instance`'s public `action_*`/`hosting_*` methods, it also reaches
directly into at least nine underscore-prefixed "private" methods
(`_get_status_dict`, `_get_cancellable_unpaid_invoice`, `_env_branch`,
`_get_all_invoices`, `_capacity_summary`, and others). With only one addon
boundary in the whole platform, that boundary is already being bypassed by
convention alone. **Any new split must ship an enforced contract, not just
a naming convention** — see §4.6.6.

Two grab-bag Odoo-core extensions confirm the same pattern at smaller
scale: `res_config_settings.py` (492 lines, ~45 fields) is roughly 30
billing/pricing fields plus infra settings (backup credentials, port
range) plus generic settings, all in one file; `res_partner.py` (205
lines) mixes wallet/trial-eligibility fields (billing) with phone/country
validation (generic identity) in one file. Odoo's own idiom already solves
this — each addon should extend `res.config.settings`/`res.partner` with
*only* its own fields, in its own file, and Odoo merges them into one
settings screen / one partner record at runtime. §4.6.4 applies this
explicitly.

#### 4.6.3 Proposed addon layout

| Addon | Depends on | Owns |
|---|---|---|
| **`saas_core`** (thinned, redefined) | `base`, `mail`, `portal` | Kernel infra + the tenant aggregate root's *identity* shape only. |
| **`saas_provisioning`** (new) | `saas_core` | Everything about *how* a tenant instance runs and is operated on. |
| **`saas_billing`** (new) | `saas_provisioning` | Everything about the *commercial* relationship — plans' pricing, wallet, payments, invoicing, subscription state. |
| **`saas_website`** (existing, unchanged role) | `saas_billing` (was `saas_core`) | Portal/API — unchanged responsibility, dependency updated since it needs both provisioning actions and billing data. |

Dependency direction is a straight line — `saas_core → saas_provisioning →
saas_billing → saas_website` — deliberately, so there is no cycle to
reason about. `compute/` (the Kubernetes operator) sits outside this
chain entirely, reached only via the Kubernetes API from inside
`saas_provisioning`, exactly as today.

**What stays in `saas_core` and why.** Two categories:

1. *The aggregate root's thin identity shape*: `saas.instance` keeps only
   what every other addon needs and none of them should own —
   subdomain/name, `partner_id`, `state` (draft/running/suspended/
   cancelled — read and written by both provisioning transitions *and*
   billing's non-payment suspension, so it can't live in either), and
   physical-placement FKs (`docker_server_id`, `region_id`,
   `odoo_version_id`, `domain_id`, `environment`). `saas.server`,
   `saas.region`, `saas.odoo.version`, `saas.based.domain`, and
   `saas.docker.container` (per-host container/capacity inventory,
   tightly coupled to `saas.server`) stay with it — they're the things
   `saas.instance`'s placement fields point at, and moving them to a
   higher-dependency addon while `saas.instance` stays in `saas_core`
   would be circular.
2. *Domain-agnostic kernel infrastructure*: `saas.job` (the durable queue),
   `saas.audit.log`, `saas.alert`, `saas.rate.limit`, the
   `EncryptedChar`/`EncryptedBinary` field types, base security groups
   (`group_saas_manager`, `group_saas_host_shell`), `saas.ssh.key.pair`,
   `saas.terminal.session`. **[Code-confirmed]** none of these five
   reference `saas.instance`, `saas.plan`, or any other business model in
   their own code — they genuinely don't know what a "tenant" or a "plan"
   is. **This is deliberately not its own addon.** Splitting purely generic
   infrastructure out would not reduce coupling — every other addon would
   still depend on it either way — and it doesn't correspond to a
   business domain a team would want to own separately from the rest of
   the platform kernel. A fourth addon here would be pure module-count
   inflation with no payoff: exactly the over-engineering this evaluation
   was asked to avoid.

#### 4.6.4 What moves to `saas_provisioning`

Everything about *operating* a tenant instance, added to `saas.instance`
via `_inherit` (same table, relocated code) plus wholesale-relocated
satellite models:

- **Via `_inherit = 'saas.instance'`**: deploy/redeploy/scale (~3,500
  lines), backup/restore orchestration (~1,600 lines), hosting
  self-service DB operations (~1,950 already-contiguous lines — the
  cheapest single win in this whole plan, see §4.6.7), git-deploy hooks,
  reconciliation/health/metrics-sampling crons, addon/pip-package
  management, and the compute-driver/SSH plumbing helpers.
- **Relocated outright** (new tables stay new, addon ownership changes):
  `saas.instance.backup`, `saas.instance.db.operation`,
  `saas.instance.container` (the currently-empty scale-out seam — moves
  as-is, still inert, still a seam not an active feature),
  `saas.docker.container` stays in `saas_core` (see above) but
  `saas.instance.repo`, `saas.build`, and `saas.instance.package` (the
  per-instance pip-package list — **[Code-confirmed]** a hosting/deploy
  concern, not billing, despite the name suggesting a purchasable add-on)
  move here.
- **The `ComputeDriver` abstraction, both driver implementations, and
  `DataService`** (`saas_core/drivers/`, `saas_core/dataservice/`) move
  from `saas_core` to `saas_provisioning`. They're *behavior* — how to
  talk to a compute backend — not part of the aggregate root's identity;
  `saas_core`'s thin `saas.instance` doesn't need to know how deployment
  happens, only `saas_provisioning`'s inherited methods do.
- **Owns the *resource-limit* shape of `saas.plan`** (`cpu_limit`,
  `ram_limit`, `storage_limit`, `workers`) — the fields provisioning
  actually reads at deploy/resize time. This is the key move that keeps
  the dependency chain acyclic; see §4.6.5.
- **Its own `res.config.settings` extension**, adding only the fields it
  owns (backup-provider credentials, port-allocation range) rather than
  sharing the current 45-field grab-bag file.

#### 4.6.5 What moves to `saas_billing`

Everything about the *commercial* relationship, depending on
`saas_provisioning` (never the reverse):

- **Relocated outright**: `saas.pricing` (the pricing engine), `saas.wallet`,
  `saas.payment`, `saas.product`, `saas.support.plan`, and
  **[Code-confirmed]** `saas.addon` (the sellable commercial add-on
  catalog — e.g. "Daily Snapshots" — confirmed genuinely a billing
  concern, not deploy-time addon management, which is
  `saas.instance.package` above and stays with provisioning).
- **Via `_inherit = 'account.move'`**: invoice-cancel → wallet-credit
  refund, payment-state computation, payment-reversal → suspension
  triggering — this addon's `account.move` code calls `saas.instance`'s
  (provisioning-defined) suspend/resume actions, which is fine: billing
  depends on provisioning, so it can call any public method provisioning
  exposes.
- **Via `_inherit = 'saas.instance'`**: `plan_id`, `billing_period`,
  `next_invoice_date`, and the renewal/plan-change/proration methods
  (`_generate_renewal_invoice`, `_apply_pending_plan_change`,
  `_request_upgrade`/`_downgrade`, `_proration_credit`,
  `_wallet_settle_consumption`). **`plan_id` living in `saas_billing`
  rather than `saas_core` is deliberate** — it's what keeps the whole
  chain acyclic. If `saas.instance.plan_id` were declared in `saas_core`,
  `saas_core` would need to know about `saas.plan`, which needs to know
  about resource limits, which `saas_provisioning` needs — a cycle.
  Instead, `saas_core` never declares `plan_id` at all; `saas_billing`
  adds it via `_inherit`, same as any other addon-contributed field.
- **Via `_inherit = 'saas.plan'`**: adds the *commercial* shape — price,
  currency, proration rules, `saas_product_ids` — on top of
  `saas_provisioning`'s resource-limit shape. **This split-ownership of
  one model across two addons (provisioning owns the technical tier
  shape, billing owns the commercial tier shape layered on it) is the
  specific design move that resolves what would otherwise be a real
  circular dependency**: provisioning needs resource limits at deploy
  time and must not depend on billing; billing needs to trigger
  provisioning actions (suspend on non-payment, resize on upgrade) and
  correctly depends on it. Neither needs the other's half of `saas.plan`.
- **Via `_inherit = 'res.partner'`**: wallet balance, trial-eligibility
  fields — moved out of today's mixed `res_partner.py`, leaving a
  separate, smaller `saas_core` extension for the generic identity fields
  (phone/country validation, instance count) that don't belong to any one
  domain.
- **Its own `res.config.settings` extension**, taking the ~30
  billing/pricing fields out of the current grab-bag file.

**Why Subscriptions is *not* split out from Billing as a fifth addon —
the case the user's own example raised.** "Subscription" (what plan a
customer is on, upgrade/downgrade requests, proration) and "Billing"
(invoice generation, payment processing, wallet) are legitimately
different concerns in mature SaaS platforms, and some businesses do staff
and own them separately. But in *this* codebase, right now, they are not
separable without fighting real, present coupling: proration math,
plan-change requests, and invoice generation currently live interleaved
in the same ~2,800 lines across two non-contiguous clusters of
`saas_instance.py` — there is no existing seam to split along, and forcing
one today would mean inventing a boundary the code doesn't have yet,
purely on the theory that it might be wanted later. That is exactly the
premature-fragmentation this evaluation was asked to avoid. The right
move now is to keep them as one `saas_billing` addon but organize the
*files inside it* along that seam from day one — a `models/
saas_subscription.py` mixin for plan-state/proration/renewal, separate
from `models/saas_payment.py`/`saas_wallet.py` for money movement — so the
seam exists in code organization without paying for a second addon's
dependency-graph and migration overhead until real demand (a second team,
a genuine need to deploy/version them independently) shows up. This is
the same "seam now, infrastructure later" principle as ADR-5 in §8,
applied one level down from infrastructure to code organization.

#### 4.6.6 How modules communicate, and the contract problem

Within this chain, modules communicate by **calling public methods
directly** on `_inherit`-merged recordsets — one process, one registry, no
serialization. That only stays safe if "public" actually means something.
Today it doesn't (§4.6.2's nine-method finding). This redesign should ship
with:

- A **naming/documentation convention**: a method another addon is allowed
  to call has no leading underscore and a docstring stating which
  addon(s) call it. Underscore-prefixed methods are addon-private by
  construction — a downstream addon reaching into one is treated as a bug,
  not a shortcut.
- A **static lint in CI**, following the exact precedent already
  established this cycle for `csrf=False` routes
  (`control-plane/scripts/lint_csrf_routes.py`): an AST-based check that
  flags any addon's code calling an underscore-prefixed method defined in
  a *different* addon's model class. This turns "we agreed not to do
  that" into something CI actually enforces, the same way the CSRF lint
  did for route safety. **Implemented** as
  `control-plane/scripts/lint_addon_boundaries.py` (billing/pricing
  architecture plan, Step C) — scoped to calls through an explicit
  `self.env['model.name']` string-literal lookup (optionally chained
  with `.sudo()`/`.with_context()`), checked against each addon's
  `models/*.py` `_name =` declarations to know which addon owns which
  model. Caught and fixed 5 real violations in `saas_core` calling into
  `saas_billing`-owned `saas.wallet`/`saas.payment.*` private helpers
  (`_for_partner`, `_default_for_partner`,
  `_save_method_from_transaction`, `_charge`) — all four renamed to
  drop the leading underscore, with a docstring naming their cross-addon
  callers, since they are genuinely public contracts now.
- A specific **status-aggregation pattern** for the one method
  (`_get_status_dict`) that legitimately needs data from every domain at
  once (it's the portal's main per-instance status payload): `saas_core`
  defines a thin `_get_status_dict()` that returns the identity-level
  fields and calls a small set of extension points (e.g.
  `_status_dict_provisioning_extra()`, `_status_dict_billing_extra()`);
  `saas_provisioning` and `saas_billing` each `_inherit` just their own
  hook to merge in their own keys. The portal keeps calling one method: it
  becomes an aggregation point every domain contributes to explicitly,
  not a hole in the boundary.

#### 4.6.7 Migration strategy

This is a live production Odoo module with real customer data — moving a
model from one addon to another is **not** a plain file move. Odoo tracks
which addon owns which model/table/XML-ID via `ir.model.data` and
`ir.module.module`; moving ownership needs a migration script (Odoo's
standard `migrations/<version>/pre-` or `post-migrate.py` hook) that
re-parents the relevant `ir_model_data` rows to the new addon's name
*before* the new addon's models are loaded against the existing table —
done correctly, the table and its data are untouched, only the metadata
saying which addon "owns" it changes. Getting this wrong (e.g. letting the
new addon's `_auto_init` try to create a table that already exists, or
leaving stale `ir_model_data` pointing at the old module name) breaks
upgrades or duplicates data. This is well-trodden ground in the Odoo
ecosystem but must be treated with real caution, tested against a copy of
production data, and never attempted as a live edit against the actual
production database.

**Recommended order — extract the least-coupled pieces first, prove the
pattern, then tackle the god model itself:**

1. **`saas_billing`'s wholesale-relocated models first**: `saas.pricing`,
   `saas.wallet`, `saas.payment`, `saas.product`, `saas.support.plan`,
   `saas.addon`. These are new tables with no `_inherit` entanglement with
   `saas.instance` — the lowest-risk, highest-confidence proof that the
   migration mechanism works, before touching anything that needs
   `_inherit` re-parenting.
2. **Split the two grab-bag Odoo-core extensions** (`res_config_settings.py`,
   `res_partner.py`) along the same lines, into each addon's own file —
   small, mechanical, no schema risk (adding a field via `_inherit` from a
   different addon than before is a routine Odoo operation, not a
   migration hazard, as long as the field itself doesn't move addons after
   already existing — these do move, so this step does need the
   `ir.model.data` re-parenting treatment, just at field/view granularity
   rather than whole-model).
3. **`saas_provisioning`'s hosting self-service DB operations** (the
   ~1,950 already-contiguous lines) as the first `_inherit` extraction
   from `saas_instance.py` itself — chosen deliberately because it's
   already the best-bounded section in the file, i.e. the cheapest way to
   validate the `_inherit`-based god-model decomposition pattern before
   applying it to the messier, non-contiguous clusters.
4. **The rest of `saas_provisioning`'s `_inherit` extraction** (deploy/
   redeploy/scale, backup/restore, reconciliation/metrics, git-deploy
   hooks, compute-driver/`DataService` relocation) — the largest and
   riskiest step, done last and incrementally, one cohesive cluster per
   change, full characterization-test coverage before each move, exactly
   the discipline already used for the `ComputeDriver` routing work this
   cycle (§3.1).
5. **`saas_billing`'s `_inherit` extraction from `saas.instance`/
   `saas.plan`** (the two non-contiguous billing clusters, plus the
   `saas.plan` shape split described in §4.6.5) — last, because it's the
   most entangled with both the god model and step 4's provisioning
   extraction landing first.
6. Ship the naming convention + CI lint (§4.6.6) **before or alongside**
   step 3, not after — enforcing the contract from the first extraction
   means the next four steps can't quietly reintroduce the same kind of
   boundary leak `saas_website` already has.

**This is a continuation of Phase 10 (§5), not a new competing
initiative** — do not start this migration until `saas_instance.py`'s line
count has demonstrably stopped growing (Phase 10's own existing
acceptance signal). Splitting a file that's still actively accreting new
lines faster than it's decomposed is fighting the tide; the growth needs
to stop first.

**Acceptance criteria for this redesign** (once Phase 10 picks it up):
each extraction step lands as its own reviewable change with the full
test suite green before and after; the CI lint from §4.6.6 has zero
findings against the final four-addon layout; `saas_core`'s own manifest
dependency list has shrunk to `base, mail, portal` (no more `sale,
account, payment, account_payment` — those move to `saas_billing`'s
manifest, a concrete, checkable signal the boundary is real and not just
aspirational); and `saas_instance.py` itself is reduced to the thin
identity shape described in §4.6.3 with no business-domain method bodies
left in it.

---

## 5. Phased roadmap

Phases are ordered by dependency, not strictly by calendar priority — read
the "Priority" column alongside the ordering. **P0** = blocks everything
downstream or is a live security/reliability risk; **P1** = high-value,
should start once its dependencies clear; **P2** = valuable, can wait;
**P3** = opportunistic/backlog.

### Phase 0 — Foundation (repo hygiene, security, CI) — **DONE**

Repo hygiene, dependency audit, dead-code pass, and the SEC-00x closure
pass in §3.6 are complete except the items explicitly marked open there.
CI (`spa`/`odoo-tests`/`csrf-lint`/`secret-scan`/`compute`/`image-scan`) is
live. **Acceptance criteria met**: full test suite green, no critical/high
open findings from §3.6 except SEC-004 (open by documented decision) and
SEC-006/009/011/012/015/016/017 (each independently tracked below, not
blocking).

### Phase 1 — Compute microservice exists — **DONE**

The Kubernetes operator, CRD, Helm charts, `ComputeDriver` abstraction, and
`KubernetesDriver` implementation all exist and are live-verified against a
real cluster (create/health/stop/start/destroy, and a full backup→restore
round trip against both PVC and ObjectStorage backends). `saas_instance.py`'s
call sites are routed through `ComputeDriver` (§3.1). **Dependencies**: none
(foundational). **Acceptance criteria met**: operator passes its own test
suite; control-plane routes container operations through the driver
interface with characterization-test coverage; live cluster round-trip
verified.

### Phase 2 — Kubernetes cutover — **IN PROGRESS. Top priority.**

**Priority: P0.** **Depends on**: Phase 1 (done).

Actually provision real tenants on Kubernetes instead of the legacy fleet.
Work items:
- Build the migration path: move an existing tenant's live data
  (database + filestore) onto a fresh `OdooInstance`, verified
  bit-for-bit or functionally equivalent, with a rollback path if the
  cutover instance fails health checks. **[Live-verified]** — done, see
  `DataService.migrate_to_kubernetes` in §3.1 for the full writeup
  (including two real findings from the live run worth reading before
  relying on this: a Postgres client version-compatibility constraint on
  tenant images, and a not-yet-crash-safe DB write during migration).
- Decide and implement the cutover mechanism: a per-tenant, health-gated,
  reversible flip. **[Test-verified, partially live-verified]** — done,
  see `saas.instance.action_cutover_to_kubernetes` in §3.1 for the full
  writeup. Remains a manual, admin-invoked tool for moving an *existing*
  tenant between backends; automating it into a canary cohort / staged
  rollout is still open.
- `compute_driver` now gates a brand-new tenant's *initial* provisioning
  too — **[Live-verified]**, see `_do_deploy_locked_kubernetes` in §3.1.
  Kubernetes is the platform default (`saas_master
  .default_compute_driver`); Docker Compose remains available as a
  simpler alternative. A separate, customer-facing, priced High
  Availability add-on (2 pod replicas vs. 1 — **[Live-verified]**, §3.1)
  is deliberately NOT a backend switch.
- Multi-region mapping seam (§4.2) — confirm `saas.region` can carry a
  target-cluster reference before the first tenant moves. **[Live-verified]**
  — `saas.region.kubeconfig` (one cluster per region) already did this; this
  session added `ingress_host`/`ingress_port` alongside it (see §3.1) for
  the same reason — where a cutover's traffic flip actually connects to.
- CloudNativePG database mode needs real-cluster validation before any
  tenant relying on HA/PITR uses it — today it's implemented but
  **[Documented, unverified]** against a live CNPG install.
- Decommission plan for the legacy fleet once cutover is complete and
  stable (see Phase 6).

**Acceptance criteria**: at least one real paying tenant fully served from
Kubernetes end-to-end (deploy, backup, restore, scale) for a defined
soak period (recommend 30 days) with no P0/P1 incidents attributable to
the new path; a documented, tested rollback procedure exists and has been
exercised at least once against a non-production tenant.

### Phase 3 — Observability: Prometheus + Grafana + Loki — **NOT STARTED**

**Priority: P0** (this is the capability this document was commissioned to
plan). **Depends on**: Phase 2 having moved enough tenant volume that
building this against real pods is worthwhile (a small pilot cohort is
enough to start — this doesn't need to wait for 100% cutover).

Work items, in build order:
1. Deploy `kube-prometheus-stack` cluster-wide; confirm per-pod CPU/RAM/
   network/disk metrics are flowing, labeled by tenant namespace, for the
   Phase-2 pilot cohort.
2. Build and ship the tenant-scoping reverse-proxy endpoint (§4.3, option
   1) in the control plane; adversarially test the isolation boundary
   before any customer-facing exposure.
3. Ship a minimal customer-facing metrics view in the portal SPA (CPU/RAM/
   storage over time, replacing the homegrown sampler's equivalent view
   for cutover tenants).
4. Deploy Loki + a log-shipping DaemonSet; extend the scoping proxy to
   cover log queries; ship a customer-facing log viewer in the portal,
   replacing the SSH-tail feature for cutover tenants.
5. Wire Alertmanager into the existing `saas_alert.py` webhook as a
   platform-ops receiver.
6. (Phase 3b, later) Evaluate embedding real Grafana dashboards (§4.3,
   option 2) once option 1 is trusted in production.

**Acceptance criteria**: a customer with an instance on the Kubernetes path
can see real CPU/RAM/storage/network graphs and searchable application
logs for their own instance, and only their own instance, in the portal;
the isolation boundary has been adversarially tested (cross-tenant query
injection, label tampering, enumeration) with no findings; platform-ops
alerting fires on real reconcile-error/resource-pressure conditions.

### Phase 4 — Scalability hardening — **NOT STARTED**

**Priority: P1** (concurrency bugs on shared infra, independent of compute
backend). **Depends on**: none — can run in parallel with Phase 2/3.

Fix, in roughly descending severity: SCALE-005 (capacity-cache
overcommit race), SCALE-001/002/003 (nginx proxy concurrency + port-lock
scope + non-atomic config writes — legacy path only, lower priority once
Phase 2 reduces legacy volume), SCALE-007 (global sampler lock — likely
moot for any tenant migrated per Phase 3, but still live for
not-yet-migrated tenants until Phase 6), SCALE-006/008 (port-pool
degradation, template-DB lock granularity), SCALE-009/010/011 (lower
severity). Turn on real HPA (§4.4) once Phase 3's metrics pipeline exists.

**Acceptance criteria**: each fixed item has a regression test
demonstrating the concurrency bug is closed (not just "code looks right");
no open Critical/High scalability finding remains for the *active* compute
backend (legacy items can be downgraded to "won't fix, path being
decommissioned" once Phase 6 completes).

### Phase 5 — Provisioning reliability & data layer — **NOT STARTED**

**Priority: P1.** **Depends on**: none, can run in parallel.

Fix PROV-001 through PROV-006 (§3.8). Complete `DataService.materialize()`
(cross-instance) and `neutralize()` (§4.5). Add PITR/WAL archiving. Add a
real, scheduled restore-drill process feeding measured RTO/RPO into §3.13's
DR targets.

**Acceptance criteria**: a crashed background provisioning thread fails
fast with a customer-visible error instead of an indefinite spinner; a
partial-create failure releases its reserved subdomain; a real, scheduled
restore drill has been run at least once with a measured RTO/RPO.

### Phase 6 — Legacy path decommission — **NOT STARTED**

**Priority: P2.** **Depends on**: Phase 2 reaching 100% tenant migration.

Retire the SSH/Docker fleet once every tenant is confirmed running on
Kubernetes for a stable soak period. Remove the `SshDockerDriver` code path
only after this, not before (keep it as the safety net during Phase 2's
staged rollout). Reconstruct or formally retire the deleted legacy runbook
content (§2, "Document hygiene") depending on whether any host provisioning
is still needed post-decommission.

**Acceptance criteria**: zero tenants on the legacy path for a defined
period (recommend 60 days) before removing `SshDockerDriver` and its
associated infrastructure.

### Phase 7 — SaaS feature parity — **NOT STARTED**

**Priority: P2.** **Depends on**: §3.9's ownership-key unification (a
prerequisite specifically for teams/collaborators; the other features in
this phase don't depend on it).

Build, roughly in order of customer value: unify the three ownership keys
(§3.9) as a prerequisite, then customer-facing teams/RBAC, self-service API
keys, self-service custom domains + automatic SSL, a customer-facing
audit/activity log, deploy rollback, an in-place version-upgrade wizard,
customer-configurable alerts (building on Phase 3's metrics pipeline), and
a public status page.

**Acceptance criteria**: each feature ships with its own portal UX and
API-level tenant-isolation tests; no feature in this phase reintroduces the
ownership-key ambiguity closed at the start of the phase.

### Phase 8 — UX and trust surface — **NOT STARTED**

**Priority: P2/P3 depending on item** — see §3.12 for the full list. In-SPA
password reset and the production-merge danger-confirmation are the two
items worth treating as P2 (real accidental-deploy and account-recovery
risk); the rest are P3 polish.

### Phase 9 — Business, legal & operational readiness — **NOT STARTED**

**Priority: P1 for a real go-live, P3 if this stays a personal/internal
project.** See §3.13 for the full list (DR/RPO/RTO, GDPR, PCI/tax, ToS/AUP,
deliverability, patch cadence, on-call/status-page/SLA, backup
encryption/immutability + drills). This is not engineering work in the
traditional sense but blocks any real commercial go-live regardless of how
complete the technical phases above are.

### Phase 10 — God-model decomposition & `saas_core` module split — **ONGOING, OPPORTUNISTIC. Concrete design exists (§4.6), not yet started.**

**Priority: P3, continuous.** Not a dedicated phase with a start/end date —
per every prior planning document's own conclusion, this should be peeled
one concern at a time, behind characterization tests, whenever a change
already touches a given area of `saas_instance.py` (the same discipline
already used for the Phase 1 `ComputeDriver` routing work). Track progress
by line count and method count at each check-in; the goal is "stops
growing, then shrinks," not a fixed target.

**§4.6 gives this phase an actual target architecture**: a redesign of
`saas_core` into a `saas_core → saas_provisioning → saas_billing →
saas_website` addon chain (thin core + kernel infra, `_inherit`-based
behavior extraction, no table splits, no new runtime service boundaries),
with a specific migration order (§4.6.7) starting from the
least-coupled, wholesale-relocatable billing models and ending with the
god model's own `_inherit` extraction. **Do not start the migration until
line-count growth has genuinely stopped** — see §4.6.7's own gating note.

**Acceptance criteria**: see §4.6.7's own criteria (manifest dependency
shrinkage, CI lint with zero findings, `saas_instance.py` reduced to its
thin identity shape) once this phase actually begins.

---

## 6. Dependency graph (summary)

```text
Phase 0 (done) ──► Phase 1 (done) ──► Phase 2 (cutover) ──► Phase 3 (observability) ──► Phase 4's HPA work
                                            │                        │
                                            ├──► Phase 6 (decommission, needs 100% cutover)
                                            │
Phase 4 (scalability) ─────────────────────┤  (mostly independent, can run anytime)
Phase 5 (reliability/data) ────────────────┤  (independent, can run anytime)
Phase 9 (business/legal) ──────────────────┘  (independent, can run anytime — but gates real go-live)

Phase 7 (feature parity) ──► needs §3.9 ownership-key fix first (small, standalone prerequisite)
Phase 8 (UX/trust) ── independent
Phase 10 (god-model) ── continuous, opportunistic, no hard dependency
```

---

## 7. Risk register

| Risk | Impact | Current state | Tracked in |
|---|---|---|---|
| Kubernetes path never gets real traffic (stays a parallel unused system) | High — the entire Phase 1 investment and this roadmap's Phase 3/4 plans depend on real tenants existing on it | Zero tenants cut over today | Phase 2 |
| No customer-facing observability | High — direct product gap vs. every competitor named in §3.11 | Confirmed absent platform-wide | Phase 3 |
| Metrics/logs isolation boundary is a new, high-consequence attack surface | High if shipped carelessly | Not yet built | Phase 3, §4.3 |
| Server-capacity race lets a host be overcommitted | Medium-high, live today on the path carrying 100% of traffic | SCALE-005, open | Phase 4 |
| No PITR — RPO is "since last daily backup," not continuous | Medium-high for any customer needing real DR guarantees | Confirmed absent | Phase 5, §4.5 |
| God-model keeps growing, raising the cost of every future change | Medium, compounding | 12,619 lines and rising | Phase 10, §4.6 |
| `saas_core` module split, if attempted, corrupts model/table ownership metadata on a live DB | High if mishandled, but avoidable — a well-understood Odoo migration pattern (§4.6.7), not attempted yet | Design only, not started | Phase 10, §4.6.7 |
| Existing single addon boundary (`saas_website` → `saas_core`) is already bypassed by convention (9 private-method call sites) | Medium — shows a naming convention alone won't hold under a real split | Confirmed, documented | §4.6.2, §4.6.6 |
| SEC-004 (tenant `CREATEDB` grant) | Medium, scoped/accepted tradeoff, not silently ignored | Documented decision, two forward paths recorded | §3.6 |
| Multi-contact orgs can't share instance access | Medium, blocks Phase 7's teams feature | ISO-002, open | §3.9, Phase 7 |
| No DR drills, no GDPR/compliance posture, no SLA | High for a real commercial launch, low if this stays internal | Untouched | Phase 9 |

---

## 8. Architecture decisions worth preserving (ADR summary)

1. **Job queue is Postgres-backed, not Redis/Celery.** Explicitly rejected
   both Celery/Redis ("new infra, another SPOF, contradicts the no-new-infra
   goal") and OCA `queue_job` (new dependency + a big-bang migration of
   ~20 call sites) in favor of a DB-backed `saas.job` table reusing the
   platform's existing heartbeat/reaper/advisory-lock patterns. The executor
   interface is intentionally left swappable if scale ever demands an
   external broker — treat that as a deliberate, deferred decision if it
   ever comes up again, not a gap.
2. **`ComputeDriver` boundary is deliberately narrow.** `DataService`
   (Postgres provisioning + backup/restore), ingress/nginx management, and
   source-fetch are kept **out** of `ComputeDriver` on purpose — the driver
   covers container lifecycle only. Don't fold them in without a fresh
   design discussion.
3. **Namespace-per-tenant, not shared-namespace, on Kubernetes.** Chosen
   specifically for NetworkPolicy/ResourceQuota isolation and one-namespace-
   delete teardown, over a shared namespace (rejected — no isolation) or
   namespaced-CRD-with-pre-created-namespace (rejected — duplicates
   operator logic).
4. **Gateway API (HTTPRoute) is the default ingress mechanism**, with
   classic Ingress as an operator-wide fallback flag — chosen to give one
   shared Gateway and O(tenants) HTTPRoutes instead of O(tenants)
   LoadBalancers.
5. **"Seam now, infrastructure on demand."** If a step only adds an
   interface/seam, do it now (cheap insurance, e.g. the `saas.region` →
   cluster mapping in §4.2). If it adds infrastructure that must be run and
   paid for, defer until a tenant's actual load demands it (e.g. a second
   Kubernetes cluster, a long-term metrics remote-write tier, multi-host
   load balancing). This principle should keep governing Phase 2 onward the
   same way it governed everything built so far.
6. **`tenant_id` (or namespace) labeling on every metric/log/cost record is
   non-negotiable from the first line of code**, not a retrofit — this is
   what makes Phase 3's isolation model possible at all. Any new
   observability code that doesn't carry this label is a bug, not a
   style choice.
7. **`saas_core`'s proposed split (§4.6) is addon-level, not service-level.**
   Every addon in the proposed `saas_core → saas_provisioning →
   saas_billing → saas_website` chain shares one process, one database,
   one ORM registry — modules communicate via direct method calls, not a
   queue or network API. This is deliberately a *different kind* of
   boundary than the control-plane/compute split (§4.1), which is a real
   service reached over the Kubernetes API. Don't conflate the two, and
   don't introduce a runtime service boundary inside the control plane
   itself without a much stronger reason than "clean architecture" —
   that would be exactly the over-engineering this design was asked to
   avoid. Also: Odoo's `_inherit` mechanism means decomposing a god model
   never requires splitting one table into several — prefer relocating
   code across addons over relocating data across tables whenever both
   would achieve the same separation of concerns.

---

## 9. Appendix — where to look for more detail

- **Compute microservice design**: `compute/docs/architecture.md` (English)
  / `architecture.ar.md` (Arabic) — CRD shape, reconciler internals, backup/
  restore mechanism, security posture, in full detail.
- **Compute quick start**: `compute/README.md` / `README.ar.md`.
- **Control-plane original architecture spec**: `control-plane/docs/architecture/architecture-spec-v1.md`
  and the as-built deltas in `control-plane/docs/architecture/AS-BUILT.md`.
- **Local development**: `control-plane/docs/LOCAL-TESTING.md` (seeded demo
  accounts, `devctl.sh` subcommands, and the important caveat that local dev
  has no real Docker/SSH hosts — provisioning is mock-only there) and
  `control-plane/docs/LOCAL-RUNTIME-SYSTEMD.md` (persistent systemd-based
  alternative to `devctl.sh`'s `nohup` dev mode).
- **Full historical audit detail** (exact file:line findings, superseded
  status trackers, the original incident report): `git log --diff-filter=D`
  on this repository — every document consolidated into this roadmap is
  still in history, just no longer maintained as a live file.
