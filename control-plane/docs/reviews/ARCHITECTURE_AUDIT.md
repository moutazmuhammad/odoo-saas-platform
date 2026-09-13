# ARCHITECTURE & RELIABILITY AUDIT — Odoo 18 SaaS Platform

> Pre-acquisition due-diligence. Covers architecture, coupling, multi-tenancy, failure
> isolation, DR readiness, and operational reliability. File:line verified.
>
> Severity counts (this file): **Critical 3 · High 7 · Medium 9 · Low 3** (22 findings)

---

## ARCH-001
- **Severity:** Critical
- **Component:** Domain model — `saas.instance`
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** whole file — **12,324 lines**, ~289 methods
- **Description:** A single Odoo model concentrates provisioning (SSH/Docker), billing,
  metrics, backups, repos, databases, environments, capacity, networking, and lifecycle. No
  mixins, services, or sub-models separate these concerns.
- **Business Impact:** Every change risks cross-domain regressions; onboarding new engineers
  is slow and risky; velocity drops as the platform grows. A diligence buyer must price in
  6-12 months of de-risking refactor before safe scaling.
- **Technical Impact:** Untestable in isolation, high merge-conflict surface, ORM `init()`,
  crons, and HTTP logic all coupled; impossible to reason about transaction boundaries.
- **Recommended Fix:** Decompose into bounded services/mixins (ComputeProvisioner,
  BillingEngine, BackupManager, RepoManager, MetricsCollector) with explicit interfaces;
  move orchestration behind the existing `dataservice`/driver seams.

---

## ARCH-002
- **Severity:** Critical
- **Component:** Control-plane topology
- **File Path:** `saas_core/models/saas_instance.py`; `saas_core/models/saas_instance_backup.py`
- **Line Numbers:** deploy `:5104-5260`; backup `saas_instance_backup.py:864-1050`
- **Description:** A single control-plane Odoo DB orchestrates **all** tenant operations
  (create/deploy/restore/backup) synchronously over SSH. There is no agent on the hosts — the
  master pushes every action. If the master DB is down or in maintenance, no tenant can be
  provisioned, restored, or backed up across the whole fleet.
- **Business Impact:** Platform-wide single point of failure; control-plane maintenance =
  fleet-wide provisioning outage; scaling provisioning means scaling one Postgres.
- **Technical Impact:** No separation of control plane vs data plane; DR requires restoring
  the master before any tenant recovery.
- **Recommended Fix:** Introduce stateless host agents that reconcile desired state from the
  control plane (control loop / queue), making the master a control plane rather than the
  executor; add control-plane HA (replica + failover).

---

## ARCH-003
- **Severity:** Critical
- **Component:** Disaster recovery readiness
- **File Path:** repo-wide (no DR artifacts found)
- **Line Numbers:** n/a
- **Description:** No documented RTO/RPO, no DR runbook, no automated restore test, no
  geo-replication strategy for the control DB, and the control DB is itself the secret store
  (SEC-002). Backups exist per-tenant (restic/zip per project history) but there is no
  validated full-platform recovery procedure.
- **Business Impact:** Unknown recovery guarantees; cannot offer credible enterprise SLA;
  acquirer cannot quantify continuity risk.
- **Technical Impact:** Recovery is ad-hoc and untested; first real disaster is the first test.
- **Recommended Fix:** Define RTO/RPO per component, implement control-DB replication +
  tested failover, automate periodic restore drills, and geo-replicate backup buckets.

---

## ARCH-004
- **Severity:** High
- **Component:** Async execution / job queue
- **File Path:** `saas_website/controllers/api.py`; `saas_core/models/saas_instance.py`
- **Line Numbers:** `api.py:740-859`; background helper `saas_instance.py:5072-5077`
- **Description:** Long provisioning/redeploy operations are kicked off from HTTP handlers via
  an in-process `run_in_background()` thread; the handler returns 200 immediately and thread
  failures are only logged. There is no durable job queue, no retry/backoff at the job level,
  and no first-class operation/status object the client can poll for the actual outcome.
- **Business Impact:** Customers see "success" while deploys silently fail; support cannot
  trace job state; worker restarts drop in-flight work.
- **Technical Impact:** Threads die with the worker; no idempotency keys; no dead-letter queue;
  no concurrency control beyond DB advisory locks.
- **Recommended Fix:** Adopt a durable queue (OCA `queue_job`, Celery, or a DB-backed job
  table) with retries, status, idempotency, and a client-pollable operation resource.

---

