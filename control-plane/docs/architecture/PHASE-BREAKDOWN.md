# Phase Breakdown — Granular Executable Steps

**Companion to:** `IMPLEMENTATION-PLAN.md` (the roadmap) and `architecture-spec-v1.md` (the target).
**Purpose:** every phase decomposed into the smallest safe, verifiable units of work.
**How to use:** execute top-to-bottom within a phase; respect `(needs: …)` dependencies; check the
"Done when" before moving on; tick the box and log it in `IMPLEMENTATION-PLAN.md` §10.
**Step sizing rule:** each step ≈ a few hours, one clear deliverable, independently verifiable, reversible.

> What "stronger than Odoo.sh + profitable" actually requires from this breakdown:
> (a) per-tenant margin visibility (Phase 4) so you know you're profitable;
> (b) a large-tenant scale-out tier (Phase 5) Odoo.sh charges a premium for;
> (c) clean seams (Phases 1–2) so you add capacity without rewrites.
> These three are the profit/differentiation levers — everything else is hygiene.

---

## START SMALL, SCALE WITHOUT A REWRITE — what to build NOW vs LATER

The principle: **a seam is cheap, an implementation is expensive.** Build every seam now so scaling is
additive; defer every heavy implementation until real demand. This keeps day-one cost near the
~$100/mo / one-server profile while guaranteeing no Control-Plane rewrite later.

### ✅ DO NOW (cheap, enables scale, prevents future rewrites)
- **Phase 0** — safety net (so we can change anything fast and safely).
- **Phase 1** — ComputeDriver + DataService seams. *This is the #1 anti-rewrite investment.* Cheap to
  build, it's what lets Kubernetes/scale-out/upgrades be additions instead of rewrites.
- **Phase 2** — object-storage filestore + immutable registry images. Makes compute disposable; cheap to
  run on one server; prerequisite for everything multi-host.
- **Phase 4** — per-tenant margin dashboard. Cheap, and it's how you know you're *profitable* (your goal).
- **Phase 3 (light)** — a minimal reconciler so a dead container self-heals.

> Minimal "start small" footprint = ONE server, single-container tenants, shared PostgreSQL,
> object-storage filestore, registry images, the seams, and the margin dashboard. Cheap to run,
> fully scale-ready.

### 🟡 SEAM NOW, IMPLEMENTATION LATER (design the boundary, don't build the heavy part yet)
- **Phase 5 model only** — when convenient, avoid hardcoding "one container per instance" (allow the
  `container_ids`/`role` concept to exist). But do NOT build LB / PgBouncer / read replica / dedicated PG
  until a real large tenant needs it.
- **Phase 6 interface only** — the `ComputeDriver` interface from Phase 1 already IS the K8s seam. Do not
  implement `KubernetesDriver` until single-server bin-packing actually hurts.

### 🔴 DEFER UNTIL DEMAND (do not build now — this is where money/time gets wasted)
- **Phase 5 scale-out implementation** (multi-host LB, read replica, dedicated tuned PG, PgBouncer) →
  triggered by the first large tenant, not by anticipation.
- **Phase 6 KubernetesDriver** → triggered by running out of single-server headroom.
- **Phase 7 upgrade automation** → triggered by the first real version-migration need.

**Rule of thumb:** if a step adds a *seam/interface*, it's probably DO-NOW (cheap insurance). If it adds
*infrastructure you must run and pay for*, it's probably DEFER until a tenant's load demands it.

---

## PHASE 0 — Audit & Safety Net

### 0.1 Get a verification environment running
- [ ] **0.1.1** Inventory the machine: confirm presence/versions of Odoo 18, PostgreSQL, Docker, and
      python deps (paramiko, jinja2, boto3, google-cloud-storage). *Done when:* a written list of
      what's installed vs missing.
- [ ] **0.1.2** Install whatever is missing (or document the gap). *Done when:* all deps resolvable.
- [ ] **0.1.3** Create a local Odoo 18 dev config (odoo.conf, addons_path includes `custom/saas`).
      *Done when:* `odoo-bin` starts without import errors.
- [ ] **0.1.4** Create a fresh dev DB and install `saas_core` + `saas_website`. *Done when:* both modules
      install clean; backend loads.
