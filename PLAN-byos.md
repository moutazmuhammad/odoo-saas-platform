# Veltnex BYOS Plan

Updated 2026-10-13 · @Mostafa

**Product:** the customer supplies and pays for a server. Veltnex manages Odoo installation, deployments, updates, monitoring and subscription-dependent backups on that server. Veltnex charges for management; server costs are excluded.

This is an independent execution plan and launch gate. It does not require the external PostgreSQL subscription service, Spaces filestore adapter, trusted object-quota ledger or DOKS autoscaling from [Managed Hosting](PLAN-managed-hosting.md). Sharing a control plane does not make those Managed services BYOS prerequisites. Optional access to external database services can be offered later, after those services pass their own acceptance gate.

## 1. Current environment and implementation status

The existing environment is test-only, with zero customers. Instances labelled Production are test fixtures. Planned test interruption is acceptable; preserve required test data and configuration. This plan does not authorize deleting servers, workloads or data, and does not claim any deployment or launch acceptance.

Existing foundations, reviewed during planning:

| Component | Current evidence | Remaining work |
| --- | --- | --- |
| BYOS installer | `scripts/byos/install.sh` and `scripts/tests/test_byos_install.py` exist; installer targets a fresh dedicated server and configures Docker, Traefik, Netdata, firewall and SSH | Complete registration/resume/progress/complete/detach API integration, pinned release inputs, security review and real-server failure tests; script existence is not end-to-end acceptance |
| Infrastructure interface | `control-plane/saas_core/drivers/base.py` exists | Finish the shared runtime contract (P1.36); remove Kubernetes assumptions from BYOS business paths |
| Runtime | Active implementation is Kubernetes; `saas.server.compute_driver` currently selects Kubernetes | Implement and register `DockerComposeDriver` (P1.32), including lifecycle, jobs and recovery |
| Historical SSH driver | Deleted `SshDockerDriver` at commit `cb9038d`, approximately 294 lines | Review as reference material, then adapt to today's security and driver contracts; do not treat historical code as delivered functionality |
| Portal and platform services | Dashboard, project/account models and billing foundations exist | Add capability-aware BYOS flows, server ownership checks, subscriptions and enforceable feature gates; verify integration |

**Tracking rule:** record code, local tests, deployment, independent review and launch acceptance separately for every B-item below. None of the new BYOS items is accepted by this document. Codex implementation evidence does not replace Claude's independent verification. Record phase-specific commands, expected results and missing dependencies in this plan.

Original P0.x/P1.x IDs remain historical references (older documents were retired 2026-10-13). Use the B1–B9 acceptance criteria in §4 of this plan. Before each implemented BYOS phase deploys, publish its exact deployment, validation and recovery procedure using that record template. The existing Kubernetes runbooks apply only where BYOS uses those shared components.

**Shared image/security review:** no Odoo version is certified yet; the proposed version scope and qualification are in [the Managed plan](PLAN-managed-hosting.md) (P4, backlog phase 9). BYOS must supply its own native-path image/feature evidence and safely isolate any shared build infrastructure it uses.

**Pending owner decision (PLAN-managed-hosting.md, P7):** BYOS is proposed to be deferred; if revived, run it as a single-node k3s cell with the same operator instead of a new Docker Compose driver. Until decided, the content below is unchanged.

## 2. Architecture and product boundary

- One control plane manages accounts, organisations, permissions, subscriptions, operations, secrets and audit records for both products. Keep its database on the owner-managed separate VM (P0.29), with tested backup/recovery and restricted service identities.
- BYOS uses Docker Compose, Traefik and Netdata on a fresh, dedicated customer server, managed over SSH. No agent is required by the chosen launch design.
- PostgreSQL and the native Odoo filestore normally run on the customer's server, with persistent paths outside disposable containers. No `saas_object_storage` dependency or Spaces credentials are required to run Odoo locally or on the BYOS host.
- Use limited Odoo database roles; database administrator credentials belong to trusted management operations. Validate supported Odoo 14–20 initialization, module updates and recovery.
- Multiple customer projects may share that customer's server. A customer-supplied host is not a Veltnex shared multi-customer Managed node. Container limits reduce accidental contention; customer root access means Veltnex cannot promise enforcement against that server's owner.
- The shared interface (P1.36), capability matrix (P1.37) and conformance tests (P1.38) must distinguish runtime support. Return explicit unsupported-capability errors instead of falling through to Kubernetes or provisioning Managed services.
- Existing platform shell features stay within project permissions. The separate interactive SSH gateway remains deferred. Internal management SSH is required and must be secured.
- Start with one supported Linux/architecture matrix qualified by real-server tests. Do not advertise arbitrary servers or providers based only on installer checks.