## ARCH-005
- **Severity:** High
- **Component:** Driver abstraction (Compute seam)
- **File Path:** `saas_core/drivers/base.py`, `ssh_docker_driver.py`, `kubernetes_driver.py`
- **Line Numbers:** `kubernetes_driver.py:~73` (`NotImplementedError`); SSH call sites in `saas_instance.py:5238-5246, 8392-8470`
- **Description:** The Compute driver seam is partial. The Kubernetes driver's `create()` is
  unimplemented and never run against a cluster, while business logic still issues many raw
  SSH calls (mkdir, chown, git, DB ops) outside the driver. The abstraction does not yet
  encapsulate enough to support a second backend.
- **Business Impact:** Advertised/implied Kubernetes capability is vestigial; migrating off
  single-host SSH/Docker would require rewriting ~100+ call sites — a major hidden cost.
- **Technical Impact:** Leaky abstraction; the "stable seam" is not actually stable for a
  non-Docker backend.
- **Recommended Fix:** Either complete and test the K8s driver against a real cluster or
  remove the dead code and document Docker-only; route **all** infra operations through the
  driver interface so backends are swappable.

---

## ARCH-006
- **Severity:** High
- **Component:** No Infrastructure-as-Code / GitOps
- **File Path:** repo root (no Terraform/Helm/Ansible/k8s manifests)
- **Line Numbers:** n/a
- **Description:** All infrastructure state lives in Odoo records and Jinja templates rendered
  at deploy time. Hosts, networks, registries, and buckets are configured imperatively. No
  drift detection, no reproducible environment bootstrap, no versioned infra history.
- **Business Impact:** Standing up a new region or rebuilding a lost host is manual and
  error-prone; DR and audit are weakened; "snowflake" servers.
- **Technical Impact:** No declarative source of truth for infra; changes are untracked.
- **Recommended Fix:** Introduce Terraform for cloud/host resources and a declarative agent
  config; version everything; add drift detection.

---

## ARCH-007
- **Severity:** High
- **Component:** No CI/CD pipeline
- **File Path:** repo root (no `.github/`, no GitLab CI, no Jenkinsfile)
- **Line Numbers:** n/a
- **Description:** Despite 34k LOC Python + 14k LOC TS and 15 test files, there is no
  automated CI: no test gate, no lint, no build/publish of the SPA, no security scanning. The
  SPA bundle is committed as built assets (`static/spa/assets/*`) and deploy is "git pull +
  restart" per project notes.
- **Business Impact:** Regressions reach production unguarded; the documented stale-cache /
  asset-hash gotcha is a direct symptom; release quality depends on manual discipline.
- **Technical Impact:** Tests exist but are not enforced; no reproducible SPA build provenance.
- **Recommended Fix:** Add CI running the Odoo test suite + `tsc`/lint + SPA build with
  artifact hashing; gate merges; add dependency/image scanning.

---

## ARCH-008
- **Severity:** High
- **Component:** Partial-failure cleanup (provisioning saga)
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** deploy sequence `:5104-5260`
- **Description:** Provisioning is a multi-step remote saga (mkdir → chown → role → DB → restore
  → container up). On failure mid-sequence, compensating cleanup is incomplete — DBs, roles,
  filestores, ports, and containers can be orphaned on the host, requiring manual SSH cleanup.
- **Business Impact:** Hosts accumulate garbage (disk exhaustion, port pressure), causing
  downstream provisioning failures and noisy-neighbor effects; manual ops toil.
- **Technical Impact:** No idempotent `destroy()`/compensation invoked on each failure path;
  state can diverge between control DB and host reality.
- **Recommended Fix:** Implement an idempotent teardown invoked on every failure branch;
  reconcile host vs control-plane state via the reconciliation loop (ARCH-002).

---

## ARCH-009
- **Severity:** High
- **Component:** Advisory-lock hold time
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** `:3584-3587`, `:3723-3762`, `:5114-5122`
- **Description:** Transaction-scoped Postgres advisory locks for port allocation and deploy
  are held across long-running SSH operations (deploys run minutes). A slow/hung deploy holds
  the server/instance lock and blocks all other provisioning on that server.
- **Business Impact:** One stuck deploy stalls a host's entire provisioning pipeline; under
  load this serializes throughput and looks like an outage.
- **Technical Impact:** Critical section is far larger than the actual invariant (port choice);
  no lock timeout/backoff.
- **Recommended Fix:** Hold locks only around allocation/DB-create; release before SSH;
  add `lock_timeout`, retries, and rely on the unique index to catch collisions.

---

