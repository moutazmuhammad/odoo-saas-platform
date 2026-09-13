# SECURITY AUDIT — Odoo 18 SaaS Platform

> Pre-acquisition technical due-diligence. Scope: `saas_core`, `saas_website`, `veltnex`.
> Method: direct code reading with file:line verification. Findings that earlier automated
> passes overstated have been **downgraded or dropped after verifying the actual guard
> exists** — those are recorded at the end under "Cleared / Overstated" so the board sees the
> full picture.
>
> Severity counts (this file): **Critical 3 · High 6 · Medium 7 · Low 3** (19 findings)

---

## SEC-001
- **Severity:** Critical
- **Component:** Registration / OTP authentication (JSON API + SPA)
- **File Path:** `saas_website/controllers/api.py`; `veltnex/src/pages/Register.tsx`
- **Line Numbers:** `api.py:233`, `api.py:250`; `Register.tsx:130`, `Register.tsx:224`
- **Description:** The phone-OTP registration endpoints return the freshly generated code to
  the client in the JSON body (`return ok({'otp_sent': True, 'debug_otp': otp.code})`), and
  the SPA reads and renders it (`setDebugOtp(res.debug_otp ?? null) // TODO: REMOVE before
  production`). Anyone calling the public register-start/resend endpoint receives a valid OTP
  for any phone number without ever receiving the SMS.
- **Business Impact:** Complete bypass of the only signup identity check. Enables mass
  fake-account creation, trial farming, phone-number enumeration, and impersonation. Ships a
  documented "remove before production" backdoor into a product about to be acquired — an
  immediate diligence red flag.
- **Technical Impact:** The OTP gate provides zero assurance. Any downstream control that
  assumes "verified phone == real human" is void.
- **Recommended Fix:** Remove `debug_otp` from both API responses and all SPA state/render
  paths. Gate any dev convenience behind a server-side `ir.config_parameter` that is false in
  production and never serialize the code to the client. Add a regression test asserting the
  field is absent.

---

## SEC-002
- **Severity:** Critical
- **Component:** Secrets management — platform DB
- **File Path:** `saas_core/models/saas_instance.py`; `saas_core/models/saas_ssh_key_pair.py`
- **Line Numbers:** `saas_instance.py:319` (`admin_password`), `:332` (`db_password`); `github_token` usages `:2168, :3100, :4145`; `saas_ssh_key_pair.py:32` (`private_key_file` Binary)
- **Description:** Tenant DB passwords, Odoo admin passwords, Git provider tokens, and server
  SSH **private keys** are stored as plain `fields.Char`/`fields.Binary` in the control-plane
  Odoo database with no application-level encryption. A single read of the platform DB (SQL
  injection elsewhere, a leaked nightly dump, a rogue support user with DB access, or a
  restored backup) exposes credentials to **every tenant database and every host**.
- **Business Impact:** One control-plane compromise = total platform compromise and a
  reportable breach of all customers' data. Fails SOC 2 / ISO 27001 / GDPR "appropriate
  technical measures." Likely a deal-blocker for an enterprise acquirer.
- **Technical Impact:** No blast-radius containment; credential rotation is manual and
  platform-wide; backups of the control DB are themselves crown-jewel secrets.
- **Recommended Fix:** Move secrets to a KMS/secret manager (Vault, AWS/GCP Secrets Manager).
  At minimum, envelope-encrypt these columns with a key held outside the DB. Rotate all
  currently stored credentials post-migration, since they must be assumed exposed.

---

## SEC-003
- **Severity:** Critical
- **Component:** Tenant container runtime
- **File Path:** `saas_core/templates/Dockerfile.tenant.jinja`
- **Line Numbers:** `:13` (`USER root`); compose run path `saas_instance.py:5130-5246`
- **Description:** Tenant containers run as `root`, and `docker compose` is invoked over SSH
  as a privileged host user. Combined with shared Docker hosts (many tenants per host), a
  container breakout (kernel/runtime CVE, misconfigured mount) escalates straight to host
  root and therefore to **every co-tenant on that host**.
- **Business Impact:** Converts a single-tenant application bug into a multi-tenant data
  breach. Undermines any "isolated hosting" marketing claim and SLA.
- **Technical Impact:** No defense-in-depth between the (customer-controllable) Odoo workload
  and the host. Customers can install arbitrary pip packages and code into these containers.