**Responsibility:** the customer owns provider billing, host capacity and provider-level access. Veltnex owns the management actions and backup services promised by the subscription. Publish supported OS versions, patching responsibilities, maintenance/reboot policy, recovery targets and limits of support before onboarding.

## 3. Subscription plans and backup policy

Owner-approved prices are USD per month, with **server cost excluded** and **no annual management discount**.

| Plan | Management price | Agreed limits and audience | Backup rule |
| --- | --- | --- | --- |
| Free | $0 | 1 server, 2 projects; one account per verified person or company; community support | No backups; permanent portal warning and signup acknowledgement |
| Starter | $19 | Up to 2 servers | Daily backups to the customer's own S3-compatible destination |
| Pro | $39 | Unlimited projects; servers, backup storage, build minutes and support subject to published fair use | Veltnex off-site automated backups, retention, integrity checks, restore drills and a second location |
| Agency | $199 | Hosting companies and resellers; client organisations (P1.35) | Pro backup baseline; publish reseller-specific limits and responsibilities |

Connecting a server is free. Paid features include Git auto-deploy, monitoring, teams/permissions, automated backup features and advanced monitoring according to the published tier matrix. **Before selling a plan, finalize exact project allowances for Starter/Agency and tier assignment/limits for monitoring, teams, builds, backups and support.** Do not infer unspecified allowances or expose paid features based only on UI hiding. Free still needs minimal operational health/error reporting for installation and management; define its boundary against paid monitoring.

Enforce limits in APIs and asynchronous jobs. Use idempotent subscription/webhook handling and audit plan changes. When downgrading, preserve workloads and data; reject or stage a change that conflicts with active projects or retained backups, and show the required resolution. Publish retention/access after cancellation; do not silently delete data or turn an upgrade into permission to erase backups.

Free servers inactive for 90 days are detached under the published policy. Define inactivity from management activity/heartbeat, notify before detachment and test the procedure. Detachment revokes management access and does not imply deletion of the customer's Odoo installation or data.

## 4. Must-haves and acceptance criteria

| ID | Workstream | Historical references | Done when |
| --- | --- | --- | --- |
| B1 | Shared control-plane security and recovery | P0.1 where Kubernetes is used, P0.20, P0.29 | Authentication, organisation/project/server authorization, secret isolation, audit, restricted service access and platform restore pass; customer apps use a separate registrable domain from the portal |
| B2 | Safe onboarding and SSH management | P1.32 | Real fresh-server install, interruption/resume, host-key verification, per-server key isolation, reconnect, rotation and detach are verified without cross-server access or host lockout |
| B3 | Compose driver and capability interface | P1.32, P1.36–P1.38 | Provision, deploy, status, logs, permitted shells, restart, backup, restore and detach pass contract tests and live-host tests; BYOS flows do not call Managed-only services |
| B4 | Persistent data and bounded resources | P0.24 principles | Odoo/PostgreSQL roles, durable paths, CPU/RAM admission, disk alerts and failure handling work across container/host restarts; no implicit data deletion |
| B5 | Safe builds, deploys and upgrades | P0.3, P0.19 if shared build infrastructure is used; P1.11 | Untrusted builds cannot reach management keys/control-plane secrets; per-tenant image access, health checks, durable operation IDs and tested recovery protect releases |
| B6 | Subscription-dependent backups and recovery | P1.5 | Free warnings, Starter customer-S3 and Pro/Agency off-site policies work; restore matching database, attachments and code to a fresh server and verify data |
| B7 | Monitoring and incident handling | P0.11 principles | Netdata is private; host/app/database/disk/certificate/backup alerts and unreachable states are verified, with notifications and escalation appropriate to the plan |
| B8 | Billing, feature gates and cost metering | P1.31, P1.33 | Published limits are enforced server-side; retries do not double-charge; actual management, backup, build and support costs are attributed |
| B9 | Portal, reseller scope and truthful marketing | P1.34, P1.35, P1.37, P1.39 | Dashboard distinguishes host capacity from subscription allowances; tenant/reseller permissions and supported features are tested; claims match evidence |

### B2. Onboarding and SSH security