## ARCH-010
- **Severity:** High
- **Component:** Reliability — SSH timeouts & retries
- **File Path:** `saas_core/utils.py` (SSHConnection); `saas_core/models/saas_instance.py`
- **Line Numbers:** SSH timeout consts (`SSH_COMMAND_TIMEOUT=120`, init override `600`)
- **Description:** SSH operations use coarse fixed timeouts and largely lack idempotent retry
  with backoff for transient network failures. A single transient failure fails the whole
  operation; a hung command ties up a worker thread for the timeout duration.
- **Business Impact:** A small transient-failure rate translates into many manual-intervention
  tickets at fleet scale; hung commands reduce provisioning capacity.
- **Technical Impact:** No circuit breaker to quarantine an unhealthy host; threads block.
- **Recommended Fix:** Wrap remote ops in bounded retries with exponential backoff + jitter;
  add per-command soft/hard timeouts and a host circuit breaker.

---

## ARCH-011
- **Severity:** Medium
- **Component:** Cron idempotency / partial commits
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** `:3977, 3998, 4121, 4458, 4504` (commits inside loops)
- **Description:** Several crons commit per-iteration inside loops. A mid-loop crash leaves
  earlier records committed and later ones unprocessed; a re-run can reprocess already-handled
  records if state guards are imperfect (e.g. billing/suspension transitions).
- **Business Impact:** Risk of double-applied state (double suspension, duplicated side
  effects); inconsistent batch outcomes.
- **Technical Impact:** Non-atomic batches; recovery depends on precise idempotency guards.
- **Recommended Fix:** Make each item's processing idempotent and guarded by state
  transitions; prefer per-item savepoints over per-item commits, or a job per item.

---

## ARCH-012
- **Severity:** Medium
- **Component:** Multi-tenancy — shared Postgres cluster
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** `:3244-3471` (provision + template clone)
- **Description:** Tenants share Postgres clusters (DB-per-tenant via template clone). There is
  no per-tenant resource quota (connections, disk, CPU), so one tenant can degrade co-tenants.
  Combined with tenant `CREATEDB` (SEC-004) and SQL Console access, isolation is logical only.
- **Business Impact:** Noisy-neighbor outages; one heavy tenant impacts many paying customers
  on the same cluster.
- **Technical Impact:** No cgroup/quota enforcement at the DB tier.
- **Recommended Fix:** Apply per-role connection/disk limits; consider cluster-per-tier and
  dedicated clusters for large tenants; monitor per-tenant DB load.

---

## ARCH-013
- **Severity:** Medium
- **Component:** Filestore isolation
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** filestore mount `:2814-2839`
- **Description:** Tenant filestores are path-isolated (`<mount>/<partner>/<subdomain>`) on
  shared hosts and mounted into root containers. With tenant root + shared host, path-based
  isolation is weaker than a hard boundary (object storage or per-tenant volumes with strict
  mount options).
- **Business Impact:** Elevated risk of cross-tenant file access if a container escapes or a
  mount is misconfigured.
- **Technical Impact:** Isolation depends on correct host config rather than enforced
  boundaries.
- **Recommended Fix:** Use object storage or per-tenant volumes mounted `nosuid,nodev,noexec`,
  non-root containers, and avoid broad host bind mounts.

---

## ARCH-014
- **Severity:** Medium
- **Component:** Template-DB lifecycle automation
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** template clone path `:3454-3471`
- **Description:** Fast provisioning depends on pre-built `__odoo_template_<sub>` databases,
  but template (re)build appears manual/ad-hoc. Stale or corrupt templates ship stale code to
  every new instance derived from them.
- **Business Impact:** Inconsistent instance versions; hard-to-trace "works for some new
  tenants, not others" support load.
- **Technical Impact:** No automated, versioned template build/validation pipeline.
- **Recommended Fix:** Automate template rebuilds on code change, version them, validate
  health before promotion, and record which code revision each template represents.

---

## ARCH-015
- **Severity:** Medium
- **Component:** Health/recovery race in stuck-provisioning cron
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** `:4063-4116`
- **Description:** The stuck-provisioning recovery decides to re-queue based on a point-in-time
  health probe; between probe and action the container state can change, risking interruption
  of an in-progress restore (half-applied DB/filestore).
- **Business Impact:** Rare but severe data-consistency risk during recovery.
- **Technical Impact:** Recovery decision is not derived from authoritative in-progress state.
- **Recommended Fix:** Gate recovery on a definitive "restore-in-progress" marker (lock/log
  sentinel), not health alone.

---

## ARCH-016
- **Severity:** Medium
- **Component:** Webhook re-registration de-dup
- **File Path:** `saas_core/models/saas_instance.py`; `saas_core/data/saas_pending_provision_cron.xml`
- **Line Numbers:** `_cron_verify_webhooks` `~:4129-4165`
- **Description:** Webhooks are periodically re-registered; without robust de-dup, restarts or
  overlapping cron runs can create duplicate webhooks, so one push fans out into multiple
  deploys.
