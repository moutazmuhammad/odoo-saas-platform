# Production-Readiness Plan

> Status: **active plan**, repo root scope (all three components). This is
> the master roadmap; it incorporates
> [`control-plane/docs/architecture/MICROSERVICES-PLAN.md`](../control-plane/docs/architecture/MICROSERVICES-PLAN.md)
> as **Phase D** rather than duplicating it, and treats
> [`control-plane/docs/reviews/`](../control-plane/docs/reviews/) as the
> historical source of the security/architecture/business/performance/UX
> findings referenced throughout.
>
> Written for a fresh Claude Code session to pick up cold: every phase has an
> ID, concrete file references, acceptance criteria, and a rollback note.
> Update **§11 Progress Log** after every completed step.

---

## Production-First Principle

**Every phase, task, and implementation decision in this plan — and in
`MICROSERVICES-PLAN.md`'s Phase D — is production work, not a prototype or a
proof of concept.** This is not a separate phase to schedule; it is the
standard every other section's acceptance criteria are evaluated against.
Nothing in this plan is "done" if it only satisfies its own phase's
checklist but fails this one.

Concretely, for every change made under this plan:

- **No unused, dead, duplicated, or unnecessary code survives a change that
  touches it.** If a step in Phase A–H touches a file, that file leaves the
  change cleaner than it was found — this is how `saas_instance.py` (12,552
  lines / 293 methods, per §1) actually gets smaller over time instead of
  growing indefinitely: not a dedicated "decomposition phase" that never
  gets scheduled, but a standing rule applied every time the file is
  touched for any other reason.
- **Clean code, maintainability, security, scalability, and reliability are
  requirements, not preferences.** A change that "works" but leaves a
  God-method longer, a query without an index (§ PERF-007 in the old
  audit), or a route with no input validation is not complete.
- **The algorithm/data structure must fit the actual requirement and scale
  — chosen deliberately, not inherited by default.** Example already in
  this codebase: `_allocate_docker_server`/`_allocate_db_server` do no real
  bin-packing today; when Phase D.6 replaces placement logic for
  Kubernetes-backed tenants, the replacement must be chosen for the actual
  expected tenant/region scale (documented in the PR), not carried over
  unexamined just because it compiles.
- **No temporary workarounds or shortcuts that would need replacing later.**
  If a step can only be done with a shortcut today, the shortcut and the
  condition under which it must be revisited are written down explicitly
  in that step's acceptance criteria (see, e.g., `MICROSERVICES-PLAN.md`
  §11's open questions) — never left as silent, undocumented debt.
- **Proper error handling, input validation, logging/observability, and
  security are part of the change, not a follow-up.** A new route (Phase
  B.1) ships with validated input and meaningful error responses in the
  same PR as its test, not "add validation later."
- **Every change is reviewed** for correctness, performance,
  maintainability, and production-readiness — not just "does it pass CI."
- **Every change is thoroughly tested** — unit, integration, and
  end-to-end where applicable — per Phase B's own conventions (real
  `HttpCase`/`url_open` calls, real envtest/live-cluster runs for the
  operator, real component tests for the SPA). "Unit-tested manifest
  strings" (the exact failure mode that described the old `KubernetesDriver`
  stub before this session's work) is explicitly **not** sufficient
  evidence of correctness anywhere in this plan.
