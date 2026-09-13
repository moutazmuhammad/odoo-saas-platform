# Local testing guide

How to run the SaaS platform on your machine and exercise every customer-facing
and admin flow. This is a **seed-only / mock-provisioning** setup: the UI,
funnel, billing, wallet, trials, checkout, portal and admin backend are all
fully testable, but tenant instances are **not actually deployed** (no real
Docker/SSH hosts) — see [Caveats](#caveats).

## 1. Start / stop

```bash
scripts/devctl.sh up        # start PostgreSQL + Odoo  -> http://127.0.0.1:8069
scripts/devctl.sh status    # show what's running
scripts/devctl.sh down      # stop both
scripts/devctl.sh logs      # tail the Odoo log
```

Paths auto-derive from the checkout and are env-overridable (`SAAS_DEV_BASE`,
`SAAS_VENV`, `SAAS_ODOO_SRC`, `SAAS_RUNTIME`, `SAAS_PGROOT`, `SAAS_PGBIN`,
`SAAS_PGPORT`, `SAAS_DB`). Default layout = siblings of the repo:
`../odoo18` (source), `../odoo18-venv` (venv), `../saas-odoo` (conf+data),
`../saas-pg` (userspace PG cluster on port 5455).

## 2. Log in

Open **http://127.0.0.1:8069**.

**Seeded customer accounts** — password `demo1234`:

| Login | What they have (for testing) |
|-------|------------------------------|
| `acme@example.com`     | Live hosting project + staging/dev envs + one provisioning + **$500 wallet** |
| `globex@example.com`   | An awaiting-payment order + a running **pharmacy trial** + $150 wallet |
| `initech@example.com`  | Suspended, cancelled, and failed instances |
| `umbrella@example.com` | Stopped (yearly, US region) + a running clinic service + $1000 wallet |

**Admin backend:** http://127.0.0.1:8069/web — login `admin`, password `admin`.

## 3. Flows you can test

### Register a new account
1. Go to **/register**, fill the form, submit — an OTP is "sent" (out-of-band).
2. Get the code: `scripts/devctl.sh otp`  → paste it to finish sign-up.

### Free trial (no payment)
Hosting: **/hosting** → "Start your free trial" → pick name/subdomain/region →
"Start free trial". Lands on the project with **no checkout** ($0).
Services: **/services** → a product → its trial plan. (One trial per customer
per type — hosting vs services are separate.)

### Paid order + checkout (simulated card)
1. **/hosting** → configure workers/storage/region → order → you land on the
   project **checkout**.
2. Pay with the **Demo** provider (payment_demo is installed + enabled). Use any
   test card values it accepts — the payment is simulated, no real gateway.
3. On payment the order flips to `paid` and auto-deploys (deploy is mocked —
   see caveats). Wallet-covered small orders may auto-complete without the card.

### Portal
- **Dashboard / Projects** (`/my`) — your projects, statuses, "needs attention".
- **Project workspace** (`/my/instances/<id>`) — environments tree
  (production/staging/dev), tabs (Overview / Metrics / Databases / Shell / SQL /
  Logs / Snapshots), start/stop/re-deploy, backups, daily-backup add-on.
- **Billing** (`/my/billing`) — invoices, wallet balance + ledger, auto-renew.
- Plan change / upgrade / downgrade, cancel, reactivate.

### Admin backend (`/web`)
The **SaaS** app: servers/regions/domains/versions, products & plans, support
plans, wallets, margins, and the **crons** (Settings ▸ Technical ▸ Scheduled
Actions) — see below for running one on demand.

## 4. Reset the data

```bash
scripts/devctl.sh seed      # re-apply the seed (idempotent — safe to re-run)
scripts/devctl.sh reset     # DANGER: drop + reinit the DB, then reseed
```

The seed is `scripts/seed_dev.py`: 3 regions, 2 mock hosts, 3 products (hosting +
pharmacy + clinic), 9 plans, 4 customers with wallets, instances across every
lifecycle state, the demo payment provider, and 24h of metrics/backups on the
Acme project.

## Caveats

- **Mock provisioning.** There are no real Docker/SSH hosts, so instances don't
  actually boot. Lifecycle actions (deploy/start/stop/re-deploy) enqueue a
  durable job that will *fail* against the mock host — the UI, state machine,
  billing and portal are all still fully exercisable. "Open app" links won't
  resolve.
- **Background crons are OFF** by default so the seeded data stays put (the
  health cron would otherwise flag the mock hosts unreachable and empty the
  region picker). To exercise a specific one:
  `scripts/devctl.sh cron "Trial Expiry"` (name fragment). Turn all on/off with
  `scripts/devctl.sh crons-on` / `crons-off` (server must be down for those).
- **PDF invoices** need `wkhtmltopdf` (not installed). Everything else works
  without it; invoice data shows in the portal, only the PDF render is absent.
- **Run the test suite:** `scripts/devctl.sh test` (spins a throwaway `saas_test`
  DB with the right db-filter + port, runs the saas test tags).
