# Production cluster setup

Step by step: a MicroK8s cluster that runs the tenant Odoo instances, connected to the SaaS control plane.

**How to use this guide**

- You write your values **once**, in `/root/cluster.env` on each node (step 2). Every block after that reads them, so you paste the blocks as they are.
- Each block says where it runs: **every node**, **node1**, **your workstation**, or the **SaaS server**. On the nodes, work as `root`.
- After step 2, `kubectl` and `helm` are aliases for `microk8s kubectl` and `microk8s helm3`.
- **3 nodes** = production (HA). **2 nodes** = testing only: it works, but if either node goes down, the Kubernetes API stops.
- Tested on DigitalOcean, Ubuntu 24.04, MicroK8s 1.35, Longhorn 1.12.1.

Other guides: `SAAS-SERVER-SETUP.md` (the control-plane server), `MICROK8S-CLUSTER-SETUP.md` (a single-node test cluster), `RUN-GUIDE.txt` (dev).

---

## Overview

```
 customers ──https──▶ *.apps.example.com ──▶ load balancer (TCP 80/443)
                                                  │
                     ┌────────────────────────────┼────────────────────────────┐
                     ▼                            ▼                            ▼
                  node1                        node2                        node3
       Kubernetes + Traefik + tenants    (same on every node)       (private network between them)
       Longhorn: a copy of every volume on every node
                     ▲
 SaaS server ──▶ Kubernetes API :16443 (firewalled)        tenant backups ──▶ S3 bucket
```

| Piece | What it gives you |
|---|---|
| 3 MicroK8s nodes | The cluster survives the loss of any one node. |
| Load balancer → all nodes | Customers never depend on a single node. |
| Longhorn | Tenant data survives a node loss, and the tenant restarts on another node. |
| cert-manager + Let's Encrypt | Every tenant gets HTTPS automatically. |
| ServiceAccount token | The control plane's cluster access, revocable at any time. |
| S3 backups | Protection against losing the whole cluster. |

**Known platform gaps** (fix them or accept them before real customers):

1. Rolling updates across nodes (PLAN.txt 3.7): zero downtime isn't guaranteed when the new pod lands on another node. Test 15.3 checks this.
2. The operator images are public on Docker Hub (`moutazmuhammad/*`). Move them to a private registry.
3. Tenants can't reach SMTP ports (25/465/587). Mail must go through an HTTPS provider.
4. Let's Encrypt allows about 50 new certificates per domain per week.
5. Not tested yet: the cloud load balancer, API failover (step 12.1), and tenant traffic across nodes (15.1).

---

## 0. What you need

| Item | Recommended |
|---|---|
| 3 servers (or 2 for testing) | Ubuntu 24.04, same size, same datacenter, **in the same VPC**. Production: 8 vCPU / 32 GB (minimum 4 / 16). |
| Data disk per server (optional) | SSD, 200+ GB. Each node holds a copy of *all* tenant data. |
| Load balancer | TCP 80 + 443. Optional for testing. |
| Cloud firewall | Attached to all the nodes (step 1). |
| Domain | A wildcard for tenants, e.g. `*.apps.example.com`. |
| S3 bucket | At a different provider or region, for backups. |

---

## 1. Firewall (provider console)

Use the provider's firewall, not `ufw` (it conflicts with Calico). Attach it to **all** the nodes.

| Inbound | From |
|---|---|
| TCP 22 | your IP |
| TCP 80, 443 | everyone (or only the load balancer) |
| TCP 16443 | the SaaS server's IP + your IP |
| All TCP, all UDP, ICMP | the VPC range, e.g. `10.135.0.0/16` |
| Anything else | denied |

Outbound: allow all.

**Don't skip this.** Without it, the Kubernetes API (16443), kubelet (10250) and cluster join port (25000) are open to the internet.

---

## 2. Prepare each server

### 2.1 Find the private IPs (every node)

```bash
ip -4 -br addr | grep -E ' (10|172\.(1[6-9]|2[0-9]|3[01])|192\.168)\.'
```

On DigitalOcean, use the **eth1** address (the VPC). Ignore eth0: it has the public IP and a `10.x` "anchor" IP, and neither is the VPC.

```
eth0   UP   159.223.25.140/20 10.19.0.13/16   ← don't use
eth1   UP   10.135.0.9/16                     ← use this
```

### 2.2 Write your values (every node, the same block)

