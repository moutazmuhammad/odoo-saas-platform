# Local runtime: control-plane as a systemd service

A second way to run the control-plane locally, alongside
[`LOCAL-TESTING.md`](LOCAL-TESTING.md)'s `devctl.sh up` (which uses `nohup`
and stops when you log out or run `devctl.sh down`). This setup instead
registers two **systemd user services** — no root required — so the control
plane survives independently of any single terminal session, restarts
automatically on failure, and starts on login (see Autostart below).

Do not run both at once against the same ports/database — pick one. If
`devctl.sh status` reports Postgres/Odoo already up, that's very likely this
systemd setup already running; don't also run `devctl.sh up`.

## What's running, and where

| Thing | Location | Notes |
|---|---|---|
| Odoo 18 source | `/home/moutaz/odoo-saas-runtime/odoo18` | Shallow clone, branch `18.0` |
| Python venv | `/home/moutaz/odoo-saas-runtime/odoo18-venv` | Python 3.12; Odoo core `requirements.txt` + `control-plane/requirements.txt` (see that file's own header for why the two must be installed together, not independently) |
| Userspace PostgreSQL | `/home/moutaz/odoo-saas-runtime/saas-pg` | Own data dir, own socket, port **5455** — entirely separate from any system Postgres cluster (this machine also has system clusters on 5432/5433 that this setup does not touch) |
| `odoo.conf` + logs | `/home/moutaz/odoo-saas-runtime/saas-odoo/` | `db_filter = ^saas_dev$`; `logfile` set, so `journalctl --user -u saas-control-plane` shows only start/stop lifecycle events, not Odoo's own request logs — tail the logfile for those |
| Microk8s kubeconfig | `/home/moutaz/odoo-saas-runtime/kubeconfig` | Copied out for convenience; `microk8s config` is the source of truth. **Not consumed by any control-plane code today** — see the integration-status warning below |

**Local-dev-only credentials** (both machine-local, not reachable from
outside this host, not used anywhere else): Postgres role `odoo` /
password `odoo`; Odoo master password `localdev_admin`. Do not reuse
either of these anywhere a real credential is expected.

## systemd units

```
~/.config/systemd/user/saas-postgres.service        # userspace PG, port 5455
~/.config/systemd/user/saas-control-plane.service   # Odoo, depends on the above
```

```bash
systemctl --user status saas-control-plane.service saas-postgres.service
systemctl --user restart saas-control-plane.service   # e.g. after an addon-code change
systemctl --user stop saas-control-plane.service saas-postgres.service
journalctl --user -u saas-control-plane -f            # lifecycle events (not request logs)
tail -f /home/moutaz/odoo-saas-runtime/saas-odoo/log/odoo.log   # actual Odoo logs
```

Both units are `enabled` (`WantedBy=default.target`), so they start
automatically the next time this user's systemd instance starts. That
normally means "on next login" unless lingering is enabled
(`loginctl enable-linger moutaz`) — lingering was **not** enabled as part of
this setup (would need to be checked/granted separately); without it, the
services stop when the last session for this user ends, same as any other
per-user systemd unit.

## Using `devctl.sh`'s other subcommands (seed / shell / otp / cron) against this instance

`devctl.sh` derives its paths from `SAAS_DEV_BASE`, defaulting to the
monorepo root (a sibling of `control-plane/`) — **not** where this systemd
setup actually lives. Point it here explicitly:

```bash
export SAAS_DEV_BASE=/home/moutaz/odoo-saas-runtime
control-plane/scripts/devctl.sh status   # should report both already up
control-plane/scripts/devctl.sh seed     # runs scripts/seed_dev.py against this DB
control-plane/scripts/devctl.sh otp
control-plane/scripts/devctl.sh shell
```

`devctl.sh up`/`down` still use `nohup`/a pidfile for the Odoo process, so
don't run `devctl.sh down` here — it would look for a pidfile this systemd
setup never created and likely report "not running" without actually
stopping anything; use `systemctl --user stop` instead.

## Verified working (2026-09-14)

- `saas_core` + `saas_website` (and all 59 modules in the dependency chain)
  installed cleanly into a fresh `saas_dev` database: "59 modules loaded in
  38.07s", "Registry loaded in 46.587s", zero errors (a few pre-existing,
  harmless warnings: duplicate field labels, a view accessibility lint, an
  unrecognized `unaccent` field parameter — none block loading, none
  introduced by this setup, not fixed here as it's out of this task's
  scope).
- HTTP layer confirmed serving real traffic: `GET /web/login` → 200, the
  actual VELTNEX-branded login page (not a generic Odoo page) — confirms
  `saas_website`'s theming/branding is really wired up, not just installed.
- The built SPA is served correctly (`GET /saas_website/static/spa/assets/*.js`
  → 200).
- The JSON API layer works end-to-end: `POST /saas/api/v1/meta` with a
  JSON-RPC body returns real pricing/config data from the database (not a
  stub) — confirms controller → ORM → response serialization all work
  together for a real request, not just at the Python-import level.

## ⚠️ Integration status: still none

Microk8s (the Compute microservice's cluster — see
[`/ROADMAP.md`](../../ROADMAP.md) §3.3) is running independently on this
same machine, with the operator from `compute/operator` deployed in its
`odoo-system` namespace. **Both being alive at the same time does not mean
they are connected** — nothing in this control-plane instance has any code
path that talks to microk8s for real tenant provisioning. The kubeconfig
above is saved for convenience, ready for when `ROADMAP.md`'s Phase 2
(Kubernetes cutover) actually builds that connection; it is not consumed by
real provisioning today. See `ROADMAP.md` §1 for the full warning — it
applies exactly as much with both services running as it did before.