1. Support fresh, dedicated servers only at launch. Explain the installer's privileged actions before execution: Docker installation, UFW reset, CSF disabling, root-key access, Traefik, Netdata and fail2ban. This preserves the owner's approved install approach; it does not authorize running it on an existing shared server.
2. Preflight OS, resources, existing services, occupied ports, connectivity and dedicated-server eligibility before changes. Reject unknown conflicting installations. Recognize Veltnex-owned state so resume/retry does not reset an established server or replace keys unexpectedly.
3. Bind short-lived, single-use onboarding tokens to the correct account/server and operation. Protect registration, progress and completion endpoints against replay, forged server identity and leaked tokens. Pin installer dependencies and runtime image digests from a trusted release manifest; replace placeholders before publishing onboarding.
4. Use a unique per-server root SSH key, as approved by the owner. Encrypt private keys, restrict access to the management executor, redact logs and never mount keys in customer/build containers. Restrict SSH key source addresses and disable forwarding; keep password root login disabled.
5. Establish and persist the expected SSH host fingerprint through an authenticated onboarding exchange; verify every connection. A changed fingerprint blocks commands until an explicit verified recovery process completes. Do not auto-accept replacement host keys.
6. Validate destinations against server ownership and approved network reachability; prevent registration/SSH/telemetry from becoming access to control-plane private services or another account's host. Use structured, validated commands and paths; avoid injecting repository names, domain names or user arguments into privileged shell commands.
7. Stage firewall/SSH changes with reconnect verification and recovery before completing onboarding. Test failures midway, reruns, source-IP changes, key rotation, revocation and lost connectivity. Provide a customer-console recovery procedure.
8. Build a clean-room Veltnex installer; keep the approved approach without copying oec.sh code. Review the existing installer against these requirements rather than assuming its presence proves them.

**Trust boundary:** a customer with root can modify their host, credentials and telemetry. Protect the control plane and other customers even when a BYOS host is malicious. Never describe host-local limits as tamper-proof quotas against that host's owner. Keep Netdata and management endpoints private; no customer container receives the Docker socket or platform SSH keys.

### B3–B5. Runtime, resources and updates

- Keep operation IDs and durable states for install, deploy, upgrade, restore and detach. Serialize conflicting server/project operations with leases and fencing; recover after executor crashes. Record a clear success, rollback or recovery-required result, plus audit evidence. Do not reuse Managed resize code without adapting its host-capacity contract.
- Store Compose configuration and persistent database/filestore paths under known per-project identities. Restrict ports/networks and credentials between projects; test restart, reboot and disk failure behavior. Images and temporary build directories must not be the only copy of customer data.
- Validate workers against CPU/RAM, PostgreSQL connections and **actual available host capacity**, including host services, neighbouring projects and deployment surge. Enforce container CPU/memory limits where supported. Reject oversized configurations with a clear requirement to enlarge the customer's server; BYOS does not automatically provision DOKS nodes or assume cloud-provider credentials.
- Resource changes within host capacity can be automated after validation. Provider-level server resizing, storage expansion and migration need an explicit supported workflow or customer action. Never promise seamless physical resizing on arbitrary providers. Do not automatically shrink persistent volumes or bill Managed storage capacity on BYOS.
- Alert before local disk fills; bound logs, build caches and temporary files, and reserve recovery space. Pause the affected write/deploy operation when necessary and preserve existing data. This is host management, not the Managed service's independently enforced database/object-storage quota product.
- Builds must not run in the control-plane process or inherit host management secrets. If paid Git auto-deploy uses the shared Veltnex Build pool, P0.19 isolation and P0.3 registry boundaries are BYOS feature prerequisites too, even if Managed Hosting has not launched. Qualify an equivalent isolated path if choosing another build execution model.
- Deploy pinned images with validated configuration, readiness checks and bounded deadlines. Image rollback alone cannot reverse an Odoo database schema migration. Take a consistent recovery point and prove database/filestore/code restoration for failed module updates and supported upgrades. Major Odoo-version upgrades remain later work unless separately accepted.
- Test custom domains/TLS, logs, permitted shells and branch/project operations actually offered by the plan. Declare unsupported features in the capability matrix; do not advertise Managed-only sleep or autoscaling for BYOS. Sleep is not enabled by default because the customer pays the host bill.

### B6. Backups and portable recovery

- Capture a consistent PostgreSQL restore point and matching native filestore, plus image/addon versions, configuration and a manifest. Coordinate writes and cron through a tested quiesce/snapshot procedure; independent uncoordinated copies are not sufficient evidence.
- Schedule Starter daily backups to the customer's configured S3-compatible destination. Scope destination credentials to that account/server and validate access safely. Expose failures and missed backups; customer bucket loss is a documented responsibility, not proof of successful recovery.
- Pro/Agency use Veltnex-managed off-site backups with retention, integrity verification, drills and a second location. Carry forward the earlier 7 daily / 4 weekly / 6 monthly retention assumption only after verifying costs and publishing the final contract. Encrypt backups and isolate access per customer.
- Backup storage is separate from Odoo's live native filestore. Using S3 for backups does not require Spaces-backed live attachments, `saas_object_storage`, M14 or shared Managed databases.
- Restore to a disposable fresh server and verify database records, actual attachment contents, application startup and version compatibility. Include missing/corrupted archives, interrupted restore, full target disk and lost source host. Publish measured recovery point/time targets; do not invent an SLA.
- Full export must include native database plus filestore and required code/version information, allowing local development without Spaces or Veltnex services. Bare SQL dumps do not include native file attachments. Neutralize copied environments, disable outgoing mail/production cron and remove platform credentials.
- Migration to/from Managed Hosting is optional separate work and must use that product's validated quota/import/export workflow after its gate passes. It is not a BYOS launch dependency.

