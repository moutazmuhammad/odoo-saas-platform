# Veltnex Managed Hosting Plan

2026-10-13 · **Status: PROPOSED — awaiting owner approval (§11).** Nothing below is authorized for implementation until §11 is ticked. This is the single Managed Hosting plan; [PLAN-byos.md](PLAN-byos.md) is the BYOS plan. The earlier M1–M14 plan, its reviews, archive and acceptance/checklist documents were retired on 2026-10-13; their still-relevant content is carried here (security runbooks: [P0.24](docs/P0.24-database-roles.md), [P0.1](docs/P0.1-control-plane-rbac.md), [P0.19](docs/P0.19-build-pool.md); test commands: Appendix A).

**Evidence tags**

| Tag | Meaning |
| --- | --- |
| **[M]** | Measured |
| **[L]** | Provider list price/limit, seen 2026-10-13 |
| **[R]** | Repository code |
| **[H]** | Hypothesis: an assumption, not a forecast |
| **[P]** | Owner policy to confirm |
| **UNVERIFIED** | A benchmark that has not been run |

**Model:** [`docs/profit-model.py`](docs/profit-model.py), rewritten as v2. Every table in §1 is its output. **All outputs are scenario arithmetic from [H] inputs, not profit forecasts.**

---

## 1. Corrected financial model

### 1.1 Audit of v1: errors found and fixed

| # | v1 problem | Effect | v2 fix |
| --- | --- | --- | --- |
| 1 | The 10% reserve was subtracted as an operating cost | Mixed cash allocation with profit | Reserve (5%) and reinvestment (10%) are allocations **after** salary and tax [P] |
| 2 | Founder take = contribution before labour, with unlimited founder hours and no tax | Overstated sustainable founder income | Separate lines: cash before founder, salary, replacement cost, staffed profit, distributable cash; founder capacity 140 h/month, excess hired at $20/h [H] |
| 3 | DOKS had no failure spare; metal had N+1 in the totals but ×1.25 in the per-plan table | Inconsistent $/GiB | Same 75% utilization for both; metal N+1 (N+2 from 8 servers); per-plan cost allocated from the same totals |
| 4 | RAM-only capacity; metal disk treated as free | Metal understated (it is disk-bound below ~25 customers on AX42) | Servers/nodes = max(RAM, CPU, disk, DO 15-volume cap) |
| 5 | One volume per environment, no headroom | Unsafe at 100% (§3) and understated cost | Two volumes per environment (DB + filestore), 20% unsold headroom [P] |
| 6 | Backups at 1.5× data | Ignored PITR bytes | WAL base+7 d (2.0× DB) + daily dumps (0.6× DB) + filestore snapshots (1.2× files), primary + immutable off-site [H] |
| 7 | No annual discount, setup fees, churn-driven sales work or failure cost | Missing revenue/cost lines | All modelled [H] |
| 8 | No tax | Overstated distributable cash | Corporate tax 22.5% on profit after salary [H — confirm for your entity] |
| 9 | Hetzner object-storage overage formula | Minor | Fixed |

**Bridge for the dedicated-server ("metal") $4,000 case** (v1 said 58 customers):

| Step | Customers needed |
| --- | --- |
| v2 BASE | **100** |
| Without tax | 85 |
| Without tax and without reinvestment | 65 |
| With reserve at 10% instead | 76 |

The remaining gap to v1 comes from the cost lines v1 omitted (rows 4–7 of the audit table).

### 1.2 Definitions (no double counting)

| Line | Definition |
| --- | --- |
| **Cash before founder** | Revenue − payment fees − all infrastructure − tools − marketing cash − failure costs − *hired* labour |
| **Founder salary** | Cash actually paid to the founder (the $2,000/$4,000 targets) |
| **Founder replacement cost** | Founder hours × $25/h [H]: the economic value of the founder's labour, non-cash |
| **Fully staffed operating profit** | Cash before founder − founder hours × $25 (every hour paid at market rate) |
| **Distributable cash** | Cash before founder − salary − tax − reserve − reinvestment. Sustainable only if ≥ 0 |

### 1.3 Results (BASE)

**BASE inputs [H]:**

