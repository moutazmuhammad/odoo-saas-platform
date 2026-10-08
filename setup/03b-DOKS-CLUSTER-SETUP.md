# DOKS cluster setup (DigitalOcean Kubernetes)

From an empty DigitalOcean account to a cluster that takes customer orders from the SaaS control plane.

Before this guide: [01-ARCHITECTURE.md](01-ARCHITECTURE.md) (how it works) and [02-SAAS-SERVER-SETUP.md](02-SAAS-SERVER-SETUP.md) (install the SaaS server first; step 9 here runs on it). For your own servers instead, use [03a-MICROK8S-CLUSTER-SETUP.md](03a-MICROK8S-CLUSTER-SETUP.md).

The control-plane side is the same in both cluster guides:

| What | This guide | MicroK8s guide |
|---|---|---|
| Control-plane ServiceAccount, staff terminal, kubeconfig | 7–8 | 12.3–12.4 |
| Register the cluster in the control plane (same script) | 9 | 13 |
| Backend settings (backup storage, catalog, payments) | 10 | 13 notes, 14.1 |
| Image registry | 11 | 11 |
| Test before taking customers | 12 | 15 |

What you end up with:

| Part | Where |
|---|---|
| Kubernetes, nodes, disks | DOKS (DigitalOcean runs the control plane) |
| Traffic | vendored Traefik in `ingress`, behind one DigitalOcean Load Balancer |
| TLS | vendored cert-manager, issuer `letsencrypt-prod` |
| Tenants | Odoo operator in `odoo-system` |
| Usage metrics | vendored Prometheus in `monitoring` |
| Storage | `do-block-storage` (DigitalOcean Volumes); no Longhorn |

Every chart is in the repo (`compute/charts/...`) and every values file is in `compute/examples/doks/`. Run all commands from the repo root.

---

## 0. What you need

- A DigitalOcean account and a domain whose DNS you control (example: `apps.example.com`).
- The SaaS server already installed ([02-SAAS-SERVER-SETUP.md](02-SAAS-SERVER-SETUP.md)), with SSH access as root.
- On your workstation: `kubectl` (1.30+), `helm` (3.x), and this repository.
- An image registry account if customers deploy their own Git repositories (step 11).

**Sizing.** Each tenant is one Odoo pod (web + cron sidecar) and one PostgreSQL pod, sized from its plan's workers. During an update, the new Odoo pod starts **before** the old one stops, on the same node (it shares the volume). Keep room for one extra Odoo pod per node. For tests, 1 node of 4 vCPU / 8 GB is enough; for production, 3+ nodes.

**Costs on DigitalOcean.** The node droplets, one Load Balancer, and one Volume per tenant (PVC) plus 8 GB for Prometheus. Volumes are billed for their size even when a tenant is suspended, and Kubernetes cannot shrink them.

---

## 1. Create the cluster

In the DigitalOcean console: **Kubernetes → Create cluster**.

- **Region:** where your customers are. The SaaS server should be in the same region (lower API latency).
- **VPC:** the same VPC as the SaaS server, if possible.
- **Node pool:** see the sizing above. Turn on autoscaling only if you also watch the costs.
- **High availability control plane:** on for production (a small monthly charge); off is fine for tests.

Download the kubeconfig (**Download Config File**) and point your shell at it:

```bash
export KUBECONFIG=~/Downloads/<cluster>-kubeconfig.yaml
kubectl get nodes -o wide          # every node Ready
kubectl get storageclass           # do-block-storage (default)
```

This file holds an **admin** token that expires after a few days. Use it only for setup. The control plane gets its own permanent, revocable token in step 9.

---

## 2. Traffic: Traefik behind a Load Balancer

```bash
helm upgrade --install traefik compute/charts/vendor/traefik \
  -n ingress --create-namespace -f compute/examples/doks/traefik-values.yaml --wait
```

DigitalOcean creates a Load Balancer named `saas-ingress`. Wait for its IP:

```bash
kubectl -n ingress get svc traefik -w      # EXTERNAL-IP filled in (1–3 minutes)
LB_IP=$(kubectl -n ingress get svc traefik -o jsonpath='{.status.loadBalancer.ingress[0].ip}'); echo $LB_IP
```

The values set:
- the `traefik` IngressClass, the default, which the operator uses;
- `publishedService`, so every tenant Ingress reports the LB address. The operator waits for that address before it marks a tenant ready.

Don't rename the namespace (`ingress`) or the pod label (`app.kubernetes.io/name=traefik`). Each tenant's NetworkPolicy allows only those pods (step 5).

