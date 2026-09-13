# Security Incident — test server compromise (Outlaw botnet)

**Date:** 2026-06-17 · **Host:** `165.245.245.196` (disposable DigitalOcean test box, tenants `rt1`/`rt2`).
**Severity:** High (host compromise) · **Data impact:** none (disposable test box, no customer data).
**Status:** **Contained** in place. Rebuild still recommended for any non-disposable use.

## What it was
The **Outlaw / "Dota" SSH cryptomining botnet** (XMRig miner + Go SSH-bruteforce spreader). Discovered while
inspecting the box for Phase 2: a process `cc05d646` running as the **`odoo`** user at 99.8% CPU / 2.4 GB RAM
for ~12h.

## Indicators of compromise (IOCs)
- Malware dirs: `/var/tmp/f6ff69dc/`, `/var/tmp/824e59e9/`, `/var/tmp/.ladyg0g0/`, file `/var/tmp/cache`.
- Binaries: `cc05d646` (3.1 MB, XMRig miner), `24f28456` (885 KB, Go spreader), `cache` (1 MB, SSH scanner),
  `.c` / `.pr1nc35` / `.ladyg0g0` (config/marker files).
- Miner C2/pool: outbound TLS to `85.11.167.190:443`. Spreader: outbound SSH to `:222` targets.
- Persistence: `odoo` user crontab (`* * * * *`, `@reboot`, `@daily`, `@monthly`, `*/30 … /.c`) +
  Outlaw backdoor key in `/home/odoo/.ssh/authorized_keys` (`ssh-rsa AAAAB3…JQAA…`).
- Active SSH brute-force from `87.251.64.149` at time of cleanup.

## Likely entry vector
- **PostgreSQL listening on `0.0.0.0:5432`** (publicly exposed) and/or **SSH password auth enabled** with weak
  creds (the root password had been pasted in chat previously). Ran as `odoo`, not root — root was not
  backdoored (root `authorized_keys` held only the legit control-plane ed25519 key).

## Remediation performed (in order)
1. Removed cron persistence first (stop respawn): `crontab -r -u odoo` + cleared spool.
2. Removed backdoor key `/home/odoo/.ssh/authorized_keys`.
3. Killed all malware processes (by `/proc/*/exe` path + comm name).
4. Deleted all malware artifacts under `/var/tmp`.
5. Firewalled PostgreSQL: iptables DROP `:5432` except `172.16.0.0/12` (docker bridge) + `127.0.0.0/8`.
6. Hardened SSH: `PasswordAuthentication no`, `PermitRootLogin prohibit-password` (fixed DO's
   `50-cloud-init.conf` which had re-enabled password auth); locked `odoo` password; blocked the brute-forcer.

## Verification (post-cleanup)
- CPU idle, RAM reclaimed (used 3.6 GB → 1.3 GB). No malicious outbound connections. `/var/tmp` clean.
- All crontabs empty. No `ld.so.preload`, no rogue systemd units, no script hooks, no process respawn.
- Tenants `rt1`/`rt2` healthy and serving HTTP 200. Control-plane key login confirmed working post-hardening.

## Follow-ups (for the operator)
- [ ] **Rebuild the droplet** when convenient (containment ≠ guaranteed trust). Phase-2 setup is scripted so
      re-provisioning a fresh box is cheap.
- [ ] On the rebuild, bake in the hardening: PG bound to `172.17.0.1` + `hba` `172.16/12` only (never
      `0.0.0.0`), SSH key-only from the start, no passwords pasted in chat (rotate any that were).
- [ ] iptables rules added here are runtime-only — persist them (or re-apply via the provisioning script)
      if the box survives a reboot.
