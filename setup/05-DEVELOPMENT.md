# Development

A local control plane (Odoo 18 + userspace PostgreSQL), the tests for every component, and how to build the SPA, the customer docs and the images. For a real server, use [02-SAAS-SERVER-SETUP.md](02-SAAS-SERVER-SETUP.md); for a tenant cluster to test against, the single-node variant of [03a-MICROK8S-CLUSTER-SETUP.md](03a-MICROK8S-CLUSTER-SETUP.md).

---

## 1. Prerequisites

- Linux, Python 3.12, PostgreSQL 14 binaries (`/usr/lib/postgresql/14/bin`; other versions via `SAAS_PGBIN`)
- Odoo build deps: `libpq-dev libldap2-dev libsasl2-dev libxml2-dev libxslt1-dev libjpeg-dev zlib1g-dev`
- Node 20 (frontend), Go (version in `compute/operator/go.mod`), Docker, `kubectl`, `helm`

---

## 2. Local layout

`control-plane/scripts/devctl.sh` expects these next to each other under one base directory, `SAAS_DEV_BASE` (default: the repo's parent directory). Every path is overridable (`SAAS_ODOO_SRC`, `SAAS_VENV`, `SAAS_RUNTIME`, `SAAS_CONF`, `SAAS_PGROOT`, `SAAS_PGBIN`, `SAAS_PGPORT`, `SAAS_DB`).

| Path | Content |
|---|---|
| `$SAAS_DEV_BASE/odoo18` | Odoo 18 source |
| `$SAAS_DEV_BASE/odoo18-venv` | Python venv |
| `$SAAS_DEV_BASE/saas-odoo/odoo.conf` | config; `data/` and `log/` beside it |
| `$SAAS_DEV_BASE/saas-pg/data` | PostgreSQL cluster on `127.0.0.1:5455` |
| database | `saas_dev` |

```bash
export SAAS_DEV_BASE=~/odoo-saas-runtime
mkdir -p $SAAS_DEV_BASE/saas-odoo/{data,log} $SAAS_DEV_BASE/saas-pg/sock

git clone --depth 1 --branch 18.0 https://github.com/odoo/odoo.git $SAAS_DEV_BASE/odoo18
python3.12 -m venv $SAAS_DEV_BASE/odoo18-venv
$SAAS_DEV_BASE/odoo18-venv/bin/pip install -r $SAAS_DEV_BASE/odoo18/requirements.txt
$SAAS_DEV_BASE/odoo18-venv/bin/pip install -r control-plane/requirements.txt coverage

/usr/lib/postgresql/14/bin/initdb -D $SAAS_DEV_BASE/saas-pg/data -U odoo -W
```

`$SAAS_DEV_BASE/saas-odoo/odoo.conf`:

```ini
[options]
admin_passwd = <master password>
db_host = 127.0.0.1
db_port = 5455
db_user = odoo
db_password = <postgres password>
dbfilter = ^saas_dev$
addons_path = <BASE>/odoo18/addons,<repo>/control-plane
data_dir = <BASE>/saas-odoo/data
logfile = <BASE>/saas-odoo/log/odoo.log
http_port = 8069
limit_time_real_cron = 120
limit_time_real = 1800
```

---

## 3. Create the database

```bash
control-plane/scripts/devctl.sh reset
```

**Destructive:** drops `saas_dev`, installs `saas_core,saas_billing,saas_website,payment_demo` with demo data, then runs `control-plane/scripts/seed_dev.py` (fake customers, regions, a mock cluster; seed users use password `demo1234`, e.g. `acme@example.com`). It needs PostgreSQL up and Odoo down.

`saas_iam` is not part of `reset`. Install it afterwards when you work on team permissions:

```bash
cd $SAAS_DEV_BASE/odoo18 && $SAAS_DEV_BASE/odoo18-venv/bin/python odoo-bin \
  -c $SAAS_DEV_BASE/saas-odoo/odoo.conf -d saas_dev -i saas_iam --stop-after-init
```

---

## 4. Run it

Pick **one** of the two ways. Don't mix them: `devctl.sh down` stops PostgreSQL under the systemd services.

### 4.1 systemd user services (what the main dev machine uses)

Unit files in `~/.config/systemd/user/`:

| Unit | Runs |
|---|---|
| `saas-postgres.service` | `pg_ctl -D <BASE>/saas-pg/data ... -p 5455 start` (Type=forking) |
| `saas-control-plane.service` | `odoo-bin -c <BASE>/saas-odoo/odoo.conf -d saas_dev`; requires `saas-postgres` |
| `saas-job-worker.service` | `control-plane/scripts/devctl.sh worker` with `Environment=SAAS_DEV_BASE=...`, `TimeoutStopSec=900` so running jobs can finish |

```bash
systemctl --user enable --now saas-postgres saas-control-plane saas-job-worker
systemctl --user status saas-control-plane saas-job-worker
```

### 4.2 devctl.sh (no services)

| Command | Does |
|---|---|
| `devctl.sh up` / `down` / `status` | Start/stop PostgreSQL and Odoo (Odoo with `nohup`, pid file in `log/`) |
| `devctl.sh worker` | The job worker in the foreground (`odoo-bin saas-jobs`) |
| `devctl.sh logs` | `tail -f` the Odoo log |
| `devctl.sh shell` | `odoo-bin shell` on `saas_dev` |
| `devctl.sh seed` | Run `seed_dev.py` again |
| `devctl.sh otp` | Print the latest registration code from the log (sign-up in the browser) |
| `devctl.sh cron "<name fragment>"` | Run one cron now |
| `devctl.sh crons-off` / `crons-on` | Disable/enable all `SaaS:` crons (stops Odoo first) |
| `devctl.sh test` | The Odoo test suite (section 5.1) |
| `devctl.sh reset` | Section 3 |

### 4.3 After a code change

- Any Python change: restart **both** the server and the worker (the worker runs the same code).
- Model, view or data changes: also upgrade the modules.

```bash
systemctl --user stop saas-control-plane saas-job-worker
cd $SAAS_DEV_BASE/odoo18 && $SAAS_DEV_BASE/odoo18-venv/bin/python odoo-bin -c $SAAS_DEV_BASE/saas-odoo/odoo.conf \
  -d saas_dev -u saas_core,saas_billing,saas_website --stop-after-init     # add saas_iam if installed
systemctl --user start saas-control-plane saas-job-worker
```

A new `store=True` field computed from another addon also needs an upgrade test on a database **with data**, not only the fresh-install test run.

**Check:** backend `http://127.0.0.1:8069/web` (as `admin`; *SaaS* group Manager is set on install), portal `http://127.0.0.1:8069/`.

---

## 5. Tests

### 5.1 Control plane (Odoo)

```bash
SAAS_DEV_BASE=~/odoo-saas-runtime control-plane/scripts/devctl.sh test
```

It drops and recreates a throwaway `saas_test` database, installs `saas_core,saas_billing,saas_website`, runs their tests under `coverage` on port 8093 with `--db-filter=^saas_test$`, and prints `N failed, M error(s) of K tests` and a coverage report. Full log: `<BASE>/saas-odoo/log/test.log`. It never touches `saas_dev`.

`saas_iam` tests, same pattern:

```bash
cd $SAAS_DEV_BASE/odoo18 && $SAAS_DEV_BASE/odoo18-venv/bin/python odoo-bin -c $SAAS_DEV_BASE/saas-odoo/odoo.conf \
  -d saas_test_iam -i saas_iam --test-enable --test-tags=/saas_iam \
  --db-filter='^saas_test_iam$' --http-port=8093 --log-level=test --stop-after-init
```

**Never test mutating flows on `saas_dev` through an `odoo-bin shell` and a rollback:** billing crons call `cr.commit()`. Use a copy (`createdb -T saas_dev ...`) or a `pg_dump` first.

### 5.2 Lints and script tests (plain Python)

```bash
python3 control-plane/scripts/lint_addon_boundaries.py
python3 control-plane/scripts/lint_csrf_routes.py
(cd control-plane/scripts && python3 -m unittest test_lint_csrf_routes test_lint_addon_boundaries)
python3 -m unittest compute/tools/backup-tool/test_backup_restore.py
$SAAS_DEV_BASE/odoo18-venv/bin/python -m unittest scripts/tests/test_build_storage.py   # needs the control-plane venv
```

### 5.3 Operator (Go)

```bash
cd compute/operator
make test          # regenerates CRDs/deepcopy, fmt, vet, downloads envtest assets into bin/, go test ./... -race
make test-unit     # only internal/resources, no envtest
```

`make test` (via `make manifests`) regenerates the CRD into `config/crd/bases` and copies it to `compute/charts/odoo-operator/crds/`, which is what clusters apply. If `config/rbac/role.yaml` changed, sync it by hand into `compute/charts/odoo-operator/templates/clusterrole.yaml`.

### 5.4 Frontend

```bash
cd frontend/veltnex && npm ci
npm test               # vitest run
npm run lint           # tsc --noEmit
npm run test:coverage
```

CI (`.github/workflows/ci.yml`) runs the SPA tests + build, the Odoo tests (`saas_core`, `saas_website`), the CSRF lint, gitleaks, the operator `make test`, and a trivy scan of both images on every PR.

---

## 6. Build the SPA

```bash
cd frontend/veltnex && npm run build     # tsc --noEmit && vite build
```

Output goes straight into `control-plane/saas_website/static/spa/` (committed; servers need no Node). Commit the rebuilt bundle together with the source change.

`npm run dev` serves the SPA standalone and proxies `/saas`, `/web` and `/my/invoices` to `http://localhost:8018` (`vite.config.ts`). Change it to `8069` for the setup above.

---

## 7. Customer documentation

The customer docs are generated: the only source is `frontend/veltnex/src/lib/docs-catalog.json` (English + Arabic). Never edit `docs/customer/en|ar` by hand.

```bash
python3 scripts/generate-customer-docs.py           # regenerate docs/customer/
python3 scripts/generate-customer-docs.py --check   # fails if the output is stale
```

Editorial rules: [docs/customer/MAINTAINING.md](../docs/customer/MAINTAINING.md). Translation: [docs/frontend-languages.md](../docs/frontend-languages.md), [docs/arabic-localization-glossary.md](../docs/arabic-localization-glossary.md).

---

## 8. Images

```bash
cd compute/operator && make docker-build docker-push IMG=docker.io/moutazmuhammad/odoo-saas-operator:<tag>
docker build -t docker.io/moutazmuhammad/odoo-saas-backup-tool:<tag> compute/tools/backup-tool
docker push docker.io/moutazmuhammad/odoo-saas-backup-tool:<tag>
```

Always a new tag. Which files carry the tag, and how to roll it out: [04-IMAGES-AND-REGISTRY.md](04-IMAGES-AND-REGISTRY.md), section 2.

---

## 9. Troubleshooting

| Symptom | Look at |
|---|---|
| Errors | `<BASE>/saas-odoo/log/odoo.log` (server and worker share it; different PIDs) |
| Jobs don't run | `systemctl --user status saas-job-worker`. Without a worker, the server runs jobs itself under its time limits. |
| Queued work | `psql -h 127.0.0.1 -p 5455 -U odoo saas_dev -c "select id, model, res_id, method, state, attempts from saas_job order by id desc limit 20"` |
| Tenant stuck | `kubectl describe odooinstance -n <ns> <name>`: the conditions say why (image pull, database, route/TLS, update Job) |

`control-plane/scripts/rt_register.py` and `view-metrics-locally.sh` are leftovers from the removed SSH/Docker backend and specific test databases; don't use them.