---

## 3. DNS

At your DNS provider, add:

```
*.apps.example.com    A    <LB_IP>
```

**Check** (a 404 from Traefik is correct, since there are no tenants yet):

```bash
getent hosts test.apps.example.com                                         # <LB_IP>
curl -s -o /dev/null -w "%{http_code}\n" http://test.apps.example.com/     # 404
```

Let's Encrypt (step 4) needs this record before a tenant can get its certificate.

---

## 4. TLS: cert-manager and Let's Encrypt

```bash
helm upgrade --install cert-manager compute/charts/vendor/cert-manager \
  -n cert-manager --create-namespace -f compute/examples/doks/cert-manager-values.yaml --wait

ACME_EMAIL=ops@example.com     # expiry notices from Let's Encrypt
cat <<EOF | kubectl apply -f -
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    email: $ACME_EMAIL
    server: https://acme-v02.api.letsencrypt.org/directory
    privateKeySecretRef: {name: letsencrypt-prod-account-key}
    solvers:
      - http01: {ingress: {ingressClassName: traefik}}
EOF
kubectl get clusterissuer letsencrypt-prod      # READY True
```

---

## 5. The Odoo operator

The operator image is public on Docker Hub (`docker.io/moutazmuhammad/odoo-saas-operator`, the tag in `compute/charts/odoo-operator/values.yaml`: 0.1.32, chart 0.4.19). Helm doesn't install or upgrade CRDs from `crds/` on upgrades, so apply them first:

```bash
kubectl apply --server-side --force-conflicts -f compute/charts/odoo-operator/crds/
helm upgrade --install odoo-operator compute/charts/odoo-operator \
  -n odoo-system --create-namespace -f compute/examples/doks/operator-values.yaml --wait
kubectl -n odoo-system get pods        # 1 Running
kubectl -n odoo-system logs deploy/odoo-operator --tail=5     # "Starting workers"
```

The operator runs as **one** replica with a `Recreate` rollout, so an old and a new operator never act at the same time. If its pod is down for a moment, tenants keep running; only changes wait.

Each tenant gets its own namespace (`odoo-tenant-…`), the `restricted` Pod Security level and a default-deny NetworkPolicy. `operator-values.yaml` lets only Traefik in.

---

## 6. Prometheus (usage metrics)

```bash
helm upgrade --install prometheus compute/charts/vendor/prometheus \
  -n monitoring --create-namespace -f compute/charts/monitoring/prometheus-values.yaml --wait
kubectl -n monitoring get pods,pvc      # Running; PVC Bound on do-block-storage
```

The control plane reads it through the Kubernetes API, so it is never exposed publicly.

---

## 7. Accounts for the control plane and the staff terminal

**7.1 Control plane.** A ServiceAccount with its own long-lived token, which you can revoke by deleting its Secret:

```bash
kubectl -n kube-system create serviceaccount saas-control-plane --dry-run=client -o yaml | kubectl apply -f -
kubectl create clusterrolebinding saas-control-plane --clusterrole=cluster-admin \
  --serviceaccount=kube-system:saas-control-plane --dry-run=client -o yaml | kubectl apply -f -
kubectl -n kube-system apply -f - <<'EOF'
apiVersion: v1
kind: Secret
metadata:
  name: saas-control-plane-token
  annotations: {kubernetes.io/service-account.name: saas-control-plane}
type: kubernetes.io/service-account-token
EOF
```

**7.2 Staff terminal (optional).** The **Terminal** button on the cluster form runs `kubectl`/`helm` as this account. Use `--clusterrole=view` for read-only.

```bash
kubectl create namespace saas-toolbox --dry-run=client -o yaml | kubectl apply -f -
kubectl -n saas-toolbox create serviceaccount saas-toolbox --dry-run=client -o yaml | kubectl apply -f -
kubectl create clusterrolebinding saas-toolbox --clusterrole=cluster-admin \
  --serviceaccount=saas-toolbox:saas-toolbox --dry-run=client -o yaml | kubectl apply -f -
```

---

## 8. The control plane's kubeconfig

This builds a kubeconfig from the token in 7.1 and copies it to the SaaS server, without keeping it on your workstation:

```bash
SAAS=203.0.113.10        # the SaaS server
SERVER=$(kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.server}')
CA=$(kubectl -n kube-system get secret saas-control-plane-token -o jsonpath='{.data.ca\.crt}')
TOKEN=$(kubectl -n kube-system get secret saas-control-plane-token -o jsonpath='{.data.token}' | base64 -d)
cat <<EOF | ssh root@$SAAS 'umask 077; cat > /tmp/kubeconfig-prod; chown odoo /tmp/kubeconfig-prod'
apiVersion: v1
kind: Config
clusters: [{name: doks, cluster: {server: "$SERVER", certificate-authority-data: $CA}}]
users: [{name: saas, user: {token: $TOKEN}}]
contexts: [{name: doks, context: {cluster: doks, user: saas}}]
current-context: doks
EOF
unset TOKEN
```

**Restrict API access (production):** in the cluster's **Settings → Trusted sources**, allow only the SaaS server's IP and your admin IP.

---

## 9. Register the cluster in the control plane

The control plane needs three records:
- a **Region**, which customers pick at checkout;
- a **Kubernetes Cluster** inside it, which holds the kubeconfig, TLS issuer and Prometheus. New instances in a region go to its least-loaded healthy cluster;
- a **Base domain**. Its wildcard DNS points at this cluster's LB, so instances on that domain are placed here.

On the **SaaS server**, edit the `CONFIG` lines, then paste the whole block. It creates or updates the three records, so it is safe to run again.

```bash
cat > /tmp/register_cluster.py <<'PY'
# ---- CONFIG ------------------------------------------------------------------
CLUSTER_NAME = 'doks-fra1'            # the cluster record's name
REGION_CODE = 'default'               # 'default' = the preinstalled Region
REGION_NAME = 'Default'               # used only when creating a new Region
LB_IP = '198.51.100.20'               # step 2
NODE_PRIVATE_IP = '10.0.0.11'         # any node's INTERNAL-IP (kubectl get nodes -o wide)
BASE_DOMAIN = 'apps.example.com'      # tenants get <sub>.apps.example.com
TLS_ISSUER = 'letsencrypt-prod'       # step 4
# -----------------------------------------------------------------------------
import base64
E = env

def upsert(model, domain, vals):
    rec = E[model].sudo().with_context(active_test=False).search(domain, limit=1)
    if rec:
        rec.write(vals)
    else:
        rec = E[model].sudo().create(vals)
    return rec

region = upsert('saas.region', [('code', '=', REGION_CODE)],
    {} if E['saas.region'].sudo().search_count([('code', '=', REGION_CODE)])
    else {'name': REGION_NAME, 'code': REGION_CODE})
srv = upsert('saas.server', [('name', '=', CLUSTER_NAME)], {
    'name': CLUSTER_NAME, 'compute_driver': 'kubernetes', 'region_id': region.id,
    'kubeconfig_file': base64.b64encode(open('/tmp/kubeconfig-prod', 'rb').read()),
    'kubeconfig_file_name': 'kubeconfig-prod.yaml',
    'tls_cluster_issuer': TLS_ISSUER,
    'prometheus_namespace': 'monitoring', 'prometheus_service': 'prometheus-server:80',
    'ip_v4': LB_IP, 'private_ip_v4': NODE_PRIVATE_IP})
ok, err = srv._probe_reachable(timeout=10)
srv._update_health(ok, err)
dom = upsert('saas.based.domain', [('name', '=', BASE_DOMAIN)], {
    'name': BASE_DOMAIN, 'proxy_server_id': srv.id})
E.cr.commit()
print('RESULT cluster:', srv.name, '| region', region.name, '| health', srv.health_state, srv.last_health_error or '')
print('RESULT domain :', dom.name, '| cluster', dom.proxy_server_id.name, '| region', dom.region_id.name)
PY
chown odoo /tmp/register_cluster.py
cd /opt/saas && sudo -u odoo venv/bin/python odoo18/odoo-bin shell -c /etc/odoo/saas.conf -d saas \
  --no-http --logfile=/dev/null < /tmp/register_cluster.py 2>&1 | grep -E 'RESULT|Error|Traceback'
rm -f /tmp/register_cluster.py /tmp/kubeconfig-prod
```

**Check:** `health ok`.

| Health message | Fix |
|---|---|
| `unreachable` / timeout | The SaaS server can't reach the API: check *Trusted sources* (step 8). |
| `Unauthorized` | The token Secret was recreated: redo steps 8 and 9. |
| `Odoo operator is missing` | Step 5 isn't done, or its pod isn't Running. |

**Replacing a cluster:** to move an existing cluster record to a new cluster, run step 9 with the **old** `CLUSTER_NAME`. That keeps its region and base domain. Point the wildcard DNS at the new LB. Tenants still on the old cluster are not moved: back them up and restore them first.

---

## 10. Backend settings

