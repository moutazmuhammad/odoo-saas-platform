# Microservices Migration Plan (Kubernetes Compute Layer)

> Status: **active plan**, supersedes `architecture-spec-v1.md` §1.4/§7 and
> `PHASE-BREAKDOWN.md` Phase 6 for the compute layer only. Everything else in
> those documents (Control Plane = Odoo, DataService seam, job-queue design,
> object storage/registry) stays as-is and is reused, not replaced.
>
> Written for a fresh Claude Code session to pick up cold: every phase has an
> ID, concrete file references, acceptance criteria, and a rollback note.
> Update **§12 Progress Log** after every completed step — that's the one
> section a future session should read first to know where things stand.

---

## 0. Purpose & how this relates to the existing docs

The existing architecture-evolution effort (`docs/architecture/*`) deliberately
chose to keep the compute layer **in-process** inside the Odoo monolith: a
`ComputeDriver` Python interface (`saas_core/drivers/base.py`), one real
implementation (`SshDockerDriver`, over the existing SSH transport), and a
**stub** second implementation (`KubernetesDriver`, `saas_core/drivers/
kubernetes_driver.py`) that drives `kubectl` over SSH with hand-templated YAML
strings — explicitly "NOT YET run against a live cluster" (its own header
comment).

Separately, we built a genuinely production-quality Kubernetes operator for
Odoo — now living in this same repo at `compute/operator/`: an
`OdooInstance` CRD + controller with Managed/CloudNativePG/External database
modes, filestore PVCs, backup CronJobs, a restore-from-backup Job (mirroring
backup, gated the same way `odoo-init` gates the web Deployment), Gateway-API/
Ingress routing, NetworkPolicy isolation, hardened non-root pod security, and
an envtest + live-microk8s-verified test suite. Two real bugs were found and
fixed by actually running it (a Service selector that could route traffic to
the wrong pod; a NetworkPolicy that only ever allowed Gateway-API topology).

**Decision (confirmed with the user 2026-09-13): the Compute microservice is
this operator**, not the `kubectl`-over-SSH stub. The Kubernetes API server
becomes the real network-API boundary between the Control Plane (Odoo) and
the Compute Service (the operator) — which is exactly the "communicate via
APIs" requirement, achieved by finishing a seam that already exists rather
than inventing a new one.

> ⚠️ **Current integration status: NONE — read this before touching Phase
> 1/2.** The operator and the Control Plane have never been connected, in
> any form, by anyone, at any point. This is true even though both now live
> in one repo — physical proximity is not integration. Specifically:
> - `saas_core/drivers/kubernetes_driver.py` (the existing stub) does **not**
>   know the `OdooInstance` CRD exists. It renders its own raw
>   Deployment+Service YAML (`render_manifest()`) and has never been run
>   against a live cluster at all (its own header comment already said so
>   before this plan existed).
> - The operator (`compute/operator/`) has never received a single request
>   that originated from `saas_core`/`saas_website`/`veltnex`. Every
>   `OdooInstance` this project has ever created was applied by hand
>   (`kubectl apply -f -`) directly by a developer/Claude session, for
>   testing the operator in isolation — never through any Control Plane
>   code path.
> - No `saas.instance` record has ever caused an `OdooInstance` to be
>   created, and no `OdooInstance`'s status has ever fed back into a
>   `saas.instance` record. Zero rows, zero requests, zero wiring.
> - Phase 2 below is therefore not "swap a working backend for a better
>   one" — it is **building the first-ever connection between these two
>   systems**, from nothing. Treat every acceptance criterion in Phase 2 as
>   unproven until it is demonstrated end-to-end (Control Plane action →
>   real Kubernetes API call → real cluster state change → status flows
>   back), not as a formality on top of something that already basically
>   works.

### Why not a bigger rewrite
- The audits (`docs/reviews/*`) are clear this platform is **not
  production-ready today** (10 critical / 29 high findings) for reasons mostly
  orthogonal to microservices (auth bypass, plaintext secrets, root
  containers, no DR). A wholesale rewrite would re-introduce all of those
  risks from zero. Phase 0 below closes the ones that block everything else;
  the rest keep their existing remediation tracking in `REMEDIATION_PLAN.md`.