- Prices: Starter $59 / Business $129 / Scale $299.
- Customer mix: 70 / 25 / 5%.
- 30% of customers on annual contracts (10 months for 12).
- Add-ons: 10% on top of plan revenue.
- Setup fee: $149.
- Churn: 3% per month.
- Support: 0.75 / 1.5 / 3 h per customer per month.
- Utilization: 75%.

| Customers | 5 | 10 | 25 | 50 | 100 | 250 |
| --- | --- | --- | --- | --- | --- | --- |
| MRR | $485 | $970 | $2,424 | $4,848 | $9,695 | $24,238 |
| **DOKS** gross margin | 27% | 42% | 47% | 51% | 52% | 54% |
| DOKS cash before founder | $33 | $299 | $998 | $2,296 | $3,627 | $6,346 |
| DOKS distributable after a $2k salary | −$2,040 | −$1,847 | −$1,366 | −$498 | −$193 | −$267 |
| **Metal** gross margin | 39% | 57% | 72% | 77% | 80% | 81% |
| Metal cash before founder | $73 | $424 | $1,562 | $3,517 | $5,911 | $12,238 |
| Metal distributable after a $2k salary | −$2,000 | −$1,722 | −$801 | **+$448** | +$1,577 | +$4,299 |
| Metal distributable after a $4k salary | — | — | — | −$1,211 | **+$27** | +$2,749 |
| Labour hours (founder / hired), metal | 54/0 | 64/0 | 91/0 | 130/0 | 140/74 | 140/331 |

| Customers required (BASE) | Founder-operated, $2k | Founder-operated, $4k | Fully staffed, $2k | Fully staffed, $4k |
| --- | --- | --- | --- | --- |
| DOKS, as priced | not reached | not reached | not reached | not reached |
| Metal | **43** | **100** | 95 | 147 |

**What these results say:**

- DOKS is a valid validation platform: a founding-price pilot is cash-negative only below ~7 customers, by under $150/month.
- **At these prices DOKS cannot sustainably fund founder income plus tax, reserves and reinvestment.** Its gross margin stays at 51–54%, and labour grows with customers.
- Sustainable founder income at these prices requires metal economics, higher prices, or a partner-heavy mix (§8).

### 1.4 Sensitivity: customers needed for a $4,000 founder-operated income (DOKS / metal)

| Case | DOKS | Metal |
| --- | --- | --- |
| BASE | — | 100 |
| Occupancy 60% / 85% | — / — | 140 / 94 |
| Heavy workloads (×1.5 RAM/CPU) | — | 160 |
| Strict support boundary (×0.5 support) | 220 | 76 |
| No support boundary (×2 support) | — | **620** |
| Starter-heavy mix | — | 185 |
| Partner/Business-heavy mix | 644 | 70 |
| Partners + strict boundary | 133 | **61** |
| Churn 2% / 6% | — / — | 98 / 112 |
| Server failures 2 per server per year | — | 119 |
| Provider prices +20% / +40% | — / — | 113 / 137 |
| Prices +20% / +35% | 161 / 90 | 65 / 55 |
| No add-ons | — | 147 |
| All customers annual (no churn benefit modelled) | — | 164 |
| Reserve + reinvestment 5% total | 180 | 66 |
| Tax 0% | 618 | 85 |

**Segment check** (metal):

| Segment [H] | $2k | $4k |
| --- | --- | --- |
| Partner case: 20% discount, ×0.6 support, 2% churn | 40 | 78 |
| Direct SMB case: Starter-heavy, ×1.5 support, 5% churn | 87 | **501** |

**Ranking of levers:**

1. Support boundary.
2. Customer segment (partners).
3. Price.
4. Hosting platform.
5. Everything else.

**Steady-state customers = new per month ÷ churn:** 4 new/month gives 133 customers at 3% churn and 67 at 6%.

### 1.5 $/GiB reconciled with profile cost