- **Business Impact:** Redundant deploys waste compute and can cause confusing double-deploy
  behavior for customers.
- **Technical Impact:** No registration-time idempotency / last-registered guard.
- **Recommended Fix:** Track last registration time + provider webhook ID; skip if current.

---

## ARCH-017
- **Severity:** Medium
- **Component:** Git clone integrity
- **File Path:** `saas_core/models/saas_instance_repo.py`
- **Line Numbers:** `assert_safe_git_url()` `:34-82`
- **Description:** The URL guard blocks private-IP SSRF but does not enforce TLS verification
  or certificate validation at clone time; a DNS-control or downgrade scenario could MITM the
  clone of customer code into a root container.
- **Business Impact:** Tampered code reaches the customer application.
- **Technical Impact:** Integrity of fetched code is not cryptographically assured.
- **Recommended Fix:** Force `http.sslVerify=true`, prefer pinned providers / SSH host-key
  verification, and reject invalid certs.

---

## ARCH-018
- **Severity:** Medium
- **Component:** Test coverage gating
- **File Path:** `saas_core/tests/*` (15 files)
- **Line Numbers:** n/a
- **Description:** A meaningful test suite exists (billing, pricing, drivers, metrics,
  security fixes), but with no CI (ARCH-007) it is not enforced, and the 12k-line god-model is
  largely integration-tested rather than unit-tested.
- **Business Impact:** Tests provide weaker protection than their existence suggests;
  regressions slip through.
- **Technical Impact:** Coverage of provisioning edge/failure paths is limited by the
  monolith's untestability.
- **Recommended Fix:** Enforce tests in CI; expand failure-path coverage after decomposition.

---

## ARCH-019
- **Severity:** Medium
- **Component:** Metrics collection scalability
- **File Path:** `saas_core/models/saas_instance.py`; `saas_core/models/saas_instance_metric.py`
- **Line Numbers:** `_cron_record_metrics` `:4467-4505`
- **Description:** Metrics sampling batches per host (good design per project notes) but runs
  hosts sequentially over SSH `docker stats`. At many hosts × 5-minute cadence, the sampler
  can exceed its window and stack with the next run.
- **Business Impact:** Stale/laggy metrics undermine autoscaling guidance and alerting; cron
  stacking wastes resources.
- **Technical Impact:** No parallel fan-out across hosts; no per-run watchdog.
- **Recommended Fix:** Parallelize host sampling with a worker pool and a hard per-run
  deadline; consider a push-based agent emitting to a TSDB.

---

## ARCH-020
- **Severity:** Low
- **Component:** Configuration coupling
- **File Path:** `saas_core/models/res_config_settings.py`
- **Line Numbers:** whole file (492 LOC); see also project memory on config-param falsy trap
- **Description:** Platform behavior is spread across many config-settings fields with known
  pitfalls (Boolean/Integer `config_parameter` falsy-value trap documented in project memory),
  making misconfiguration easy and silent.
- **Business Impact:** Silent misconfiguration changes pricing/limits behavior.
- **Technical Impact:** Settings semantics are subtle and easy to regress.
- **Recommended Fix:** Centralize and validate critical settings; add startup assertions.

---

## ARCH-021
- **Severity:** Low
- **Component:** SPA build artifacts in VCS
- **File Path:** `saas_website/static/spa/assets/*`
- **Line Numbers:** n/a (committed bundles, e.g. `index-*.js`, `Globe-*.js`)
- **Description:** Built SPA bundles are committed and deployed by hash; the working tree
  already shows orphaned old bundles vs new ones. No build provenance ties a bundle to source.
- **Business Impact:** Stale-asset incidents (documented cache gotcha); unclear what source a
  deployed bundle corresponds to.
- **Technical Impact:** Manual hash verification step in deploy.
- **Recommended Fix:** Build in CI, publish hashed assets as artifacts, and stop committing
  bundles to source.

---

## ARCH-022
- **Severity:** Low
- **Component:** Documentation of operational runbooks
- **File Path:** `SESSION_NOTES.md`, `saas_core/docker/SERVER-SETUP.md`
- **Line Numbers:** n/a
- **Description:** Operational knowledge lives in session notes and setup READMEs rather than
  versioned runbooks; some critical facts (PG must listen on the Docker bridge) are noted as
  "not in code, can drift."
- **Business Impact:** Tribal knowledge; onboarding and incident response depend on individuals.
- **Technical Impact:** Drift between docs and reality.
- **Recommended Fix:** Convert to versioned runbooks and encode invariants as IaC/health checks.
