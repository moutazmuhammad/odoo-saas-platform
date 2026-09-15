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
    three slices done: (1) `saas_website/tests/` (didn't exist before at
    all) + `TestPortalInstanceSecurity`, covering the
    `_document_check_access` ownership boundary (shared by every route in
    the file) and the restart/stop/start self-service actions (state
    guards, overdue-invoice block, success paths); (2) `TestPortalDatabaseOps`
    covering `databases/{create,duplicate,drop,upgrade-module,
    reset-admin-password,op/<id>/dismiss}` as thin HTTP smoke tests (model
    methods mocked out — their real success paths are already covered at
    the model layer in `test_job_queue.py`), verifying only the portal
    layer's own job: auth boundary, form validation, param pass-through,
    redirect wiring; (3) `TestPortalChangePlan` covering `change-plan`/
    `do-change-plan`/`cancel-upgrade`/`cancel-downgrade` — real portal-layer
    logic this time (storage-reduction block, no-change detection, config
    clamping, upgrade-vs-downgrade branch selection), with only the
    heavier billing methods (`action_request_plan_change`/
    `_request_downgrade`, already covered in `test_billing_overhaul.py`)
    mocked out; (4) `TestPortalDataRestoreRequests` (request-restore/
    dismiss-restore-banner/decline-restore — synchronous, no infra
    mocking needed) and `TestPortalInstanceFolders` (folder CRUD +
    move-to-folder — pure ORM, ownership enforced by hand-filtering on
    `partner_id` rather than `_document_check_access`, tested the same
    way); (5) `TestPortalBackups` covering `backups/ondemand`,
    `backups/<id>/discard`, `backups/<id>/download`, `backup/<id>/restore`
    (route paths are inconsistently plural/singular — that's the app, not
    a typo); (6) `TestPortalRepoManagement` covering `update-repo`/
    `remove-repo`/`pull-repo` — these three always redirect to the same
    `/my/instances/<id>` on success or internal no-op alike, so coverage
    checks the resulting `saas.instance.repo` state and which model
    method fired, not the redirect target; (7) `TestPortalSubscribeAndCheckout`
    covering `subscribe` (trial->paid conversion) and `checkout` (the
    invoice payment page, exercised for real with a genuinely posted
    `account.move` since there's no async work to isolate away from).
    **`portal.py`'s route coverage is now complete** — 7 test classes, 100
    tests, from zero test infrastructure at the start of this session.
    `spa.py` surveyed and covered too (`TestSpaShellRoutes`, 15 tests) —
    turned out much smaller than the ~90-route estimate: almost every
    route is a one-line `return spa_shell()` (the SPA owns client-side
    routing/auth), so coverage focuses on the handful of routes with
    real server-side branching (`_section_enabled` gates,
    logged-in-vs-public redirects, `SaasWebLogin`'s override) plus
    `spa_shell()` itself (theme-cookie injection, the "frontend not
    built" fallback). **B.1.5 is now complete**: `saas_website/tests/`
    has 115 tests across 8 test classes, up from zero test
    infrastructure at the start of this session.
- **B.2** Add test files for the 4 zero-coverage models: `saas_payment.py`,
  `res_config_settings.py`, `saas_instance_package.py`, and
  **`saas_terminal_session.py`** — prioritize the terminal one, since it's
  the backend for a feature (`ShellConsole.tsx`) that will also be the
  Phase D.4.3 migration target (SSH-exec → Kubernetes `pods/exec`) — you
  want a real regression baseline *before* changing its transport, not
  after. **Done**, see the progress log — all 4 models covered (52 new
  tests: `saas_terminal_session.py` + `saas_instance_package.py`,
  `res_config_settings.py`, `saas_payment.py`).
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
  over-invest here). **Done**, see the progress log — all 13 methods
  covered across 3 slices (18 + 15 + 10 = 43 new tests).
- **B.4** Stand up a frontend test runner (Vitest + React Testing Library —
  the natural fit for a Vite project already on CI). Start with the pages
  identified in the feature-coverage report as calling the highest-risk
  endpoints (`Databases.tsx`, `Environments.tsx`, `ShellConsole.tsx`,
  `SqlConsole.tsx`) — component-level tests that mock `api.ts`, not full
  e2e. Wire into `.github/workflows/ci.yml`'s existing `spa` job. **Done**,
  see the progress log — Vitest + RTL + jsdom stood up, all 4 pages
  covered (14 tests), wired into CI.
- **B.5** Add a coverage report (Odoo's `coverage.py` integration, or the
  simplest thing that reports a number in CI) so future regressions in
  *coverage* (not just test failures) are visible, even if you don't gate
  on a hard threshold yet. **Done**, see the progress log — `coverage.py`
  wired into both `devctl.sh test` and CI's `odoo-tests` job (44% on
  `saas_core`+`saas_website`, Odoo core excluded); `@vitest/coverage-v8`
  wired into the frontend's `test:coverage` script and CI's `spa` job
  (~17% statements). No threshold gate yet, per scope — just visibility.

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
  **Done**, see the progress log for the full 19-item table — 7 fixed, 7
  partially fixed, 5 still open, plus corrections to two of the audit's
  own claims (SEC-012 turns out already fixed but was never tracked in
  `REMEDIATION_PLAN.md`; SEC-019's "all `csrf=False` routes are `type='json'`"
  premise is factually wrong for 2 of the 4, though all 4 remain safe for
  other reasons).
- **C.2** Rotate the root SSH password that was pasted into chat
  (`control-plane/SESSION_NOTES.md`, "Live server" section) — this is a
  real, still-open credential exposure regardless of anything else in this
  plan. **Not done — needs the account owner** (this requires live-server
  access this session doesn't have); flagged separately, not delegated.
- **C.3** Add automated secret-scanning to CI (e.g. `gitleaks` as a new CI
  job) — proactive guard, direct response to the SSH-password-in-chat and
  the prior `SECURITY-INCIDENT-2026-06-17.md` compromise, so a future
  leaked credential is caught at PR time instead of discovered after an
  incident. **Done**, see the progress log — `gitleaks` job + `.gitleaksignore`
  baseline, commit `202e1df`.
- **C.4** Verify tenant DB `CREATEDB` grant (SEC-004) status for any
  remaining SSH/Docker-driven tenants — note this becomes structurally moot
  per-tenant as each one migrates to the Compute Service in Phase D (each
  tenant gets its own isolated Postgres instance there, so a broad grant on
  a shared server is no longer the relevant risk model), but do not treat
  Phase D as an excuse to defer it for tenants that stay on the legacy path
  longer than expected. **Done (verification only, no code change)**, see
  the progress log — still granted fleet-wide (Phase D hasn't started, so
  "remaining" tenants means *all* of them today).

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

2026-09-14 — Step B.1.5 (second slice) — `TestPortalDatabaseOps` (12
tests) for `databases/{create,duplicate,drop,upgrade-module,
reset-admin-password,op/<id>/dismiss}`. Deliberately mocked the
instance model methods themselves
(`hosting_db_create_async`/`hosting_db_duplicate_async`/
`hosting_db_drop_async`/`hosting_db_upgrade_module_async`/
`hosting_db_reset_admin_password`) rather than re-deriving the SSH/
job-queue mocking already done for these at the model layer in
`test_job_queue.py` — this layer's own job is the portal wrapper (auth,
form validation, param pass-through, redirect querystring), which is
what these tests actually verify. Refactored the shared
instance/owner/intruder fixture into a `_PortalTestBase` mixin (used by
both test classes now) and added a `_form_post()` helper for the
file's `csrf=True` routes: computes the token via
`http.Request.csrf_token(self)`, reusing the same trick Odoo's own
`account/tests/test_portal_attachment.py` uses (the method only reads
`self.env` + `self.session.sid`, both of which `HttpCase` already sets,
so the unbound method can be called with the TestCase standing in for
the request). Verified: scoped `saas_website` run first (23/23 clean),
then the full suite via devctl.sh test — 284 tests (272+12), 0 failed,
0 errors — commit: 6d93351. Remaining B.1.5 scope unchanged from above
minus what's now done: `checkout`/`subscribe`/`change_plan` (money
paths), backup routes, repo management routes, folder CRUD, `spa.py`.

