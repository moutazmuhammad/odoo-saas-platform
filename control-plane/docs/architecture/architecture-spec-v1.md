# Architecture Specification v1

**Document status:** Draft for engineering and stakeholder review
**Scope:** Multi-tenant Odoo hosting platform (Odoo.sh-class)
**Audience:** Engineering teams, technical leadership, founding stakeholders
**Date:** 2026-06-17

---

## 0. Preamble

### 0.1 Purpose

This document specifies the v1 architecture of a cloud-native, multi-tenant application
hosting platform. It defines the Control Plane and its components, the storage layer, and
the operational flows (deployment, backup, restore, upgrade).

The architecture is designed to start on a **single server at minimal cost (~$100/month)**
while remaining structurally capable of scaling to **thousands of tenants without
rewriting the Control Plane.** This is achieved by isolating all infrastructure-specific
behavior behind stable internal interfaces.

### 0.2 Guiding principles

| #  | Principle                       | Consequence                                                        |
|----|---------------------------------|--------------------------------------------------------------------|
| P1 | State is the source of truth    | The Control Plane stores *desired state*; reality is reconciled.   |
| P2 | Compute is disposable           | Containers/nodes hold no system of record; storage is addressable. |
| P3 | One interface, one impl first   | DockerDriver now; KubernetesDriver later. No plugins/RPC.          |
| P4 | Edition is an attribute         | Community/Enterprise differ by image + entitlement, not platform.  |
| P5 | Postpone aggressively           | Build invariants; defer everything that won't force a rewrite.     |
| P6 | Every tenant action is metered  | `tenant_id` label on every metric/log/cost record from day one.    |

### 0.3 Explicit assumptions

- A1: v1 runs on a single dedicated server; multi-node arrives as a configuration change.
- A2: Tenant workloads are Odoo (Python monolith + PostgreSQL + filestore).
- A3: Object storage is the system of record for filestore and artifacts; compute caches it.
- A4: Enterprise hosting is gated on Odoo Partner status + legal sign-off (out of v1 scope).
- A5: The orchestrator is Docker in v1; the design must not couple business logic to it.

---

## 1. Control Plane

The Control Plane is the brain of the platform. It is **infrastructure-agnostic**: it stores
desired state and drives the underlying backend (Docker today) toward that state through a
stable driver interface. It never imports Docker APIs in business logic.

### 1.1 Component overview

```
                        ┌──────────────────────────────────────────────────┐
   Customers / CLI ───► │                   API LAYER                        │
   Admin / Webhooks     │     (auth, validation, desired-state writes)       │
                        └───────────────┬────────────────────────────────────┘
                                        │ writes desired state
                                        ▼
                        ┌──────────────────────────────────────────────────┐
                        │            RECONCILIATION ENGINE                    │
                        │   (diff desired vs actual → drive to convergence)  │
                        └───┬───────────┬───────────┬───────────┬────────────┘
                            │           │           │           │
                    ┌───────▼──┐  ┌─────▼─────┐ ┌───▼──────┐ ┌──▼──────────┐
                    │  DOCKER  │  │   DATA    │ │  BUILD   │ │  PLACEMENT  │
                    │  DRIVER  │  │  SERVICE  │ │ PIPELINE │ │  (future)   │
                    └────┬─────┘  └─────┬─────┘ └────┬─────┘ └──────┬──────┘
                         │              │            │              │
        ┌────────────────┴──────────────┴────────────┴──────────────┴───────┐
        │                       BILLING SYSTEM                                │
        │             (consumes usage from Observability)                     │
        └─────────────────────────────────────────────────────────────────────┘
        ┌─────────────────────────────────────────────────────────────────────┐
        │   OBSERVABILITY LAYER (metrics, logs, per-tenant cost — tenant_id)    │
        └─────────────────────────────────────────────────────────────────────┘
```

### 1.2 API Layer