- [ ] **0.1.5** Build the SPA (`cd veltnex && npm ci && npm run build`) and confirm it serves.
      *Done when:* the VELTNEX homepage renders locally.
- [ ] **0.1.6** Write `scripts/dev-up.md` documenting exact start/stop/rebuild steps. *Done when:* a
      second person could reproduce the env from the doc.

### 0.2 Map the as-built system (output: `docs/as-built/*.md`)
- [ ] **0.2.1** Read `SESSION_NOTES.md`, `docker/SERVER-SETUP.md`, `docker/README.md`; write a 1-page summary.
- [ ] **0.2.2** Trace the **provisioning flow** end to end (`action_deploy → _do_deploy →
      _provision_postgresql → _render_and_write_configs → container up → webhooks`) as a numbered sequence.
- [ ] **0.2.3** Trace the **backup / restore / clone** flow (restic + `_pg_clone_db` + `_restore_snapshot`).
- [ ] **0.2.4** Trace the **billing/wallet/pricing** flow + list every cron from `__manifest__` data files.
- [ ] **0.2.5** Trace **environments** (create/merge/delete, parent_id billing rules).
- [ ] **0.2.6** Catalog **every Docker/SSH call site** with `file:line` (grep paramiko/docker/ssh). This is
      the Phase-1 driver boundary. *Done when:* a complete table exists.
- [ ] **0.2.7** Bucket the **248 god-model methods** into concerns (provisioning · PG ops · environments ·
      billing · usage · deploy/build · crons). *Done when:* a method→concern table exists.
- [ ] **0.2.8** **Decide:** is deploy source-clone (mutable) or image-based? *Done when:* answer recorded;
      it sets Phase-2 scope.

### 0.3 Build the safety net (tests we refactor behind)
- [ ] **0.3.1** Stand up the Odoo test harness for `saas_core` (`TransactionCase`, tagged tests, runner cmd).
- [ ] **0.3.2** Characterization test: provisioning with the driver/SSH **mocked** — assert the side effects
      (DB created, configs rendered, ports assigned). *Done when:* test passes on dev.
- [ ] **0.3.3** Characterization test: **backup → destroy → restore** round-trip against a disposable DB;
      assert data identical. *Done when:* green and repeatable.
- [ ] **0.3.4** `scripts/test.sh` one-command runner. *Done when:* runs all saas tests locally.

**Phase 0 acceptance:** dev env reproducible + as-built docs written + a green provision and
backup/restore test. Only then do we touch the god-model.

---

## PHASE 1 — Seams: ComputeDriver + DataService  *(needs: Phase 0)*

### 1.1 Define the ComputeDriver contract
- [ ] **1.1.1** Create `saas_core/drivers/base.py` with abstract `ComputeDriver`: `create / destroy /
      start / stop / restart / exec / logs / endpoint / health` — signatures + docstrings, no impl.
- [ ] **1.1.2** Define a plain descriptor object for a container/env handle (server, name, ports, db).
      *Done when:* importable, type-checked, no Odoo coupling in the interface.

### 1.2 Implement SshDockerDriver (the ONE impl)
- [ ] **1.2.1** Create `drivers/ssh_docker_driver.py` skeleton implementing the interface (raises NotImpl).
- [ ] **1.2.2** Move the SSH connection helper (`_ensure_can_ssh` / paramiko setup) into the driver.
- [ ] **1.2.3** Move container lifecycle (`action_stop`/start/restart/create/destroy) Docker calls in.
- [ ] **1.2.4** Move `exec` + `logs` (container_logs / ssh_terminal paths) behind driver methods.
- [ ] **1.2.5** Move stats/health sampling (`_sample_live_metrics_for_host`, docker stats) into
      `health()`/`endpoint()`. *Done when:* driver fully implements the interface.

### 1.3 Wire the model to the driver (incremental, test each)
- [ ] **1.3.1** Add `instance._driver()` factory (selects SshDockerDriver by server type).
- [ ] **1.3.2** Replace direct call sites in `saas_instance.py` with `self._driver().X()` — **one method per
      commit**, run 0.3 tests after each.
- [ ] **1.3.3** Remove now-unused direct paramiko/docker imports from `saas_instance.py`.
- [ ] **1.3.4** Full test pass; confirm zero behavior change. *Done when:* no Docker/SSH ref outside the driver.