- Billing (`account.move`, `saas.wallet`, `saas.pricing`, payment providers)
  stays inside the Odoo Control Plane. The business audit flags billing
  correctness as already fragile (proration, dunning, trial conversion) —
  extracting it for architectural purity with no scaling benefit yet is not
  worth the revenue risk. Revisit only if/when Control Plane CPU/DB load from
  billing itself becomes a bottleneck (it is not, today).
- The job queue (`saas_job.py`, Postgres `FOR UPDATE SKIP LOCKED`) stays. It
  was a deliberate, already-implemented, already-tested rejection of Celery/
  Redis (`ARCH-004-JOB-QUEUE-DESIGN.md`) for sound reasons ("no new infra").
  Nothing about moving compute to Kubernetes requires revisiting that
  decision — the Compute Service's own async work is handled by
  controller-runtime's reconciliation loop, which is its own appropriate
  mechanism, not something the Control Plane's queue needs to reach into.

---

## 1. Target architecture

```mermaid
flowchart LR
    subgraph CP[Control Plane microservice — Odoo 18: saas_core + saas_website]
        MODELS[saas.instance / saas.plan / saas.wallet / ...]
        JOBQ[(saas.job — Postgres queue)]
        DRIVER[ComputeDriver interface]
        MODELS --> JOBQ --> DRIVER
    end

    subgraph FE[Frontend microservice]
        SPA[veltnex React SPA]
    end

    SPA -->|"/saas/api/v1/* (JSON)"| CP

    DRIVER -->|"Kubernetes API\n(create/patch/delete OdooInstance CRs,\nwatch status, pods/log, pods/exec)"| K8SAPI

    subgraph COMPUTE[Compute microservice — one per saas.region]
        K8SAPI[Kubernetes API server]
        OP[odoo-instance-operator\n(compute/operator)]
        CRD[(OdooInstance CRs)]
        TENANTS[Tenant namespaces:\nOdoo Deployment, PostgreSQL,\nfilestore PVC, backup/restore Jobs]
        K8SAPI --> OP --> CRD --> TENANTS
    end

    subgraph OBS[Observability microservice]
        PROM[Prometheus]
        GRAF[Grafana]
    end
    OP -->|/metrics| PROM --> GRAF

    subgraph STORAGE[Shared infra — already built, reused as-is]
        REG[(Image registry — Phase 2 of saas_core)]
        OBJ[(Object storage — MinIO/GCS, Phase 2 of saas_core)]
    end
    TENANTS -->|backup/restore, images| REG
    TENANTS -->|BackupDestinationSpec: ObjectStorage| OBJ
```

### Service catalog

| Service | Deployment unit | Owns | Talks to others via | Source |
|---|---|---|---|---|
| **Control Plane** | Odoo 18 (`saas_core`+`saas_website`), current VPS or later containerized | Tenant/billing/plan records, auth, job queue, public API | Kubernetes API (as a client); HTTPS to Frontend | Reused almost entirely as-is |
| **Frontend** | Static SPA (`veltnex`) served by `saas_website` | UI only | `/saas/api/v1/*` JSON | Reused as-is, zero changes required for the compute swap |
| **Compute** | Kubernetes cluster(s) running the operator, one deployment per `saas.region` | Tenant provisioning, DB, filestore, backup/restore, routing, pod security | Kubernetes API (server); exposes Prometheus `/metrics` | **Reused as-is**: `compute/operator` (this repo) |
| **Observability** | Prometheus + Grafana on each cluster | Metrics, dashboards, margin/cost data | Scrapes Compute's `/metrics` | New, near-zero code (the operator already exports the right metrics) |
| **Shared storage** | Registry + object storage (MinIO/GCS) | Immutable images, backup/restore artifacts | S3-compatible API | Reused as-is (`saas_core/docker/provision-registry.sh`, `provision-object-storage.sh`) |

"Talks to others via" above describes the **target** connection, not an
existing one — see the integration-status warning in §0: as of this
writing, Control Plane and Compute have never exchanged a single request.
"Reused as-is" in the Source column means the component's own code needs no
rewrite to serve its role, not that it is already wired to the other side.