| | $/usable GiB | $/guaranteed GiB at 75% | Small production (1.5 GiB) | Starter bundle (2.25 GiB) |
| --- | --- | --- | --- | --- |
| DOKS s-4vcpu-8gb | 8.00 | 10.67 | $16.0 | $24.0 |
| DOKS s-8vcpu-16gb | 7.38 | 9.85 | $14.8 | $22.2 |
| DOKS 8 vCPU/32 GB AMD | 6.00 | 8.00 | $12.0 | $18.0 |
| Metal AX42-LTD, N+1 with 2 / 4 / 8 active servers | 1.42 | 2.84 / 2.37 / 2.13 | $4.3 / $3.6 / $3.2 | $6.4 / $5.3 / $4.8 |

RAM is not the only constraint:

- **DOKS:** at 25–50 customers the model chooses 8 GB or 16 GB nodes. That is $44.6/customer all-in at 50 customers, including storage, backups and LB.
- **Metal on AX42:** **disk binds before RAM** at 25 customers (47.5 GiB sold/customer [H]). From ~40 customers AX102 is cheaper ($18.8/customer all-in at 50).
- Per-plan gross margin at 50 customers, allocated by guaranteed RAM:

  | | Starter | Business | Scale |
  | --- | --- | --- | --- |
  | DOKS | 44% | 49% | 56% |
  | Metal | 75% | 77% | 80% |

---

## 2. Recommended architecture: verified vs hypothetical

**Recommendation:**

- Sell the existing Managed Hosting product on DOKS as the reference platform.
- Fix storage safety, memory and backups first.
- Measure real workloads on DOKS.
- Rent a metal test cell only for a bounded qualification.
- Migrate only when the §6 trigger and the §4 recovery qualification are both met.

| Claim | Status |
| --- | --- |
| Operator and Kubernetes driver are provider-neutral; only Traefik LB values and bootstrap checks are DO-specific | **Verified** [R] |
| Operator already supports a separate PostgreSQL volume (`storage.sharedWithDatabase`, immutable per environment); the driver forces shared mode | **Verified** [R `odooinstance_types.go:135`, `kubernetes_driver.py:545`] |
| DO volume cap is 15 per node | **Verified** [L][M] |
| DOKS allocatable memory by node size | **Verified** [L]; 4 GB node measured 2.93 GiB [M] |
| Test environments peak at 0.38–0.83 GiB and ≤0.5 core, idle with no users | **Verified** [M] (lower bound only) |
| Memory requests are 25% of limits; per-worker Odoo limits derived from the pod limit (OOM risk) | **Verified defect** [R `deployment.go odooMemoryLimitArgs`, `saas_plan.py:139`] |
| Guaranteed RAM per profile (0.75/1.5/3/6.5 GiB), CPU per profile, environments per server | **UNVERIFIED** [H] |
| CPU and storage I/O contention, simultaneous cron/import/report/backup/build | **UNVERIFIED** |
| PostgreSQL isolation and noisy-neighbour bounds on a shared node or server | **UNVERIFIED** |
| Metal RPO/RTO, spare capacity sufficiency, patch downtime | **UNVERIFIED** |
| Metal ops hours (18 h + 0.12 h/customer + 1.5 h/server per month) | **UNVERIFIED** [H] |
| Migration pays back at ~25 customers (previous proposal) | **Retracted.** With extra ops hours and migration amortized, the BASE hypothesis crosses only at ~45–50 customers (§6) |

---

## 3. Storage architecture

### 3.1 Options compared

