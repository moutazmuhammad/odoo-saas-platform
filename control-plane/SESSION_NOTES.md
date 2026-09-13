# Work Log — SaaS Hosting Platform / VELTNEX

> A running log of what we've done together, so we can resume later.
> Last updated: 2026-06-15 — branch: `newhosting`

---

## Project overview

- **Odoo 18 SaaS platform**: `saas_core` (models/controllers) + `saas_website`
  (controllers + QWeb + serves the SPA from `static/spa`).
- **VELTNEX React SPA**: in `custom/saas/veltnex` (Vite + React + TypeScript +
  Tailwind + react-router-dom). JSON API at `/saas/api/v1`. The build is
  emitted into `saas_website/static/spa/`.

---

## Live server (Production)

- Site: **https://odoo.odex.sa** = `165.22.69.199` (DB: `veltnex`).
- Repo path on server: `/home/odoo/saas`.
- SPA path: `/home/odoo/saas/saas_website/static/spa`.

### Deploy workflow (important)
1. `npm run build` locally in `veltnex/`.
2. commit + push to `origin/newhosting`.
3. On the server: `cd /home/odoo/saas && git fetch + reset --hard origin/newhosting`.
4. **Always** run `systemctl restart odoo` after the pull (a cache otherwise
   keeps serving the old homepage/asset bundle).
5. Verify: the served `assets/index-*.js` hash must equal the local build's
   hash (`saas_website/static/spa/index.html`).
6. Deploys are done over paramiko SSH (no `sshpass` installed).

> ⚠️ **Security**: the root SSH password was typed into chat more than once —
> it should be rotated.

---

## What has been done

### 1) Odoo.sh-style Environments (complete, deployed)
- Production (`saas.instance`, the billing anchor) + Staging/Development as
  children (`parent_id`). Children never self-bill — their cost rides the
  Production renewal via `_environment_order_lines`, and they're excluded from
  the crons via `('parent_id','=',False)`.
- Staging/Dev are always lowest spec (fixed per-server price from the pricing
  engine).
- One-click create/delete/merge: `action_create_environment`,
  `action_delete_environment`, `action_merge_environment`.
- Per-project Git branch automation in `saas_instance_repo.py`
  (`_create_branch_on_provider`, `_delete_branch_on_provider`,
  `_merge_branch_on_provider`, `_list_remote_branches`).
- Repo + token are **per-PROJECT** (set on Production, propagated to children).
- `saas.build` model for build/deploy history — **ops-only** (the
  customer-facing UI/API was removed intentionally).

### 2) Google Cloud Console-style UI/UX redesign (complete, deployed)
- Design tokens via CSS variables in `src/index.css` (primary `#1a73e8`,
  page `#f8f9fa`, white cards, borders `#dadce0`, Roboto font, Material buttons).
- New shell in `PortalLayout.tsx`: top app bar + collapsible left nav
  (collapsed by default), `ProjectSwitcher`, centered search, avatar dropdown
  (Settings + Sign out only).
- `PublicNav.tsx`: solid white bar, UserMenu matching the dashboard avatar
  dropdown.
- `Footer.tsx`: compact single-row GitHub-style footer.
- Shared components: `PageHeader`, `DataTable`, `CommandPalette` (⌘K),
  `NotificationsBell`, `ProjectSwitcher`, `Dropdown`, `StatusBadge`, `Skeleton`.
- `Home.tsx`: rich landing (globe kept) + "Start free trial" CTA.
- `Logs.tsx`: themed log colors (text-danger/warning/muted/foreground).
- `/my/account` redirects to `/my/settings` (in `portal.py`).

### 3) Latest change (complete, deployed — commit `5f38d21`)
On the workspace page `/my/instances/<id>/environments`:
- **In-page tabs (no navigation)**: Overview / Databases / Logs / Backups now
  swap in place (a `tab` state in `MainPanel`) — like switching from one server
  to another.
- **Code moved to project-level Settings** (no longer per-server):
  `ProjectSettingsPanel` reached from the sidebar "Project settings" button,
  showing the project's repo/token/packages config (bound to the Production
  instance).