Edit the values, then paste the block on **every** node:

```bash
cat > /root/cluster.env <<'EOF'
NODES="node1 node2 node3"                  # 2-node test: "node1 node2"
declare -A PRIV=([node1]=10.0.0.11      [node2]=10.0.0.12      [node3]=10.0.0.13)
declare -A PUB=( [node1]=198.51.100.11  [node2]=198.51.100.12  [node3]=198.51.100.13)
REPLICAS=3                    # Longhorn copies: 3 with 3 nodes, 2 with 2
LB_IP=203.0.113.50            # load balancer IP; no LB: node1's public IP
API_HOST=k8s-api.example.com  # API address for the SaaS server; test: node1's public IP
ACME_EMAIL=                   # optional: Let's Encrypt expiry emails
DATA_DISK=                    # optional: e.g. /dev/sdb; empty = use the OS disk
REPO=/root/odoo-saas-platform # where step 5 copies the charts
EOF
grep -q cluster.env ~/.bashrc || cat >> ~/.bashrc <<'EOF'
. /root/cluster.env
alias kubectl='microk8s kubectl'; alias helm='microk8s helm3'
EOF
. ~/.bashrc
```

If a later block says `/root/cluster.env: No such file or directory`, stop and do this step on that node first. Otherwise every value is empty.

### 2.3 Hostname (every node, the only line you edit by hand)

```bash
hostnamectl set-hostname node1    # node2 on the 2nd server, node3 on the 3rd
```

### 2.4 Packages and settings (every node)

```bash
. /root/cluster.env

# Packages. open-iscsi + nfs-common are needed by Longhorn.
apt update && DEBIAN_FRONTEND=noninteractive apt -y full-upgrade
apt -y install unattended-upgrades open-iscsi nfs-common
systemctl enable --now iscsid unattended-upgrades

# Kernel modules for Longhorn, now and at boot.
printf 'iscsi_tcp\ndm_crypt\n' > /etc/modules-load.d/longhorn.conf
modprobe iscsi_tcp; modprobe dm_crypt

# multipathd breaks Longhorn disks.
systemctl disable --now multipathd multipathd.socket 2>/dev/null || true

# Node names → private IPs. cloud-init rebuilds /etc/hosts from its template at boot, so write both.
for f in /etc/hosts /etc/cloud/templates/hosts.debian.tmpl; do
  [ -f "$f" ] || continue
  sed -i '/# k8s-node$/d' "$f"
  for n in $NODES; do echo "${PRIV[$n]} $n # k8s-node"; done >> "$f"
done

# Optional data disk. It formats the disk only if it has no filesystem yet.
if [ -n "$DATA_DISK" ] && ! mountpoint -q /var/lib/longhorn; then
  [ -z "$(lsblk -no FSTYPE "$DATA_DISK")" ] && mkfs.ext4 "$DATA_DISK"
  mkdir -p /var/lib/longhorn
  grep -q "^$DATA_DISK " /etc/fstab || echo "$DATA_DISK /var/lib/longhorn ext4 defaults,nofail 0 2" >> /etc/fstab
  mount -a
fi
```

**Check:**

```bash
. /root/cluster.env
for n in $NODES; do ping -c1 -W2 ${PRIV[$n]} >/dev/null && echo "$n ok" || echo "$n UNREACHABLE"; done
systemctl is-active iscsid                  # active
lsmod | grep -cE '^(iscsi_tcp|dm_crypt)'    # 2
```

### 2.5 SSH keys only (every node, when your key works)

Test first from your workstation: `ssh -o PasswordAuthentication=no root@<node>`. If that logs you in:

```bash
sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication no/; s/^#\?PermitRootLogin .*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
rm -f /etc/ssh/sshd_config.d/50-cloud-init.conf    # it can switch passwords back on
systemctl restart ssh
```

---

## 3. Install MicroK8s (every node)

```bash
. /root/cluster.env; ME=$(hostname)
snap install microk8s --classic --channel=1.35/stable
snap refresh --hold microk8s          # no automatic Kubernetes upgrades

# Use the private network. Without this, the nodes talk over their public IPs.
ip -4 -br addr | grep -q " ${PRIV[$ME]}/" || echo "!! ${PRIV[$ME]} is not on $ME: fix cluster.env or the hostname"
sed -i '/^--node-ip=/d' /var/snap/microk8s/current/args/kubelet
echo "--node-ip=${PRIV[$ME]}" >> /var/snap/microk8s/current/args/kubelet
snap restart microk8s
microk8s status --wait-ready
kubectl get nodes -o wide             # INTERNAL-IP must be the private IP
```