| Criterion | A. Shared volume (DB + filestore) — today | B. Separate DB and filestore volumes, unsold headroom, DB fence | C. Platform backup layer: WAL-G PITR + restic filestore snapshots + daily logical dump | D. S3 filestore (OCA `fs_attachment_s3`) + bucket quota | E1. ZFS dataset per environment (metal only) | E2. DO Managed PG per tenant |
| --- | --- | --- | --- | --- | --- | --- |
| Monthly storage cost per sold GiB [L/H] | DO $0.10; metal disk share | DO ~$0.12 (+20% headroom); metal disk share | Backups ≈ (2.6×DB + 1.2×files) × $0.014 (metal) to $0.027 (DOKS) per GiB, primary + off-site | Spaces $0.02 (5× cheaper than DO volumes); self-hosted quota store adds ops | Disk share; lz4 compression | $13–24/tenant minimum [L]; reject |
| Hard boundary | Volume (combined) | Each volume | n/a (backup layer) | Provider/bucket quota, often approximate | `refquota` per dataset | Plan size |
| Grow-only | Yes (DO volumes cannot shrink [L]) | Yes (DO; XFS/LVM cannot shrink) | n/a | Policy only | Policy only | Yes |
| At 100% | **Filestore growth can fill WAL space and stop PostgreSQL** | DB: fence makes the app role read-only at the sold limit; physical headroom protects WAL. Physical-full is last resort: PG may shut down, restart after expansion (**must test**). Files: upload fails, DB untouched | n/a | PUT rejected → attachment create should fail and roll back (**test per version**) | Like B | Provider-managed |
| WAL/temp/log headroom | None reserved | 20% (min 2 GiB) unsold + `temp_file_limit`; logs to stdout with rotation | WAL archived continuously, so retention is bounded | n/a | Like B | Provider |
| Cross-tenant isolation | Separate volume per environment | Same | Separate repos/prefixes; separate keys per tenant [H] | Bucket/key per tenant | Dataset per environment | Separate cluster |
| Attachment/SQL consistency | Native: file written before commit; orphans GC'd | Same | PITR to T + union of filestore snapshots around T (content-addressed files); **test** | Object before commit; orphans need GC | **Atomic snapshot of DB + files** | n/a |
| Backup/PITR consistency | Dump and tar not simultaneous | With C: consistent within tested window | PITR ≤15 min target (**UNVERIFIED**) | Versioning + object lock | Snapshots + `zfs send` (RPO minutes) | Provider PITR |
| Provider migration | Portable (dump + tar) | Portable | Portable (logical + restic) | Portable objects; export needs conversion to native | Metal-only format | Lock-in |
| Ransomware/deletion recovery | Only via off-site | Via C | **Immutable off-site** (B2 Object Lock), separate write-only credentials | Object lock | Snapshots on-host, plus off-site needed | Provider |
| Engineering / ongoing burden | None / **high incident risk** | Small–medium / low | Medium / low after automation | Medium–high, per version / medium | Medium / medium (ARC tuning, expertise); a second backup design | Low / costly |

### 3.2 Final preferred design: B + C, one design for DOKS and metal

**Database:**

- One PostgreSQL instance per environment (exists).
- Its own volume, sized sold DB allowance × 1.2 (min +2 GiB) [P].
- The platform fence sets `default_transaction_read_only` for the Odoo role and alerts at 100% of the sold allowance; alerts also fire at 80% and 90%.
- Growth is grow-only expansion through billing.

**Filestore:** native Odoo filestore on its own volume (sold allowance × 1.1).

**On metal:** the same two volumes become TopoLVM/LVM logical volumes. ZFS (E1) is evaluated only inside the metal qualification.

**Backups (C):**

- Daily logical dump + restic filestore snapshots to **two independent stores**: same-provider primary, plus Backblaze B2 with Object Lock (30 days) using separate write-only credentials.
- WAL-G continuous archiving is **required before any metal tenant and for the Business/Scale PITR promise**. It is not a DOKS pilot blocker, because DO network volumes survive node loss.
- Automated monthly restore verification of a random tenant into a scratch namespace, with row counts and attachment checksums.

**Customer-facing:** the bundle shows two itemized lines, Database GiB and Files GiB, each expandable and never reducible. This keeps the owner's separate DB/file subscriptions without new infrastructure.

**Removed permanently:**

- M14 trusted gateway/ledger and M10 external PG VMs with XFS project quotas: their outcomes are met by B + C.
- S3 filestore (D): not in pilot or Stage B. Reopen only if filestore bytes exceed ~40% of DOKS storage cost or a customer needs multi-replica Odoo.
- DO Managed PG per tenant (E2): too expensive per tenant [L].

---

## 4. Metal-cell design to validate (nothing accepted until tested)

**Design:**

- One dedicated server = one single-node k3s cluster registered as a `saas.server`.
- TopoLVM volumes (B), backups (C) and WAL-G.
- One spare server per location of the largest server size, holding no tenants; builds run there and are evicted on recovery.
- DNS per cell via the Cloudflare API.
- **Emergency recovery target:** the DOKS account (images in the registry; restore from off-site).