### B7–B9. Monitoring, billing and support

- Collect bounded, plan-appropriate metrics over authenticated management channels: host/app reachability, CPU/RAM, disk/inodes, PostgreSQL, TLS expiry and backup freshness. Limit telemetry payloads and logs from an untrusted host. Distinguish stale observations from confirmed workload failure.
- Test alerts, notification delivery, on-call response and status reporting. Define OS security update/reboot maintenance and incident escalation; customer provider outages and revoked SSH access need actionable portal states.
- Gate features in backend APIs and workers. A successful payment does not imply a successful install; model subscription activation/failure/refund policy explicitly and prevent retry-driven duplicate charges. Preserve access to recovery/export according to published cancellation rules.
- Agency launch includes client organisations with tested separation and a published responsibility model. White-label and client billing remain post-launch (P1.35); do not advertise them as available before acceptance.

## 5. Costs, margins and effort

BYOS avoids Veltnex buying customer compute, volumes and load balancers. The previous **$12–14 per environment** avoided-infrastructure estimate is a Managed comparison, not net profit or measured savings. The previous **about $1 per server plus support** platform-cost estimate is unverified. The customer still pays their server provider.

Measure control-plane share, SSH execution, monitoring/logs, build minutes, registry/transfer, backup bytes/requests/second-location costs, payment fees and especially support time. Attribute shared platform costs once across both products; do not charge the same idle overhead twice in margin calculations. Profit is management revenue minus real Veltnex costs. A Free account has costs and no management revenue.

Before paid launch, benchmark test servers and synthetic projects, publish fair-use allowances and support boundaries, and model break-even for each tier. Validate costs against invoices; future support and margins remain projections until real customers exist. Review costly accounts without silently removing promised backups or deleting data. Marketing claims (P1.34) need published calculations and no invented social proof.

Historical effort estimates remain assumptions: P1.32/P1.36/P1.37/P1.38 together **5–8 engineer-weeks**, and reuse of the old SSH driver **about 1–2 weeks saved**. Re-estimate after reviewing the installer/API gaps, security, recovery and current runtime coupling. Odoo 14–20 support previously added **3–5 weeks**; budget shared compatibility work once, not separately as two savings claims.

## 6. Execution order and independent launch gate

1. **Baseline and contracts:** inventory test servers/data, define supported host matrix and shared driver/capability contracts, finalize the tier matrix/fair use, responsibility and cancellation policies. Record current code/test/deployment status. Preserve fixtures before choosing reset versus upgrade.
2. **Secure shared platform and onboarding:** B1/B2, API registration/state flow, pinned installer release, encrypted per-host credentials, ownership and host-key validation. Verify fresh install, safe resume, access revocation and no cross-account/private-network reach.
3. **Implement runtime and data lifecycle:** B3/B4, Compose deployment, limited PostgreSQL roles, native persistent storage, capacity checks, monitoring foundations and durable operation reconciliation. Test Odoo 14–20 on the supported matrix.
4. **Qualify paid services:** B5/B6/B7, isolated Git builds and registry access, consistent backups for each paid tier, fresh-host restore, updates/failure recovery and incident handling. No backup service or auto-deploy subscription opens before its implementation and recovery tests pass.
5. **Finish product and economics:** B8/B9, dashboard/capabilities, organisation/reseller permissions, server-side feature gates, idempotent billing, measured costs and truthful pricing/marketing. Exercise Free and every paid tier through their actual flows.
6. **Gate BYOS:** independently review and accept the deployed BYOS release before onboarding the first BYOS customer. Require recorded evidence for B1–B9, real supported-host install/resume, compromised-host boundary tests, deployment/update rollback, source-server-loss restore, quota/feature-gate enforcement, billing retries, load/cost checks, operations/legal/support policies and the end-to-end portal walkthrough. Mark unavailable tests blocked. Managed M10–M14 and Gate MH are not prerequisites; shared components used by BYOS must pass their applicable checks.
7. **After launch:** Agency white-label/client billing, major-version upgrades, additional supported hosts/providers, optional external database subscriptions and optional Managed migration. Each needs its own acceptance before sale; shared Managed services retain their separate release gate.

**Release evidence:** identify the accepted deployment and exact commit/working-tree state, test commands/results, supported Odoo/host versions, security findings, recovery artifacts, measured costs and unresolved limitations. Independent review remains pending until actual results are recorded. This documentation split changes no running infrastructure.