Two services, one clear API boundary (Kubernetes API), everything else stays
where the existing project already put it. This is intentionally the
**smallest** microservices split that satisfies "communicate via APIs" — it
does not manufacture extra service boundaries (e.g. a separate billing
service, a separate job-queue broker) where the audits gave no evidence one is
needed yet.

---

## 2. Phase 0 — Security gate (do this regardless of anything else, first)

These are prerequisites, not part of the microservices work — but shipping a
new Compute Service on top of an auth-bypassed, secret-leaking platform would
be pointless. Cross-check against `REMEDIATION_PLAN.md`'s own checkboxes
first; several of these may already be closed.

- **0.1** Remove the `debug_otp` echo (`saas_website/controllers/api.py:233,250`,
  `veltnex/src/pages/.../Register.tsx:130,224`). Acceptance: OTP is never
  present in any API response or client-visible state outside of a
  server-side log gated behind an explicit dev-only config flag.
- **0.2** Confirm/finish secret encryption for DB/admin passwords, Git tokens,
  SSH private keys (`saas_instance.py:319,332`, `saas_ssh_key_pair.py:32`) —
  `EncryptedChar`/Fernet per the remediation plan's own notes. Acceptance: a
  `pg_dump` of the control DB contains no plaintext credential.
  **This becomes moot for tenant DB/admin passwords once a tenant is migrated
  to the Compute Service**, since the operator already generates and stores
  those exclusively as Kubernetes Secrets, never in the Odoo DB (see
  `internal/resources/secrets.go` in the operator) — but Git tokens and any
  remaining SSH keys for legacy (pre-migration) tenants still need this fix.
- **0.3** Confirm the compromised test box (`165.245.245.196`,
  `SECURITY-INCIDENT-2026-06-17.md`) has been rebuilt from a known-clean image
  and is not the target of any new work; do not reuse its "proven live"
  benchmarks as evidence for this plan's acceptance criteria — re-verify on a
  clean host/cluster.
- **0.4** Rotate the root SSH password that was pasted into chat
  (`SESSION_NOTES.md` "Live server" section flags this explicitly).

Acceptance for the whole phase: `docs/reviews/SECURITY_AUDIT.md`'s SEC-001/
002/003 rows can be marked closed with a one-line evidence note each.

---

## 3. Phase 1 — Stand up the Compute microservice for real use

Goal: the operator at `compute/operator` is running on a real cluster, reachable,
and able to provision a tenant end-to-end for at least one `saas.region`,
independently of the Control Plane migration in Phase 2.

- **1.1** Pick the first cluster: either the existing microk8s box used to
  develop/test the operator, or a fresh managed cluster (EKS/GKE/a
  bare-metal kubeadm cluster) for the first real `saas.region`. Install the
  operator via `compute/charts/odoo-operator` (already built), with
  `--networking-provider` matching what that cluster actually has (Ingress
  vs. Gateway API — see `docs/architecture.md` §7 in the operator repo).
- **1.2** Decide the operator-per-region topology: **one operator Deployment
  per Kubernetes cluster, one cluster per `saas.region`** (mirrors the
  existing `saas.region`/`saas.server` model, where a region already has its
  own pool of Docker hosts — a region now has its own cluster instead). Add a
  `kubeconfig` (or a ServiceAccount token + API server URL) field to
  `saas.region`, analogous to how `saas.server` holds SSH credentials today.
- **1.3** Confirm the registry + object storage already built in
  `saas_core/docker/` (Phase 2 of the existing plan) are reachable from the
  new cluster; wire the operator's `--backup-tool-image`/image repository
  references at that registry instead of `ghcr.io/...` placeholders.
- **1.4** Extend `spec.addons`/image strategy: today the CRD's `spec.addons`
  is metadata only (images are pre-built, immutable — see the operator's
  docs/architecture.md §9). `saas_core`'s existing build pipeline
  (`build-all.sh`, `provision-build-sandbox.sh`) already produces per-tenant
  images; point its output at this cluster's registry with the same
  `<version>-<sha>` tagging convention the operator's `--allow-mutable-tags`
  validation expects. This is the one place real new integration code is
  needed — everything else in this phase is configuration.