- **Recommended Fix:** Run containers as a non-root UID, drop Linux capabilities
  (`cap_drop: [ALL]`), add `no-new-privileges`, seccomp/AppArmor profiles, read-only root
  filesystem where possible, and run the daemon rootless or gVisor/Kata for untrusted code.

---

## SEC-004
- **Severity:** High
- **Component:** PostgreSQL tenant roles
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** `:3269`, `:3271` (`CREATE/ALTER ROLE ... WITH LOGIN CREATEDB`)
- **Description:** Every tenant DB role is granted `CREATEDB`. Tenants can reach the shared
  Postgres cluster (customers have SQL Console + shell access), enumerate `pg_database`, and
  create databases on the shared server. There is no per-tenant Postgres instance or strict
  privilege confinement.
- **Business Impact:** A tenant can exhaust shared cluster resources (disk/connections),
  causing noisy-neighbor outages for paying co-tenants, and probe cluster metadata.
- **Technical Impact:** `CREATEDB` is broader than needed (it exists to support the
  template-clone DB-create flow). Combined with a shared cluster, it weakens tenant isolation.
- **Recommended Fix:** Remove `CREATEDB` from tenant roles; perform DB creation as a
  dedicated provisioning role from the control plane only. Enforce per-role connection/disk
  quotas. Long term, isolate clusters per tier or per high-value tenant.

---

## SEC-005
- **Severity:** High
- **Component:** Host shell terminal
- **File Path:** `saas_core/controllers/ssh_terminal.py`
- **Line Numbers:** `:55` (`TERMINAL_GROUP`), `:300-304`, `:614`
- **Description:** The **host** shell terminal is gated only by membership in
  `saas_core.group_saas_manager`, with no per-host scoping or per-action audit. Any internal
  user added to that group gets an interactive root-ish shell to **all** Docker hosts.
  *(Note: the per-instance container shell, lines ~644-667, DOES correctly enforce instance
  ownership via `access_token` — the gap is specifically the host-level shell.)*
- **Business Impact:** Over-broad standing access for staff; a single compromised or malicious
  internal account reaches all customer infrastructure. No least-privilege story for support.
- **Technical Impact:** Group membership is binary and platform-wide; there is no break-glass,
  no session approval, no command logging to an immutable store.
- **Recommended Fix:** Split host-shell from manager group into a tightly held, audited role;
  require just-in-time elevation, scope to specific hosts, and stream all keystrokes/commands
  to an append-only audit log (see SEC-010).

---

## SEC-006
- **Severity:** High
- **Component:** Plaintext credentials on tenant hosts
- **File Path:** `saas_core/templates/odoo.conf.jinja`; `saas_core/templates/docker-compose.yml.jinja`
- **Line Numbers:** `odoo.conf.jinja:11` (`db_password = {{ db_password }}`); `docker-compose.yml.jinja:33` (`environment:`)
- **Description:** The DB password is rendered in cleartext into `odoo.conf` on each host, and
  the compose file exposes an environment block. Anyone with host access, a stray
  `docker inspect`, or host backups reads tenant DB credentials. Tenants themselves have shell
  access to their own container/config.
- **Business Impact:** Credential sprawl across every host multiplies the breach surface;
  rotating a tenant's DB password requires re-rendering and redeploying config everywhere.
- **Technical Impact:** Secrets live on disk in plaintext on shared infrastructure with no TTL.
- **Recommended Fix:** Inject secrets via Docker/K8s secrets mounted to tmpfs, not config
  files or env vars; restrict `odoo.conf` to `0600` owned by a non-tenant user; never expose
  the DB password to the tenant's own shell.

---

## SEC-007
- **Severity:** High
- **Component:** Supply chain — customer pip packages / Git
- **File Path:** `saas_core/models/saas_instance.py`; `saas_core/templates/pip_install.sh`; `saas_core/models/saas_instance_repo.py`
- **Line Numbers:** pip regex `saas_instance.py:~1318-1343`; repo URL guard `saas_instance_repo.py:34-82`
- **Description:** Tenants supply arbitrary `pip_packages` and Git repos that are installed
  and executed inside root containers. `assert_safe_git_url()` blocks private-IP SSRF (good),
  but package/code content is unvetted — any tenant runs arbitrary code in their (root)
  container by design, and a crafted PEP 508 specifier could smuggle pip options
  (`--index-url`) if the validating regex is not strictly anchored.