- `Databases` / `Logs` / `Backups` / `Code` are now embeddable via an `embedId`
  prop (suppresses their breadcrumb/heading when rendered inside the workspace).

---

### 4) Checkout/portal palette = dashboard (complete, deployed — `a793263`)
- The QWeb portal pages (`/my/instances/<id>/checkout`, change-plan, invoices,
  ...) render via `cloudodoo_layout → website.layout` and already had VELTNEX
  header/footer/logo, but used an OLD navy/near-black palette. Re-pointed the
  shared `saas_website/static/src/css/cloudodoo.css` design tokens to the SPA's
  Google Cloud palette so the whole QWeb side matches the dashboard.
  - `:root` (dark) → GCP dark: `--primary #8ab4f8`, page `#202124`, card
    `#292a2d`, border `#3c4043`, text `#e8eaed/#9aa0a6`, solid navbar `#292a2d`.
  - `[data-theme=light]` → GCP light: `--primary #1a73e8`, page `#f8f9fa`,
    card `#fff`, border `#dadce0`, text `#202124/#5f6368`, solid white navbar.
  - Added `--primary-foreground` (dark text on the light-blue dark-mode primary,
    white in light mode) → applied to `.btn-primary`, `.co-btn-primary`, header
    + sidebar avatars so primary buttons stay readable in dark mode.
  - Logo box (`veltnex-icon.svg`) + header "NEX" wordmark → `#1a73e8`.
- No SPA rebuild (QWeb CSS/XML/SVG only). Bundle is content-hashed → restart
  regenerates it. Verified deployed source tokens + no log errors.

### 4b) Header/footer matched to the dashboard 1:1 (`4d74cc9`)
- The QWeb header had been built to mirror the marketing **PublicNav** (a
  bordered "pill" avatar + name + chevron). Users compare it to the
  **dashboard** (`PortalLayout`), so re-did it to match that instead:
  - Avatar = plain 36px circle (`vx-avatar`, bg-primary + initials), no
    pill/name/caret, primary ring on open (SPA `size-9`).
  - Account dropdown = `vx-account-menu`: rounded-xl + border + bg-card +
    `p-1.5` + shadow-2xl + `w-64`, with a name+email header, divider, and
    `rounded-lg` items (Settings, Backend for staff, Sign out in danger).
  - Header `fixed-top` → `sticky-top`; removed the matching
    `padding-top:120px` spacer from every QWeb section (sed across
    portal/hosting/services templates).
  - Footer rewritten to mirror `Footer.tsx` (logo + copyright · center links ·
    social icons, text-xs on page bg).