If INTERNAL-IP shows the public IP, fix it **now**, before joining.

---

## 4. Join the nodes

**node1**: print a join line. Run it once per node, since each line works only once:

```bash
. /root/cluster.env
microk8s add-node | grep -m1 "^microk8s join ${PRIV[node1]}:"
```

**node2** (then **node3**, with a new line): paste the printed line. It looks like this:

```bash
microk8s join 10.0.0.11:25000/<token>/<hash>
```

**node1**: once all the nodes have joined:

```bash
kubectl -n kube-system set env daemonset/calico-node IP_AUTODETECTION_METHOD=kubernetes-internal-ip
kubectl -n kube-system rollout status ds/calico-node

microk8s status | grep high-availability                      # yes (3 nodes) / no (2 nodes, expected)
kubectl get nodes -o wide                                     # all Ready, private INTERNAL-IPs
kubectl -n kube-system get pods -o wide | grep calico-node    # one per node, private IPs
```

**Nodes joined with public IPs?** While the cluster is still empty:
1. Run `microk8s leave` on each joined node, and wait for it to finish.
2. Run `microk8s remove-node <name>` on node1.
3. Fix `cluster.env`, repeat steps 2.4 and 3 on every node, then join again.

---

## 5. Addons and charts

**node1:**

```bash
microk8s enable dns ingress cert-manager helm3
kubectl -n ingress rollout status ds/traefik
kubectl -n cert-manager rollout status deploy/cert-manager-webhook
```

Don't enable `hostpath-storage` (single-node data). Longhorn is the storage.

**Your workstation**, from the repo root: copy the charts to node1:

```bash
NODE1=198.51.100.11      # node1 public IP
tar czf - compute/charts | ssh root@$NODE1 'mkdir -p /root/odoo-saas-platform && tar xzf - -C /root/odoo-saas-platform'
```

**Check (node1):** `kubectl get pods -A` shows everything Running, and `ls /root/odoo-saas-platform/compute/charts/vendor` shows `longhorn prometheus`.

---

## 6. Storage: Longhorn (node1)

The chart is vendored at `compute/charts/vendor/longhorn` (1.12.1).

```bash
. /root/cluster.env; cd $REPO
helm upgrade --install longhorn compute/charts/vendor/longhorn \
  -n longhorn-system --create-namespace \
  --set csi.kubeletRootDir=/var/snap/microk8s/common/var/lib/kubelet \
  --set persistence.defaultClass=true \
  --set persistence.defaultClassReplicaCount=$REPLICAS \
  --set defaultSettings.defaultReplicaCount=$REPLICAS \
  --set defaultSettings.defaultDataPath=/var/lib/longhorn \
  --set defaultSettings.nodeDownPodDeletionPolicy=delete-both-statefulset-and-deployment-pod
kubectl -n longhorn-system rollout status deploy/longhorn-driver-deployer --timeout=10m
kubectl -n longhorn-system rollout status ds/longhorn-csi-plugin --timeout=10m
```

- `kubeletRootDir`: MicroK8s's kubelet path. Without it, volumes never mount.
- `nodeDownPodDeletionPolicy`: when a node dies, the tenant restarts elsewhere without manual help.

**Check:** a test volume is written, with one copy per node, then deleted:

```bash
kubectl get sc                                       # longhorn (default)
kubectl create -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: longhorn-test, namespace: default}
spec: {accessModes: [ReadWriteOnce], resources: {requests: {storage: 1Gi}}}
EOF
kubectl run lh-test --image=busybox --restart=Never --overrides='{"spec":{"volumes":[{"name":"v","persistentVolumeClaim":{"claimName":"longhorn-test"}}],"containers":[{"name":"c","image":"busybox","command":["sh","-c","echo ok > /v/t && cat /v/t"],"volumeMounts":[{"name":"v","mountPath":"/v"}]}]}}'
kubectl wait --for=jsonpath='{.status.phase}'=Succeeded pod/lh-test --timeout=3m
kubectl logs lh-test                                 # ok
kubectl -n longhorn-system get replicas.longhorn.io -o custom-columns=NODE:.spec.nodeID,STATE:.status.currentState
kubectl delete pod lh-test; kubectl delete pvc longhorn-test
```