In the backend (see [02-SAAS-SERVER-SETUP.md](02-SAAS-SERVER-SETUP.md), step 8):

- **Backup storage (required):** Settings → SaaS Manager → backup storage. Use a DigitalOcean Spaces (S3) bucket with a key that can access only that bucket.
- **Catalog:** Odoo versions, the hosting product and plans. For tests, install the *SaaS Demo Catalog* app (`saas_demo_data`).
- **Payment provider and outgoing mail**, before real customers.

---

## 11. Image registry (customers' Git repositories)

Builds push `<host>/<prefix>/tenant-<sub>:<tag>`, and tenants pull the same image. On the cluster form, open the **Image Builds** tab:

| Registry | Host | Prefix | Username | Password |
|---|---|---|---|---|
| Docker Hub | `docker.io` | your Docker Hub user or org | that user | an **access token** (Account settings → Personal access tokens, Read & Write) |
| GHCR | `ghcr.io` | your org | a user | a token with `write:packages` |
| DO Container Registry | `registry.digitalocean.com` | your registry name | any | a DO API token |

Leave *Push Host* empty and *Plain-HTTP* off. Which images exist, how each is built, and how to move all of them to a private registry: [04-IMAGES-AND-REGISTRY.md](04-IMAGES-AND-REGISTRY.md).

**Docker Hub on a free plan creates public repositories:** anyone can then pull a tenant image, including the customer's custom addons. Use a private repository/plan (or GHCR / DOCR) before real customers.

---

## 12. Test before taking customers

**12.1 A test tenant.** Order one instance from the portal on `apps.example.com`, then:

```bash
kubectl get odooinstances -A                              # PHASE Ready
SUB=<sub>; NS=$(kubectl get odooinstances -A --no-headers | awk -v s=$SUB '$2 ~ s {print $1}')
kubectl -n $NS get pods -o wide        # odoo-… 2/2 (web + cron), postgresql-0 1/1
kubectl -n $NS get pvc                 # Bound, do-block-storage
kubectl -n $NS get certificate         # READY True
curl -s "https://$SUB.apps.example.com/web/health?db_server_status=1"   # "status": "pass", "db_server_status": true
```

In the backend and the portal, the instance shows **Online** within a minute. If it's stuck, `kubectl describe odooinstance -A` shows the reason in its conditions (image pull, database, route/TLS, update Job).

**12.2 Zero-downtime update.** Poll the tenant while you change its plan or update a module. Every response must be 200:

```bash
while true; do curl -s -o /dev/null -w "%{http_code} " https://<sub>.apps.example.com/web/login; sleep 1; done
```

A rollout stuck `Pending` means the node has no room for the second Odoo pod (see Sizing).

**12.3 Suspend and resume.** Suspend the test tenant from the backend. Its pods go to zero and the backend shows *Stopping*, then *Suspended*. Resume it; it comes back with its data.

**12.4 Backup and restore.** Back up the test tenant, restore it, log in.

**12.5 A Git repository** (if you use step 11): add a repo to the test tenant and deploy. The build Job pushes to the registry and the tenant rolls to the new image.

---

## 13. Operations

**Upgrade the operator** (when its chart version changes):

```bash
kubectl apply --server-side --force-conflicts -f compute/charts/odoo-operator/crds/
helm upgrade odoo-operator compute/charts/odoo-operator -n odoo-system -f compute/examples/doks/operator-values.yaml --wait
```

Existing tenants roll one by one as the operator reconciles them. Each tenant's new pod starts before the old one stops.

**Upgrade Kubernetes:** DOKS upgrades nodes one at a time (surge upgrades). Each tenant is a single pod, so it restarts once when its node is replaced. Its volume detaches and attaches to the new node, which takes about 1–3 minutes of downtime per tenant. Schedule the maintenance window accordingly.

**A node fails:** DOKS replaces it. Its tenants restart on another node once their volume attaches there. DigitalOcean Volumes are network disks, so no data is lost.

**Add capacity:** raise the node pool size. New tenants go to nodes with room; existing ones stay where they are.

---

## Final checklist

- [ ] All nodes Ready; `do-block-storage` is the default StorageClass
- [ ] `*.apps.example.com` resolves to the LB IP; `letsencrypt-prod` READY
- [ ] Operator 1/1 Running; Prometheus PVC Bound
- [ ] Step 9 shows `health ok`; API restricted to trusted sources
- [ ] Backup storage set; a restore tested
- [ ] Registry set with a token (private before real customers), if Git repos are used
- [ ] Tests 12.1–12.4 passed