2026-09-14 — Step B.1.5 (third slice) — `TestPortalChangePlan` (16
tests) for `change-plan`/`do-change-plan`/`cancel-upgrade`/
`cancel-downgrade`. Unlike the `databases/*` slice, `do-change-plan`
carries real portal-layer logic worth exercising directly (not just a
thin wrapper): the storage-reduction hard block, both-fields-required
and no-actual-change validations, and config-limit clamping — all
tested against the real (unmocked) `_get_or_create_hosting_plan`/
pricing-engine path, since that's pure ORM logic with no SSH/job
involvement. Only `action_request_plan_change`/`_request_downgrade`
themselves are mocked (already covered in `test_billing_overhaul.py`),
to isolate the one thing genuinely new here: which of the two the
portal route picks and how it turns the return value into a redirect.
Found and fixed a real bug in my own first draft: a downgrade test
held onto the mocked method's recordset argument and read a field off
it after the HTTP request thread's own cursor had already closed
("AssertionError: Cannot use a closed cursor") — fixed by capturing
plain values inside the mock's `side_effect` while that cursor was
still open, never holding the recordset itself past the request.
Verified: scoped class alone (16/16), all of `saas_website` (39/39),
then the full suite via devctl.sh test — 300 tests (284+16), 0 failed,
0 errors — commit: efd355b. Remaining B.1.5 scope: `subscribe`/
`checkout` (trial->paid + invoice payment page — the largest remaining
single route, ~140 lines, touches `payment.provider`/`payment.method`
compatibility), backup routes, repo management routes, folder CRUD,
`spa.py` entirely.

2026-09-14 — Step B.1.5 (fourth slice) — `TestPortalDataRestoreRequests`
(9 tests: request-restore/dismiss-restore-banner/decline-restore, all
synchronous JSON routes with no SSH/job queue, so no infra-mocking
hazard) and `TestPortalInstanceFolders` (11 tests: folder create/
rename/delete + move-to-folder). The folder routes have no
`_document_check_access` call at all — ownership is enforced by
hand-filtering every search on `request.env.user.partner_id` — so
those tests confirm the boundary the way the code actually implements
it: a folder or instance belonging to someone else is silently not
found / not moved, never an `AccessError`. request-restore's real
`mail.mail.send()` is exercised for real (Odoo's test-mode mail queue
never actually delivers), since the one behaviour worth covering here
is the "delivery failed" branch turning into a customer-facing error
instead of a false "request sent". Cleaned up a first-draft
`if False else ...` dead-code leftover in two test helpers (caught
before running anything, not a functional bug). Verified: scoped run
of both new classes (20/20), all of `saas_website` (59/59), then the
full suite via devctl.sh test — 320 tests (300+20), 0 failed, 0 errors
— commit: e82a95b. Remaining B.1.5 scope: `subscribe`/`checkout`
(trial->paid + invoice payment page, still the largest remaining
single piece), backup routes, repo management routes, `spa.py`
entirely.

2026-09-14 — Step B.1.5 (fifth slice) — `TestPortalBackups` (21 tests)
for `backups/ondemand`, `backups/<id>/discard`, `backups/<id>/download`,
`backup/<id>/restore`. Reused `test_job_queue.py`'s SSH/`_compute_driver`
stub pattern for `hosting_db_list()` (called for real by ondemand to
validate the db name), and patched `run_in_background` at its source
module attribute — the route does a fresh local import from
`odoo.addons.saas_core.utils` inside the function body, so patching the
module attribute is what the re-import picks up each call, same
standing rule as every other `run_in_background()` route already in
this plan. `action_restore_backup`/`action_restore_full_instance`
mocked entirely (async, SSH-heavy, model-layer concern not portal).
Caught a real bug in my own first draft, not the app: copied restore's
singular `/backup/<id>/...` path onto the download route too, which is
actually registered plural (`/backups/<id>/download`) — 4 tests 404'd
against a nonexistent route instead of testing anything, caught
immediately by the scoped run (1 failed + 3 errors) rather than by a
false green. Verified: scoped class alone (21/21 after the fix), all
of `saas_website` (80/80), then the full suite via devctl.sh test — 341
tests (320+21), 0 failed, 0 errors — commit: 24766d8. Remaining B.1.5
scope: `subscribe`/`checkout` (still the largest remaining piece), repo
management routes (`update`/`remove`/`pull`), and `spa.py` entirely.