- **Business Impact:** Combined with SEC-003 (root) and shared hosts, a malicious package is a
  path to co-tenant compromise. Also a vector for crypto-mining abuse (already observed on the
  test box per project history).
- **Technical Impact:** No package allow-listing, no egress restriction on the install step
  beyond the build sandbox network, no image scanning.
- **Recommended Fix:** Strictly anchor and whitelist the package-name grammar (reject
  whitespace/`;`/`--`); pin an internal PyPI mirror with `--index-url` forced server-side;
  run installs in the egress-restricted sandbox only; scan resulting images; combine with
  non-root containers.

---

## SEC-008
- **Severity:** High
- **Component:** Backup data exposure (presigned URLs)
- **File Path:** `saas_core/models/saas_instance_backup.py`
- **Line Numbers:** `:13` (`PRESIGNED_URL_EXPIRY = 7 * 24 * 3600`), `:201-212`, `:611-624`
- **Description:** Full database+filestore backups are exposed via presigned object-storage
  URLs valid for **7 days**, and the URL is cached on the record. The link is bearer
  authority — anyone it is shared with (or who sees it in logs/history) can download a
  complete copy of the tenant's data for a week with no re-auth.
- **Business Impact:** A single leaked URL is a full data exfiltration of one tenant; 7-day
  validity maximizes the window. Insider exfiltration is trivial and hard to attribute.
- **Technical Impact:** No download audit, no IP binding, no short TTL, no one-time semantics.
- **Recommended Fix:** Reduce TTL to minutes, generate per-download on authenticated request,
  bind to the requester where the provider supports it, and log every download to the audit
  trail.

---

## SEC-009
- **Severity:** High
- **Component:** Observability of security events
- **File Path:** platform-wide (no integration found)
- **Line Numbers:** n/a — `grep` for `sentry|prometheus|datadog|statsd` returns nothing
- **Description:** There is no security/error telemetry pipeline (no Sentry, no metrics
  export, no SIEM, no auth-failure alerting). Failed logins, OTP brute force, restore/delete
  operations, and privilege use are not centrally alertable.
- **Business Impact:** Breaches and abuse go undetected until customers complain; no incident
  timeline for post-mortems or regulatory reporting; no SLA monitoring.
- **Technical Impact:** Mean-time-to-detect is effectively unbounded; forensics depend on
  mutable per-instance text logs (see SEC-010).
- **Recommended Fix:** Add Sentry for exceptions, export auth/abuse counters to Prometheus
  with alerting, and forward security-relevant events to a SIEM.

---

## SEC-010
- **Severity:** Medium
- **Component:** Audit logging / tamper-evidence
- **File Path:** `saas_core/models/saas_instance.py` (provisioning_log text field; unlink/restore paths)
- **Line Numbers:** `provisioning_log` writes throughout; `unlink` `~:1379`, restore `~:7594`
- **Description:** Operational history is kept in a mutable `provisioning_log` text field and
  Odoo `mail.message` chatter, both editable/removable by privileged users. There is no
  append-only audit of who scaled, deployed, restored, or deleted an instance.
- **Business Impact:** No accountability or non-repudiation; blocks SOC 2 CC7/CC8; a malicious
  insider can delete a tenant and erase the trail.
- **Technical Impact:** Compliance evidence is unreliable; incident reconstruction is guesswork.
- **Recommended Fix:** Emit immutable, write-once audit events (actor, action, target,
  result, timestamp) to a dedicated store; forbid update/delete on it.

---

## SEC-011
- **Severity:** Medium
- **Component:** Webhook ingestion
- **File Path:** `saas_core/controllers/webhook.py`
- **Line Numbers:** `:46-49` (lookup by secret), `:56-60` (signature header)
- **Description:** The handler first looks up the repo by `webhook_secret` via ORM `search`,
  then verifies the HMAC signature header (`X-Hub-Signature-256` / `X-Gitlab-Token`). The HMAC
  verification is the real control (good), but the initial secret-equality DB lookup is a
  minor timing/enumeration oracle and the endpoint lacks rate limiting, so it can be probed
  and used to fan out deploy triggers.
- **Business Impact:** An attacker who guesses/leaks a webhook secret can trigger repeated
  deploys (resource abuse / forced redeploys). Lower risk than the automated pass implied,
  because signature verification still gates action.