- ⚠️ These are **template (XML)** changes → deploy needs `-u saas_website`
  (a plain restart won't re-read view arch). New CSS classes live in
  `cloudodoo.css` (`.vx-avatar*`, `.vx-account-*`, `.co-footer-social`).

### 5) Awaiting-payment orders: hidden from list + abandonable (`02f0f9b`)
- `pending_payment` instances are **incomplete orders**, not real projects:
  - Hidden from the customer's **projects list + search** via a new
    `_LIST_STATES` (= `_ACTIVE_STATES` minus `pending_payment`) in
    `api.py::instances()`. Cancelled states stay visible (reactivation).
  - Still surfaced on the **Dashboard "Needs your attention"** (separate
    dashboard endpoint keeps them) + filtered out of the Dashboard "Your
    projects" preview.
- **"Don't complete"** button beside each awaiting-payment alert (Dashboard)
  → confirm dialog → `api.invoiceCancel` → `action_client_cancel_invoice`
  (cancels the instance, frees the subdomain), then reloads.
- ⚠️ The blocker: `_invoice_is_client_cancellable` refused `SAAS:INITIAL:`
  invoices. Fix: a never-deployed (`pending_payment`/`draft`) order is always
  client-cancellable (nothing provisioned); the non-cancellable rule now only
  guards LIVE renewals/restorations.
- Deploy: SPA rebuild + Python (api.py + model) → `git pull` + **restart**
  (no `-u` — no XML/view change this round).

### 6) Local-testing aids + free-trial-through-funnel + Home redesign (local only, 2026-06-20)
- **OTP on screen for testing**: `register_start`/`register_resend` (`api.py`)
  echo `debug_otp` again (the SPA already had the display panel); resend
  cooldown `Register.tsx` 45s→**120s**. ⚠️ `TODO: REMOVE before production`
  (this is the SEC-1 blocker). Verified: API `debug_otp` == server-log code,
  verify with resent code creates the user.
- **Free trial now runs the normal SPA funnel** (was: trial CTA jumped to the
  QWeb review/pay page, bypassing the wizard). `Hosting.tsx` gains **trial
  mode** via `/hosting?trial=1`: starts at step 2, collects project
  name/subdomain/version/region, **skips the paid specs step + all paid
  add-ons** (support, daily backup, staging/dev), summary shows **Free**,
  CTA "Start free trial" → `api.hostingOrder` with `is_trial=1`. Backend was
  already trial-ready (trial plan, skip billing, deploy, redirect to instance)
  — **frontend-only change**. Verified: trial order → `/my/instances/N` (no
  pay), instance `is_trial=t state=provisioning $0`; paid order → `/checkout`
  `pending_payment $21.50`.
- **Home.tsx redesigned** as a pure two-service landing — **no trial CTAs on
  home** (final decision): hero + two cards describe **Hosting** (you bring
  code, empty DB) vs **Ready-Made Apps** (ready code + ready DB, e.g. pharmacy
  mgmt, customizable); CTAs are **"Explore Hosting"** / **"Explore Ready-Made
  Apps"** → the respective pages. One informative line only ("Free trial
  available inside each"). Removed all trial state/badges from Home.
- **Trial lives INSIDE the offering pages** (eligible first-timers only,
  gated by `meta.trial.*_available`): `Hosting.tsx` hero has "Start your
  N-day free trial" (→ `/hosting?trial=1` wizard) above the purchase wizard;
  `ServiceDetail.tsx` hero has "Start your N-day free trial" above the plans.
  Returning/paid users see only the purchase path.
- `Services.tsx` listing reworded to the ready-made-apps framing.
- Headless-Chrome verified: Home has NO trial action (only Explore buttons);
  Hosting hero shows the trial + purchase wizard; Services listing renders the
  new copy. (No services published in saas_dev, so ServiceDetail trial is
  code-verified, not data-exercised locally.)
- Services trial already flowed through its (QWeb) configure funnel ending
  without payment — left as-is functionally.

### 6b) Seeded a sample service + client-tested BOTH trials (2026-06-20)
- Seeded a published service in `saas_dev` via odoo shell (`/tmp/seed_service.py`):
  **Pharmacy Management** (`saas.product` id 101, `is_hosting=False`,
  `is_published=True`, `backup_bucket_path='pharmacy/seed/snapshot.zip'`) +
  plans: **Free Trial** (157, `is_trial_plan`, $0), **Professional** (158, $99),
  **Business** (159, $199). Shows in `/services` + `/services/101`.
- **Client-tested both trials end-to-end** (register via debug-OTP → order):
  - Hosting trial → `/my/instances/153` (no checkout), inst is_hosting✓
    is_trial✓ **$0** provisioning.
  - Services trial → 303 `/my/instances/154` (no checkout), service✓ is_trial✓
    **$0** provisioning.
  - Contrast: services PAID (plan 158) → `/my/instances/155/checkout`. Hosting
    paid earlier → `…/checkout` `$21.50`.
- **Trial rule learned**: `_check_one_trial_per_client` (saas_instance.py:1189)
  = **one trial total per commercial entity** (hosting OR services), plus a
  per-type `saas_*_trial_used` create-gate. So testing both needs TWO clients.
- ⚠️ Minor bug spotted (not fixed): a trial create that fails the
  one-trial constraint inside `service_order`/`hosting_order` leaks a **draft
  instance** (the caught ValidationError doesn't roll back the INSERT; left
  instances 151/152 in `saas_dev`). Worth a savepoint around the create.
- ⚠️ Local only (saas_dev) — **not deployed to the server** (debug_otp is a
  prod security blocker; branch is `architecture-evolution`).

### 7) Root-cause fix: never strand a client on an unreachable Docker host (2026-06-21)
- **Problem**: a project (`ensan`) stuck in `pending_provision` — the allocator
  had placed it on docker host `rt-default` @ `203.0.113.10` (an RFC-5737
  placeholder IP), so every deploy SSH-timed-out and the customer waited.
  Root cause: `_allocate_docker_server` considered ANY `is_docker_host` server
  with no reachability check.
- **Fix (saas_core, additive)**:
  - `saas.server`: new `health_state` (unknown/ok/unreachable) + `last_health_check`
    + `last_health_error`; `_probe_reachable()` (fast TCP connect to the SSH
    endpoint) + `_update_health()`; `_cron_health_check()` (every 5 min,
    `data/saas_server_health_cron.xml`).
  - Allocation (`_allocate_docker_server` + `_allocate_overcommit_server`):
    exclude `health_state=='unreachable'` AND **live-probe** the chosen
    candidate, failing over to the next healthy host; return None if none.
  - `saas.region.has_capacity()`: a known-unreachable docker host no longer
    counts → the order is **refused at checkout** with a clear message instead
    of creating a doomed project.
  - Server form view shows the health fields (badge + last error).
- **Tested**: `tests/test_server_health.py` (5 tests, real local sockets);
  full suite **145/145 green** on saas_ci (no regression).
- **Proven live on saas_dev**: health cron flagged `203.0.113.10` unreachable
  / `165.245.245.196` ok; allocator routed to the healthy host; with only the
  unreachable host left, a new order returned **"Service temporarily unavailable
  in the selected region"** (fail-fast, no stranded instance).
- ⚠️ Deploy needs `-u saas_core` (new fields/cron/view). This fix is clean and
  prod-deployable independent of the debug_otp/branch caveats above.

## Bugs hit and their fixes (so we don't repeat them)
- "column environment does not exist" on live → needed `-u saas_core` (module
  upgrade after adding fields).
- `_get_env_plan` created a plan at price 0 → violated `_check_price_floor` on
  the live DB. Fix: stamp a floor-aware monthly price + link a hosting product.
- Stale homepage cache after git pull → fix: always `systemctl restart odoo` +
  verify the hash match.
- `tsc noUnusedLocals` breaks the build on any unused import left after a
  refactor — clean up imports.
- JSX broke once due to an `// eslint-disable` comment placed inside a JSX tag.

---

## Fixed rules/decisions (do not revert)
- Pricing: yearly discount = infra only (support/backup flat ×12), cheapest
  region = default, backup billed at checkout, unified tier+custom pricing.
- Services vs hosting: same system + pricing engine (services priced higher);
  services is a locked-down MANAGED app — only snapshots exposed, no
  code/logs/DB-ops.
- Billing overhaul v46: wallet, auto-renew + provider abstraction, blocks-only
  overage, full proration, trial 25%×3, recommended-region default.
- Backup: daily = restic deduplicated, on-demand = zip ephemeral (don't unify).
- Hosting DB create = PG template clone (`__odoo_template_<sub>` +
  `CREATE DATABASE WITH TEMPLATE` + filestore `cp -a`).

---

## Open / possible next tasks
- (Nothing urgent open right now — the last two requests are done, deployed,
  and verified.)
- Follow up on any further UI/UX notes from the user.
- Confirm the SSH password has been rotated (security).

---

## Key files for reference
- `saas_core/models/saas_instance.py` — environment fields/billing/crons.
- `saas_core/models/saas_instance_repo.py` — branch automation.
- `saas_core/models/saas_build.py` — build records.
- `saas_website/controllers/api.py` — env serializer + repo-per-project.
- `veltnex/src/pages/portal/Environments.tsx` — the workspace + in-page tabs +
  ProjectSettingsPanel.
- `veltnex/src/components/layout/PortalLayout.tsx` / `PublicNav.tsx` / `Footer.tsx`.
- `veltnex/src/index.css` — design tokens.