**Longhorn UI:** tunnel only, never public. Run `kubectl -n longhorn-system port-forward svc/longhorn-frontend 8080:80`, then open http://localhost:8080.

---

## 7. Traffic

Traefik (from the ingress addon) runs on every node, on ports 80/443. So every node answers.

**7.1 node1:** register the node IPs and the public address on the Traefik Service. The operator waits for that address before it marks a tenant ready.

```bash
. /root/cluster.env
IPS=$(for n in $NODES; do printf '"%s",' "${PRIV[$n]}"; done)
kubectl -n ingress patch svc traefik --type=merge -p "{\"spec\":{\"externalIPs\":[${IPS%,}]}}"
kubectl -n ingress patch svc traefik --subresource=status --type=merge \
  -p "{\"status\":{\"loadBalancer\":{\"ingress\":[{\"ip\":\"$LB_IP\"}]}}}"
kubectl -n ingress get svc traefik
```

**7.2 Load balancer (provider console):** TCP 80→80 and 443→443, passthrough (no TLS on the LB). Targets: all the nodes. Health check: TCP 80.

**7.3 DNS:** `*.apps.example.com` → an `A` record to the LB IP.
No LB? Add one `A` record per node's public IP, and set `LB_IP` to node1's public IP.

**Check (your workstation):** a 404 is correct, since there are no tenants yet.

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://anything.apps.example.com/     # 404
```

**Check pod traffic between the nodes (node1):**

```bash
POD1=$(kubectl -n ingress get pods -l app.kubernetes.io/name=traefik --field-selector spec.nodeName=node1 -o jsonpath='{.items[0].status.podIP}')
kubectl run nettest --image=busybox --restart=Never --overrides='{"spec":{"nodeName":"node2"}}' -- \
  sh -c "nslookup kubernetes.default.svc.cluster.local >/dev/null && echo dns-ok; wget -qO- -T5 http://$POD1:8000/ 2>&1 | head -1"
kubectl wait --for=jsonpath='{.status.phase}'=Succeeded pod/nettest --timeout=90s; kubectl logs nettest; kubectl delete pod nettest
# expect: dns-ok, then "404 Not Found". A timeout means the firewall blocks the VPC range.
```

---

## 8. TLS: Let's Encrypt (node1)

```bash
. /root/cluster.env
cat <<EOF | kubectl apply -f -
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    ${ACME_EMAIL:+email: $ACME_EMAIL}
    server: https://acme-v02.api.letsencrypt.org/directory
    privateKeySecretRef: {name: letsencrypt-prod-account-key}
    solvers:
      - http01: {ingress: {ingressClassName: traefik}}
EOF
sleep 15; kubectl get clusterissuer letsencrypt-prod    # READY True
```

---

## 9. Odoo operator (node1)

The operator isolates each tenant: its own namespace, the `restricted` Pod Security level, and a default-deny NetworkPolicy. The values below allow only Traefik (namespace `ingress`, label `app.kubernetes.io/name=traefik`) to reach tenants.

```bash
. /root/cluster.env; cd $REPO
cat > $REPO/operator-values-prod.yaml <<'EOF'
replicaCount: 2
leaderElection: true
networking:
  provider: ingress
  ingressClassName: traefik
  gateway:
    namespace: ingress
    podSelector:
      app.kubernetes.io/name: traefik
# imagePullSecrets: [{name: regcred}]   # once the operator images are private
EOF
kubectl apply --server-side --force-conflicts -f compute/charts/odoo-operator/crds/
helm upgrade --install odoo-operator compute/charts/odoo-operator \
  -n odoo-system --create-namespace -f operator-values-prod.yaml
kubectl -n odoo-system rollout status deploy/odoo-operator
kubectl -n odoo-system get pods -o wide     # 2 Running, on different nodes
```

Keep `operator-values-prod.yaml`. Every operator upgrade uses it.

---

## 10. Prometheus (node1)

The control plane reads tenant CPU/RAM usage through the Kubernetes API. Prometheus is never exposed publicly.

```bash
. /root/cluster.env; cd $REPO
helm upgrade --install prometheus compute/charts/vendor/prometheus \
  -n monitoring --create-namespace -f compute/charts/monitoring/prometheus-values.yaml
