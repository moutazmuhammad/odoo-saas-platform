# Test cluster setup: control plane + a real remote Kubernetes cluster

This documents the exact steps taken to wire this repo's local control
plane (Odoo `saas_core`/`saas_website`) to a real, dedicated remote
microk8s cluster and get a genuinely working, browser-testable tenant
running on it — real `OdooInstance` CR, real Ingress, real Let's Encrypt
TLS certificate, no mocks. Follow it in order to reproduce the setup on a
fresh cluster, or to understand what's already configured on the current
one.

**Do not commit real credentials to this file.** SSH/root passwords and
kubeconfig contents are handled outside of git (see each step below).

---

## 0. What you end up with

- A `saas.region` in the control plane pointing at the remote cluster,
  with Kubernetes-native TLS (cert-manager + the cluster's own Ingress) —
  no external Nginx/Certbot-over-SSH layer needed for this region.
- A real wildcard DNS domain (or the free `nip.io` fallback) resolving to
  the cluster's public IP, so Let's Encrypt's HTTP-01 challenge succeeds
  for any subdomain with zero extra DNS work per tenant.
- The ability to create a `saas.instance` through the normal
  `action_deploy()` path and have it become a real, publicly reachable,
  HTTPS-secured Odoo tenant on the remote cluster.

---

## 1. Local control-plane environment

This project's local dev Odoo + PostgreSQL run as **systemd user
services**, not the `devctl.sh up`/`down` nohup flow — see
`control-plane/docs/LOCAL-RUNTIME-SYSTEMD.md`'s content for the full
background (now folded into this doc's history; the services are what
matters going forward):

```bash
systemctl --user status saas-postgres.service saas-control-plane.service
systemctl --user stop saas-control-plane.service     # before any DB reset
systemctl --user start saas-control-plane.service
journalctl --user -u saas-control-plane -f            # lifecycle only
tail -f ~/odoo-saas-runtime/saas-odoo/log/odoo.log     # actual request/job logs
```

`devctl.sh down` must **not** be used here — it runs a raw `pg_ctl stop`
against the same Postgres data directory the systemd service manages,
stopping Postgres out from under a live Odoo process.

### Resetting the database to a clean slate

```bash
export SAAS_DEV_BASE=/home/moutaz/odoo-saas-runtime
systemctl --user stop saas-control-plane.service
control-plane/scripts/devctl.sh reset   # drop + fresh -i install + reseed
systemctl --user start saas-control-plane.service
```

`reset` is safe to run with the systemd Postgres service still up (its own
`pg_up`/`pg_down` no-op around an already-running instance), but the
**Odoo service must be stopped first** — otherwise its live connections
block `DROP DATABASE`.

Two real bugs in `control-plane/scripts/seed_dev.py` were found and fixed
while doing this (they only surface on a genuine fresh install, which
`devctl.sh test` never exercises since it always starts from zero rows):
a support-plan XML ID referencing the wrong module (`saas_core.saas_support_plan_free`
→ actually owned by `saas_billing`), and a wrong method name on
`saas.wallet` (`_for_partner` → `for_partner`). Both are already fixed in
the script.

---

## 2. Remote cluster: baseline checks

SSH root access to the cluster's public IP is required (credentials are
tracked outside this repo — ask whoever provisioned the cluster). From
this machine, `paramiko` is used for ad-hoc commands (no `sshpass`
installed); a plain `ssh root@<ip>` works identically for manual checks.

```bash
ssh root@<cluster-ip> 'microk8s kubectl get nodes -o wide'
ssh root@<cluster-ip> 'microk8s kubectl -n odoo-system get pods,crd | grep -i odoo'
```

Confirms the node is `Ready` and the `odoo-operator` (from
`compute/operator`) is running in its `odoo-system` namespace with the
`odooinstances.saas.odoo.example.com` CRD installed.

---

## 3. Fix the operator's networking provider

**Check this first, independent of any TLS choice** — a cluster can be
deployed with the operator expecting Gateway API infrastructure that was
never actually set up, in which case every tenant hangs forever waiting
for a Gateway that doesn't exist:

```bash
microk8s kubectl -n odoo-system get deployment odoo-operator \
  -o jsonpath='{.spec.template.spec.containers[0].args}'
microk8s kubectl get ns | grep gateway-system   # empty = no Gateway API set up
microk8s kubectl get ingressclass                # classic Ingress fallback
```