**Blast radius:**

- **One server** = all its tenants. At BASE density [H] that is ~6 customers on an AX42 (disk-bound) or ~17 on an AX102 (CPU-bound).
- **Two failures in one location** exceed one spare: restore by plan priority onto DOKS.
- **Location or provider-account loss** = all cells there: restore from off-site to DOKS or a second metal provider. RTO measured in hours (**UNVERIFIED**).

### Qualification tests

Each test needs a recorded command, result and timing.

| ID | Test | Pass criterion (to approve) |
| --- | --- | --- |
| Q1 | Kill server (power off) → restore all its tenants to the spare from WAL-G + restic | Every tenant's last acknowledged write ≤15 min old; all tenants serving within the RTO the owner sets after measurement |
| Q2 | Total disk loss (wipe both disks) | Same as Q1, from off-site only |
| Q3 | Spare capacity | Spare holds 100% of the largest server's tenants at guaranteed resources |
| Q4 | PITR one tenant to time T | DB at T; every attachment referenced at T present (checksums); other tenants untouched |
| Q5 | DNS switch and rollback | Cutover and rollback each ≤5 min; TLS valid |
| Q6 | DB volume: fence at the sold limit, then physical full | Fence engages; no corruption after expansion; neighbours unaffected |
| Q7 | Filestore volume full; inode exhaustion | Upload error, DB consistent; neighbours unaffected |
| Q8 | Online LV expansion while serving | No restart required, or a measured restart time |
| Q9 | Noisy neighbour: heavy import + report + backup in one tenant | Neighbours' p95 ≤ 1.3× their baseline; no OOM in neighbours |
| Q10 | Hostile custom module (fork bomb, memory bomb, disk fill, egress scan, metadata/private network probes) | Contained to the tenant; no cross-tenant read; egress blocked |
| Q11 | Host patch + reboot | Measured downtime per server, published as a maintenance window |
| Q12 | Provider incident drill (hardware ticket, abuse-lock simulation) | Runbook executed; restore path to DOKS works |
| Q13 | 14-day soak at target density | Founder ops hours recorded; no data-loss event |

---

## 5. Real-data test plan: DOKS vs metal

| Item | Plan |
| --- | --- |
| **Harness** | Locust (or k6) Odoo scenarios: login, list/search, quotation → confirm → invoice PDF, inventory move. Also 10k-line CSV import, batch PDF report, scheduled actions and mail queue, backup during load, Git build during load |
| **Datasets** | Odoo 18 with generated data: S ≈ 2 GB DB + 5 GB files; M ≈ 10 + 20 GB; L ≈ 40 + 80 GB [H sizes] |
| **Metrics** | p50/p95/p99 latency, error rate, per-worker RSS, container working set, OOM kills, CPU throttling, PostgreSQL (locks, temp files, WAL rate), disk IOPS/latency (fio baseline + node exporter), restore timings, founder hours |
| **Procedure** | (1) single-tenant baseline per profile; (2) density ramp on one node/server until p95 > 1.5× baseline, errors > 0.5% or any OOM — safe density = 80% of that point; (3) mixed-profile run; (4) noisy neighbour (Q9); (5) node drain/failure and restore; (6) cost per environment at safe density |
| **Targets** | DOKS s-8vcpu-16gb and 8 vCPU/32 GB AMD on the existing account (hourly billing; a few days). Metal: 2 × AX42-LTD for one month |
| **Cost** | Metal ≈ 2 × (€79 + €39 setup) ≈ €236 [L] — needs owner approval. DOKS test nodes are billed hourly; record the actual cost |
| **Output** | Replace every [H] density/RAM/CPU value in the model; rerun §1; decide §6 |

---

## 6. Economic trigger for migration

**Migrate Standard-tier tenants to metal only when all three hold:**

1. **Savings:** `S = D − M − H×r − X/12 ≥ max($300, 0.25 × D)` for two consecutive invoice months, where:

   | Term | Meaning | Source |
   | --- | --- | --- |
   | D | DOKS tenant infrastructure cost | DO invoice |
   | M | Projected metal cost for the same tenants at **measured** safe density, including spare, setup amortization and backups | §5 measurements |
   | H | Measured extra ops hours/month from the Q13 soak | Q13 |
   | r | Labour rate, $25/h | [H] |
   | X | One-time cell build + per-tenant moves | Recorded hours |