- **Technical Impact:** Unbounded unauthenticated requests reach DB lookups; no per-IP limit.
- **Recommended Fix:** Rate-limit the endpoint, move secret lookup behind a constant-time
  indexed compare, and de-dupe deliveries by event ID.

---

## SEC-012
- **Severity:** Medium
- **Component:** Container log streaming authorization
- **File Path:** `saas_core/controllers/container_logs.py`
- **Line Numbers:** `:33-46` (`stream_instance_logs`), `:55-61` (`stream_logs`)
- **Description:** `stream_instance_logs(instance_id)` is `auth='user'` and only explicitly
  checks `state` + admin group for non-running instances; the per-instance ownership
  enforcement on the `instance_id` path is not clearly applied before streaming logs, and the
  raw `stream_logs(container_id)` path is admin-group gated only. Logs frequently contain
  secrets, tokens, and PII.
- **Business Impact:** Potential cross-tenant log disclosure if ownership is not enforced on
  `instance_id`; staff can read any container's logs without per-instance authorization.
- **Technical Impact:** Authorization relies on record rules implicitly; needs an explicit
  ownership assertion mirroring `api.py:_instance()`.
- **Recommended Fix:** Reuse the `_instance()` ownership check before streaming; scrub known
  secret patterns from log output; restrict the raw container-id path further.

---

## SEC-013
- **Severity:** Medium
- **Component:** OTP brute-force window
- **File Path:** `saas_website/controllers/api.py` / `registration.py`; `saas_core/models/saas_rate_limit.py`
- **Line Numbers:** rate-limit calls (`otp_verify, 10, 600`); limiter `saas_rate_limit.py:37-48`
- **Description:** A real fixed-window rate limiter exists (good), but the OTP verify policy
  allows ~10 guesses per 600s against a 6-digit code with a 10-minute validity. Across many
  concurrent registrations this is a practical mass-guessing surface, and the OTP is stored
  unhashed.
- **Business Impact:** Residual account-takeover risk on signup; smaller than it appears
  because a limiter exists, but the parameters are loose.
- **Technical Impact:** Fixed-window counters allow burst-at-boundary; unhashed OTP storage
  means a DB read reveals live codes.
- **Recommended Fix:** Shorten OTP validity to ~3 min, lower the attempt ceiling, hash stored
  OTPs, and switch to a sliding window or token-bucket.

---

## SEC-014
- **Severity:** Medium
- **Component:** SSH command construction
- **File Path:** `saas_core/drivers/ssh_docker_driver.py`; `saas_core/models/saas_instance.py`
- **Line Numbers:** numerous `ssh.execute(...)` call sites (e.g. `saas_instance.py:5238-5246`, `8392-8470`)
- **Description:** Provisioning composes many shell commands from model fields (subdomain,
  db_name, paths). Several are correctly `shlex.quote`d (e.g. build-network at `:2957`), but
  the breadth of string-built commands across ~100+ call sites makes injection-by-omission a
  standing risk; field validators (subdomain/db_name regex) are the primary defense.
- **Business Impact:** A single unquoted interpolation of a tenant-controllable value is host
  command execution.
- **Technical Impact:** No central, audited command-builder; quoting is per-call-site and easy
  to get wrong during future edits.
- **Recommended Fix:** Route all remote commands through one helper that always quotes
  arguments and forbids raw f-string command assembly; add tests with metacharacter inputs.

---

## SEC-015
- **Severity:** Medium
- **Component:** Error/exception hygiene
- **File Path:** `saas_core/models/saas_instance.py`
- **Line Numbers:** 85 occurrences of `except Exception:` (e.g. `:912, 1553, 1943, 3841, 5009`)
- **Description:** Broad exception swallowing throughout the god-model hides security-relevant
  failures (auth errors, failed cleanups, partial provisioning) and can mask attacks or leave
  systems in undefined states without alerting.
- **Business Impact:** Incidents are invisible; failed security operations (e.g. a cleanup
  that should revoke access) silently no-op.
- **Technical Impact:** Debuggability and detectability are degraded across the most critical
  module.
- **Recommended Fix:** Narrow exception types, always log with traceback, and re-raise or set
  explicit error state; wire to SEC-009 telemetry.

---

## SEC-016
- **Severity:** Medium
- **Component:** Access-control matrix review
- **File Path:** `saas_core/security/ir.model.access.csv`; `saas_core/security/saas_security.xml`
- **Line Numbers:** whole files
- **Description:** The platform exposes powerful operations (shell, SQL console, restore,
  delete, billing) largely behind a single `group_saas_manager`. There is no granular
  role separation (support vs billing vs infra vs read-only), so least privilege cannot be
  expressed for internal staff.