If it's set to `--networking-provider=gateway-api` with no real Gateway
API controller/`Gateway` object present, switch it to classic Ingress
(an `IngressClass` named `traefik` ships with microk8s's `ingress`
addon):

```bash
microk8s kubectl -n odoo-system patch deployment odoo-operator --type=json -p='[
  {"op":"replace","path":"/spec/template/spec/containers/0/args","value":[
    "--leader-elect=false","--metrics-secure=true",
    "--networking-provider=ingress","--ingress-class-name=traefik",
    "--supported-odoo-versions=17.0,18.0,19.0","--allow-mutable-tags=false"
  ]}
]'
microk8s kubectl -n odoo-system rollout status deployment odoo-operator
```

⚠️ If this operator was installed via `helm3` (check with
`microk8s helm3 -n odoo-system get values odoo-operator`), this `kubectl
patch` is **not** persisted through a future `helm upgrade` of that
release — carry the same flag change into whatever chart values drive the
next upgrade, or it will silently regress back to the broken mode.

---

## 4. TLS: cert-manager + a real Let's Encrypt ClusterIssuer

```bash
microk8s enable cert-manager
```

Then apply a `ClusterIssuer` (HTTP-01 challenge solved via the `traefik`
Ingress class — no DNS provider API needed):

```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-test-client
spec:
  acme:
    email: <your-email>
    server: https://acme-v02.api.letsencrypt.org/directory
    privateKeySecretRef:
      name: letsencrypt-test-client-account-key
    solvers:
    - http01:
        ingress:
          ingressClassName: traefik
```

```bash
microk8s kubectl apply -f clusterissuer.yaml
microk8s kubectl get clusterissuer letsencrypt-test-client -o jsonpath='{.status.conditions}'
# expect: {"status":"True","type":"Ready","reason":"ACMEAccountRegistered"}
```

---

## 5. Route ports 80/443 to Traefik without a cloud load balancer

microk8s's `ingress` addon (Traefik) is `type: LoadBalancer` by default
but stays `<pending>` with no LB provisioner, reachable only via its
NodePorts. Two things get real port 80/443 traffic flowing **and** satisfy
the operator's own readiness check:

```bash
# 1. Route the box's real ports 80/443 straight to Traefik
microk8s kubectl -n ingress patch svc traefik --type=merge \
  -p='{"spec":{"externalIPs":["<cluster-ip>"]}}'

# 2. Populate status.loadBalancer so the operator's Ingress readiness
#    check (and Traefik's own per-Ingress status mirroring) sees an
#    address — this does NOT happen automatically from externalIPs alone.
microk8s kubectl -n ingress patch svc traefik --subresource=status --type=merge \
  -p='{"status":{"loadBalancer":{"ingress":[{"ip":"<cluster-ip>"}]}}}'
```

**Do not use `microk8s enable metallb:<ip>-<ip>` with the node's own
primary IP as the pool.** This was tried first and caused intermittent SSH
banner-read failures against the same box — MetalLB's L2 speaker doing
gratuitous ARP for an IP the NIC already owns fights the kernel's own ARP
responses. The `externalIPs` + manual status-patch approach above achieves
the same routing with zero ARP involved and no observed instability.