### 1.4 Consolidate DataService
- [ ] **1.4.1** Create `saas_core/dataservice/service.py` with `snapshot(instance)` + `materialize(snapshot,
      target, neutralize=False)` signatures.
- [ ] **1.4.2** Wrap existing restic backup logic into `snapshot()` (delegate, don't rewrite).
- [ ] **1.4.3** Wrap `_restore_snapshot` into `materialize()`.
- [ ] **1.4.4** Implement `clone = snapshot + materialize(new identity)`; route `_pg_clone_db` through it.
- [ ] **1.4.5** Implement `neutralize` (disable outbound mail + crons) for non-prod materializations.
- [ ] **1.4.6** Parity tests: backup/restore/clone via DataService match pre-refactor behavior. *Done when:* green.

**Phase 1 acceptance:** business logic calls `ComputeDriver` + `DataService` only; a future
`KubernetesDriver` is a new file, not a rewrite; all Phase-0 tests still pass.

---

## PHASE 2 — Storage to spec: object-storage filestore + container registry  *(needs: Phase 1)*

### 2.1 Object-storage-first filestore (JuiceFS)
- [x] **2.1.1** MinIO (dev) standing up via repo-tracked idempotent script `saas_core/docker/provision-object-storage.sh`;
      bucket `saas-filestore`, creds in `/etc/saas/object-storage.env`. (prod = R2/B2, same JuiceFS steps.)
- [x] **2.1.2** JuiceFS volume `saasfs` formatted (metadata in PostgreSQL `juicefs_meta`, data in MinIO), mounted at
      `/mnt/jfs` via systemd (`jfs-mount.service`, local NVMe cache). Verified on 165.245.245.196: POSIX round-trip
      (md5 match) + 2 MB write confirmed landing as MinIO objects (`mc du` = 2.0 MiB / 7 objects).
- [x] **2.1.3** Server capability `saas.server.object_filestore_mount` (e.g. `/mnt/jfs`) → instances on that
      host bind-mount `<mount>/<partner>/<sub>/filestore` at the container's `/var/lib/odoo/filestore`
      (conditional compose volume + provisioning mkdir/chown). Verified on rt2: container shows the bind-mount,
      a real `ir.attachment` round-trips through Odoo (read-back OK) and its block lands as a new MinIO object
      (7→8); rt2 web UI still serves 200 (assets regenerate). 73/73 unit tests (+3).
- [x] **2.1.4** `DataService.migrate_filestore_to_object_store(instance, recreate=)` — copies the local
      filestore onto the object mount (`cp -a`, chown to container uid). `recreate=True` = full safe flow
      (pre-copy → stop → final copy → re-render compose → recreate); `recreate=False` = back-fill a tenant
      already bind-mounted. Verified on rt2: caught via a **Selenium** UI test that asset bundles 500'd after
      a bare reconfigure (empty object fs), then the migration back-filled them → **0 severe console errors**,
      UI fully renders. NB: NEW tenants need no migration (empty fs regenerates fresh).
- [x] **2.1.5** Benchmarked on the test box (8×2 MB reads): JuiceFS **warm 4–5 ms/file ≈ local-disk warm
      4 ms/file** (no regression for hot attachments ✅); cold (MinIO fetch) ~30 ms/file vs 13 ms local —
      a one-time miss penalty that warms away. User-facing: rt2's 474 KB filestore-backed asset bundle
      serves in ~0.12–0.15 s over HTTPS. Caveat: cold reads in prod (DO Spaces, not loopback) will be
      higher, but the local NVMe cache keeps hot files at local speed.
- [x] **2.1.6** `_hosting_filestore_path` is object-store-aware; `_hosting_clone_filestore` uses
      **`juicefs clone` (copy-on-write)** instead of `cp -a` on an object-store host. Verified on rt2:
      created a 2nd DB (`rt2_cow`) — all 10 attachment files byte-identical to the template, DB serves, and
      **MinIO usage stayed flat (20 MiB / 40 objects)** ⇒ zero data duplication; drop cleaned the object
      filestore. NB: the CoW skips Odoo's `checklist/` GC hard-links (rebuilt by Odoo — no data loss).

### 2.2 Container registry + immutable images
- [x] **2.2.1** `provision-registry.sh` — self-hosted `registry:2`, localhost-bound (the box has an exposure
      history), bcrypt basic auth, persistent volume. Verified: login + push/pull round-trip + catalog API.
- [x] **2.2.2** `Dockerfile.odoo-base` + `build-base-image.sh` bake the Odoo source into `odoo-base:<ver>`
      at `/opt/odoo` (so the hardcoded `addons_path` works unchanged + tenant images need no host mount).
      Built/pushed `odoo-base:18.0` on the box. ONE-TIME per version (tenant builds are `FROM odoo-base`).
      NB: a `.dockerignore` excludes `.git` (193 MB) — the COPY then takes ~42 s, not minutes.
- [x] **2.2.3** `templates/Dockerfile.tenant.jinja` + `_render_tenant_dockerfile()` — `FROM odoo-base` then
      pip layer (cached before) then custom-modules layer. `saas.server.registry_host` +
      `_tenant_base_image()` select the ref; `saas.build.image_ref`/`image_digest` record the result.
- [x] **2.2.4** `_build_and_push_tenant_image()` — assembles a content-hashed build context (Dockerfile +
      requirements + addons), builds `tenant-<sub>:<sha>`, pushes, records `saas.build` (image_ref/digest).
      Verified on rt2: built+pushed in **6.8 s** (FROM cached base + minimal layers). Custom modules bake to
      `/opt/tenant-addons` (non-VOLUME path) + `addons_path` re-rooted there in immutable mode.
      **Custom-addons gap CLOSED (live):** placed a real module + a cloned repo on rt2 → built the immutable
      image → deployed by digest → the module is baked at `/opt/tenant-addons/probemods/saas_addon_probe`
      (no mount), `addons_path` includes it, and Odoo `update_list()` finds it
      (`state=uninstalled, 'SAAS Addon Probe'`); rt2 served 200, then reverted clean.
- [x] **2.2.5** `provision-build-sandbox.sh` + `_image_build_cmd()` build untrusted tenant images on an
      **egress-restricted** `saas-build` network (legacy builder + `--network`). DOCKER-USER (FORWARD) +
      INPUT firewall rules confine RUN steps: **cloud metadata (169.254.169.254) and internal PostgreSQL
      (172.17.0.1:5432) are blocked**, while PyPI + DNS work. Verified on the box: build→metadata/PG time
      out, build→PyPI = 200, a real `docker build` with `pip install` succeeds, normal tenants unaffected.
      No platform creds in the build context; push is a separate trusted step. (Further hardening — fully
      rootless buildkit / ephemeral worker — remains a follow-up, but the untrusted-code-reaches-secrets
      threat is now mitigated + proven.)
- [x] **2.2.6** `deploy_immutable_image()` + `saas.instance.deploy_image` + compose immutable mode (image by
      digest; no source/addons/pip mounts). Verified on rt2: ran `tenant-rt2@sha256:f12c…`, healthy, HTTP 200
      local+HTTPS, **Selenium 0 console errors**, no source mount.
- [x] **2.2.7** `rollback_image(build)` redeploys a prior successful build's image (repoint `deploy_image` +
      recreate, no rebuild). Verified the revert path on rt2 (immutable → legacy → healthy 200); image→image
      rollback uses the same `deploy_immutable_image` path (validation unit-tested).