2. **Qualification:** Q1–Q13 passed and independently reviewed.
3. **Recovery target:** a DR target exists for the whole cell (spare + off-site + DOKS fallback).

**Separate pricing trigger:** if interviews show the market only pays below the DOKS 50%-margin floor for Starter, start §5 metal testing before scaling acquisition.

**Indicative, BASE [H], not a decision input:**

| Customers | Net saving S | Threshold |
| --- | --- | --- |
| 25 | +$58 | $300 |
| 40 | +$96 | $398 |
| 50 | **+$708** | $514 → crosses |

---

## 7. Pilot launch blockers (exact)

| # | Blocker | Exists today? |
| --- | --- | --- |
| L1 | Untracked P0 security work committed; CI on every push; CD requires green CI; branch protection | No |
| L2 | Memory fix: per-worker `limit_memory_soft/hard` from per-worker budget; requests = guaranteed profile (measured) | Defect present |
| L3 | P0.24, P0.1, P0.19 deployed and verified on DOKS; P0.3 per-tenant registry credentials; P0.20 separate tenant domain; egress policy (deny private/metadata/control plane; SMTP only via relay) | Code partly; not deployed |
| L4 | Storage B for new environments + DB fence + 80/90/100% alerts + grow-only expansion; remove `action_release_storage_block` | Operator ready; driver/billing not |
| L5 | Off-site immutable backups (logical + restic) with separate credentials; automated restore verification; documented RPO ≤24 h | No |
| L6 | DR drill on DOKS: restore one tenant from off-site only into a fresh namespace; measured RTO | No |
| L7 | Payment via merchant of record; bundles (two storage lines), setup fee, annual; terms, privacy, DPA, AUP, refund | No |
| L8 | Mail relay (Postfix → SES), per-tenant rate limit; Mailpit for non-prod | No |
| L9 | Custom domains (DNS verification + TLS); export download (DB + filestore + manifest); onboarding docs | No |
| L10 | Alertmanager → phone; external uptime; status page; incident runbook | No |
| L11 | Odoo 17/18/19 pinned images pass smoke matrix (init, install/update, PDF, cron, backup/restore, staging copy) | No |
| L12 | DOKS baseline measurements (§5 steps 1–4 for S and M) feeding final requests and prices | No |

**Not blockers for the pilot:** WAL-G PITR (needed for metal and the Business PITR promise), metal cell, sleep, S3, M11 state machine, BYOS.

---

## 8. Commercial plan

**Pricing:**

- Starter/Business/Scale with two storage lines, a $149 setup fee and annual = 10 months.
- Add-ons: extra environment $19, extra worker $15, storage per GiB, paid support blocks, paid version-upgrade projects.
- **List prices are tested, not decided:** Starter at $49/$59/$79 in interviews.
- On DOKS, Starter at $59 is 44% gross margin at 50 customers. Founding $49 (max 10 customers, 12 months) is accepted as validation cost.

**Evidence ledger:** keep three separate totals, never merged.

| Prospect | Segment (partner/agency/SMB) | Interview date | Stated willingness to pay | Commitment (LOI / pre-payment, amount) | Recognized revenue (invoiced and paid, per month) |
| --- | --- | --- | --- | --- | --- |

**Segment:** partners and agencies are the highest-margin segment in the model (§1.4: 78 vs 501 customers for $4k) because of lower support and churn and multiple projects. Prove it with the ledger: target ≥10 of 15 interviews with partners.

**Lever sizes** (metal, $4k founder-operated, from 100 BASE):

| Lever | Customers needed |
| --- | --- |
| Strict support boundary | 76 |
| Prices +20% | 65 |
| No add-ons | 147 |
| Partners + strict boundary | 61 |

- **Setup fee and annual contracts:** roughly cancel each other in the model. Annual's churn reduction is not modelled; measure it.
- **Paid upgrades:** e.g. 20% of customers per year × $500 [H] ≈ $8/customer-month revenue for ≈0.17 h labour. A small but positive add-on.