Verify:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://<cluster-ip>/   # 404 is fine (no Ingress host matches yet)
```

---

## 6. DNS for tenant domains

Two options, both already configured on the current cluster:

- **A real domain you own**, with a wildcard `A` record
  (`*.<subdomain>.yourdomain.tld` → `<cluster-ip>`). This is the
  preferred option once available — real Let's Encrypt certs, no
  per-tenant DNS work.
- **`nip.io` fallback** (`<anything>.<ip-with-dashes>.nip.io` resolves
  publicly to the embedded IP) — zero setup, works immediately without
  owning a domain, and Let's Encrypt issues real certs against it exactly
  the same way.

Both can coexist as separate `saas.based.domain` records pointed at the
same region/server; a customer effectively picks whichever domain their
order's `domain_id` resolves to.

---

## 7. Code changes: Kubernetes-native TLS as a per-region option

Three files in this repo were changed to support a region skipping the
existing SSH/Nginx/Certbot layer entirely in favor of the cluster's own
Ingress + cert-manager:

- **`control-plane/saas_core/models/saas_region.py`** — added
  `native_ingress_tls` (Boolean) and `tls_cluster_issuer` (Char) fields.
- **`control-plane/saas_core/models/saas_instance.py`**
  (`_do_deploy_locked_kubernetes`) — when `region.native_ingress_tls` is
  set, skips the `ingress_host` requirement and the entire
  `_provision_nginx`-over-SSH block; instead passes `tls_enabled`,
  `tls_issuer_name`, `tls_issuer_kind` into the `ComputeSpec.env` dict.
- **`control-plane/saas_core/drivers/kubernetes_driver.py`**
  (`_build_odoo_instance`) — this was **silently non-functional for TLS
  before this fix**: it set `domain.tls.enabled` from
  `spec.env['tls_enabled']` but never set `issuerRef`, so cert-manager
  would never have actually been invoked by any prior Kubernetes deploy
  with TLS turned on. Now reads `tls_issuer_name`/`tls_issuer_kind` from
  `spec.env` and populates `domain.tls.issuerRef` on the CR.
- **`control-plane/saas_core/views/saas_region_views.xml`** — exposes the
  two new region fields in the admin form.

After pulling these changes, upgrade the module to apply the schema/view
change:

```bash
systemctl --user stop saas-control-plane.service
/home/moutaz/odoo-saas-runtime/odoo18-venv/bin/python \
  /home/moutaz/odoo-saas-runtime/odoo18/odoo-bin \
  -c /home/moutaz/odoo-saas-runtime/saas-odoo/odoo.conf \
  -d saas_dev -u saas_core --stop-after-init
systemctl --user start saas-control-plane.service
```

---

## 8. Register the region/kubeconfig/server/domain

Done via `odoo-bin shell` against `saas_dev` (ORM, not raw SQL, so
encryption-at-rest for the kubeconfig/SSH key is applied correctly):

```python
import base64

Kubeconfig = env['saas.kubeconfig'].sudo()
with open('/path/to/kubeconfig-file', 'rb') as f:
    kc = Kubeconfig.create({
        'name': 'Test Client Cluster',
        'kubeconfig_file': base64.b64encode(f.read()),
        'kubeconfig_file_name': 'kubeconfig-test-client',
    })

Region = env['saas.region'].sudo()
region = Region.create({
    'name': 'Test Client Cluster',
    'code': 'test-client',
    'kubeconfig_id': kc.id,
    'ingress_host': '<cluster-ip>',
    'ingress_port': 80,
    'native_ingress_tls': True,
    'tls_cluster_issuer': 'letsencrypt-test-client',
})

Server = env['saas.server'].sudo()
server = Server.create({
    'name': 'test-client-k8s',
    'compute_driver': 'kubernetes',
    'region_id': region.id,
    'is_docker_host': True,   # required for allocation to consider it
    'is_proxy_server': True,  # required for saas.region.has_capacity()
    'is_db_server': True,     # required for has_capacity(); unused by the
                               # k8s path itself (DB lives in-cluster)
    'ip_v4': '<cluster-ip>',
})
server.db_server_id = server.id

Domain = env['saas.based.domain'].sudo()
Domain.create({
    'name': 'your.wildcard.domain.tld',   # or '<ip-dashes>.nip.io'
    'proxy_server_id': server.id,          # region_id derives from this
})
env.cr.commit()
```

No SSH key pair is needed on `server` for a `native_ingress_tls` region —
the SSH/Nginx path is skipped entirely, so `server.ssh_key_pair_id` can
stay unset.

---

## 9. Deploy and verify a real tenant

Through the actual production path (`action_deploy()` → durable
`saas.job` queue → `_do_deploy_locked_kubernetes`), not a shortcut:

```python
inst = env['saas.instance'].sudo().create({
    'subdomain': 'acme-test',
    'domain_id': <domain-id>,
    'region_id': <region-id>,
    'odoo_version_id': <version-id>,
    'compute_tier_id': <tier-id>,
    'partner_id': <partner-id>,
})
inst.action_deploy()
```

Verify independently of the control plane's own state (useful since a
long deploy can legitimately outlast Odoo's cron-thread time limit — see
Known gaps below):

```bash
microk8s kubectl get odooinstances -A          # PHASE=Ready, READY=True
curl -s -o /dev/null -w "%{http_code}\n" https://<tenant-domain>/web/login   # 200
echo | openssl s_client -connect <cluster-ip>:443 -servername <tenant-domain> 2>/dev/null \
  | openssl x509 -noout -issuer -subject -dates   # issuer = Let's Encrypt