- **Integration is verified, not assumed.** Per the integration-status
  warning in §1: two components sitting in the same repo, or even
  compiling/passing their own tests independently, is not integration.
  A cross-component claim ("the Control Plane can provision a tenant via
  the Compute Service") is only true once demonstrated end-to-end against
  real infrastructure, per `MICROSERVICES-PLAN.md`'s own acceptance
  criteria.

### Definition of Done (applies to every task in every phase)

A task is not complete unless **all** of the following are true — this list
is the actual gate, more specific than "tests pass":

1. No dead/unused/duplicated code remains in any file the task touched.
2. Error handling, input validation, and logging are present for every new
   or changed code path (not just the happy path).
3. The change was reviewed against correctness, performance, and
   maintainability — not just functional behavior.
4. It is covered by real tests at the appropriate level(s) — unit,
   integration, and end-to-end where applicable — and those tests were
   actually run, with the output checked, not assumed green.
5. Any cross-component behavior it claims was verified end-to-end against
   real infrastructure, not asserted from reading the code.
6. No new temporary workaround was introduced without an explicit, written
   condition for when it must be revisited.

> **A general testing rule this plan's own work surfaced (2026-09-14,
> B.1.3): any route/method that ends in `saas.job._enqueue(...)` must have
> its success path tested at the model layer (`TransactionCase`, with
> `saas.job._spawn_worker` patched to a no-op — the established pattern in
> `test_job_queue.py`/`test_compute_driver.py`), never driven through a
> live `HttpCase` HTTP request.** The suppression that reliably prevents
> the real background worker thread under `TransactionCase` does not
> reliably hold under `HttpCase` — a real attempt corrupted unrelated
> tests in the same class (see B.1.3's progress-log entries). This is not
> a one-off gotcha to route around per-test; treat it as a standing
> constraint on how every future `_enqueue`-backed route gets tested.

Phase H's go-live gate (§9) is this same Definition of Done applied to the
whole platform, plus the phase-specific criteria already listed there.

---

## 0. How this plan was built

Before writing this, four research passes were run against the actual
current repo state (not assumptions carried over from older docs):
1. Architecture-evolution docs (`control-plane/docs/architecture/*`) — fed
   into `MICROSERVICES-PLAN.md`, not repeated here.
2. Audit reports (`control-plane/docs/reviews/*`) — fed into
   `MICROSERVICES-PLAN.md`'s Phase 0 and this plan's Phase C/E.
3. **Dependency/dead-code/hygiene audit** (fresh, this session) — results in
   §1.1 below.
4. **Feature-to-test-coverage map** (fresh, this session) — results in §1.2
   below.

## 1. Current state snapshot (as of this review)

> ⚠️ **`control-plane` and `compute` are not integrated. At all. Today.**
> They were developed independently, moved into this monorepo as two
> untouched folders, and have never exchanged a request. The existing
> `saas_core/drivers/kubernetes_driver.py` stub does not use the
> `OdooInstance` CRD and has never run against a live cluster; every
> `OdooInstance` ever created in `compute/operator`'s history was applied
> by hand (`kubectl apply`) for testing the operator in isolation, never
> triggered by `saas_core`. Building that first connection is Phase D
> (= `MICROSERVICES-PLAN.md` Phase 2) — see that document's §0 for the full
> warning. Nothing below in this snapshot should be read as implying any
> cross-component wiring exists yet.

### 1.1 Dependencies, dead code, hygiene

| Area | Finding |
|---|---|
| `control-plane` debug/dead code | **Clean.** Zero `TODO`/`FIXME`/`XXX`/`HACK` in `saas_core`/`saas_website` models/controllers. No hardcoded secrets, no `.env` files, no commented-out dead blocks. |
| `debug_otp` (was SEC-001, critical) | **Already fixed.** Zero hits outside 3 regression-test assertions (`test_security_billing_fixes.py:270,275,293`) that actively guard it stays removed. No action needed — just don't regress it. |
| `control-plane` Python deps | **Unpinned.** `saas_core/__manifest__.py` lists `paramiko, jinja2, boto3, google-cloud-storage` by name only; no `requirements.txt` anywhere; CI installs them unpinned. Reproducibility gap — a breaking upstream release can silently change CI/prod behavior between runs. |
| `compute/operator` Go deps | **Clean and current.** Direct deps (`k8s.io/*` v0.37.0, `controller-runtime` v0.25.0, `gateway-api` v1.6.2) are current; only indirect deps are a minor/patch behind. Zero `TODO`/`panic()`-outside-tests/hardcoded credentials. |
| `frontend/veltnex` deps | **Stale — every direct dependency is ≥1 major behind.** Vite is 3 majors behind (5.4→8.3) and is the one blocking a clean `npm audit fix`; React 18→19, TypeScript 5→7, react-router-dom 6→7, Tailwind 3→4, three.js 26 minors behind. `npm audit`: **11 vulnerabilities (4 high, 6 moderate, 1 low)** — high-severity in `browserslist` (DoS), `nanoid` (infinite loop), `postcss` (arbitrary file disclosure via source maps). |
| Repo hygiene | 4 empty scaffolding directories (`compute/deploy`, `compute/operator/internal/{database,network}`, `compute/operator/test/e2e`) — harmless, never populated. No stray `.orig`/`.rej` files, no duplicate lockfiles, no unexpectedly large tracked files. |

### 1.2 Feature-to-test-coverage gaps

| Area | Finding |
|---|---|
| **Controller/HTTP test coverage** | **Corrected 2026-09-14** (the original research pass undercounted — its regex missed `self.url_open(`/`self._call(` calls whose route argument was split across lines, which is how most of them are actually written): **156** total routes across `saas_website/controllers/*` + `saas_core/controllers/*` (terminal, container logs, webhook). **10 already have real HTTP-level coverage** in 3 `HttpCase` classes (`TestWebhookSecurity`, `TestApiSecurityHttp`, `TestOrderControllerFixes`) — and they're exactly the highest-risk ones: login, register/start+resend, reset/start+verify, `instances/<id>` read/action/environments, `hosting/order` (partial), webhook. **146 remain genuinely uncovered** — still a real, large gap, just not "effectively zero." See B.1 below for the corrected, precise remaining list. |
| Zero-test-coverage models | `saas_payment.py`, `res_config_settings.py`, `saas_instance_package.py`, **`saas_terminal_session.py`** (backs the in-browser SSH terminal feature — `ShellConsole.tsx` on the frontend has no backend model test at all). |
| Untested cron jobs | Most of the billing-lifecycle and live-metrics crons in `saas_instance.py` have no test referencing them at all: `_cron_check_overdue_invoices`, `_cron_retry_failed_payments`, `_cron_send_renewal_reminders`, `_cron_apply_paid_pending_changes`, `_cron_check_trial_expiry`, `_cron_renew_daily_backup_addons`, `_cron_verify_webhooks`, `_cron_check_container_health`, `_cron_sample_live_metrics`, `_cron_record_metrics`; plus core provisioning methods `_pg_clone_db`, `_provision_nginx`, `_clone_product_repos`. |
| `compute/operator` test coverage | **Solid, no gap.** Every file in `internal/resources/` and `internal/controller/` is covered by its package's test file(s); envtest exercises real controller-runtime reconciliation. |
| `frontend` test coverage | Typecheck (`tsc --noEmit`) runs in CI; **no component/unit test runner exists at all** (no Vitest/Jest/RTL) — every page/component is verified only by TypeScript compiling, never by behavior. |
| Frontend/backend contract | No mismatch found in the routes/fields sampled. One discoverability nit: the SSH-terminal/log-streaming/webhook routes live in `saas_core/controllers/`, not `saas_website/controllers/` where the rest of the HTTP surface lives — inconsistent module boundary, not a bug. |

---

## 2. Phase A — Repository hygiene & dependency health

Cheap, low-risk, do first — nothing here touches business logic.

- **A.1** Add `control-plane/requirements.txt` pinning `paramiko`, `jinja2`,
  `boto3`, `google-cloud-storage` to the exact versions currently installed
  in any working dev/CI environment (capture with `pip freeze` from a
  known-good run); update `.github/workflows/ci.yml`'s `pip install` step
  and `control-plane/scripts/devctl.sh`'s setup instructions to install from
  this file instead of a bare package list.
  **Acceptance:** CI installs from `requirements.txt`; a second CI run
  produces byte-identical dependency versions to the first.
- **A.2** Frontend security patches: `cd frontend/veltnex && npm audit fix`
  (non-breaking fixes only) — re-run `npm audit` and confirm the
  vite-blocked esbuild advisory is the only one left, and document it as
  "requires the Vite 8 major bump, tracked separately" rather than silently
  ignored.
- **A.3** Frontend major-version upgrades — **do NOT bundle into one PR**.
  One dependency family per step, each with `npm run build` (typecheck +
  vite build) green and a manual smoke-test of the affected pages:
  - A.3.1 TypeScript 5→7 (smallest blast radius, do first — surfaces any
    stricter-type issues before touching runtime behavior).
  - A.3.2 Vite 5→8 + `@vitejs/plugin-react` (build tool; closes the last
    npm-audit high).
  - A.3.3 Tailwind 3→4 (has known breaking config-format changes — expect
    to touch `tailwind.config.js`).
  - A.3.4 react-router-dom 6→7 (check every route definition against its
    migration guide — this project has ~27 pages routed).
  - A.3.5 React 18→19 (last, highest blast radius — do only after A.3.1-4
    are stable, since React 19 changes interact with all of the above).
  - ~~A.3.6 `three`/`@react-three/fiber` (isolated to `Home.tsx`'s globe —
    can happen anytime, independent of the above chain).~~ **Wrong,
    corrected in the progress log**: `@react-three/fiber` peer-depends on
    a specific React major range and turned out to be tightly coupled to
    A.3.5, not independent — the two were done together. Left here
    struck through rather than silently deleted, so the original
    (incorrect) assumption is visible alongside the correction.
  **Acceptance per step:** `npm run build` green, affected pages
  manually verified in a browser (per this project's own existing
  practice — see `docs/architecture/*` "Headless-Chrome verified" entries),
  no new `npm audit` findings introduced.
- **A.4** Remove or populate-with-a-README-stub the 4 empty scaffolding
  directories found in §1.1 — either delete them (git doesn't track empty
  dirs anyway, so this is purely about not confusing the next person who
  finds them via `find`) or add a one-line `.gitkeep`-style README stating
  what's expected to land there and when (e.g.
  `compute/operator/test/e2e/README.md`: "Reserved for Phase D live-cluster
  e2e tests — see MICROSERVICES-PLAN.md §10").

**Rollback:** every step here is additive or isolated to one dependency
family; revert the single offending commit if a build breaks.

---

## 3. Phase B — Close the test-coverage gaps

This is the highest-leverage phase before anything else touches
`saas_core`/`saas_website` business logic — per this project's own stated
discipline ("run the test suite before moving on"), that discipline is only
as good as what the suite actually exercises. §1.1's correction applies
here: some real HTTP-level coverage already exists (10 of 156 routes,
`saas_core/tests/test_security_billing_fixes.py`'s `TestApiSecurityHttp` +
`test_order_eligibility_fixes.py` + `test_webhook_security.py`) — don't
duplicate it. Check current coverage with the route-vs-`url_open` cross
check below before adding a test for anything, since the naive grep that
undercounted it once can undercount it again.

```bash
# Re-run this before starting any B.1.x sub-step to get the current
# precise covered/uncovered list (route declarations vs url_open/_call
# call sites, both normalized to strip dynamic path segments):
python3 - <<'PY'
import re, glob
routes = []
for f in glob.glob('saas_website/controllers/*.py') + glob.glob('saas_core/controllers/*.py'):
    src = open(f).read()
    for m in re.finditer(r"@http\.route\(\s*(\[[^\]]+\]|['\"][^'\"]+['\"])", src):
        for p in re.findall(r"['\"]([^'\"]+)['\"]", m.group(1)):
            routes.append((re.sub(r'<[^>]+>', '*', p), p, f))
covered = set()
for f in glob.glob('saas_core/tests/test_*.py'):
    src = open(f).read()
    for pat in (r"url_open\(\s*\n?\s*(?:route\s*,|['\"]([^'\"]+)['\"])",
                r"_call\(\s*\n?\s*['\"]([^'\"]+)['\"]"):
        for m in re.finditer(pat, src):
            if m.groups() and m.group(1):
                covered.add(re.sub(r'%s|%d|<[^>]+>', '*', m.group(1)))
uncovered = sorted({raw for norm, raw, f in routes if norm not in covered})
print(f"{len(routes)} declared, {len(uncovered)} uncovered:")
for r in uncovered: print(" ", r)
PY
```

(Run from `control-plane/`. As of 2026-09-14, right after B.1.1: 156
declared, 143 uncovered — B.1.1's 3 new tests closed `register/verify` and
`logout`, the two genuine auth-family gaps this script found; login,
register/start+resend, reset/start+verify were already covered.)

- **B.1** Add `HttpCase`-based tests for the highest-risk routes first, in
  this order (risk = revenue/data impact if broken, not code size).
  **Re-run the script above at the start of each sub-step** — the list
  below was accurate for B.1.1 but may drift as work proceeds:
  - B.1.1 `auth/*` (login/logout/register/reset) in `api.py` — **done**,
    see the progress log. Only `register/verify` (the actual signup
    completion — distinct from `register/start`/`resend`, which only send
    the code) and `logout` had no HTTP-level test; both do now.
  - B.1.2 `hosting/order` (only partially covered — the existing test hits
    one trial-rejection edge case, not the general happy path) and
    `services/calculate*`/`hosting/calculate*` (uncovered) — checkout path,
    real money.
  - B.1.3 `instances/*` nested families — **partial, see progress log**:
    done: `databases/{create,drop,duplicate}` auth-boundary (token refused)
    at the HTTP layer, and **now also their success/rejection paths at the
    model layer** (`TestDbOpViaQueue` in `test_job_queue.py`, per the
    `TransactionCase`-not-`HttpCase` rule this plan captured), plus
    `environments/{reserve,release}` full success+rejection paths (pure
    billing logic, no infra needed) and `databases/upgrade` **still
    untested**. The reusable compute-layer mocks exist in two places now:
    `_mock_db_ops_infra` (`test_security_billing_fixes.py`, HTTP-layer,
    auth/rejection-only paths) and `_mock_hosting_db_list`
    (`test_job_queue.py`'s `TestDbOpViaQueue`, model-layer, success paths).
    **Non-obvious finding worth carrying into any future `databases/*`
    work**: `create` is not like `drop`/`duplicate` — it deliberately does
    **not** use `saas.job._enqueue` at all (its own code comment: SEC-002,
    the queue would persist the new DB's plaintext admin password to a
    durable row) and instead uses the older `run_in_background()` utility,
    which needs its own, differently-shaped mock (patch
    `run_in_background` itself, not `_spawn_worker`) and a positive
    assertion that no `saas.job` row was created (a row existing would
    itself be the regression). Check which mechanism a route actually uses
    before assuming either pattern applies. **Still open**, in priority
    order:
    - `databases/upgrade` (module upgrade) and a thin HTTP-layer smoke
      test for `create`/`drop`/`duplicate` (mock the model method itself
      and assert the route calls it with the right args, rather than
      letting the real async chain run inside `HttpCase` — confirms the
      route/auth/param-wiring layer without re-triggering the hazard
      above).
    - `backups/*` (`create`, `<id>/restore`) — check whether these also
      end in `_enqueue`; if so, apply the same TransactionCase-not-HttpCase
      rule immediately rather than rediscovering the hazard.
    - `environments/create` and `environments/merge` — not yet covered;
      `create` may also touch billing/payment provider mocking similar to
      `hosting/order`'s paid path.
    - `metrics/*`, `packages`, `repo`, `sql`, `storage/*`, `auto-renew`,
      `invoice/cancel`, `daily-backup/enable`, `builds`, `branches` — not
      yet investigated at all; check each for real infra dependencies AND
      whether they enqueue a job before writing tests, same discipline as
      above.
  - B.1.4 `webhook.py` — **done**, see progress log. Extended
    `test_webhook_security.py` from 2 tests (unsigned/unknown-secret
    indistinguishability, rate limiting) to 10: SHA-1 downgrade rejection,
    invalid JSON body, non-push event, branch mismatch, instance-not-running,
    repo-not-cloned, duplicate-delivery idempotency, and the successful-push
    path (build record fields + `_enqueue` call args). The last two patch
    `saas.job._enqueue` itself, per the standing rule above.
  - B.1.5 (new, not in the original plan) `saas_website/controllers/spa.py`
    and `portal.py` — the QWeb/form-post surface (`/my/instances/*`,
    ~90 routes) is entirely uncovered and wasn't broken out separately
    before; it's a distinct testing style (form posts + redirects, not
    JSON-RPC) from `api.py`'s routes, so budget it as its own sub-step
    rather than folding it into B.1.3. **In progress, see progress log** —
    first slice done: created `saas_website/tests/` (didn't exist before
    at all) and `TestPortalInstanceSecurity`, covering the
    `_document_check_access` ownership boundary (shared by every route in
    the file) and the restart/stop/start self-service actions (state
    guards, overdue-invoice block, success paths). Still open: `databases/*`
    and `databases/upgrade_module` at the portal layer, `checkout`/
    `subscribe`/`change_plan` (money paths), backup `create`/`restore`/
    `download`/`discard`, repo `update`/`remove`/`pull`, folder CRUD, and
    `spa.py` entirely.
- **B.2** Add test files for the 4 zero-coverage models: `saas_payment.py`,
  `res_config_settings.py`, `saas_instance_package.py`, and
  **`saas_terminal_session.py`** — prioritize the terminal one, since it's
  the backend for a feature (`ShellConsole.tsx`) that will also be the
  Phase D.4.3 migration target (SSH-exec → Kubernetes `pods/exec`) — you
  want a real regression baseline *before* changing its transport, not
  after.
- **B.3** Add tests for the untested cron methods listed in §1.2, grouped by
  what they actually risk if silently broken: billing-lifecycle crons
  (`_cron_check_overdue_invoices`, `_cron_retry_failed_payments`,
  `_cron_send_renewal_reminders`, `_cron_apply_paid_pending_changes`,
  `_cron_check_trial_expiry`) first (direct revenue impact), then
  operational crons (`_cron_renew_daily_backup_addons`,
  `_cron_verify_webhooks`, `_cron_check_container_health`,
  `_cron_sample_live_metrics`, `_cron_record_metrics`), then the
  provisioning helpers (`_pg_clone_db`, `_provision_nginx`,
  `_clone_product_repos` — note these will be superseded by Phase D
  anyway, so a thin test proving current behavior is enough; don't
  over-invest here).
- **B.4** Stand up a frontend test runner (Vitest + React Testing Library —
  the natural fit for a Vite project already on CI). Start with the pages
  identified in the feature-coverage report as calling the highest-risk
  endpoints (`Databases.tsx`, `Environments.tsx`, `ShellConsole.tsx`,
  `SqlConsole.tsx`) — component-level tests that mock `api.ts`, not full
  e2e. Wire into `.github/workflows/ci.yml`'s existing `spa` job.
- **B.5** Add a coverage report (Odoo's `coverage.py` integration, or the
  simplest thing that reports a number in CI) so future regressions in
  *coverage* (not just test failures) are visible, even if you don't gate
  on a hard threshold yet.

**Acceptance for the whole phase:** `.github/workflows/ci.yml`'s
`odoo-tests` job exercises at least the B.1 routes via real
`url_open`/`HttpCase` calls (verifiable by temporarily breaking one route's
auth check and confirming the specific new test — not just an unrelated
ORM test — fails); the 4 zero-coverage models each have ≥1 test; the
`compute` and `spa` CI jobs are joined by a real frontend test step.

**Rollback:** test-only changes; nothing here can break production, only
CI red/green.

---

## 4. Phase C — Close remaining security/compliance items

Per `control-plane/docs/reviews/SECURITY_AUDIT.md` and `REMEDIATION_PLAN.md`,
cross-checked against the fresh review in §1.1:

- **C.1** Confirm the status of every audit SEC-00x item against current
  code (not the audit's original snapshot — several were already fixed per
  `REMEDIATION_PLAN.md`'s own checkboxes, and `debug_otp` (SEC-001) is
  independently reconfirmed fixed by this session's review). Produce a
  one-line-per-item status table as this step's deliverable, so the audit
  document stops being the only source of truth on what's actually closed.
- **C.2** Rotate the root SSH password that was pasted into chat
  (`control-plane/SESSION_NOTES.md`, "Live server" section) — this is a
  real, still-open credential exposure regardless of anything else in this
  plan.
- **C.3** Add automated secret-scanning to CI (e.g. `gitleaks` as a new CI
  job) — proactive guard, direct response to the SSH-password-in-chat and
  the prior `SECURITY-INCIDENT-2026-06-17.md` compromise, so a future
  leaked credential is caught at PR time instead of discovered after an
  incident.
- **C.4** Verify tenant DB `CREATEDB` grant (SEC-004) status for any
  remaining SSH/Docker-driven tenants — note this becomes structurally moot
  per-tenant as each one migrates to the Compute Service in Phase D (each
  tenant gets its own isolated Postgres instance there, so a broad grant on
  a shared server is no longer the relevant risk model), but do not treat
  Phase D as an excuse to defer it for tenants that stay on the legacy path
  longer than expected.

**Acceptance:** a re-run of `SECURITY_AUDIT.md`'s checklist shows every
Critical item either closed-with-evidence or explicitly tracked with an
owner and phase; `gitleaks` (or equivalent) is green on the current repo
and blocks new leaks in CI going forward.

---

## 5. Phase D — Compute-layer migration

**This phase is fully specified in
[`control-plane/docs/architecture/MICROSERVICES-PLAN.md`](../control-plane/docs/architecture/MICROSERVICES-PLAN.md)
— do not duplicate it here.** Summary for this master timeline's sake:
Phase 0 (security gate, overlaps with this plan's Phase C) → Phase 1 (stand
up the Compute microservice for real) → Phase 2 (real `KubernetesDriver`
behind the existing seam) → Phase 3 (backup/restore cutover) → Phase 4
(frontend/API gateway verification + SSH-terminal transport swap — depends
on this plan's B.2 terminal-session tests existing first) → Phase 5
(observability) → Phase 6 (multi-region) → Phase 7 (decommission legacy
path).

**Sequencing note:** Phase D.2's tenant-by-tenant cutover should not start
until this plan's Phase B (test coverage) is at least far enough along that
a regression in a migrated tenant's behavior would actually be caught —
otherwise the migration is validated only by manual spot-checks, which is
exactly the failure mode the prior `KubernetesDriver` stub already
demonstrated ("unit-tested manifest strings... NOT YET run against a live
cluster").

---

## 6. Phase E — Feature completeness

From `control-plane/docs/reviews/UX_AUDIT.md` and `BUSINESS_AUDIT.md`,
scoped to what's realistic to build once the platform underneath is stable
(i.e., sequence this after Phase D has at least one region migrated, so new
features are built against the target architecture, not the legacy one):

- **E.1** Teams / RBAC beyond the single-owner-per-account model (UX-002).
- **E.2** Deploy rollback — a natural extension once Phase D's immutable
  per-tenant images exist (rollback = redeploy a prior SHA, which per
  `AS-BUILT.md` is explicitly *not possible* on the legacy hybrid deploy
  mechanism — this is a capability Phase D unlocks, not one available
  today).
- **E.3** Alerting (UX-007) — build on Phase D.5's Prometheus/Grafana
  stack rather than inventing an Odoo-native alerting path.
- **E.4** Self-service custom domains/SSL (UX-008) — needs the Compute
  Service's `spec.domain`/cert-manager integration (already built in the
  operator) exposed through the Control Plane API; mostly wiring, not new
  infrastructure.
- **E.5** Audit log (UX-009) — `saas_audit_log.py` already exists per the
  code inventory; assess whether it needs extending to cover the new
  Compute-Service-driven actions or is already sufficient.
- **E.6** API keys for programmatic access (UX-010).
- **E.7** Per-tenant cost/margin attribution (BIZ-010) — covered by
  MICROSERVICES-PLAN.md §7 (Phase 5.2) using real Kubernetes resource-usage
  data; listed here too since it's as much a business feature as an
  observability one.

**Acceptance:** each sub-item is its own PR with its own tests (per Phase B's
now-established HTTP-test convention) — do not batch these.

---

## 7. Phase F — Reliability & disaster recovery

- **F.1** Continuous WAL archiving / PITR for tenant databases — the
  audit's ARCH-003 gap. For `DatabaseModeCloudNativePG` tenants (Phase D),
  this is close to free (CloudNativePG supports it natively); for any
  tenant remaining on `DatabaseModeManaged` or the legacy path, this needs
  explicit design — do not let Phase D's convenience for CNPG-mode tenants
  quietly become "PITR only for some tenants" without that being a
  deliberate, documented tier distinction.
- **F.2** A tested, documented restore runbook (not just "the code path
  exists") — schedule an actual quarterly restore drill once Phase D has
  real tenants, using the `spec.restore` feature already built in the
  operator; track results in this plan's progress log.
- **F.3** Control-plane HA — today it's a single Odoo instance (a fleet-wide
  SPOF per `ARCH-002`). Scope: at minimum, documented recovery time for a
  control-plane host failure; at most, an active/standby setup. Decide the
  target tier explicitly rather than leaving it implicit.

**Acceptance:** a documented, executed (not just theoretical) restore drill
with a measured RTO/RPO; a written control-plane failure/recovery runbook.

---

## 8. Phase G — Documentation & operational readiness

- **G.1** Keep `control-plane/docs/architecture/*` in sync as Phase D
  progresses — per that plan's own §7.2, several of those documents become
  historical once the compute layer moves; don't let them silently rot
  into contradicting the new reality.
- **G.2** Write the operational runbooks this plan's phases produce
  evidence for (restore drill results → a runbook; control-plane failure
  handling → a runbook) rather than leaving them as one-off Slack/chat
  knowledge.
- **G.3** Update `SESSION_NOTES.md`-style running notes to reflect the
  monorepo reality (some entries there already predate this reorg and
  reference the old repo layout — leave them as historical, but don't keep
  writing new entries as if the old layout still exists).

---

## 9. Phase H — Go-live gate

Before calling this "production-ready" for real, previously-unmigrated
customer traffic:

- [ ] Every phase below satisfies the **Production-First Principle**'s
      Definition of Done, not just its own phase-specific criteria — this
      is a standing check, not a one-time item on this list.
- [ ] Phase A complete (dependencies pinned/current, hygiene clean)
- [ ] Phase B complete (HTTP-level test coverage on the highest-risk routes,
      zero-coverage models closed, frontend test runner exists)
- [ ] Phase C complete (audit re-run shows no open Critical items, secret
      scanning live in CI)
- [ ] Phase D: at least one region fully migrated and running real tenant
      traffic on the Compute Service for one full billing + backup-retention
      cycle, per that plan's own acceptance criteria
- [ ] Phase F.2: at least one real, successful, measured restore drill on
      record
- [ ] A fresh pass of `MASTER_EXECUTIVE_REPORT.md`'s scoring rubric shows
      material improvement across all six dimensions (Architecture,
      Security, Production Readiness, Reliability, Scalability, UX) —
      re-score it explicitly rather than assuming progress.

Phase E (feature completeness) is deliberately **not** a go-live blocker —
it's roadmap work that continues after go-live, sequenced this way so
"production-ready" means "safe and correct," not "every competitor feature
shipped."

---

## 10. Cross-cutting rules

- **One dependency family, one model's tests, one route's tests, one
  tenant's cutover — per commit.** No batching across unrelated concerns in
  a single PR, for the same reason `MICROSERVICES-PLAN.md` already commits
  to this for the compute migration specifically.
- **A claim of "tested" means a real, demonstrated run** — an HTTP request
  that actually went through `url_open`/`HttpCase`, a build that actually
  ran, a restore that actually completed against real data — never "the
  code looks correct" or "unit-tested manifest strings" (the exact phrase
  that described the previous `KubernetesDriver` stub's insufficient
  verification before this session's work replaced it).
- **Re-run the relevant audit/report after closing its findings**, don't
  just mark a checkbox — `MASTER_EXECUTIVE_REPORT.md`'s scoring rubric
  exists precisely so "production-ready" is a measured claim, not a vibe.

---

## 11. Progress log

> Append one entry per completed step, in this format.

```
YYYY-MM-DD — Step X.Y — <one-line result> — verified: <how> — commit: <sha>
```

2026-09-14 — Step A.1 — Pinned control-plane's external Python deps
(paramiko/jinja2/boto3/google-cloud-storage) into control-plane/requirements.txt,
resolved against Odoo 18's own requirements.txt as a constraints file to
avoid the conflicts a naive unconstrained resolve produced (cryptography,
Jinja2, idna, requests, urllib3, python-dateutil all would have diverged
from Odoo core's own pins) — verified: fresh install from the committed
file reproduces byte-identical versions (diffed pip freeze); confirmed
paramiko 5.0.0's removal of DSSKey is already safely handled by an
existing hasattr() guard in saas_core/utils.py, no code change needed —
commit: fdb2e9b

2026-09-14 — Step A.2 — Applied non-breaking frontend security patches
(`npm audit fix`, no `--force`): closed fflate/nanoid/postcss/
postcss-selector-parser (7 of 11 findings) within existing semver ranges
(package.json unchanged). Remaining 4 (esbuild/vite, react-router) need
major bumps — left for A.3.2/A.3.4, not force-fixed — verified: `npm run
build` (tsc --noEmit + vite build) green, rebuilt SPA output committed —
commit: bc36fb1

2026-09-14 — Step A.3.1 — Upgraded TypeScript 5.6->7.0. Found and fixed a
real breaking change (TS5102: baseUrl removed) by running the compiler,
not by assumption — removing it was a behavioral no-op since it equaled
tsconfig.json's own directory. Verified: tsc --noEmit clean (1680
modules), npm run build output byte-identical to the prior commit's —
commit: c77f63c

2026-09-14 — Step A.3.2 — Upgraded Vite 5.4->8.3 + @vitejs/plugin-react
4->6. Closes the last high-severity npm audit finding (esbuild). Flagged
a real, non-obvious behavior change: Vite 8 defaults to the Rolldown
bundler, not Rollup (build time/output size both shifted) — no plugin
incompatibility found. Verified: tsc clean, build succeeds, both JS
bundles pass `node --check`, file sizes sane — commit: 823ba1e

2026-09-14 — Step A.3.3 — Upgraded Tailwind CSS 3.4->4.3 using the
official `@tailwindcss/upgrade` codemod (config moved from
tailwind.config.js into a CSS @theme block; 27 template files rewritten
to v4's renamed utility scales). Verified: diffed compiled CSS output
value-for-value against the pre-migration build for every renamed
utility actually used (rounded-sm/shadow-xs/etc. resolve to the exact
same pixel values as their v3 predecessors), and manually reviewed every
changed line across all 27 files to confirm each fits a known, documented,
visually-equivalent rename — nothing unexplained. tsc + build clean —
commit: 1a9073a

2026-09-14 — Step A.3.4 — Upgraded react-router-dom 6.27->7.18. `npm audit`
now reports 0 vulnerabilities (was 11 at the start of Phase A). App only
uses declarative-mode APIs (37 usages checked) — a true drop-in, zero
source changes needed. Verified: tsc clean, build succeeds, bundles pass
node --check, confirmed live via the running systemd service — commit:
bb6e511

2026-09-14 — Step A.3.5 — Upgraded React 18.3->19.2 + @react-three/fiber
8->9 (discovered mid-step these are coupled, not independent as A.3.6
below assumed — fiber@8 hard peer-caps at react<19, and fiber@9 itself
peer-caps at react<19.3, so landed on 19.2.8, the latest release
satisfying both real constraints, not a canary). One real type error
found and fixed correctly (a ref callback's ambiguous return under
React 19's new ref-cleanup typing) — not suppressed. Verified: tsc clean
across all 1656 modules, build succeeds, bundles pass node --check, the
Globe chunk confirmed to actually contain fiber's runtime code. NOT
verified: actual WebGL rendering in a browser (none available this
session, user declined the extension) — disclosed as an open item, not
glossed over — commit: aad107b

**A.3.6 status: done, folded into A.3.5 above** (the plan's assumption
that `@react-three/fiber` could be upgraded independently/"anytime" was
wrong — corrected here rather than left stale).

2026-09-14 — Step A.4 — Removed 3 empty scaffolding directories with no
evidence of intended purpose anywhere (compute/deploy,
compute/operator/internal/{database,network} — the latter two clearly
superseded by internal/controller/{database,network}.go which already do
this work). Kept + documented compute/operator/test/e2e instead of
inventing reasons for the others, since MICROSERVICES-PLAN.md's own
Phase 1 genuinely calls for it — commit: 2b87208

**Phase A: complete** (A.1-A.4, including the A.3 dependency chain).
`npm audit`: 11 -> 0. control-plane's Python deps: unpinned -> pinned and
verified against a real Odoo 18 install. Next per the plan: Phase B
(close the HTTP-level test-coverage gap before Phase D/compute migration
work begins).

2026-09-14 — Step B.1.1 — Corrected §1.1's overstated coverage-gap finding
(real count: 10/156 routes already covered, not ~0 — a research grep had
missed multi-line url_open()/_call() calls) and added the two genuine
auth-family gaps it revealed: register/verify (full signup completion)
and logout, both with real end-to-end assertions (new account can log
in; session is actually invalidated after logout), not just response-shape
checks. Verified: ran the full real Odoo test suite via devctl.sh test
against actual Postgres+Odoo (not envtest/mocks) — 237 tests (234+3 new),
0 failed, 0 errors; confirmed in the log that all 3 new tests actually
executed — commit: 67fc7c5. Remaining B.1 sub-steps re-scoped with the
corrected route list (see B.1.2-B.1.5 above) and a reusable script to
re-derive it before each one, so this doesn't drift stale again.

2026-09-14 — Step B.1.2 — Added HTTP-level tests for the checkout path:
hosting/order's paid-vs-trial redirect split (previously only res['ok']
truthiness was checked, never where either outcome actually redirects),
plus hosting/calculate, hosting/calculate-project, and services/calculate
(previously zero coverage). Reused TestOrderControllerFixes' existing
fixtures. Caught a real bug in my own first draft via an actual failing
test run (asserted synchronous state=='running' after an async
action_deploy() call) — fixed to assert the real invariant instead.
Verified: full suite via devctl.sh test — 242 tests (237+5 new), 0 failed,
0 errors after the fix — commit: 51d5d67.

2026-09-14 — Step B.1.3 (partial) — Investigated databases/create+drop+
duplicate before writing anything: they all call hosting_db_list(),
which execs into the tenant container over SSH — real infra, not
mockable in 5 minutes without a proper reusable fixture. Deliberately
scoped that (the success path) OUT rather than rush a fragile mock;
covered instead: the access-token-must-not-authorize-writes boundary for
those 3 routes (cheap, no infra, extends the existing
test_access_token_is_read_only pattern), and the FULL success+rejection
paths for environments/reserve+release (pure billing logic, no SSH
dependency at all) — release's wallet-credit amount verified to the
cent via real proration math, not just "no error." Verified: full suite
via devctl.sh test — 248 tests (242+6 new), 0 failed, 0 errors, first
run clean — commit: 244404f. Remaining B.1.3 scope (databases/backups
success paths, environments/create+merge, metrics/packages/repo/sql/
storage/etc.) re-listed above in priority order with the specific
technical reason each needs its own investigation before testing.

2026-09-14 — Step B.1.3 follow-up — Built the reusable compute-layer mock
fixture (`_mock_db_ops_infra`) and attempted the actual goal: full HTTP
success-path tests for databases/create+drop+duplicate. Found a real,
structural hazard rather than a mocking mistake: these routes end in
`saas.job._enqueue(...)`, which spawns a real background worker thread;
even with `_spawn_worker` patched to a no-op exactly like
`test_job_queue.py`'s own setUp does for precisely this reason, the
suppression did not hold through a live `HttpCase` HTTP round trip (that
pattern has only ever been used in this codebase under `TransactionCase`)
— a real run showed a job reach `state='running'`, then a
`psycopg2.ProgrammingError` and a broken savepoint cascading into
failures in every alphabetically-later test in the same class. Root
cause: the worker thread's own fresh DB connection cannot see
savepoint-nested, uncommitted test data, regardless of mocking fidelity.
Rather than ship something that intermittently corrupts unrelated tests,
backed out the 3 success-path tests and kept only the one safe rejection
path (never reaches `_enqueue`). **Re-scoped conclusion: testing these
success paths belongs at the model layer (`TransactionCase`, mirroring
`test_compute_driver.py`'s own established pattern), not through a live
HTTP request** — update the B.1.3 remaining-work list above accordingly
before attempting `backups/*` (likely the same shape) or any other
`_enqueue`-backed route via HTTP. Verified: full suite via devctl.sh
test — 249 tests (248+1), 0 failed, 0 errors, and confirmed zero
test-level errors anywhere in the run's own log (not just the summary
line) — commit: d1c0307.

2026-09-14 — Step B.1.3 follow-up — Databases create+duplicate success
paths, done properly this time at the model layer
(`TestDbOpViaQueue`/`test_job_queue.py`, `TransactionCase`). Found a
second real, non-obvious thing along the way: `create` doesn't use
`saas.job._enqueue` at all (deliberately, per SEC-002 — the queue would
persist the new DB's plaintext password) — it uses `run_in_background()`
instead, which needed a differently-shaped mock (patch the function
itself, not `_spawn_worker`) and a positive assertion that no `saas.job`
row exists. `drop`/`duplicate` do use `_enqueue` and were covered the way
B.1.3's earlier finding prescribed. Verified: full suite via devctl.sh
test — 253 tests (249+4 new), 0 failed, 0 errors, confirmed all 4 new
tests ran and zero ERROR/FAIL entries in the run's log tail — commit:
488c1a3. Remaining: `databases/upgrade` + a thin HTTP smoke test for
create/drop/duplicate (route-reaches-model-method only, not the full
async chain), `backups/*`, `environments/create`+`merge`, and the
not-yet-investigated remainder (see the re-scoped list above).

2026-09-14 — Step B.1.4 — `webhook.py` full branch coverage. Added 8
tests to `TestWebhookSecurity` (2 -> 10): SHA-1 downgrade rejection,
invalid JSON, non-push event, branch mismatch, instance-not-running,
repo-not-cloned, duplicate-delivery idempotency, and the successful-push
path (asserts the created `saas.build` record's fields and `_enqueue`'s
call kwargs). The last two patch `saas.job._enqueue` directly — never
`_spawn_worker` — so no real job row or worker thread is created, per
this plan's standing HttpCase rule. Caught a real bug in my own first
draft along the way: `test_non_push_event_ignored` reused the
push-shaped payload fixture (`ref` + `commits` list) and only swapped
the `X-GitHub-Event` header to `pull_request` — but `_is_push_event()`
has a deliberate header-less fallback (some providers omit the event
header) that treats any `{ref, commits: [...]}`-shaped body as a push
regardless of the header. So the app correctly ran the real push path,
an unmocked `_enqueue` fired for real, and its background worker thread
later crashed and cascaded `ERROR`s into unrelated later tests — the
exact corruption pattern already documented above from B.1.3, just
triggered by a wrong test fixture instead of a wrong mock choice. Fixed
by giving that test a genuinely PR-shaped payload (no `ref`/`commits`
keys) instead of relying on the header alone. Verified: full suite via
devctl.sh test — 261 tests, 0 failed, 0 errors, run twice (once scoped
to `TestWebhookSecurity` alone: 10/10 clean, then the full suite) —
commit: 5cf19ca. Next: B.1.5 (`saas_website/controllers/spa.py` +
`portal.py`, ~90 QWeb form-post routes, untested, distinct
form-post/redirect testing style from `api.py`).

2026-09-14 — Step B.1.5 (first slice) — `saas_website` had no `tests/`
directory at all; created one plus `TestPortalInstanceSecurity` (11
tests) covering `portal.py`'s cross-cutting access-control boundary
(`_document_check_access`, which every one of its ~55 routes delegates
to) via the `status`/`upgrade`/`restart`/`start`/`stop` routes, plus the
restart/stop/start self-service actions' state guards (must-be-running,
must-be-stopped, overdue-invoice block) and success paths. Confirmed
`action_restart()`/`action_portal_start()` end in `saas.job._enqueue`
and `action_stop()` ends in `run_in_background()` — same two hazards
already documented above — so success-path tests patch those directly
(never `_spawn_worker`), and patch `_ensure_can_ssh()` to a no-op (same
pattern as `test_job_queue.py`) since SSH provisioning itself isn't
what's under test here. Verified: scoped run of the new class alone
first (11/11 clean), then the full suite via devctl.sh test — 272 tests
(261+11), 0 failed, 0 errors — commit: 037575b. Remaining B.1.5 scope:
`databases/*` + `upgrade_module` at the portal layer, `checkout`/
`subscribe`/`change_plan` (money paths), backup `create`/`restore`/
`download`/`discard`, repo `update`/`remove`/`pull`, folder CRUD, and
`spa.py` entirely — large surface, budget as further incremental slices
rather than one pass.