**When the founder can be paid sustainably (BASE, metal):**

- **$2,000:** at ~43 customers, after tax, 5% reserve and 10% reinvestment.
- **$4,000:** at ~100 customers.
- **Rule [P]:** pay a salary only after 3 consecutive months of distributable cash ≥ 0 at that salary.

---

## 9. Revised owner decisions (P1–P15)

| # | Decision | Status requested |
| --- | --- | --- |
| P1 | DOKS is the reference and pilot runtime; per-environment PostgreSQL pod stays | Approve |
| P2 | Remove M14 (gateway/ledger) and M10 (external PG VMs, XFS quotas) permanently; S3 filestore not in pilot/Stage B, reopen only on the §3.2 trigger | Approve |
| P3 | Remove sleep; price non-production environments | Approve |
| P4 | Pilot versions 17/18/19; 20 after qualification; 14–16 later as paid legacy | Approve |
| P5 | Metal: approve only the §5 test budget (≈€236 + DOKS test hours); migration solely by §6 trigger | Approve budget |
| P6 | Storage design B + C; customer bundle shows Database GiB and Files GiB lines | Approve |
| P7 | BYOS deferred; if revived, single-node k3s with the same operator, not a Compose driver | Approve |
| P8 | Pricing tested at Starter $49/$59/$79; founding $49 max 10 customers × 12 months; setup $149; annual = 10 months | Approve for testing |
| P9 | Pilot threat model: custom code only from contracted, identified customers; dedicated node for higher isolation | Approve |
| P10 | Support boundary: platform support included; Odoo functional/custom-code work is paid | Approve (largest lever) |
| P11 | Merchant of record unless your entity can use Stripe | Choose |
| P12 | Control plane, platform DB and off-site backups at a different provider from tenant cells | Approve |
| P13 | This is the single Managed plan; after approval its status changes from PROPOSED to ACTIVE, with no new strategy documents | Approve |
| P14 | Partner-first go-to-market (≥10 of 15 interviews with partners/agencies) | Approve |
| P15 | Financial policy: 5% reserve + 10% reinvestment; salary only after 3 months of distributable cash ≥ 0; confirm tax rate with an accountant | Approve |

---

## 10. Engineering backlog for Codex

In order. Each phase ships with tests and a runbook entry; **no phase deploys to real customers before its acceptance passes.**