**Phase 2 acceptance:** a tenant runs from a registry image by SHA with filestore in object storage;
destroying its host and recreating elsewhere loses no data; rollback to a prior SHA works.

---

## PHASE 3 — Reconciliation Engine  *(needs: Phase 1; better after Phase 2)*

- [x] **3.1.1** `desired_state` (running/stopped/ignore) computed from the lifecycle `state`; the target image
      is `saas.instance.deploy_image` (Phase 2.2 digest) — no separate `target_image_sha` needed.
- [x] **3.1.2** `actual_state` (+ `last_reconcile`) set from `driver.health()` each pass.
- [x] **3.2.1** `reconcile(connection=)`: diffs desired vs actual → the single minimal action (start a
      down/missing container, break a crash-loop → park stopped, stop one that should be stopped).
- [x] **3.2.2** Idempotent + crash-safe: one action per pass, safe to re-run; skips instances mid-operation
      (`pending_operation`).
- [x] **3.2.3** `_cron_reconcile()` — one loop over all provisioned tenants, grouped by server for batched
      SSH; wired to the 5-min cron (the old health-check now delegates to it).
- [~] **3.2.4** Folded `_cron_check_container_health` into the reconciler. The provisioning-recovery crons
      (`_cron_retry_pending_provision` / `_cron_recover_stuck_provisioning`) drive the *provisioning* state
      machine (pending/stuck), a different concern — left as-is (not container-reconcile duplicates).