2026-09-14 — Step B.1.5 (sixth slice) — `TestPortalRepoManagement` (11
tests) for `update-repo`/`remove-repo`/`pull-repo`. Unlike most routes
in this file, these three always redirect to the same
`/my/instances/<id>` whether they succeed or silently no-op internally
— both `action_redeploy`/`action_restart` call sites wrap the call in
a bare `except Exception`, so even a raised `UserError` never reaches
the customer as a visible error — so these tests check the resulting
`saas.instance.repo` row and which model method fired, not the
redirect target (except for the auth-denied case, which redirects to
the bare `/my/instances` listing and stays distinguishable).
`action_redeploy`/`action_restart` mocked entirely (async + SSH, out
of scope here); `run_in_background` (pull-repo's fresh local import)
patched at its source module attribute, same standing rule as every
other `run_in_background()` route in this plan. Verified: scoped class
alone (11/11), all of `saas_website` (91/91), then the full suite via
devctl.sh test — 352 tests (341+11), 0 failed, 0 errors — commit:
f9bbc07. Remaining B.1.5 scope: `subscribe`/`checkout` (trial->paid +
invoice payment page, the last and largest remaining piece) and
`spa.py` entirely.

2026-09-14 — Step B.1.5 (seventh and final portal.py slice) —
`TestPortalSubscribeAndCheckout` (9 tests) for `subscribe`/`checkout`.
`action_subscribe_from_trial()` mocked (synchronous billing-only ORM,
already covered in `test_billing_overhaul.py`), same thin-smoke-test
principle as change-plan; config-limit clamping verified by reading
the real plan's `workers` field off the route's own recordset argument
*inside* the mock's `side_effect` (cursor still open) rather than
after the request completes, applying the lesson from the earlier
closed-cursor bug proactively this time instead of hitting it again.
`checkout` is a render-only GET with nothing async to isolate, so it's
exercised for real end-to-end: created and posted a genuine
`account.move` via `saas.instance._get_billing_product()` (the same
product real subscription invoices use) and confirmed the page renders
200 with a real unpaid invoice, alongside the auth-denied and
no-unpaid-invoice-redirect cases. This closes out `portal.py`'s route
coverage for B.1.5: 7 test classes, 100 tests in
`saas_website/tests/`, up from no test infrastructure at all at the
start of this session. Verified: scoped class alone (9/9), all of
`saas_website` (100/100), then the full suite via devctl.sh test — 361
tests (352+9), 0 failed, 0 errors — commit: b4e86ae. Remaining B.1.5
scope: `spa.py` entirely — not yet surveyed, separate form-post/QWeb
testing style from `portal.py`.

2026-09-14 — Step B.1.5 (eighth slice, final) — surveyed `spa.py`
(`grep -c @http.route` = ~25, far below the plan's original ~90
estimate for the combined `portal.py`+`spa.py` surface — that estimate
turned out to belong almost entirely to `portal.py`). Nearly every
route is a one-line `return spa_shell()`: the SPA owns client-side
routing and auth for these paths, so there's no per-route business
logic worth individually testing the way `portal.py` needed. Added
`test_spa_shell.py` / `TestSpaShellRoutes` (15 tests) covering what
actually has server-side branching: `_section_enabled` gates on
`/services` (+`/services/<id>`) and `/hosting`, the logged-in-vs-public
branch on `/services/register` and `/register`, `SaasWebLogin`'s
redirect-anonymous-GET-to-`/login` override, and `spa_shell()` itself
— theme-cookie injection, invalid-cookie fallback to dark, and the
"frontend not built" message (genuinely reachable right now: this
checkout's `saas_website/static/spa/` has no built `index.html`). The
theme tests patch the module-global `_INDEX_PATH` to a real temp file
rather than depending on this checkout's build state, and explicitly
reset the equally module-global `_INDEX_CACHE` dict before and after
each — it's an in-process cache shared by every request, so an
unpatched leftover would silently corrupt whichever test runs next.
Verified: scoped class alone (15/15), all of `saas_website` (115/115),
then the full suite via devctl.sh test — 376 tests (361+15), 0 failed,
0 errors — commit: dbd41e8.

**B.1.5 is now complete.** `saas_website/tests/` went from no test
infrastructure at all to 8 test classes / 115 tests covering
`portal.py` (100 tests, 7 classes) and `spa.py` (15 tests, 1 class).
Next: B.2 (the 4 zero-coverage models — `saas_payment.py`,
`res_config_settings.py`, `saas_instance_package.py`,
`saas_terminal_session.py`, prioritizing the terminal one per the
Phase D.4.3 note above).

2026-09-14 — Step B.2 (first slice) — `saas_terminal_session.py` and
`saas_instance_package.py`, the two smallest of the 4 zero-coverage
models. `saas_terminal_session.py` turned out to have no methods at
all beyond field declarations and the `sid_unique` SQL constraint (it's
pure runtime metadata read/written by `controllers/ssh_terminal.py`),
so `test_terminal_session.py` is intentionally thin: required-field
creation, the `closed=False` default, and the `sid` uniqueness
constraint (asserted via `psycopg2.IntegrityError` inside
`self.env.cr.savepoint()`, muting `odoo.sql_db` — the idiom Odoo core
itself uses for SQL-constraint tests, e.g.
`addons/loyalty/tests/test_loyalty.py`). `saas_instance_package.py` had
real logic worth covering: `create`/`write` strip whitespace from
`name`, and `create`/`write`/`unlink` all call
`instance_id._sync_text_from_packages()` to keep the instance's
`pip_packages` text field (what admins/customers actually edit) in
sync with the `package_ids` One2many — `test_instance_package.py`
verifies that sync in both directions (add syncs text in, unlink syncs
text back out, multiple packages join with `\n`), the `_check_name`
constraint (empty/whitespace-only name), and the
`unique_package_per_instance` SQL constraint (same name twice on one
instance fails; same name on two different instances is fine — a
constraint on `(instance_id, name)`, not `name` alone, so worth an
explicit "doesn't over-fire" test). 11 new tests. Verified: full suite
via `devctl.sh test` — 387 tests (376+11), 0 failed, 0 errors — commit:
f4d44cc. Next: `res_config_settings.py`, then `saas_payment.py`
(highest-value and largest of the 4 remaining).

2026-09-14 — Step B.2 (second slice) — `res_config_settings.py`. The
model's inline comments call out three "falsy-value trap" workarounds
(config_parameter= plus Odoo's `set_param(key, False)` deleting the row,
and per-integer/float Boolean coercion quirks, spring values back to
their defaults on the next read unless handled by hand in
get_values/set_values) — each got an explicit "does it survive a
reload" test, not just an in-memory assertion, since the whole point of
the workaround is what happens on the *next* read after a save. Also
covered `set_values()`'s call into `saas.plan.sudo().search([...])
._sync_auto_price()`, which re-derives every public-tier, non-trial,
non-manually-priced plan's stored price from the (possibly
just-changed) worker/storage rates — verified both that an
auto-priced plan's price moves and a manually-priced one (`manual_price
= True`) does not. 12 new tests. Verified: full suite via `devctl.sh
test` — 399 tests (387+12), 0 failed, 0 errors — commit: 019e254. Next:
`saas_payment.py` (highest-value and largest of the 4 remaining).

2026-09-14 — Step B.2 (third slice, final) — `saas_payment.py`, the
largest and highest-value of the 4 models. Covered
`SaasPaymentProviderConfig._check_single_default` (a second *active*
default routing row is rejected, a second *inactive* one is fine);
`SaasPaymentMethod`'s `_for_partner`/`_default_for_partner`/
`_make_default`/`action_remove`; and `SaasPaymentGateway`'s three real
methods. `_provider_for_partner`'s country-routing precedence (explicit
country config row > default config row > the provider's own
`available_country_ids` > any enabled provider) got one test per level,
each proving that level overrides the one below it, plus a "disabled
provider is skipped even when otherwise matched" case at the
config-row level. `_save_method_from_transaction` — idempotent per
token (repeat calls return the same method, no duplicate row), and
only the *first* saved method for a partner is forced default (a
second one is left alone, per the model's own `len(...) == 1` check —
worth a dedicated test since it's easy to assume every save should
force-default). `_charge` — all 4 early-return branches (no/inactive
token, already-paid invoice done-short-circuit, disabled provider,
currency mismatch) plus the real success/pending/other-state paths and
the exception-during-send path. Non-obvious finding: none of this
needed the `payment_demo` addon (correctly out of scope — it's not a
saas_core dependency) — `payment.transaction.create()` already
auto-generates `reference` when omitted (see its `create()` override),
so a real transaction can be constructed directly in the test and only
`_send_payment_request` itself needs patching (`patch.object(type(env
['payment.transaction']), '_send_payment_request', fn)`, `fn` setting
`tx_self.state` directly) — the same "mock only the external transport
boundary" discipline used for the job-queue/webhook tests elsewhere in
this suite. The currency-mismatch branch needed a real
`account.payment.method.line` wired to the test provider (a bare
`payment.provider` has a non-stored, search-computed `journal_id` with
no default journal) — created by hand with `account.
account_payment_method_manual_in` rather than fighting the accounting
module's automatic provider/journal-linking flow. One test-writing
mistake caught by the run itself: `test_save_method_noop_with_
inactive_token` originally deactivated the token *before* creating the
transaction, tripping `payment.transaction`'s own `_check_token_is_
active` constraint at creation time instead of testing the gateway's
handling of an already-inactive token — fixed by creating the tx first,
then deactivating. 29 new tests. Verified: full suite via `devctl.sh
test` — 428 tests (399+29), 0 failed, 0 errors — commit: 07ecc77.

**B.2 is now complete.** All 4 previously zero-coverage models
(`saas_terminal_session.py`, `saas_instance_package.py`,
`res_config_settings.py`, `saas_payment.py`) now have test coverage —
52 new tests across 3 slices (11 + 12 + 29), full suite grown from 376
to 428 tests, 0 failed / 0 errors throughout. Next: B.3 (the untested
cron methods in §1.2 — billing-lifecycle crons first for direct
revenue impact, then operational crons, then the provisioning helpers
that Phase D will supersede anyway).

2026-09-14 — Step B.3 (group 1) — the 5 billing-lifecycle crons on
`saas.instance` (`_cron_check_overdue_invoices`/`_check_dunning`,
`_cron_retry_failed_payments`/`_retry_failed_payments`,
`_cron_send_renewal_reminders`, `_cron_apply_paid_pending_changes`,
`_cron_check_trial_expiry`), none referenced by name anywhere in the
suite before. New `test_billing_crons.py` (18 tests): dunning suspends
a `stopped` instance directly (no SSH — only a `running` instance's
suspension goes through `action_suspend`/SSH) once past the grace
period, skips invoices `_is_optional_invoice` flags as optional, sends
one within-grace warning without suspending, and — the one dispatch
test — proves a `ValueError` raised inside one instance's
`_check_dunning` is caught, rolled back (via the test-cursor's
save-point-backed `commit`/`rollback`, not a real commit), and logged
without aborting the batch, while a second instance in the same cron
pass still gets suspended; retry fires the mocked
`_try_auto_charge_invoice` only on the exact 1/3/5-day-after-due-date
schedule and only once per invoice per day (a
`saas.payment.attempt` row for today short-circuits a re-attempt);
renewal reminders fire once per offset (7d/1d) and don't re-fire the
same day once the per-offset flag is set; the paid-pending-change
safety net picks `_apply_pending_upgrade` vs `_apply_pending_plan_change`
based on `is_trial` and leaves an instance alone when its pending
invoice isn't paid (or there is none); trial expiry suspends and
clears any unpaid `pending_plan_id` only when the partner's
`saas_trial_end_date` is past AND a trial-used flag is set.

Two non-obvious findings worth carrying into any future cron test in
this model: (1) every one of these crons calls `self.env.cr.commit()`/
`rollback()` per instance in its loop, and Odoo's `TransactionCase`
makes both **raise `AssertionError`** ("Cannot commit or rollback a
cursor from inside a test") rather than silently no-op — the fix,
already established in `test_job_queue.py`'s `_no_commit` helper and
now hoisted into this file's `setUp`, is `patch.object(self.env.cr,
'commit')` / `'rollback'` (bare `MagicMock`, no real effect) before
calling any `_cron_*` entry point directly, not just the queue-drain
ones. (2) `account.move.create()` flatly refuses `state='posted'` in
the create vals ("must create a draft move and post it after") — and a
line-less (`amount_total == 0`) move's `payment_state` recomputes to
`'paid'` regardless of what's written afterward, silently defeating
the dunning/retry filters that require `payment_state not in ('paid',
'in_payment')`. Test invoices therefore need one non-zero
`invoice_line_ids` line, created draft, with `state`/`payment_state`
forced via a `write()` call after `create()` (fine for these tests —
only the crons' own field reads are exercised, not real posting/
numbering). Also: `_cron_check_overdue_invoices`'s own `search()`
requires `sale_order_id != False`, easy to miss when building a bare
test instance for the dispatch-only test.

Verified: scoped class alone (18/18), then the full suite via
`devctl.sh test` — 446 tests (428+18), 0 failed, 0 errors — commit:
3261ec9. Remaining B.3 scope: group 2 (operational crons —
`_cron_renew_daily_backup_addons`, `_cron_verify_webhooks`,
`_cron_check_container_health`, `_cron_sample_live_metrics`,
`_cron_record_metrics`) and group 3 (the 3 thin provisioning-helper
tests, `_pg_clone_db`/`_provision_nginx`/`_clone_product_repos`).

2026-09-14 — Step B.3 (group 2) — the 5 operational crons:
`_cron_renew_daily_backup_addons`/`_sync_daily_backup_suspension`,
`_cron_verify_webhooks`, `_cron_check_container_health`,
`_cron_sample_live_metrics`, `_cron_record_metrics`. New
`test_operational_crons.py` (15 tests). Per the plan's own framing,
the underlying per-instance mechanics for 3 of these are *already*
covered elsewhere (container reconciliation in `test_reconcile.py`,
webhook signing/delivery in `test_webhook_security.py`, metric
series/retention in `test_metrics.py`) — only the batch/dispatch layer
around them had never been exercised, so these tests mock the
per-instance work item (`_sync_daily_backup_suspension`,
`_ensure_webhooks_registered`, `_sample_live_metrics_for_host`) and
assert the cron's own filtering/grouping/error-isolation logic:
daily-backup maintenance only touches non-trial instances with the
add-on enabled; webhook verification only re-registers repos that are
`cloned`, `webhook_enabled`, have a token, and aren't already
registered; `_cron_check_container_health` is confirmed to be exactly
the one-line back-compat shim it claims to be (`return
self._cron_reconcile()`) via a single delegation test, nothing more;
both metrics crons group running instances by `docker_server_id` and
call the per-host sampler once per host (not once per instance),
skip instances with no docker server, and a `ValueError` from one
host's batch doesn't stop the others from being recorded.
`_sync_daily_backup_suspension` itself (not just its cron) got real
depth since it wasn't covered anywhere either: pause-past-grace,
not-yet-past-grace, resume-once-paid, and the disabled-add-on no-op
(asserted by making the mocked `_daily_backup_unpaid_invoices` raise if
even called, since the method must return before reaching it).

Non-obvious finding: `_cron_sample_live_metrics` loops for up to
`LIVE_METRICS_SAMPLER_MAX_RUN` (50) real seconds, re-querying watched
instances and `time.sleep(LIVE_METRICS_SAMPLE_INTERVAL)` (5s) between
passes, for as long as anything stays watched — a naive direct call in
a test would either hang for tens of seconds or require faking
`time.monotonic`. The test instead patches `time.sleep` to a no-op AND
has the mocked `_sample_live_metrics_for_host` clear the watched
instances' `metrics_watch_until` back into the past as its side
effect, so the loop's own `if not watched: break` check ends it after
exactly one real iteration — no monkeypatching of the loop bound or
`time.monotonic` needed.

Verified: scoped class alone (15/15), then the full suite via
`devctl.sh test` — 471 tests (446+15+10 — this slice's run already
included group 3 below), 0 failed, 0 errors — commit: ee40a55.

2026-09-14 — Step B.3 (group 3, final) — the 3 provisioning helpers
Phase D will eventually supersede: `_pg_clone_db`, `_provision_nginx`,
`_clone_product_repos`. New `test_provisioning_crons.py` (10 tests),
deliberately thin per the plan's own instruction not to over-invest
here: one success-path test per method (asserting the actual SQL/shell
command issued, e.g. `_pg_clone_db`'s `CREATE DATABASE ... WITH
TEMPLATE ...` string, and `_clone_product_repos`'s `git clone --branch
<branch> <url> <dir>`), one SSH/command-failure-raises-UserError test
each, plus `_pg_clone_db`'s two guard clauses (no `db_server_id`
configured, and its own identifier-safety regex rejecting a `; DROP
TABLE` payload in the source/target name — worth a dedicated test
since it's the one place in this group with a real security property,
not just "does the happy path still work"). `_provision_nginx`'s tests
mock `_nginx_apply_vhost` (already covered in `test_nginx_vhost.py`)
and only exercise this method's own job: the certbot
--nginx-then---standalone fallback and raising when both fail.
`_clone_product_repos` needed the target instance's `docker_server_id`
set to a real `saas.server` row — `_get_instance_path()` reads
`docker_server_id.docker_base_path` unconditionally, and an empty
recordset there returns `False` rather than the field's own default,
crashing on `.rstrip()` deeper in the call chain (a good reminder that
a model-level field `default=` never applies to a *related* read
through an empty many2one — only to that field's own creation).

Verified: scoped class alone (10/10), then the full suite via
`devctl.sh test` — 471 tests (446+15+10), 0 failed, 0 errors — commit:
c270622.

**B.3 is now complete.** All 13 previously-untested cron/provisioning
methods on `saas.instance` now have coverage — 43 new tests across 3
slices (18 + 15 + 10), full suite grown from 428 to 471 tests, 0
failed / 0 errors throughout. Next: B.4 (stand up a frontend Vitest +
React Testing Library runner, starting with `Databases.tsx`,
`Environments.tsx`, `ShellConsole.tsx`, `SqlConsole.tsx` — the
highest-risk pages per the feature-coverage report).

2026-09-14 — Step B.4 — stood up the frontend's first-ever test runner:
Vitest + `@testing-library/react` + `jest-dom` + `user-event`, jsdom
environment, a shared `renderWithProviders` helper (MemoryRouter +
ToastProvider) under `frontend/veltnex/src/test/`. **Non-obvious
version trap**: the latest majors of `vitest` (5.x), `jsdom` (30.x) and
`@testing-library/jest-dom` (7.x) have all dropped Node 20 — this
repo's CI (`.github/workflows/ci.yml`) and local dev both pin Node 20,
and changing that is out of scope for a test-only step, so pinned to
the latest majors that still declare Node 20 support instead:
`vitest@4.1.11`, `jsdom@26.1.0`, `@testing-library/jest-dom@6.9.1`
(`@testing-library/react@16.3.3` and `user-event@14.6.7` had no such
constraint). Confirmed the pin was necessary, not just cautious: `npm
install` with the latest majors reproducibly failed under Node 20
(`EBADENGINE` + a crashing peer-resolution error in npm's arborist),
and succeeded cleanly once downgraded. Test files are excluded from
`tsconfig.json`'s `include` so the build-blocking `tsc --noEmit` step
doesn't need them strictly typed (Vitest itself doesn't type-check).
Covered the 4 highest-risk pages per the feature-coverage report — 14
tests: `SqlConsole.tsx` (3: db-list load + run-query, inline
`ApiError` message on a SQL error, Run disabled before a db loads),
`ShellConsole.tsx` (3: session-open → Connected status, terminal
input wired to `api.terminalInput`, error status on a failed
`terminalCreate`), `Databases.tsx` (5: loading → list, error banner,
create-database dialog calling `api.dbCreate` with the right args and
reloading the list, client-side name-pattern validation blocking the
API call, and the type-name-to-confirm delete flow calling
`api.dbDrop`), `Environments.tsx` (3: loading → project render, error
banner, create-staging-server dialog calling `api.environmentCreate`).
**Two non-obvious mocking needs, both from real jsdom gaps**: (1)
`@xterm/xterm`'s module import alone (not just `new Terminal()`)
probes for canvas support, which jsdom doesn't implement — mocked
`@xterm/xterm` + `@xterm/addon-fit` + the CSS import in
`ShellConsole.test.tsx`, and had to do the same in
`Environments.test.tsx` too, since `Environments.tsx` statically
imports every tab's page component (including `ShellPage` ->
`ShellConsole`) even though only one tab renders at a time; (2) jsdom
has no `EventSource` (used for the terminal's SSE output stream) — a
small fake class stood in via `vi.stubGlobal`. Wired into
`.github/workflows/ci.yml`'s existing `spa` job as a new "Component
tests (Vitest)" step ahead of the typecheck/build step (renamed the
job "SPA typecheck + test + build"); confirmed `npm run build` still
passes unmodified. Verified: `npm run test` — 4 test files, 14 tests,
0 failed. Commit: 1d2c3fe.

**B.4 is now complete.** The frontend went from zero test
infrastructure to a working Vitest + RTL runner wired into CI, with
component-level coverage of the 4 highest-risk portal pages (14
tests).

2026-09-14 — Step B.5 — coverage reporting, both components.
**Backend**: `scripts/devctl.sh test` and CI's `odoo-tests` job now run
the Odoo test suite under `coverage run`, scoped via `--source` to
`control-plane/saas_core` + `control-plane/saas_website` only (Odoo
core itself excluded, so the % reflects our code, not the framework),
`--omit`ting `*/tests/*` and `*/migrations/*` so test files and one-off
migration scripts don't dilute the number. A `coverage report` step
prints the per-file table + a `TOTAL` line after the existing
pass/fail assertion, so a coverage regression is visible without
failing the build. Current: **44%** (12,940 statements, saas_core +
saas_website combined). **Non-obvious bug caught while wiring this
up**: `devctl.sh`'s `$REPO` variable already resolves to the
`control-plane` directory itself (`dirname(BASH_SOURCE)/..` from
`scripts/devctl.sh`) — an initial `--source="$REPO/control-plane/saas_core,...\"`
therefore pointed at a doubled, nonexistent path and silently measured
**zero files** (`coverage report` printed "No data to report." with no
error), rather than failing loudly. Verified by first reproducing
green coverage against a hand-built `coverage run odoo-bin ...`
invocation outside the script, then diffing it against the script's
actual arguments to find the double path. Ran `devctl.sh test` after
the fix to confirm: 0 failed, 0 error(s) of 471 tests, real per-file
coverage numbers. **Frontend**: added `@vitest/coverage-v8` (pinned to
the same `4.1.11` as `vitest` itself — same Node-20 constraint B.4
already hit applies here too) and a `coverage` block in
`vitest.config.ts` (provider `v8`, `text` + `text-summary` reporters,
scoped to `src/**/*.{ts,tsx}` excluding test files and the test-setup
directory), plus a `test:coverage` npm script. CI's `spa` job now runs
`npm run test:coverage` instead of `npm run test` (same 14 tests, now
printing a summary). Current: **~17% statements** (504/3007) — expected,
given B.4 only covered 4 of the app's pages. Neither number is gated on
a threshold yet, matching the plan's explicit scope for this step.
Verified `npm run build` still passes unmodified.

**B.5 is now complete — Phase B (close the test-coverage gaps) is now
fully complete.** Summary: B.1 (146 previously-uncovered HTTP routes
across `api.py`/`webhook.py`), B.1.5 (the entire `saas_website`
form-post surface, 115 tests from zero), B.2 (4 zero-coverage models,
52 tests), B.3 (13 previously-untested cron/provisioning methods, 43
tests), B.4 (a frontend test runner from zero, 14 tests on the
highest-risk pages), B.5 (coverage visibility for both components).

2026-09-14 — Phase C — C.3 (gitleaks CI job + `.gitleaksignore`
baseline, commit `202e1df`) done first since it's pure tooling, no
investigation needed. Then C.1 + C.4 together (both are "verify
against current code" tasks): re-read all 19 `SECURITY_AUDIT.md`
SEC-00x items and grepped/read the current code for each — not
trusting `REMEDIATION_PLAN.md`'s checkboxes, which turned out to be
genuinely stale/self-contradictory in places (e.g. its own summary
block at the bottom of the file disagrees with the detailed entries
above it for SEC-001/002). Status:

| Item | Sev | Status | Evidence |
|---|---|---|---|
| SEC-001 debug_otp | Critical | **Fixed** | `grep -r debug_otp` across `saas_website` + the SPA returns nothing. |
| SEC-002 plaintext secrets | Critical | **Fixed (code); operational step pending** | `admin_password`/`db_password`/`restic_password`/`github_token`(×2)/`private_key_enc` are all `EncryptedChar` now. Encryption is opt-in via `saas_secret_key` — unset in this checkout's dev conf, so **whether it's activated in production, and whether the already-exposed credentials have been rotated post-activation, can't be verified from code alone.** |
| SEC-003 root containers | Critical | **Fixed** | `Dockerfile.tenant.jinja` ends `USER odoo`; `docker-compose.yml.jinja` has `cap_drop:[ALL]` + `no-new-privileges:true`. Read-only rootfs deliberately deferred (documented tradeoff, Odoo writes outside its volumes). |
| SEC-004 tenant `CREATEDB` | High | **Still open — now conclusively confirmed unsafe to revoke as-is (2026-09-15)** | See C.4 below. |
| SEC-005 host shell scoping | High | **Fixed (2026-09-15)** | `ssh_terminal.py`'s host terminal (raw SSH into platform machines, distinct from the customer instance shell) now requires a new, narrower `group_saas_host_shell`, not implied by `group_saas_manager` — enforced at the controller, the `saas.server.action_open_terminal()` method itself, and the view button's own `groups=`. An idempotent migration grandfathers existing Managers into the new group so the change doesn't silently revoke access on deploy. See the progress log (§11) for the full rationale. Commit `52b1394`. |
| SEC-006 plaintext host creds | High | **Partially fixed (2026-09-15)** | `odoo.conf.jinja:11` still renders `db_password`/`admin_passwd` in cleartext — Odoo itself needs to read the plaintext from its own config file to connect to Postgres, so that part is an inherent constraint of running upstream Odoo, not something this codebase can fix alone. What WAS fixable and is now fixed: every deploy/redeploy/restore path was `chmod -R 777`ing (one site `755`) the directory containing that file — world-readable AND world-writable on a shared multi-tenant host, alongside the filestore and the addons Odoo actually executes as code. Tightened to `700` (owner-only) across all 9 call sites; see the progress log for the full writeup, including 2 sites that had no `chown` at all before (papered over by the wide mode) and needed one added. Commit `3ac9e15`. |
| SEC-007 supply chain | High | **Partially fixed, more so (2026-09-15)** | `_PIP_PACKAGE_RE` (`saas_instance.py:1393`) is strictly `^...$`-anchored. **New**: a `trivy` CI job now builds and scans the operator + backup-tool container images, failing on any HIGH/CRITICAL vulnerability with an available fix (`--ignore-unfixed`, so upstream OS CVEs with no patch yet don't produce permanent unactionable red builds). Also removed a real, if minor, finding it caught: an unused `gosu` binary in the backup-tool image carrying 22 stale Go-stdlib CVEs. Package allow-listing / an internal mirror — the audit's other recommendations — are still not implemented. |
| SEC-008 presigned URL TTL | High | **Mostly fixed** | `PRESIGNED_URL_EXPIRY` cut from 7 days to **15 min** (`saas_instance_backup.py:17`), lists re-mint fresh links. Per-download audit logging (pairing with SEC-010) still not wired — no `_saas_audit` call anywhere in that file. |
| SEC-009 security telemetry | High | **Partially fixed** | `saas.alert._notify()` exists and is wired to server-health degradation + operation failures (opt-in webhook). No Sentry/Prometheus/SIEM — `grep -ri 'sentry\|prometheus\|datadog\|statsd'` across both addons is empty. |
| SEC-010 audit log | Medium | **Fixed (2026-09-15)** | Append-only `saas.audit.log` exists (write/unlink raise), now wired to **8** events: the original `instance_delete`/`db_drop`, plus `instance_deploy`, `instance_redeploy`, `instance_restore_backup`, `instance_restore_full`, and `instance_scale` (both upgrade and scheduled-downgrade paths) — every category the audit's own description named ("who scaled, deployed, restored, or deleted an instance"). Per-download audit logging for presigned backup URLs (the SEC-008 pairing note) is still not wired — a separate, narrower gap. Commit `9298189`. |
| SEC-011 webhook oracle | Medium | **Mostly fixed** | Uniform 404 deny (`webhook.py:_webhook_deny`) + per-repo rate limit (30/60ish window) added. Residual: the initial secret lookup (`webhook.py:61-64`) is still a plain ORM equality `search`, not constant-time — the "minor" timing oracle the audit already downgraded is technically still there. |
| SEC-012 log-stream authz | Medium | **Fixed — but untracked** | `stream_instance_logs` now calls `instance.check_access('read')` explicitly (`container_logs.py:41`) before streaming. This fix isn't mentioned anywhere in `REMEDIATION_PLAN.md`, i.e. it shipped without the tracking doc being updated — exactly the "audit document stops being the only source of truth" problem C.1 exists to catch. |
| SEC-013 OTP brute force | Medium | **Fixed** | Window tightened to 6/600 (`registration.py:225`, `api.py:259,354`); `code` is `EncryptedChar`; `hmac.compare_digest` constant-time verify (`saas_registration.py:32,126`). |
| SEC-014 SSH command injection | Medium | **Confirmed not currently exploitable (2026-09-15); still no central builder** | A dedicated audit traced every `.execute()`/`write_file()` call site in `saas_core` (pip_packages, subdomain, DB names/users, git repo URLs/branches, webhook payloads, `extra_config`, the SQL console) back to its origin — every one is correctly quoted or regex-validated before reaching a shell string, including every customer-facing entry point. Two latent, not-currently-reachable footguns found and fixed (`service_exec`/`_restic_cmd` quoting only an env var's value, not its key — see progress log). The architectural point stands: quoting discipline is still per-call-site, not centrally enforced, so this needs re-auditing after any change that adds a new `.execute()` site with dynamic input. |
| SEC-015 broad `except Exception` | Medium | **Still open** | Count actually **grew** to 125 (from the audit's 85) — B.3 added more per-instance cron try/except/rollback blocks following the existing (deliberate, for cron resilience) pattern. Not new backsliding, but worth flagging so the raw number isn't misread as improvement. |
| SEC-016 RBAC granularity | Medium | **Partially fixed (2026-09-15)** | A third group, `group_saas_host_shell`, now exists (split out of `group_saas_manager` as part of the SEC-005 fix below) — the first crack in the "2 groups total" ceiling, though still far short of the audit's broader recommendation (e.g. separate billing/support/infra tiers). |
| SEC-017 restore-confirm UX | Low | **Still open (cosmetic)** | `backup_restore` (`api.py:1366`) still confirms against `backup.db_name or instance.subdomain`, unchanged. Ownership enforcement itself (the actual security control) remains correct. |
| SEC-018 SPA session refresh | Low | **Fixed (2026-09-15)** | `AuthContext` now auto-logs out after 30 minutes of no activity (mouse/keyboard/touch/scroll), with a 1-minute warning toast first. Additive to server-side session expiry, not a replacement. Commit `ec2ab35`. |
| SEC-019 CSRF posture | Low | **Fixed (2026-09-15)** | 4 `csrf=False` routes exist, same count as the audit. The audit's stated reason ("these are all `type='json'`") was already known factually wrong for 2 of the 4 (`webhook.py`, `portal.py`'s `portal_instance_log_stream` are `type='http'`). **New this pass**: the audit's OTHER claim — "the 3 stream routes are all `methods=['GET']`-only" — was *also* factually wrong for `portal_instance_log_stream`, which declared no `methods=` at all; Odoo's own default when omitted is **all methods**, not GET-only (confirmed by reading `odoo/http.py`'s `route()` docstring). Not a live vulnerability (the handler has no side effects and same-origin policy blocks reading the response), but the safety property was incidental, not enforced — fixed by adding `methods=['GET']` explicitly. The recommended CI lint is now implemented (`control-plane/scripts/lint_csrf_routes.py`, wired into CI), encoding the actual verified-safe rule (`auth='none'` OR `methods` ⊆ `{GET, HEAD}`) rather than the naive `type='http'`-based rule the audit itself warned would false-positive. Commit `fd14906`. |

**C.4 (CREATEDB), in depth**: `_provision_postgresql` (`saas_instance.py:3319-3354`) unconditionally grants `CREATEDB` to every tenant Postgres role — hosting and non-hosting instances alike, regardless of whether the platform creates the DB itself (`create_db=True`) or leaves it to the tenant (`create_db=False`). Since Phase D (Compute-layer migration) has not started, **every current tenant** is on this legacy path — "remaining SSH/Docker-driven tenants" is not a shrinking edge case yet, it's the whole fleet. The portal's own customer-facing DB-create flow (`api.dbCreate` → `saas.instance` methods) already provisions databases via **control-plane-mediated** `sudo -u postgres createdb` over SSH, not as the tenant's own low-privilege role — so *that* flow does not depend on the grant.

**2026-09-15 — the open reachability question is now conclusively resolved, and the answer is unsafe-to-revoke, for two independent reasons:**

1. **The built-in database-manager UI is genuinely reachable.** Both nginx vhost templates (`templates/nginx_new_odoo_versions.jinja`, `nginx_old_odoo_versions.jinja`) use a single catch-all `location / { proxy_pass ...; }` with no `/web/database` block, deny rule, or WAF layer anywhere in front of the tenant container; `list_db` is never set to `False` anywhere (Odoo's own default — manager enabled — applies); and `proxy_mode` only affects header trust, not route dispatch. `saas_website/controllers/db_manager.py`'s `VeltnexDatabase` subclasses Odoo core's `Database` controller purely to reskin its HTML — it does not restrict or remove any route. Stronger than "technically reachable": `saas_core/data/mail_templates.xml`'s "Instance Deployed" email sent to every hosting tenant **links directly to `{{ object.url }}/web/database/manager` and prints the admin master password next to it**, explicitly instructing the customer to use it to create/duplicate/restore/delete databases. This is advertised, first-class functionality, not incidental exposure — the grant's own justification is real and in active use.
2. **A separate, currently-wired feature also depends on it.** The portal's "Duplicate Database" feature (`portal.py`/`api.py` → `hosting_db_duplicate_async` → `saas_instance.py`'s `hosting_db_duplicate`) goes through Odoo core's own XML-RPC `db` service running inside the tenant's live worker (`_hosting_xmlrpc_db_proxy`, authenticated with `admin_password`), which itself issues `CREATE DATABASE` as the tenant's configured `db_user` — requiring `CREATEDB` on that role independently of the web UI's reachability. (By contrast, `hosting_db_create`/`hosting_db_drop` already run via SSH as `sudo -u postgres` and do not need it — confirming the grant isn't blanket-necessary, just necessary for these two specific paths.)

**Revised guidance for whoever eventually picks up SEC-004 for real**: revoking the grant today would break both the advertised database-manager email flow and the "Duplicate Database" portal feature for every hosting tenant. A real fix needs to either (a) replace `hosting_db_duplicate`'s XML-RPC path with a control-plane-mediated `sudo -u postgres` equivalent (mirroring how create/drop already work) and separately decide whether to keep advertising/exposing `/web/database/manager` at all, or (b) accept the exposure as a deliberate, documented product decision and move on. Either way, this is now a scoped decision with known dependencies, not an open unknown — matches this plan's Production-First principle that a real fix isn't "add a TODO," it's understanding what breaks first.

Next: Phase D (Compute-layer migration) is out of this plan's scope
(fully specified in `MICROSERVICES-PLAN.md`); the remaining Phase C
item, **C.2** (rotating the exposed root SSH password), needs the
account owner and live-server access this session doesn't have.

2026-09-15 — SEC-005 (host shell scoping) — revisited priorities after a
stretch of Phase D/MICROSERVICES-PLAN.md work (Phase 1 complete, Phase
2.1's real `KubernetesDriver` live-verified): with production traffic
still 100% on the legacy SSH/Docker path, the highest-value next step
for actual production safety is closing a real, still-open gap on that
live path, not continuing a migration with no real tenants on it yet.

Fixed: the host terminal (`ssh_terminal.py`'s `create_session`, a raw
interactive shell on the platform's own Docker/DB machines — distinct
from the customer instance shell, which is authorized by instance
ownership and untouched by this change) was gated solely on
`group_saas_manager`, the same broad role used for routine billing/
tenant-lifecycle work. Added `group_saas_host_shell`, deliberately not
implied by Manager, enforced at three independent layers: the
controller check that actually opens the SSH channel,
`saas.server.action_open_terminal()` itself (which had no group check
of its own before — defense in depth), and the view button's `groups=`
(so it disappears for managers who lack it, instead of a confusing
403). Also closes a sliver of SEC-016 (RBAC granularity) as a side
effect — this is the first group added beyond the original 2 in the
system's history.

Since this tightens an existing permission and this session has no way
to inspect or seed the real production user list, added a small,
idempotent migration (`res_groups.py`'s
`_saas_grandfather_host_shell_group`, invoked via a `<function>` call
in `saas_security.xml` outside the `noupdate` block, so it re-runs on
every module upgrade including the one shipping this change) that
grants every *existing* Manager the new group too — the fix narrows
access going forward without silently locking out whoever already has
it today. 6 new tests (3 for the permission gate — manager-only denied,
host-shell-group allowed, plain-user denied — and 3 for the migration's
grandfathering/idempotency/no-removal behavior) — full suite green, 497
tests (491 + 6), 0 failed/errors. Commit: `52b1394`.

**Deployment note for whoever ships this**: no action needed beyond the
normal module upgrade (`-u saas_core`) — the grandfathering migration
handles the permission transition automatically and safely.

2026-09-15 — SEC-006 (plaintext host creds), the permissions half —
while fixing SEC-005 above, noticed every deploy/redeploy/restore/clone
path in `saas_instance.py` follows a `chown -R container_uid:
container_uid` with `chmod -R 777` (one site: `755`) on the tenant's
`config`/`data`/`addons` directories. The chown already gives the
container's own UID full ownership, so the wide mode was doing nothing
for the container itself — its only effect was making these world-
readable *and* world-writable for every other process on the same
shared host: `config/odoo.conf`'s plaintext `db_password`/`admin_passwd`
(the original SEC-006 finding), the customer filestore under `data/`,
and — the sharpest edge — `addons/`, which Odoo actually loads and
executes as Python. World-writable addons on a shared host is a
cross-tenant **code-injection** path (a compromised or escaped process
from a different tenant plants a file, this tenant's Odoo imports and
runs it), not merely a data-exposure concern.

Ran a dedicated audit pass (not just fixing the first occurrence found)
across all 9 `chmod -R 777`/`755` call sites before touching anything,
specifically checking: does a chown to the tenant's own `container_uid`
always precede it (yes, everywhere), could any path ever be shared
across two containers/tenants needing group rather than owner access
(no — `_get_instance_path`'s own containment checks guarantee per-
tenant-unique paths), and does anything else legitimately need group/
other access to these paths (found nothing). Two sites
(`_restore_snapshot`, `_do_restore_backup`) turned out to have **no
chown at all** before their chmod — they only worked because the wide
mode papered over the resulting ownership mismatch (files copied as the
plain SSH user, then made world-writable so the container could still
touch them despite not owning them). Tightening those two without
adding the missing chown first would have broken filestore access after
every snapshot-based deploy and every single-database restore — added
the chown rather than just narrowing the mode.

Tightened all 9 sites to `700`. 3 new tests extend the 3 sites that
already had SSH-mock fixtures to build on
(`_clone_product_repos`/`_pull_product_repos`/`_hosting_clone_filestore`);
the remaining sites (`_do_deploy_locked`, the restore flows) have no
existing direct unit coverage, and building full deploy/restore
integration fixtures from scratch was judged disproportionate to what
is, underneath the audit, a mechanical permission-string change —
correctness rests on the audit itself plus this being a small, uniform,
easy-to-review diff. Full suite green: 500 tests (497 + 3), 0
failed/errors. Commit: `3ac9e15`.

**SEC-006 is now partially closed**: the plaintext-in-a-config-file fact
itself is an inherent constraint of running upstream Odoo (it must read
its own DB password from somewhere at startup) and isn't something this
codebase can eliminate without a materially different secrets-injection
architecture — out of scope for this pass. What's closed is the
*exposure surface*: that plaintext credential (and the filestore, and
the executable addons) is no longer world-readable/writable on a shared
host.

2026-09-15 — SEC-007 (supply chain), the image-scanning half — added a
`trivy` job to CI (`.github/workflows/ci.yml`) building and scanning
both container images this repo produces (the operator, the
backup/restore tool), directly closing one of the audit's explicitly-
named-as-missing recommendations. Gate is `--severity HIGH,CRITICAL
--ignore-unfixed`: fail on what's actually actionable (a package this
Dockerfile can upgrade), not on upstream OS CVEs with no patch
available yet, which would just make the job permanently red for
reasons nobody here can fix — same "don't fail on unactionable noise"
principle the gitleaks job's own `.gitleaksignore` baseline already
established for secret-scan false positives.

Ran it against both images locally before wiring it into CI, rather
than assuming a fresh job would pass: the operator image (distroless
static) was already clean. The backup-tool image had 24 real, fixable
findings — 22 from `gosu`, the postgres base image's own privilege-drop
tool for an entrypoint script this image never runs (`ENTRYPOINT` is
overridden and `USER` already pins the non-root uid directly), carrying
its own old, statically-linked Go stdlib's CVEs for a binary nothing
here ever executes; 2 from a stale `libpcre2-8-0` with an available
`apt` upgrade. Fixed both directly in the Dockerfile (removed `gosu`,
added `apt-get upgrade` so base-image packages get patched too, not
just the ones explicitly installed), re-scanned clean, and re-verified
`pg_dump`/`rclone`/the non-root uid all still work. Commit: `9284096`.

2026-09-15 — SEC-014 (SSH command injection) — ran a dedicated audit
(not just a `shlex.quote` grep count, per this item's own prior "not
architecturally resolved" caveat) tracing every `.execute()`/
`write_file()` call site in `saas_core` back to where its interpolated
values originate: `pip_packages` (validated by `_PIP_PACKAGE_RE` before
any shell command is built, both portal entry points correctly catch
the validation error before reaching that code), `subdomain` (charset
constrained by `SUBDOMAIN_RE` at both the model and every customer-
facing controller, so it can never contain a shell metacharacter),
database names/users (re-validated against strict identifier regexes
immediately before use, every site), git repo URLs/branches (quoted,
plus SSRF/argument-injection blocked by `assert_safe_git_url`), webhook
payloads (only ever written to DB fields — the actual git pull uses the
owner-configured branch, never payload data), `extra_config` (not
exposed to any portal customer at all), and the SQL console (by design
lets a customer run arbitrary SQL against their *own* database, but the
query never touches a shell string — base64-encoded into an env var,
executed via `cr.execute()` inside a Python heredoc, `SET TRANSACTION
READ ONLY` enforced by Postgres itself). **Found no currently-
exploitable injection.**

Did find two real, if not-currently-reachable, footguns: `ssh_docker_
driver.py`'s `service_exec()` and `saas_instance_backup.py`'s
`_restic_cmd()` both build `-e K=V` env-var flags by quoting only V,
leaving the key K unquoted — safe today only because every caller
passes a hardcoded literal key. Fixed both to quote the whole `K=V`
pair as one token. Deliberately did NOT centralize `_restic_cmd`'s
`args`-list quoting the same audit also flagged: several callers
already `shlex.quote()` individual args themselves before adding them
to that list, so quoting again inside `_restic_cmd` would double-quote
those and silently corrupt the actual value restic receives — not a
risk worth taking on a live, largely-untested backup path to harden a
pattern nothing can currently reach. 3 new tests, full suite green: 503
tests (500 + 3), 0 failed/errors. Commit: `170a9f0`.

**This closes out the current pass of easily-fixable, high-value
production security items** identified opportunistically while working
the live SSH/Docker path (SEC-005, SEC-006, SEC-007, SEC-014 all
touched this session). Remaining open items (SEC-004, SEC-009, SEC-011,
SEC-015, SEC-016 beyond its first crack, SEC-017/018/019) are each
either already partially addressed, gated on Phase D, require live
production access this session doesn't have, or are low-severity/
cosmetic — none stood out as a quick, safe, high-value fix the way
these four did.

2026-09-15 — revisited two of those "remaining" items and found one was
more actionable than it looked:

- **SEC-004** — resolved the one open question C.4 itself flagged
  ("whether the built-in database manager is network-reachable was not
  conclusively determined"). It is reachable (confirmed no blocking
  layer anywhere: nginx, `list_db`, `proxy_mode`, or a controller), and
  it's advertised, not incidental — the hosting "Instance Deployed"
  email links straight to it with the master password. Also found a
  second, independent dependency: the portal's "Duplicate Database"
  feature needs the same grant via Odoo's own XML-RPC `db` service. Net
  result: **confirmed unsafe to revoke as-is** — this turns an open
  unknown into a scoped, documented decision for whoever picks up the
  real fix (see the rewritten C.4 section above for the two concrete
  paths forward). No code changed; this was verification, matching this
  plan's own "integration is verified, not assumed" standard applied to
  a security assumption instead of a cross-component claim.
- **SEC-018** — this one WAS a quick, safe, real fix: added a
  `useIdleLogout` hook to the SPA's `AuthContext` — 30 minutes of no
  mouse/keyboard/touch/scroll activity logs the user out, with a
  1-minute warning toast first. Additive to server-side session expiry,
  not a replacement. 5 new tests (fake-timer driven: warn/idle timing,
  activity reset, disable-mid-session cleanup, listener cleanup on
  unmount), full frontend suite green (19 tests, was 14), typecheck +
  build clean. Commit: `ec2ab35`.

2026-09-15 — SEC-019 (CSRF posture) — implemented the audit's own
recommended CI lint, which it had explicitly flagged as needing care:
a naive "`type='http'` + `csrf=False`" rule would false-positive on
every one of the 4 existing routes, since each is safe for a different
reason. `control-plane/scripts/lint_csrf_routes.py` (AST-based, stdlib
only, no Odoo/DB needed) instead encodes the actual rule: a
`csrf=False` route must be either `auth='none'` or explicitly
restricted to `methods` ⊆ `{GET, HEAD}`.

Running it against the real codebase immediately caught a genuine gap
in the *audit's own* prior claim, not just a hypothetical the lint was
designed to catch: "the 3 stream routes are all `methods=['GET']`-only"
was factually wrong for `portal_instance_log_stream`
(`saas_website/controllers/portal.py`) — it declared no `methods=` at
all, and Odoo's own documented default when omitted is **all methods**
(confirmed by reading `odoo/http.py`'s `route()` docstring), not
GET-only. This was never a live vulnerability (the handler has no side
effects, and same-origin policy blocks a cross-site page from reading
the response regardless of verb) — but the safety property was
incidental, resting on the handler happening to have no side effects,
not on anything actually enforced. Added `methods=['GET']` explicitly
so the route's real behavior matches its documented reasoning. Full
Odoo suite still green after the change (503 tests, 0 failed/errors) —
confirms nothing relied on a non-GET verb reaching it.

10 new tests for the lint itself (both directions: flags unsafe/
non-literal `csrf=`, allows both carve-outs, handles syntax errors),
wired into a new fast CI job. Commit: `fd14906`.

**Correction to the above**: that turned out not to be the last one —
SEC-010 (below) was also fixable.

2026-09-15 — SEC-010 (audit log) — the audit's own description named
the exact gap: "no append-only audit of who scaled, deployed, restored,
or deleted an instance." `instance_delete`/`db_drop` were already
wired; the other three categories were not. Added `_saas_audit()` calls
at the same point `action_delete_instance` already logs at (right where
the operation is queued/applied, so a subsequent failure during the
operation itself still leaves a record it was attempted):
`action_deploy`/`action_redeploy` → `instance_deploy`/
`instance_redeploy`; `action_restore_backup`/
`action_restore_full_instance` → `instance_restore_backup`/
`instance_restore_full`; and both the immediate-upgrade path
(`_apply_pending_plan_change`) and the scheduled-downgrade path (inside
`_generate_renewal_invoice`) → `instance_scale`, the closest concrete
analog to "scaled" in this codebase (a plan change is what actually
changes an instance's CPU/RAM/worker allocation).

6 new tests — deploy/redeploy extend the existing job-queue test
fixtures; restore (both kinds) and scale (both directions) get a new
test class. The scheduled-downgrade test deliberately lets the
real-invoice-generation tail of `_generate_renewal_invoice` fail if it
must (full billing fixtures are out of scope) and asserts on the audit
log regardless, since the write it cares about happens earlier in the
same method and stays visible on the same cursor either way. Full suite
green: 509 tests (503 + 6), 0 failed/errors. Commit: `9298189`.

**This is now genuinely the last easily-fixable item from the original
19 SEC-00x audit findings this session identified opportunistically** —
everything remaining needs either live production access, external
infra credentials, or a product decision on RBAC tiers this session
can't make alone. (Famous last words, per the correction above — but
this one's for real: the remaining items are SEC-004 [documented,
needs an architecture decision], SEC-009 [needs real Sentry/Prometheus
credentials], SEC-011 [genuinely minor, already downgraded, fixing it
would add real overhead for no measurable risk reduction], SEC-015
[an intentional cron-resilience pattern, not a bug], SEC-016 [needs
product input on role tiers beyond the one crack already made], and
SEC-017 [cosmetic UX].)