- **1.5** Provision one real test tenant on this cluster by hand (`kubectl
  apply -f` an `OdooInstance`, as we already proved live in this session) to
  confirm networking, backup, and (if relevant to this tenant) restore all
  work against production-shaped config (real domain, real TLS issuer, real
  registry).

**Acceptance:** one real `OdooInstance` reaches `Ready`, serves the Odoo login
page over its real domain/TLS, and a scheduled backup completes successfully
— all without any involvement from `saas_core`'s SSH/Docker code.

**Rollback:** this phase touches no existing tenant or Control Plane code;
rollback is "delete the test cluster/namespace."

---

## 4. Phase 2 — Real `KubernetesDriver`, behind the existing `ComputeDriver` seam

This is the actual "build the first connection" step (see §0's integration-
status warning — there is nothing working today to "swap"), and it is
deliberately scoped to **replace one file's implementation**, not the
interface, so the amount of new, unproven code is as small as possible.

- **2.1** Rewrite `saas_core/drivers/kubernetes_driver.py` to talk to the
  Kubernetes API directly (the `kubernetes` PyPI client, or a minimal REST
  client if a heavier dependency is undesirable) instead of `kubectl` over
  SSH, and to manage `OdooInstance` CRs instead of raw Deployment/Service
  YAML. Map methods 1:1 onto the existing `ComputeDriver` ABC
  (`saas_core/drivers/base.py:72-123`):

  | `ComputeDriver` method | Kubernetes API call |
  |---|---|
  | `create(spec)` | `POST` (apply) an `OdooInstance` built from `ComputeSpec` |
  | `destroy(handle)` | `DELETE` the `OdooInstance` (finalizer handles teardown — already built) |
  | `start`/`stop` | `PATCH spec.suspended` (already built — see the operator's `reconcileSuspended`) |
  | `restart` | re-apply the same spec (Server-Side Apply rolling update — already built) |
  | `exec` | Kubernetes `pods/exec` subresource against the web pod |
  | `logs` | Kubernetes `pods/log` subresource |
  | `endpoint` | read `status.url` |
  | `health` | read `status.conditions`/`status.phase` |

- **2.2** `ComputeSpec`/`ComputeHandle` (`base.py:22-51`) need new/renamed
  fields to carry a namespace and CR name instead of `server_id`/
  `instance_path`/host-port — a small, backward-compatible dataclass change
  (add fields, don't remove `SshDockerDriver`'s until it's retired in Phase
  7).
- **2.3** Finish what Phase 1 of the *original* plan only partially did: the
  production review (`PRODUCTION-REVIEW-2026-06-19.md` DOC-A2) found
  `_do_redeploy` (`saas_instance.py:5533,5553`) still issuing raw `docker
  compose down/up` directly, bypassing the driver entirely. Audit the full
  ~140-call-site catalog in `DRIVER-BOUNDARY.md` and route every one of them
  through `ComputeDriver`, not just the ~23 already routed. This was already
  identified as incomplete — this plan is what closes it.
- **2.4** Add a `saas.server.compute_driver` (or `saas.region`-level) value
  of `kubernetes`, and cut over **one non-production test tenant at a time**
  by flipping that field — never a flag day for the whole fleet.

**Acceptance:** a tenant created with `compute_driver=kubernetes` behaves
identically from the SPA/API's point of view (create/stop/start/restart/
logs/exec all work) to one on `SshDockerDriver`, verified by the existing
`tests/test_compute_driver.py`/`test_kubernetes_driver.py` suites extended to
run against a real cluster (not just unit-tested manifest strings, per the
gap the stub's own header comment already flagged).

**Rollback:** per-tenant — flip `compute_driver` back to `docker` for any
tenant that regresses; the SSH/Docker path is untouched until Phase 7.

---

## 5. Phase 3 — Backup/Restore cutover

- **3.1** For `compute_driver=kubernetes` tenants, point `saas.instance`'s
  backup fields at the operator's own `BackupCronJob`/status
  (`lastBackupTime`/`lastBackupStatus`, already on `OdooInstance.status` —
  same field names `saas_instance_backup.py` already expects) instead of the
  restic-over-SSH implementation.
- **3.2** For onboarding a customer with an **existing** backup from the old
  restic path, use the `spec.restore` field we built this session
  (`compute/operator/api/v1alpha1/odooinstance_types.go`): translate a
  `saas_instance_backup.py` restic snapshot reference into a
  `RestoreSourceSpec{Type: ObjectStorage, Bucket, Prefix, BackupID}` pointing
  at the same object-storage bucket, so historical backups remain restorable
  through the new path without a format migration. This is the direct
  reason we built the restore feature this session — it is not incidental.
- **3.3** Run both backup paths in parallel for one full retention window
  per migrated tenant before retiring the restic path for that tenant.

**Acceptance:** a restore-from-an-old-backup test (using a real restic
snapshot reference translated into `spec.restore.source`) reaches `Ready`
with the customer's actual data intact.

**Known limitation to carry forward:** the operator's restore Job currently
expects a `run-restore.sh` tool image that speaks `pg_restore`-format dumps
(see the operator's `docs/architecture.md` §16) — the restic format needs a
translation step (restic restore → plain `pg_dump`-format artifact) either in
that tool image or as a one-time conversion job. This is real, scoped work,
not yet built; track it as **3.2a**.

---

## 6. Phase 4 — Frontend / API gateway adjustment

- **4.1** `saas_website/controllers/api.py` (2,050 lines) already returns
  instance status/URL/logs fields to the SPA. Confirm each field's source
  swaps cleanly from SSH-polled data to driver-returned data (Phase 2 already
  makes this transparent at the `ComputeDriver` boundary — this step is
  verification, not new code).
- **4.2** `veltnex` needs **no changes** — it only ever calls `/saas/api/v1/
  ...`, confirmed by the code inventory (single typed client,
  `veltnex/src/lib/api.ts`, 59 endpoint references, zero direct
  infra-awareness). This is the payoff of the existing API-gateway boundary
  already being in place.
- **4.3** The in-browser SSH terminal (`ssh_terminal.py`, 835 lines,
  `@xterm/xterm` in the SPA) is the one real frontend-adjacent migration
  item: replace its SSH-into-container-shell backend with the Kubernetes
  `pods/exec` subresource (streamed the same way `logs`/`exec` above are),
  keeping the xterm.js client-side code unchanged. Re-run `SEC-005`'s access
  control (currently a broad `group_saas_manager` gate) as part of this —
  don't carry the audit finding forward unaddressed into the new transport.

**Acceptance:** the dashboard shows identical instance status/logs for a
Kubernetes-backed tenant as for a Docker-backed one; the terminal opens a
real shell into the tenant's actual web pod.

---

## 7. Phase 5 — Observability microservice

- **5.1** Deploy `kube-prometheus-stack` (or bare Prometheus + Grafana) on
  each Compute cluster. The operator already exposes
  `odoo_instance_reconciles_total`, `odoo_instance_reconcile_errors_total`,
  `odoo_instance_reconcile_duration_seconds`, `odoo_instances_by_phase` (see
  its `internal/controller/metrics.go`) and can create a `ServiceMonitor` via
  its own Helm chart (`metrics.serviceMonitor.enabled`) — this step is
  installation + dashboards, not new instrumentation code.
- **5.2** Address `BIZ-010` (no per-tenant cost attribution) using real
  `ResourceQuota`/actual CPU-memory usage per tenant namespace instead of
  Odoo-native estimates — a concrete, measurable improvement the audit says
  is currently missing.

**Acceptance:** a Grafana dashboard shows reconcile error rate, phase
distribution, and per-tenant resource consumption across the fleet.

---

## 8. Phase 6 — Multi-region scale-out

- **6.1** Each `saas.region` gets its own cluster + operator install (per
  Phase 1). `saas.region.has_capacity()` (existing model method) is extended
  to check that region's cluster `ResourceQuota` headroom instead of
  Docker-host capacity — same method signature, new implementation, same
  pattern as the `ComputeDriver` swap.
- **6.2** Placement (`_allocate_docker_server`/`_allocate_db_server`) is
  replaced, for Kubernetes-backed tenants, by "which region did the customer
  pick" (already a checkout-time decision) — the operator's own
  namespace-per-tenant model needs no further bin-packing logic per
  instance; Kubernetes' own scheduler does that within a cluster. Only
  cross-cluster/region selection is the Control Plane's job.

**Acceptance:** a second region's cluster accepts new orders once the first
is at capacity, mirroring the existing region-capacity UX with zero SPA
changes (per the same gateway-boundary argument as Phase 4).

---

## 9. Phase 7 — Decommission the legacy path

Only after a full cohort of real tenants has run successfully on the Compute
Service for at least one full billing + backup-retention cycle:

- **7.1** Delete `saas_core/drivers/ssh_docker_driver.py`, the root-container
  `Dockerfile.tenant.jinja` build path, and the restic-over-SSH backup code
  in `saas_instance_backup.py` (2,105 lines) once no tenant references them.
- **7.2** Update `architecture-spec-v1.md`, `IMPLEMENTATION-PLAN.md`,
  `PHASE-BREAKDOWN.md`, and `DRIVER-BOUNDARY.md` to reflect that the compute
  layer is now Kubernetes-only — this document (`MICROSERVICES-PLAN.md`)
  becomes historical record for how the transition happened, and those
  documents become the new as-built reference.
- **7.3** Re-run the full audit set (`docs/reviews/*`) against the
  post-migration system; SEC-003 (root containers) and ARCH-002 (no host
  agents/synchronous SSH orchestration) should both close as a direct
  consequence of this migration, not as separately-tracked remediation items.

---

## 10. Cross-cutting rules for every phase

> This whole plan is governed by the **Production-First Principle** in
> [`docs/PRODUCTION-READINESS-PLAN.md`](../../../docs/PRODUCTION-READINESS-PLAN.md)
> (repo root) — every step below (this is that master plan's Phase D) must
> satisfy that document's Definition of Done, not just the rules listed
> here. These rules are additional, phase-specific discipline on top of it.

- **One tenant, one commit at a time** for any cutover step (2.4, 3.3) —
  never a fleet-wide flag day. This mirrors the existing project's own stated
  discipline ("one step ≈ one commit; run the test suite before moving on,"
  `docs/architecture/README.md`).
- **Every phase's acceptance criteria must be demonstrated on a real
  cluster**, not just unit tests — the entire reason this plan exists is
  that the previous `KubernetesDriver` stub's acceptance was "unit-tested
  manifest strings," which the project's own docs already flag as
  insufficient ("NOT YET run against a live cluster").
- **Never remove the old path before the new one has processed a real
  backup+restore cycle for that tenant** — data safety takes priority over
  velocity, consistent with `docs/reviews/ARCH-003` (no DR readiness) being
  one of the platform's stated critical gaps.

---

## 11. Open questions to resolve before Phase 2 starts

- Exact translation format for restic → `pg_restore`-compatible dumps (item
  3.2a) — needs a decision on where that conversion runs (a one-off script,
  or a mode the restore tool image supports natively).
- Whether the Kubernetes API credentials the Control Plane needs
  (per-region kubeconfig/ServiceAccount token) are stored via the same
  `EncryptedChar` mechanism as other secrets (Phase 0.2) — almost certainly
  yes, just needs to be listed explicitly in that phase's scope once
  reached.
- Whether `saas.server` is repurposed to represent "a cluster" for
  Kubernetes-backed regions, or a new `saas.cluster` model is added
  alongside it. Recommendation: reuse `saas.server` (add
  `compute_driver='kubernetes'` + kubeconfig fields), since `saas.region`
  already has a one-to-many relationship to `saas.server` that maps cleanly
  onto "one cluster per region" — avoid a parallel model for the same
  concept.

---

## 12. Progress log

> Append one entry per completed step, in this format. Nothing is marked done
> until it has been verified against a real cluster (see §10).

```
YYYY-MM-DD — Step X.Y — <one-line result> — verified: <how> — commit: <sha>
```

(No entries yet — this plan was drafted 2026-09-13, prior to any
Phase 1 work starting.)