kubectl -n monitoring rollout status deploy/prometheus-server --timeout=5m
kubectl -n monitoring get pvc      # Bound, on longhorn
```

---

## 11. Image registry (only if customers deploy Git repos)

Use a private registry with a fixed token: GHCR, Docker Hub (private), Harbor, or GCP Artifact Registry. Not the in-cluster test registry, and not AWS ECR, whose tokens expire every 12 hours. Put it in the cluster's *Image Builds* tab: host, push host, path prefix (your org), and a push/pull-only token. Leave plain-HTTP off.

---

## 12. API access for the control plane

**12.1 DNS name (production only).** `k8s-api.example.com` → one `A` record per node's public IP. For a test cluster, skip this and use node1's IP as `API_HOST`.

**12.2 Add that name to the API certificate (every node, only if `API_HOST` is a name):**

```bash
. /root/cluster.env
if ! [[ $API_HOST =~ ^[0-9.]+$ ]]; then
  grep -q "DNS.99 = $API_HOST" /var/snap/microk8s/current/certs/csr.conf.template || \
    sed -i "s/^#MOREIPS/DNS.99 = $API_HOST\n#MOREIPS/" /var/snap/microk8s/current/certs/csr.conf.template
  microk8s refresh-certs --cert server.crt
fi
openssl x509 -in /var/snap/microk8s/current/certs/server.crt -noout -ext subjectAltName | tr ',' '\n' | grep -F "$API_HOST"
```

**12.3 Create the control plane's token and kubeconfig (node1).** Safe to run again, for example to rebuild the file:

```bash
. /root/cluster.env
[ -n "$API_HOST" ] || echo "!! API_HOST is empty: do step 2.2 first"
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
sleep 5
CA=$(kubectl -n kube-system get secret saas-control-plane-token -o jsonpath='{.data.ca\.crt}')
TOKEN=$(kubectl -n kube-system get secret saas-control-plane-token -o jsonpath='{.data.token}' | base64 -d)
( umask 077; cat > /root/kubeconfig-prod <<EOF
apiVersion: v1
kind: Config
clusters: [{name: prod, cluster: {server: "https://$API_HOST:16443", certificate-authority-data: $CA}}]
users: [{name: saas, user: {token: $TOKEN}}]
contexts: [{name: prod, context: {cluster: prod, user: saas}}]
current-context: prod
EOF
)
kubectl --kubeconfig /root/kubeconfig-prod get nodes      # all the nodes
```

Don't use `microk8s config` instead: it's the permanent admin certificate. This token can be revoked by deleting its Secret.

| Error | Fix |
|---|---|
| `https://:16443` / `ServerName or InsecureSkipVerify` | `API_HOST` is empty: do step 2.2, then run 12.3 again. |
| `certificate is valid for …, not <API_HOST>` | Do 12.2. For an IP, add `IP.99 = <ip>` before `#MOREIPS` in `csr.conf.template`, then run `microk8s refresh-certs --cert server.crt`. |
| Timeout on `:16443` | The firewall: allow 16443 from that machine (step 1). |
| `Unauthorized` | The token was recreated: run 12.3 again and redo step 13. |

### 12.4 Staff cluster terminal (node1, optional)

The **Terminal** button on a *Kubernetes Clusters* form opens `kubectl` and `helm` on the cluster in the browser. It runs in a toolbox pod that the control plane starts in the `saas-toolbox` namespace, as the `saas-toolbox` ServiceAccount. The control plane never grants permissions itself; you decide here what that ServiceAccount may do:

```bash
kubectl create namespace saas-toolbox
kubectl -n saas-toolbox create serviceaccount saas-toolbox
kubectl create clusterrolebinding saas-toolbox --clusterrole=cluster-admin \
  --serviceaccount=saas-toolbox:saas-toolbox
```

- **Read-only instead:** use `--clusterrole=view`. The terminal can then list, describe and read logs, but not change anything.
- **Who can open it:** users with *SaaS Terminal → Cluster Shell* on the Users form. Every session is logged on the cluster record.
- **The pod:** `alpine/k8s` (kubectl and helm). It stops on its own after 8 hours and starts again on the next open.
- **Turn it off:** `kubectl delete clusterrolebinding saas-toolbox`. To remove everything, also run `kubectl delete namespace saas-toolbox`.

---

## 13. Register the cluster in the control plane

The control plane needs three records:

- a **Region**: the location customers pick at checkout;
- a **Kubernetes Cluster** inside it: the kubeconfig, TLS issuer and Prometheus. A region can have several clusters, and new instances go to the least-loaded healthy one;
- a **Base domain**: the tenant URLs. Its wildcard DNS points at one cluster, so instances on that domain are placed on that cluster.

The script below creates or updates all three. It's safe to run again.

**13.1 Check DNS (anywhere):**

```bash
getent hosts test.apps.example.com      # the LB IP or the node IPs
```

**13.2 Move the kubeconfig to the SaaS server (your workstation).** It streams straight through, without being saved on your machine:

```bash
NODE1=198.51.100.11; SAAS=saas.example.com
ssh root@$NODE1 cat /root/kubeconfig-prod | \
  ssh root@$SAAS 'umask 077; cat > /tmp/kubeconfig-prod; chown odoo /tmp/kubeconfig-prod'
ssh root@$NODE1 rm -f /root/kubeconfig-prod
```

**13.3 Register (SaaS server).** Edit the `CONFIG` lines, then paste the whole block:

```bash
cat > /tmp/register_cluster.py <<'PY'
# ---- CONFIG ------------------------------------------------------------------
CLUSTER_NAME = 'cluster-1'            # the cluster record's name
REGION_CODE = 'default'               # 'default' = the Region that comes preinstalled
REGION_NAME = 'Default'               # used only when creating a new Region
NODE_PUBLIC_IP = '198.51.100.11'      # node1 public IP
NODE_PRIVATE_IP = '10.0.0.11'         # node1 private IP
BASE_DOMAIN = 'apps.example.com'      # tenants get <sub>.apps.example.com
TLS_ISSUER = 'letsencrypt-prod'       # step 8
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
    'ip_v4': NODE_PUBLIC_IP, 'private_ip_v4': NODE_PRIVATE_IP})
ok, err = srv._probe_reachable(timeout=10)
srv._update_health(ok, err)
dom = upsert('saas.based.domain', [('name', '=', BASE_DOMAIN)], {
    'name': BASE_DOMAIN, 'proxy_server_id': srv.id})   # the domain's cluster (and region)
E.cr.commit()

print('RESULT cluster:', srv.name, '| region', region.name, '| health', srv.health_state, srv.last_health_error or '')
print('RESULT domain :', dom.name, '| cluster', dom.proxy_server_id.name, '| region', dom.region_id.name)
PY
chown odoo /tmp/register_cluster.py
cd /opt/saas && sudo -u odoo venv/bin/python odoo18/odoo-bin shell -c /etc/odoo/saas.conf -d saas \
  --no-http --logfile=/dev/null < /tmp/register_cluster.py 2>&1 | grep -E 'RESULT|Error|Traceback'
rm -f /tmp/register_cluster.py /tmp/kubeconfig-prod
```

**Check:** the output must show `health ok`. If it shows `unreachable`, the SaaS server can't reach `API_HOST:16443`: check the firewall (step 1). Then repeat 13.2 and 13.3.

**Notes:**
- For a test catalog (Odoo versions, the "Odoo Hosting" product, plans), install the **SaaS Demo Catalog** app (`saas_demo_data`) from Apps.
- A second cluster in the same region: repeat 12.3 on that cluster, then 13.2 and 13.3 with a new `CLUSTER_NAME` and that cluster's own `BASE_DOMAIN` (its wildcard DNS points at its own load balancer).
- Still to set in the backend (`SAAS-SERVER-SETUP.md`, step 8): backup storage (step 14), a payment provider, mail, and the registry (step 11) if you use it.

---

## 14. Tenant backups

1. Settings → SaaS Manager → backup storage: the S3 bucket (provider, keys, bucket, region/endpoint).
2. The key gets access to **that bucket only**.
3. Turn on bucket versioning or object lock, if the provider has it.
4. Turn on scheduled backups on the plans.

---

## 15. Tests before go-live

1. **Tenants on every node:** order 3 test instances. `kubectl get pods -A -o wide | grep odoo-` should show them spread across the nodes, and every `https://<sub>.apps.example.com/web/login` returns 200. If one node's tenant times out, the step 9 labels are wrong.
2. **A node dies:** power off a node from the console.
   - `kubectl get nodes` still works (3 nodes only).
   - Tenants on the other nodes keep answering.
   - The tenant from the dead node restarts elsewhere within about 5–10 minutes, with its data.
   - Power the node back on: it rejoins, and Longhorn re-copies the volumes.