- [~] **3.2.5** Health gate: deploy already waits on `driver.wait_until_running` before completing; the
      reconciler's `start()` is re-checked next pass. (An explicit pre-traffic gate on every redeploy = follow-up.)
- [x] **3.2.6** Verified on rt2: `docker rm -f` the container → one `reconcile()` recreated it →
      healthy + HTTP 200 (local + HTTPS). 95/95 unit (+7 in test_reconcile.py).

**Phase 3 acceptance — MET:** killing a container is auto-corrected within one reconcile loop; a crash-loop
converges to a parked 'stopped' (with a clear error) instead of thrashing.

**Phase 3 acceptance:** reality self-heals toward desired state; a half-failed deploy converges or rolls back.

---

## PHASE 4 — Observability & per-tenant margin  *(needs: Phase 0; independent of 1–3)*

> v1 is Odoo-NATIVE (cheap, "start small"): the margin is computed from existing usage + billing data and
> shown as a backend dashboard — answers "which tenants are profitable?" without new infra. The
> VictoriaMetrics/Grafana time-series stack (4.2.x) is a later add for historical trends.

- [x] **4.1.1** `tenant_id` taxonomy = the `saas.instance` record itself (every metric/cost/revenue is
      computed per instance; child envs roll up to their Production parent).
- [ ] **4.2.1** Deploy VictoriaMetrics + Grafana (single-node). *(deferred — time-series trends; the
      Odoo-native dashboard already answers the profitability question.)*
- [ ] **4.2.2** Export container stats tagged `tenant_id`. *(deferred with 4.2.1)*
- [ ] **4.2.3** Export per-DB size + `pg_stat_statements` per tenant. *(deferred with 4.2.1)*
- [x] **4.3.1** Cost model = per-server rate card (`cost_per_cpu_month` / `_gb_ram_month` / `_gb_storage_month`
      on `saas.server`) × the tenant's provisioned CPU/RAM + used storage. `_instance_infra_cost()`.
- [x] **4.3.2** Revenue = `_instance_monthly_revenue()` — plan price (period-normalized) + flat support;
      child envs contribute 0 (they bill via the parent).
- [x] **4.3.3** Margin dashboard: `monthly_cost`/`monthly_revenue`/`monthly_margin`/`margin_pct`/
      `is_profitable` computed fields + a **Tenant Margins** list+pivot (Billing menu), sorted worst-first,
      red=loss/green=healthy. Live on rt1/rt2: rev $20 − cost $7 = **$13/mo (65%)**. 87/87 unit (+4).
- [x] **4.3.4** Alert cron `_cron_flag_unprofitable_tenants` (weekly) — logs + chatter-posts on Production
      tenants running at a loss (margin < 0). Storage-near-limit already covered by
      `_cron_check_storage_limits`; runaway CPU/RAM lives in the live-metrics sampler.

**Phase 4 (Odoo-native) acceptance — MET:** the Tenant Margins dashboard answers "which tenants are
profitable?" from live data, and an alert proactively flags losses. The VictoriaMetrics/Grafana time-series
stack (4.2.x) stays deferred per "start small" until historical trends are actually needed.

**Phase 4 acceptance:** one dashboard answers "which tenants are profitable?" from live data.

---

## PHASE 5 — Tiered PostgreSQL + large-tenant scale-out tier  *(needs: Phase 2; uses Phase 1)*

### 5.1 Multi-container instance model
- [x] **5.1.1** SEAM ONLY: `saas.instance.container` (instance_id + `role` app/cron/longpoll + name) +
      `container_ids` One2many; `_workloads()` / `_compute_handles()` enumerate a tenant's workloads —
      falling back to the implicit single 'app' container so the existing path is byte-unchanged. Empty for
      normal tenants; a scale-out instance populates explicit rows → scale-out is additive (no rewrite).
      104/104 unit (+2). **Heavy parts below stay deferred until a real large tenant.**