- **Responsibility:** The only public entry point. Authenticates and authorizes callers,
  validates requests, and writes **desired state** into the system-of-record database.
  It does not perform infrastructure actions directly.
- **Interfaces:** REST/JSON over HTTPS; webhook receivers for VCS (GitHub/GitLab/Bitbucket)
  and payments; an admin surface for operators.
- **Key rule:** API writes intent (e.g. "tenant T should run Odoo 17, prod env, plan
  Business"). It returns immediately; convergence is asynchronous.

### 1.3 Reconciliation Engine

- **Responsibility:** Continuously compares **desired state** (DB) against **actual state**
  (reported by drivers) and issues the actions needed to converge. This is the operator
  pattern. It is idempotent and crash-safe — re-running produces the same result.
- **Interactions:** Calls Docker Driver (lifecycle), Data Service (state ops), Build Pipeline
  (image readiness), and reads health/metrics from Observability.
- **Why:** Decoupling intent from execution is what makes the backend swappable and the
  system self-healing.

### 1.4 Docker Driver (ComputeDriver implementation)

- **Responsibility:** The single concrete implementation of the `ComputeDriver` interface in
  v1. Translates abstract lifecycle commands into Docker operations.
- **Interface (stable contract):**
  `create · destroy · start · stop · exec · logs · endpoint · health`
- **Extensibility:** A future `KubernetesDriver` implements the same interface; no caller
  changes. State operations (backup/restore) deliberately live in Data Service, **not** here,
  so a future driver does not re-implement them.

### 1.5 Data Service

- **Responsibility:** Owns all **stateful** tenant operations. Built on two primitives —
  `SNAPSHOT(tenant)` and `MATERIALIZE(artifact, target)` — from which backup, restore, clone,
  promote, migrate, and disaster recovery are composed.
- **Interactions:** Coordinates PostgreSQL (PITR via WAL archiving), object-storage filestore
  (content-addressed), and the Compute Driver (quiesce/start), with a neutralize step for
  non-production clones.
- **Why:** Stateful operations are backend-independent and are the platform's highest-risk
  surface; centralizing them prevents duplication and data loss.

### 1.6 Build Pipeline

- **Responsibility:** Turns a Git commit into a deployable, immutable image. VCS-agnostic via a
  `VCSProvider` interface (GitHub/GitLab/Bitbucket). Maps branches to environments
  (production/staging/development) and Odoo versions (16/17/18).
- **Security boundary:** Build workers execute untrusted customer code (`requirements.txt`,
  `setup.py`) and must run **ephemeral, rootless, egress-restricted, credential-free.**
- **Output:** `image:tenant-<version>-<commit-sha>` pushed to the Container Registry.

### 1.7 Billing System

- **Responsibility:** Converts metered usage into invoices. Consumes per-tenant usage
  (CPU, RAM, storage, egress) from the Observability Layer; integrates with an external
  payment provider; reconciles entitlements (e.g. Enterprise per-user counts in future).
- **Interactions:** Read-only consumer of Observability; writes invoice/plan state to the
  system-of-record DB.

### 1.8 Placement Service (future component)

- **Responsibility (designed, not built in v1):** Decides *where* a tenant runs (which node,
  which PostgreSQL tier) based on capacity, cost, and policy. In v1 placement is trivial
  ("the one server"); the interface exists so multi-node and tiering are additive.
- **Extensibility hook:** Reconciliation Engine asks Placement for a target before invoking
  the Compute Driver. v1 returns a constant; future versions return a scheduling decision.

### 1.9 Observability Layer

- **Responsibility:** Collects metrics, logs, and **per-tenant cost/usage** with a mandatory
  `tenant_id` label on every series. Feeds the margin dashboard (revenue − cost per tenant)
  and the Billing System.
- **Interactions:** Scrapes all components and tenant containers; provides health to the
  Reconciliation Engine and usage to Billing.

### 1.10 Component interaction summary

| From → To                         | Purpose                                        |
|-----------------------------------|------------------------------------------------|
| API → System-of-record DB         | Persist desired state                          |
| Reconciliation → Docker Driver    | Apply lifecycle actions                        |
| Reconciliation → Data Service     | Trigger backup/restore/migrate                 |
| Reconciliation → Build Pipeline   | Ensure image is built before deploy            |
| Reconciliation → Placement        | Obtain target host/tier (constant in v1)       |
| Observability → Billing           | Supply usage records for invoicing             |
| All components → Observability    | Emit metrics/logs tagged with tenant_id        |

---

## 2. Storage Layer

Three storage systems, each with a single clear role. No system owns data that belongs to another.

```
   ┌───────────────────────────┐      ┌───────────────────────────┐
   │   PostgreSQL               │      │   Object Storage            │
   │   (System of Record)       │      │   (Artifacts/Logs/Backups)  │
   │   - Control Plane meta DB  │      │   - filestore (CAS)         │
   │   - Tenant databases       │◄────►│   - snapshots / WAL         │
   │   - Desired & billing state│ PITR │   - build logs, exports     │
   └─────────────┬──────────────┘      └──────────────┬─────────────┘
                 │                                     │
                 │             ┌───────────────────────▼─────────────┐
                 └────────────►│   Container Registry                 │
                   image refs  │   (Images & Versioning)              │
                               │   image:tenant-<ver>-<sha>           │
                               └──────────────────────────────────────┘
```

### 2.1 PostgreSQL — System of Record

- **Role:** Authoritative store for (a) the **Control Plane meta DB** (tenants, environments,
  desired state, plans, entitlements, job state) and (b) each **tenant's application database**
  (one DB per tenant, Odoo's native model).
- **Tiering:** Shared cluster (Starter) → dedicated cluster (Business) → dedicated + PITR
  (Enterprise). Tier is an *operational attribute*, decoupled from billing plan, and can be
  changed by Data Service migration.
- **Durability:** Continuous WAL archiving to object storage enables point-in-time recovery.

### 2.2 Object Storage — Artifacts, Logs, Backups, Filestore

- **Role:** System of record for the **filestore** (tenant attachments) and the store for
  snapshots, WAL, build logs, and exports. Exposed to Odoo via an **object-backed POSIX layer
  with local node caching** (so Odoo's filesystem assumptions hold without per-version S3
  modules). Filestore objects are **content-addressed (CAS)** to enable cheap clones and dedup.
- **Why:** Globally addressable storage makes compute disposable and multi-region/Kubernetes
  futures additive rather than migrations.

### 2.3 Container Registry — Images & Versioning

- **Role:** Stores immutable tenant images tagged by commit SHA and Odoo version. Base images
  per version are shared across tenants; tenant layers add custom modules and dependencies.
- **Versioning:** Tag = `tenant-<version>-<sha>`; rollback is selecting a prior SHA.

### 2.4 Data flow between storage and services

| Flow                                          | Trigger                         |
|-----------------------------------------------|---------------------------------|
| API → PostgreSQL (meta)                       | Desired-state write             |
| Build Pipeline → Container Registry           | Successful build                |
| Build Pipeline → Object Storage               | Build logs/artifacts            |
| Compute Driver → Container Registry           | Image pull on deploy            |
| Tenant container ↔ Object Storage (via cache) | Filestore read/write            |
| PostgreSQL → Object Storage                   | WAL archiving / base backups    |
| Data Service ↔ Object Storage + PostgreSQL    | Snapshot / materialize          |
| Observability → Object Storage                | Long-term metric/log retention  |

---

## 3. Deployment Flow

Deploying or updating a tenant environment from a Git commit.

```
 (1) Request        (2) Build           (3) Placement      (4) Pull+Run      (5) State+Obs
 ┌────────┐        ┌──────────┐        ┌────────────┐     ┌──────────┐     ┌────────────┐
 │ push / │──────► │  Build   │──────► │ Placement  │───► │ Compute  │───► │ DataService│
 │ API    │        │ Pipeline │ image  │  Service   │ tgt │  Driver  │ run │ + Observ.  │
 └────────┘        └──────────┘        └────────────┘     └──────────┘     └────────────┘
```

1. **Request initiation.** A Git push (webhook) or an API call expresses intent. The API Layer
   validates and writes desired state to the meta DB. The Reconciliation Engine detects the diff.
2. **Build (if needed).** The Build Pipeline resolves branch → {environment, version, edition},
   builds an immutable image with cached dependencies, and pushes it to the registry tagged by SHA.
3. **Scheduling / placement decision.** The Reconciliation Engine asks the Placement Service for a
   target (host + PostgreSQL tier). In v1 this is the single server; the call exists for future
   multi-node scheduling.
4. **Image pull and execution.** The Compute Driver pulls the image from the registry and starts the
   container with the correct addons path, database binding, and cached filestore mount. A health
   gate must pass (DB registry loads, login succeeds) before traffic is admitted.
5. **State updates in Data Service.** Environment record is updated to "running" with its image SHA.
   If the deploy changes modules, Data Service runs the Odoo module update; for non-production
   environments the database is neutralized.
6. **Observability tracking.** From first start, the container emits metrics and logs tagged with
   `tenant_id`. Ingress routing is swapped to the new environment; the prior environment is retained
   briefly for rollback, then reaped.

---

## 4. Backup Flow

### 4.1 What is backed up

- **Tenant PostgreSQL database** — via base backup + continuous WAL (PITR-capable).
- **Tenant filestore** — a manifest of content-addressed object hashes (objects already durable).
- **Control Plane meta DB** — the desired-state and billing system of record.
- **Snapshot metadata** — version, edition, timestamp, and references linking DB + filestore.

### 4.2 Where it is stored

- All backups land in **object storage** under a deterministic layout, with **cross-region
  replication** for disaster recovery:

```
s3://platform/
  tenants/{tenant_id}/filestore/{sha[:2]}/{sha}       ← CAS, shared across clones
  snapshots/{tenant_id}/{snapshot_id}/manifest.json
  snapshots/{tenant_id}/{snapshot_id}/pg/             ← base + incremental + WAL
  dr/{region}/...                                     ← replicated copies
```

### 4.3 How consistency is ensured

- The database backup uses a consistent base backup plus WAL, giving a recoverable,
  transactionally consistent point in time — not a naive file copy.
- The filestore manifest is captured **against the same recovery target time** as the DB so
  attachment references and database rows agree.
- Backups are immutable and verified by checksum; a backup that fails verification does not exist.

### 4.4 Frequency and lifecycle strategy

| Tier      | Base backup | WAL (PITR)   | Retention                 |
|-----------|-------------|--------------|---------------------------|
| Starter   | Daily       | Optional     | 7 days                    |
| Business  | Daily       | Continuous   | 30 days                   |
| Enterprise| Daily       | Continuous   | 30–90 days + DR replica   |

- Lifecycle policies expire old snapshots automatically; CAS dedup means clones and incremental
  backups consume minimal additional storage.

---

## 5. Restore Flow

### 5.1 Recovery scenarios

- Accidental data loss / bad deploy (restore tenant to a point in time).
- Node or volume failure (rematerialize on healthy infrastructure).
- Tier change or server migration (restore onto a different cluster/host).
- Regional disaster (materialize from the DR replica in another region).

### 5.2 Restore ordering (DB → storage → services)

1. **Provision target.** Create the destination environment on the correct version/edition.
2. **Database first.** Restore the PostgreSQL base backup and replay WAL to the chosen
   point-in-time on the target cluster.
3. **Storage second.** Re-link the filestore from the manifest (CAS objects already exist; this is
   typically a reference operation, not a byte copy).
4. **Neutralize (non-production only).** Disable outbound mail, crons, and payment providers so a
   restored clone cannot act on the outside world.
5. **Services last.** Start the container; admit traffic only after the health gate passes.

### 5.3 Validation after restore

- Health gate: database registry loads, a login/RPC probe succeeds, key tables present.
- Integrity: checksum of restored artifacts matches the manifest.
- For DR drills: an automated job materializes a real tenant in the DR region and asserts boot.

### 5.4 Rollback strategy

- Deploys are immutable images keyed by SHA; rollback = redeploy the previous SHA.
- Restores do not overwrite the source until cutover; the prior environment is retained for a
  rollback window before being reaped, so a failed restore is reversible.

---

## 6. Upgrade Flow

Two distinct upgrade classes are handled separately.

### 6.1 Platform upgrades (Control Plane / base images)

- **Zero-downtime strategy.** Control Plane components are stateless behind the meta DB and are
  rolled one at a time; the Reconciliation Engine is idempotent and safe to restart.
- **Base image updates.** New per-version base images are built and rolled to tenants on their next
  deploy, or via a controlled re-deploy; rollback is the prior image SHA.
- **Rolling vs full redeploy.** Stateless components roll; tenant containers redeploy per environment
  behind the health gate. No global maintenance window is required.

### 6.2 Tenant application upgrades (Odoo version migration, e.g. 16 → 17 → 18)

Version migration is a **Data Service operation**, never performed in place:

1. **Clone** production to a throwaway upgrade environment (cheap via CAS).
2. **Migrate** the database (OpenUpgrade for Community; the official upgrade path under Partner
   status for Enterprise) and run the tenant modules' migration scripts.
3. **Validate** on a staging URL; the customer approves.
4. **Promote** the upgraded clone to production via routing cutover. The original remains intact as
   an instant rollback target.

### 6.3 PostgreSQL migration / tier changes

- Moving a tenant between clusters (failure, tier promotion, server migration) uses Data Service:
  - **Dump mode** (default): snapshot → materialize on target → verify → cutover. Downtime equals
    restore time.
  - **Logical-replication mode** (large tenants): replicate source → target, then a brief cutover with
    drained lag for near-zero downtime.
- Major PostgreSQL engine upgrades follow the same clone-validate-promote discipline.

### 6.4 Version compatibility handling

- Each tenant environment pins an explicit Odoo version and image SHA; the platform never
  silently changes a running version.
- Supported versions (16/17/18) each have an independently maintained base image and migration path;
  unsupported versions are not deployable.

---

## 7. Scalability & Evolution Summary

| Concern            | v1 (1 server, ~$100/mo)        | Evolution (no Control-Plane rewrite)            |
|--------------------|--------------------------------|-------------------------------------------------|
| Orchestration      | Docker Driver                  | Add Kubernetes Driver (same interface)          |
| Placement          | Constant (single node)         | Implement Placement Service scheduling          |
| PostgreSQL         | Shared cluster                 | Dedicated + PITR tiers via Data Service migrate |
| Filestore          | Object storage + local cache   | Multi-region replication (config change)        |
| Builds             | Single build worker            | Build farm / parallel workers                   |
| Observability      | Single stack, tenant_id labels | Scale-out TSDB; same label taxonomy             |

The Control Plane, the driver/Data-Service interfaces, the object-storage system of record, and the
`tenant_id` labeling are **invariants**. Everything else is additive. This is the structural meaning of
"scale to thousands of tenants without a rewrite."

---

## 8. Open Items / Out of Scope for v1

- Enterprise hosting (pending Odoo Partner status + legal sign-off).
- Kubernetes Driver, multi-region, automatic PostgreSQL failover/HA.
- Public self-service signup (white-glove onboarding in v1).
- Ephemeral per-PR environments, module marketplace, multi-cloud.

*End of Architecture Specification v1.*