3. **Zero-downtime update (PLAN.txt 3.7):** poll a tenant with `while true; do curl -s -o /dev/null -w "%{http_code} " https://<sub>.apps.example.com/web/login; sleep 1; done` while you trigger an update from the control plane. Every response must be 200. Repeat until the new pod lands on a different node.
4. **Backup and restore:** back up a test tenant, restore it, and log in.
5. **Security:**
   - From an outside machine, `nc -zv <node-public-ip> 16443` must fail, and so must ports 10250 and 25000.
   - SSH password login must be refused.

---

## 16. Operations

**Upgrade Kubernetes:** one minor version at a time, one node at a time:

```bash
kubectl drain node1 --ignore-daemonsets --delete-emptydir-data
snap refresh microk8s --channel=1.36/stable     # on node1
kubectl uncordon node1
# wait: all nodes Ready and Longhorn volumes healthy, then do the next node
```

**OS reboots:** drain → reboot → uncordon, one node at a time.

**Upgrade the operator** (copy the charts first, as in step 5):

```bash
. /root/cluster.env; cd $REPO
kubectl apply --server-side --force-conflicts -f compute/charts/odoo-operator/crds/
helm upgrade odoo-operator compute/charts/odoo-operator -n odoo-system -f operator-values-prod.yaml
```

**Upgrade Longhorn:** one minor version at a time; read its upgrade notes first. On your workstation:

```bash
helm repo add longhorn https://charts.longhorn.io && helm repo update
helm search repo longhorn/longhorn --versions | head -5
helm show chart longhorn/longhorn --version <VERSION> | grep kubeVersion    # must include your Kubernetes
rm -rf compute/charts/vendor/longhorn
helm pull longhorn/longhorn --version <VERSION> --untar -d compute/charts/vendor/
rm -f compute/charts/vendor/longhorn/*.md compute/charts/vendor/longhorn/README.md.gotmpl
git add -A compute/charts/vendor/longhorn && git commit -m "chore(charts): vendor Longhorn <VERSION>"
```

Then copy the charts (step 5) and run the step 6 install again.

**Replace a dead node:**
1. `microk8s remove-node <name> --force` on a healthy node.
2. Build a new server with steps 2–3, using the same name and IP if possible. If the IP changes, update `cluster.env` everywhere and run step 2.4 on every node.
3. Join it (step 4), and add it to the load balancer.

**Add a worker node:**
1. Add it to `cluster.env` on every node, and run step 2.4 on the existing nodes.
2. Build the new node with steps 2–3.
3. Join it using the `--worker` line from `microk8s add-node`. Keep exactly 3 control-plane nodes.

**Watch:** `kubectl get nodes`, `kubectl get odooinstances -A`, `kubectl get certificates -A`, and the Longhorn disk space (act at 70%).

**Revoke the control plane's access:** `kubectl -n kube-system delete secret saas-control-plane-token`. Then run 12.3 and 13 again.

---

## Appendix: managed Kubernetes (GKE / EKS / AKS / DOKS)

| Step | Change |
|---|---|
| 1–4 | Create a cluster with 3+ nodes in one zone (on GKE: Standard, not Autopilot). |
| 5 | Install an ingress controller and cert-manager with Helm (vendor the charts). |
| 6 | Skip it. Use the cloud disks: `standard-rwo` / `gp3` / `managed-csi` / `do-block-storage`. |
| 7 | Skip the patches. The ingress Service gets a cloud LB (EKS: use a CNAME). |
| 9 | Set `ingressClassName` and the NetworkPolicy labels for your ingress controller. |
| 12 | Skip 12.1–12.2. Restrict the API to the SaaS server's IP. 12.3 is required. |

---

## Final checklist

- [ ] Nodes Ready with private INTERNAL-IPs; `high-availability: yes` (3 nodes)
- [ ] Firewall attached: 16443 only from the SaaS server and admin; 10250/25000 not public
- [ ] SSH keys only; snap on hold
- [ ] Longhorn is the default StorageClass; test volume passed
- [ ] `*.apps.example.com` resolves; `letsencrypt-prod` READY
- [ ] Operator with 2 replicas; Prometheus Bound
- [ ] Step 13 shows `health ok`; kubeconfig copies deleted
- [ ] Backups to S3; a restore tested
- [ ] Tests 15.1–15.5 passed