- [ ] **5.1.2** Refactor provisioning so a scale-out instance creates a SET of containers via the driver.
- [ ] **5.1.3** Ensure exactly ONE cron node runs `ir.cron` (others `--max-cron-threads=0`).
- [ ] **5.1.4** Dedicated gevent/longpolling node for websockets/bus.

### 5.2 Load balancer + sessions
- [ ] **5.2.1** Put HAProxy/nginx in front of the app containers (replace `upstream=127.0.0.1`).
- [ ] **5.2.2** Sticky sessions first; design a shared session store as the follow-up.

### 5.3 PostgreSQL tiers
- [ ] **5.3.1** Model PG tier as an operational attribute (decoupled from billing plan).
- [ ] **5.3.2** Provision a dedicated, tuned PG for the Business tier.
- [ ] **5.3.3** Add PgBouncer (transaction pooling) in front.
- [ ] **5.3.4** Add a streaming read replica.
- [ ] **5.3.5** Route heavy reports/exports to the replica (config + app guardrails).

### 5.4 Promotion
- [ ] **5.4.1** Define size/load triggers (DB size, p99 query, IOPS) that flag a tenant for promotion.
- [ ] **5.4.2** Implement `DataService.migrate(tenant, to_tier)` (dump mode first; logical replication later).
- [ ] **5.4.3** Test: one tenant served by ≥2 app containers across hosts + read replica, no impact on others.

**Phase 5 acceptance:** a single large tenant scales out (app + reads) with zero effect on neighbors.
**Ceiling to communicate:** one Odoo DB can't be sharded — writes bound by one PG primary; beyond the
biggest box the levers are app-side (indexing, partitioning, archiving).

---

## PHASE 6 — Placement Service + KubernetesDriver  *(future; needs: Phase 1)*
- [~] **6.1** Placement v1 = `saas.server.compute_driver` selects the backend; `_compute_driver()` is the
      one-line switch. (A richer Placement that also decides PG tier remains a follow-up.)
- [x] **6.2** `drivers/kubernetes_driver.py` — a SECOND `ComputeDriver` (kubectl over the server's SSH:
      Deployment+Service per tenant, scale 0/1 = stop/start, delete = destroy, rollout restart, exec/logs,
      pod-phase health with the Phase-4 `tenant_id` label). **Seam proven:** `_do_stop()` on a K8s server
      issues `kubectl scale --replicas=0` through UNCHANGED business logic; `_compute_driver()` returns
      Kubernetes vs SshDocker purely by server type. NOT run against a live cluster (no demand) — command/
      manifest building unit-tested (102/102, +7). Pilot-on-real-cluster awaits Phase-5 demand.
- [ ] **6.3** Keep tenant PostgreSQL on dedicated/managed hosts — never on K8s ephemeral storage. *(policy
      for the pilot)*
- **Acceptance — STRUCTURALLY MET:** a tenant runs under either driver with **zero Control-Plane change**
      (adding K8s was a new file + a one-line driver switch). Running the pilot is a deploy-time step.

## PHASE 7 — Upgrade Flow (clone-upgrade-promote)  *(future; needs: Phase 1)*
- [ ] **7.1** Odoo version migration as a DataService op: clone → migrate (OpenUpgrade/official) → validate on
      staging URL → promote via cutover; keep original as rollback.
- [ ] **7.2** Reuse the same discipline for tier/server moves and major PG engine upgrades.
- **Acceptance:** a tenant is upgraded with a tested rollback and no in-place mutation.

---

## CROSS-CUTTING — God-model decomposition (every phase)
Each phase extracts its concern out of `saas_instance.py` behind the Phase-0 tests:
- [ ] Phase 1 removes provisioning/PG/lifecycle internals → driver + DataService.
- [ ] Phase 3 removes recovery crons → reconciler.
- [ ] Phase 5 removes single-container assumptions → multi-container model.
- **End-state:** `saas_instance` is a thin aggregate; concerns live in focused units. Never big-bang.

---

## Execution conventions
- One step = one commit (or a tight set); run `scripts/test.sh` before moving on.
- Update `IMPLEMENTATION-PLAN.md` §10 progress log after each step.
- If a step uncovers a surprise, add a sub-step rather than widening the current one.
- Production/staging changes only after the user reviews (even though there are no live customers yet).