- **Business Impact:** Over-privileged staff increase insider-risk and the blast radius of any
  account compromise; hampers SOC 2 access-control requirements.
- **Technical Impact:** Coarse RBAC; record rules carry most of the tenant-isolation weight.
- **Recommended Fix:** Introduce granular internal roles and customer-side roles (owner/admin/
  developer/read-only), and map dangerous actions to dedicated groups.

---

## SEC-017
- **Severity:** Low
- **Component:** Backup-restore confirmation UX
- **File Path:** `saas_website/controllers/api.py`
- **Line Numbers:** `:1284-1308`
- **Description:** Restore is correctly authorized (`_instance(..., write=True)` enforces
  ownership; the backup must belong to the instance), but the typed confirmation compares
  against `backup.db_name or instance.subdomain`, which is a confusing UX guard rather than a
  security control. *(This corrects an earlier "IDOR" claim — ownership IS enforced.)*
- **Business Impact:** Minor — users may be confused about what string to type; not a
  cross-tenant risk.
- **Technical Impact:** Confirmation semantics are inconsistent with the field label
  ("instance name").
- **Recommended Fix:** Confirm against the instance subdomain consistently and label it
  clearly.

---

## SEC-018
- **Severity:** Low
- **Component:** Session model in SPA
- **File Path:** `veltnex/src/lib/api.ts`; `veltnex/src/context/AuthContext.tsx`
- **Line Numbers:** `credentials: "same-origin"` in api.ts
- **Description:** Auth relies entirely on the Odoo session cookie (no tokens in
  localStorage — good), but there is no refresh flow or idle-timeout handling, and the SPA
  assumes same-origin, limiting independent API scaling and giving abrupt logouts.
- **Business Impact:** Minor UX/security friction; abrupt session expiry can lose user work.
- **Technical Impact:** Tightly couples SPA to same-origin deployment.
- **Recommended Fix:** Add session-expiry warning + refresh; document same-origin requirement.

---

## SEC-019
- **Severity:** Low
- **Component:** CSRF posture
- **File Path:** `saas_website/controllers/*.py`
- **Line Numbers:** 4 `csrf=False` occurrences
- **Description:** The `csrf=False` usages are on `type='json'` routes, which Odoo treats as
  CSRF-exempt by design (verified) — so this is not the vulnerability an automated scan might
  flag. Worth a periodic check that no `type='http'` state-changing route disables CSRF.
- **Business Impact:** None currently; included for completeness/false-positive clearance.
- **Technical Impact:** n/a.
- **Recommended Fix:** Add a CI lint asserting no `type='http'` + `csrf=False` combination.

---

## Cleared / Overstated (verified NOT vulnerabilities)

These were flagged by an automated first pass and **disproven by reading the code** — recorded
so the board does not act on false positives:

| Claim | Verdict | Evidence |
|---|---|---|
| Trial-eligibility race lets users mint multiple trials | **Cleared** | `saas_instance.py:1269-1278` takes `SELECT ... FOR UPDATE` on the commercial partner row inside `create()`, serializing concurrent trial creates. |
| Wallet concurrent-debit overspend | **Cleared** | `saas_wallet.py:_consume` calls `self._lock()` (row lock) and is idempotent per `move`; concurrent consume/credit/refund serialize on the wallet row. |
| Backup-restore IDOR (restore another tenant's snapshot) | **Cleared** | `api.py:1294` uses `_instance(..., write=True)` which ignores the share token and enforces owner record rules; backup must be in `instance.backup_ids`. |
| Tenants share one Docker bridge network | **Cleared** | Each instance gets a dedicated network `net_<subdomain>` (`saas_instance.py:4660`, `docker-compose.yml.jinja:45-64`). |
| Per-instance shell has no ownership check | **Cleared** | `ssh_terminal.py:644-667` enforces instance ownership via `access_token` for the container shell (only the *host* shell is group-gated — see SEC-005). |
| Port assignment silently collides | **Partly cleared** | A partial unique index on ports exists (`saas_instance.py:1102-1110`); a race raises `IntegrityError` rather than silently double-assigning (still worth a retry handler — see PERF/RELIABILITY). |