```

A real browser (or a headless one, `google-chrome --headless=new
--screenshot=out.png <url>`) hitting the tenant URL is the final
end-to-end proof — real cert, no browser warnings.

---

## 10. Known gaps

- **Odoo's cron-worker real-time limit can orphan a long Kubernetes
  deploy.** In threaded mode (no `workers=` set), Odoo's cron watchdog
  enforces `limit_time_real_cron` (default ~120s) independent of
  prefork-worker mode. A real deploy (image pull + CNPG bring-up + init
  Job) can legitimately take several minutes, exceeding this — the
  **whole server process reloads**, silently orphaning the `saas.job` row
  at `state='running'` forever (frozen heartbeat, no error recorded).
  **This is a documented, real production requirement, not just a local
  workaround**: any `odoo.conf` running Kubernetes deploys needs
  `limit_time_real_cron = 1800` / `limit_time_real = 1800` (or similar —
  set generously above the slowest real deploy you expect) —
  `~/odoo-saas-runtime/saas-odoo/odoo.conf` already has this. The
  underlying architectural fix (running durable-job execution outside
  Odoo's own cron watchdog entirely) is still not done — this config
  value is a real mitigation, not a full fix, and a deploy that
  legitimately exceeds even 1800s will still hit this.
- ~~`driver.create()` not idempotent against an already-created CR~~ —
  **fixed** (2026-09-21): a 409 now triggers a real read-back of the CR;
  if it genuinely exists, the retry succeeds instead of failing. Live-
  verified: deployed an instance, called `create()` again with the
  identical spec after it reached `Ready` — succeeded cleanly, tenant
  unaffected.
- The operator's `--networking-provider`/`--ingress-class-name`/
  `--backup-tool-image` flags live only in the live Deployment object on
  this cluster (patched via `kubectl`, not a chart values file in this
  repo) — see the warning in step 3, and the backup-tool note below.

## 11. Backup-tool image (built + imported, 2026-09-21)

The Kubernetes operator's backup/restore Jobs need a container image
(`compute/tools/backup-tool/` — Dockerfile + `run-backup.sh`/
`run-restore.sh`/`lib-objectstorage.sh` all already existed in this repo,
just never actually built). Built locally and loaded directly into this
cluster's containerd (no registry needed, since `ImagePullPolicy:
IfNotPresent` is already set on both the backup CronJob and restore Job):

```bash
cd compute/tools/backup-tool
docker build -t odoo-saas-backup-tool:v1 .
docker save odoo-saas-backup-tool:v1 | gzip > backup-tool.tar.gz
scp backup-tool.tar.gz root@<cluster-ip>:/root/
ssh root@<cluster-ip> 'gunzip -k backup-tool.tar.gz && microk8s ctr image import backup-tool.tar'
```

Then point the operator at it (same `kubectl patch` pattern as step 3's
networking-provider fix):

```bash
microk8s kubectl -n odoo-system patch deployment odoo-operator --type=json -p='[
  {"op":"add","path":"/spec/template/spec/containers/0/args/-",
   "value":"--backup-tool-image=docker.io/library/odoo-saas-backup-tool:v1"}
]'
```

**Live-verified end to end**: enabled backup on a real instance
(`driver.set_scheduled_backup(enabled=True)`, PVC destination — no S3
needed to prove this), triggered an on-demand run
(`driver.trigger_backup_now()`), the Job completed successfully and
produced a real `db.dump` (1.1MB)/`filestore.tar.gz`/`manifest.json` on
the backup PVC. Also confirmed this fixes the second symptom of the same
gap: deleting an instance with backups enabled — previously hung forever
on the operator's finalizer waiting for a final backup Job that could
never pull its image — now completes cleanly.

**This fix is cluster-local**: the image exists only in this cluster's
own containerd cache, not a published registry. Rebuilding this cluster,
or wiring up a different one, means repeating the build+import (or,
better long-term, actually publishing to
`ghcr.io/freightright/odoo-saas-backup-tool` or another real registry
once there's access to do so, and removing the `--backup-tool-image`
override so the operator's own compiled-in default just works).
