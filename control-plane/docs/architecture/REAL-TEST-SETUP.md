# Real-Test Server Setup (live integration environment)

**Date:** 2026-06-17 · **Branch:** `architecture-evolution`
**Purpose:** a real server to validate the provisioning pipeline end-to-end (not just local unit tests).

---

## Server
- Host: **165.245.245.196** (DNS `odoo.odex.sa`). DigitalOcean **2 vCPU / 3.8 GB RAM / 77 GB**, Frankfurt,
  hostname `odoosnapshots-s-2vcpu-4gb-fra1`. Ubuntu 24.04.3. Docker 29.5.3.
- **Disposable test box** (user will delete it). No customers, no real data.

## Access (control-plane → server)
- Control-plane SSH key: `/home/moutaz/.ssh/saas_cp_ed25519` (ed25519, `saas-control-plane`).
- Authorized for **root** on the server (minimal-footprint choice: no weakened `saas` user / no NOPASSWD).
- Server host-key fingerprint (for MITM pinning in `saas.server`):
  `SHA256:zEkqyqrjlQShNw2EDFKdgPCtzhhL0fz9T8WfHZoQDJ4`.
- ⚠️ **Rotate the root password** — it was pasted in chat. Box is disposable, so bounded risk.

## Prep approach (minimal footprint)
- SSH as **root** (avoids the `saas`/NOPASSWD:ALL the runbook uses).
- PostgreSQL scoped to the **Docker subnet only** (`172.16.0.0/12`), listen on `localhost,172.17.0.1` —
  NOT exposed to the public internet.
- First smoke test uses the **official `odoo:18.0`** image (self-contained; skips the `odoo-light` build +
  ~1 GB source clone). The compose template still bind-mounts `/opt/odoo-source/18.0` (created empty;
  harmless for the official image). Switch to `odoo-light` later for a fully faithful test.

## Status
- [x] Key access established + box inventoried.
- [~] **Background prep running** (`/root/saas_step1.sh`, log `/root/saas_step1.log`): installs
  postgresql + nginx + restic + git, pulls `odoo:18.0`. Currently waiting on the boot-time
  `unattended-upgrade` dpkg lock; proceeds automatically when it releases.
- [ ] PostgreSQL config (listen on docker bridge + `pg_hba` for `172.16.0.0/12`).
- [ ] Register control-plane records in local `saas_dev`: `saas.server`, `saas.ssh.key.pair`,
  `saas.region`, `saas.based.domain`, `saas.odoo.version` (odoo:18.0), `saas.plan`, `saas.product`.
- [ ] Provision a real `saas.instance` → verify container up + HTTP reachable + login.
- [ ] DataService round-trip: backup → destroy → restore → verify; clone + neutralize.
- [ ] Resource baseline under light load.

## Things that may still need the user (I will work around where possible)
- **Wildcard DNS** `*.odoo.odex.sa → 165.245.245.196` for tenant subdomains. Workaround: test via
  `IP:port` or a single hand-set A record, so not a hard blocker.
- **Object-storage credentials** (S3 / DO Spaces) for the restic backup→restore test. Workaround:
  test PG-template clone (no object store) first; defer object-store backup until creds exist.

## Next step
Wait for the background install to finish, then PG config + register records + provision.