| Phase | Scope (main files) | Acceptance criteria |
| --- | --- | --- |
| **0. Repo and CI/CD** | Commit P0.24/P0.1/P0.19 work; `.github/workflows/ci.yml` on push/PR; `cd.yml` needs CI; branch protection | CI green on main; a failing test blocks deploy (demonstrated) |
| **1. Memory model** | `deployment.go odooMemoryLimitArgs` → per-worker budget = (limit − cron/master overhead) ÷ workers; `saas_plan._package()` requests = guaranteed profile; profiles S/M/L/dev | Unit tests for 1/2/4/8 workers; load test (§5) with no pod OOM before an Odoo worker recycle; requests ≥ measured p99 working set × 1.2 |
| **2. Security deploy** | Deploy P0.24/P0.1/P0.19 to test DOKS; P0.3 per-tenant registry secret; P0.20 tenant domain; `networkpolicy.go` egress deny private/metadata/control plane, SMTP only to relay | `scripts/security/*` checks pass live; cross-tenant pull denied; egress negative tests pass on the real CNI |
| **3. Storage B + fence** | Driver `sharedWithDatabase: False` for new environments; DB size = sold × 1.2 (min +2 GiB); fence job (read-only role at sold limit) + alerts; billing two lines, grow-only expansion API; remove `action_release_storage_block` and overage paths | Q6/Q7 equivalents pass on DOKS; shrink rejected via API/UI/billing; expansion applies online or with a recorded restart time; no cross-tenant effect |
| **4. Backups v2 + export** | Backup tool: restic for filestore, daily dump, B2 Object Lock off-site with separate write-only key; restore tool (tenant-scoped); monthly automated verification; portal export download | Restore of a random tenant from off-site only passes checksums; deleting the primary bucket does not affect off-site; export starts locally on native Odoo |
| **5. Mail relay** | Postfix relay → SES; per-tenant SMTP credentials and rate limits; Mailpit for non-prod; NetworkPolicy allows only the relay | Production tenant sends; non-prod is captured; rate limit and manual pause work |
| **6. Domains and onboarding** | Custom-domain verification, cert-manager TLS, onboarding docs (EN/AR) | 3 external testers deploy from Git in <30 min |
| **7. Billing, MoR, legal** | MoR provider module; plan bundles, setup fee, annual; legal pages | Real charge → renewal → failed payment → grace → suspend → reactivate on the MoR sandbox, then one live charge |
| **8. Monitoring and incidents** | Enable Alertmanager; external uptime; status page; runbooks | Induced failure pages the phone in ≤5 min; runbook followed in a drill |
| **9. Version qualification** | Pinned 17/18/19 images; smoke matrix in CI | All matrix rows green per version |
| **10. Measurement** | Locust harness, datasets, metrics dashboard; cost reconciliation script vs DO invoice | §5 steps 1–4 on DOKS recorded; model rerun with measured values; reconciliation within 15% of invoice |
| **11. DR drill → Pilot Gate** | Full tenant restore from off-site into a fresh namespace/cluster | Measured RTO published; L1–L12 all evidenced → owner signs the pilot gate |
| 12. WAL-G PITR | WAL-G sidecar/archiving; PITR restore tool; filestore snapshot union restore | Q4 passes on DOKS; RPO ≤15 min measured |
| 13. Metal qualification (approved budget only) | Single-node k3s cell blueprint, TopoLVM, Cloudflare DNS, spare server, registration as cluster | Q1–Q13 pass; model rerun; §6 evaluated |
| 14. Cell production + tenant moves (trigger only) | Cross-cluster move tool (backup → restore → DNS switch → keep source until confirmed) | Moves with measured downtime in customer-chosen windows; zero checksum mismatches; source retained until sign-off |

---

## 11. Owner approval checklist

- [ ] §1 model definitions and policies accepted (5% reserve, 10% reinvestment, salary rule, tax rate to confirm)
- [ ] P1 DOKS reference/pilot · P2 remove M14/M10, defer S3 · P3 remove sleep · P4 versions 17/18/19
- [ ] P5 metal test budget (≈€236 + DOKS test hours), migration only by the §6 trigger
- [ ] P6 storage B + C with two storage lines
- [ ] P7 BYOS deferred (k3s if revived)
- [ ] P8 pricing test grid · P10 support boundary · P14 partner-first
- [ ] P9 pilot threat model · P11 payment route/entity · P12 provider separation · P15 financial policy
- [ ] Pilot blockers L1–L12 are the launch definition
- [ ] Codex backlog phases 0–11 authorized; 12–14 conditional
- [ ] After approval: mark this plan ACTIVE; no new strategy documents

---

## Appendix A — test commands

From the repository root, with Helm on PATH (`HELM_BIN` can select its binary):

```bash
python3 -m unittest discover -s scripts/ci -p 'test_*.py' -v
python3 -m unittest discover -s compute/tools/backup-tool -p 'test_*.py' -v
python3 -m unittest discover -s compute/operator/test -p 'test_database_roles*.py' -v
helm lint compute/charts/odoo-operator
helm template odoo-operator compute/charts/odoo-operator --namespace odoo-system
helm template odoo-operator compute/charts/odoo-operator --namespace odoo-system \
  --set controlPlaneRBAC.enabled=false --set buildPoolIsolation.enabled=false
sh -n control-plane/saas_core/templates/build/build.sh
bash -n compute/tools/backup-tool/run-restore.sh
git diff --check
```

Inside `compute/operator`:

```bash
go test ./internal/resources ./api/...
go vet ./...
make test ENVTEST_K8S_VERSION=1.30.0
```

**How to run them:**

- Run the Odoo test suite against a throwaway database on the SaaS server; CI documents the full invocation.
- Envtest needs permitted socket binding.
- After `make test`, inspect the regenerated CRD/RBAC files.
- Enabled chart guards require Kubernetes ≥1.30.

**How to record results:** record pass/fail/blocked counts per phase. A test that could not start is blocked, not passed.

