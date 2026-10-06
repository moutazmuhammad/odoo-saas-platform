# Production cluster setup (HA MicroK8s)

Step by step: from blank Ubuntu 24.04 servers to a highly available MicroK8s cluster (3 control-plane nodes, HA dqlite datastore) that runs the tenant Odoo instances, registered in the SaaS control plane and passing a test tenant.

**How to use this guide**

- You write your values **once**, in `/root/cluster.env` on each node (step 2). Every block after that reads them, so you paste the blocks as they are.
- Each block says where it runs: **every node**, **node1**, **your workstation**, or the **SaaS server**. On the nodes, work as `root`.
- After step 2, `kubectl` and `helm` are aliases for `microk8s kubectl` and `microk8s helm3`.
- Tested on DigitalOcean, Ubuntu 24.04, MicroK8s 1.35, Longhorn 1.12.1, operator 0.1.28 (chart 0.4.15).

Other guides: `SAAS-SERVER-SETUP.md` (the control-plane server), `MICROK8S-CLUSTER-SETUP.md` (a single-node test cluster), `RUN-GUIDE.txt` (dev).

Upgrading an existing installation: read [SINGLE-INSTANCE-ARCHITECTURE.md](SINGLE-INSTANCE-ARCHITECTURE.md) (one Odoo pod per tenant, vertical scaling) and [STORAGE-COST-FIX.md](STORAGE-COST-FIX.md) (one shared tenant volume) first.

---

## Overview

```
 customers ──https──▶ *.apps.example.com ──▶ load balancer (TCP 80/443)
                                                  │
                     ┌────────────────────────────┼────────────────────────────┐
                     ▼                            ▼                            ▼
                  node1                        node2                        node3
       API + dqlite voter + Traefik    API + dqlite voter + Traefik   API + dqlite voter + Traefik
       tenants + Longhorn              (private network between the nodes: VPC)
                     ▲
 SaaS server ──▶ Kubernetes API :16443 (firewalled)        tenant backups ──▶ S3 bucket
```

| Piece | What it gives you |
|---|---|
| 3 MicroK8s control-plane nodes (dqlite HA) | The Kubernetes API and datastore survive the loss of any **one** node. |
| Load balancer → all nodes | Customer traffic never depends on a single node. |
| Longhorn | Network volumes: a tenant can restart on another node. Its data survives a node loss only if the volume has a replica elsewhere (step 6). |
| cert-manager + Let's Encrypt | Every tenant gets HTTPS automatically. |
| ServiceAccount token | The control plane's cluster access, revocable at any time. |
| S3 backups | Protection against losing a volume or the whole cluster. |

### What HA means here, honestly

The **cluster** is HA. A **tenant** is not:

- **One pod per tenant.** Each tenant runs one Odoo pod (web + cron sidecar) and one PostgreSQL pod on the same node (they share one `ReadWriteOnce` data volume). When that node dies, the tenant is down until Kubernetes reschedules it on another node: about 5–10 minutes (300 s eviction timeout, then volume re-attach and Odoo start).
- **Node maintenance interrupts tenants too.** Draining a node (upgrades, reboots) restarts its tenants elsewhere: a short outage per tenant. Zero-downtime applies to tenant **updates** (the new pod starts beside the old one on the same node before the old one stops), not to node maintenance.
- **Longhorn replicas.** The current low-cost policy (`STORAGE-COST-FIX.md`, `SINGLE-INSTANCE-ARCHITECTURE.md`) is **one** Longhorn replica per volume. With one replica, a volume whose replica sits on a lost node is **unavailable until that node comes back**, and **lost** if its disk is gone; then the tenant comes back only from its latest S3 backup. For real data HA, use `REPLICAS=2` (survives one node) or `3` (survives one node even during a rebuild). Each replica is a full copy, so disk use and cost scale with it.
- **The operator is a single replica** (`Recreate` rollout, leader election off, so two operators never reconcile at once). If its node dies, provisioning and updates pause until it is rescheduled, and the control plane marks the cluster unhealthy meanwhile. Running tenants keep serving.
- **Losing two of three control-plane nodes** loses dqlite quorum: the API stops (tenants already running keep serving traffic). See step 16, "Lost quorum".

**Known platform gaps** (fix them or accept them before real customers):

1. The operator images are public on Docker Hub (`moutazmuhammad/*`). Move them to a private registry.
2. Tenants can't reach SMTP ports (25/465/587). Mail must go through an HTTPS provider.
3. Let's Encrypt allows about 50 new certificates per registered domain per week.
4. Not tested yet: the cloud load balancer health checks and API failover through `API_HOST` (step 12.1). Test 15.2 covers both.

---

## 0. What you need

| Item | Recommended |
|---|---|
| 3 servers (control plane) | Ubuntu 24.04, same size, same datacenter, **same VPC / private network**. Production: 8 vCPU / 32 GB (minimum 4 vCPU / 16 GB). Extra capacity later: more nodes join as workers (step 16). |
| Disk per server | SSD. OS disk 50+ GB; a separate data disk for Longhorn is recommended (200+ GB). |
| Load balancer | TCP 80 + 443 to all nodes. Optional for testing (use DNS round robin instead). |
| Cloud firewall | Attached to all the nodes (step 1). |
| Domains | A wildcard for tenants, e.g. `*.apps.example.com`, and a name for the API, e.g. `k8s-api.example.com`. |
| S3 bucket | At a different provider or region, for backups. |

**Sizing rules of thumb**

- Per node, reserve about 2 vCPU / 3 GB for MicroK8s, dqlite, Calico, Traefik, Longhorn and Prometheus.
- Each tenant needs its package's CPU/RAM requests (Odoo + cron sidecar + PostgreSQL), plus room for a **second Odoo pod on the same node** during an update.
- To absorb a lost node, the remaining nodes must hold all tenants: with 3 nodes, keep total requests under about 65% of the cluster.
- Disk: sum of tenant allowances × `REPLICAS`, plus Prometheus (8 Gi × `REPLICAS`). Act at 70% use.

---

## 1. Firewall (provider console)

Use the provider's firewall, not `ufw` (it conflicts with Calico). Attach it to **all** the nodes.

| Inbound | From |
|---|---|
| TCP 22 | your IP |
| TCP 80, 443 | everyone (or only the load balancer) |
| TCP 16443 | the SaaS server's IP + your IP (+ the load balancer, if it fronts the API) |
| All TCP, all UDP, ICMP | the VPC range, e.g. `10.135.0.0/16` |
| Anything else | denied |

Outbound: allow all.

The "all from the VPC" rule covers the node-to-node ports. For reference, these must work between the nodes and **must not** be open to the internet:

| Port | Used for |
|---|---|
| TCP 16443 | Kubernetes API |
| TCP 10250 | kubelet (logs, exec, metrics) |
| TCP 25000 | MicroK8s cluster agent (join) |
| TCP 19001 | dqlite datastore (HA replication between voters) |
| UDP 4789 | Calico VXLAN pod network |
| TCP 9500, 8500–8502, 10000–30000 | Longhorn manager, instance managers, replicas (pod-to-pod over VXLAN; listed for stricter firewalls) |

**Don't skip this.** Without it, the Kubernetes API (16443), kubelet (10250), join port (25000) and dqlite (19001) are open to the internet.

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
NODES="node1 node2 node3"
declare -A PRIV=([node1]=10.0.0.11      [node2]=10.0.0.12      [node3]=10.0.0.13)
declare -A PUB=( [node1]=198.51.100.11  [node2]=198.51.100.12  [node3]=198.51.100.13)
REPLICAS=1                    # Longhorn copies per volume. 1 = low-cost policy (no data HA); 2 or 3 = data HA (see Overview)
LB_IP=203.0.113.50            # load balancer IP; no LB: node1's public IP
API_HOST=k8s-api.example.com  # API address for the SaaS server (step 12.1); test: node1's public IP
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

# Clock sync: dqlite and TLS need the nodes' clocks to agree.
timedatectl set-ntp true

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
timedatectl show -p NTPSynchronized         # NTPSynchronized=yes
getent hosts node1 node2 node3              # private IPs
```

Reboot now if `full-upgrade` installed a new kernel (`[ -f /var/run/reboot-required ] && reboot`).

### 2.5 SSH keys only (every node, when your key works)

Test first from your workstation: `ssh -o PasswordAuthentication=no root@<node>`. If that logs you in:

```bash
sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication no/; s/^#\?PermitRootLogin .*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
rm -f /etc/ssh/sshd_config.d/50-cloud-init.conf    # it can switch passwords back on
systemctl restart ssh
```

---

## 3. Install MicroK8s (every node)

Every node uses the **same** channel.

```bash
. /root/cluster.env; ME=$(hostname)
snap install microk8s --classic --channel=1.35/stable
snap refresh --hold microk8s          # no automatic Kubernetes upgrades

# Use the private network. Without this, the nodes talk over their public IPs.
# Abort before changing kubelet settings if the address is still an example.
if ! ip -4 -br addr | grep -q " ${PRIV[$ME]}/"; then
  echo "${PRIV[$ME]} is not on $ME: fix cluster.env or the hostname"
  exit 1
fi
sed -i '/^--node-ip=/d' /var/snap/microk8s/current/args/kubelet
echo "--node-ip=${PRIV[$ME]}" >> /var/snap/microk8s/current/args/kubelet
snap restart microk8s
microk8s status --wait-ready
kubectl get nodes -o wide             # INTERNAL-IP must be the private IP
```

If INTERNAL-IP shows the public IP, fix it **now**, before joining. `grep -c node-ip /var/snap/microk8s/current/args/kubelet` must print `1` (a duplicate `--node-ip` broke the test cluster once).

**Check:** `snap list microk8s` shows `held` in the Notes column.

---

## 4. Join the nodes (HA: 3 voters)

All three nodes join as **control-plane** nodes (no `--worker`). With 3 of them, MicroK8s turns on HA by itself: each runs the API server, and the dqlite datastore has 3 voters.

**node1**: print a join line. Run it once per node, since each line works only once:

```bash
. /root/cluster.env
microk8s add-node | grep -m1 "^microk8s join ${PRIV[node1]}:"
```

**node2** (then **node3**, with a new line from node1): paste the printed line. It looks like this, **without** `--worker`:

```bash
microk8s join 10.0.0.11:25000/<token>/<hash>
```

Wait for the join to finish (about a minute) before you join the next node.

**node1**: once all the nodes have joined:

```bash
kubectl -n kube-system set env daemonset/calico-node IP_AUTODETECTION_METHOD=kubernetes-internal-ip
kubectl -n kube-system rollout status ds/calico-node

kubectl get nodes -o wide                                     # all Ready, private INTERNAL-IPs
kubectl -n kube-system get pods -o wide | grep calico-node    # one per node, private IPs
```

**Check HA (every node):**

```bash
microk8s status | grep -A2 high-availability
# high-availability: yes
#   datastore master nodes: 10.0.0.11:19001 10.0.0.12:19001 10.0.0.13:19001
#   datastore standby nodes: none
```

All three private IPs must be listed as master nodes, on port 19001. Public IPs here mean the nodes joined over the internet: fix it now (below).

**Nodes joined with public IPs?** While the cluster is still empty:
1. Run `microk8s leave` on each joined node, and wait for it to finish.
2. Run `microk8s remove-node <name>` on node1.
3. Fix `cluster.env`, repeat steps 2.4 and 3 on every node, then join again.

### 4.1 API server watch settings (every node, one node at a time)

On MicroK8s with dqlite, the test cluster saw stalled Kubernetes watches (stale pods/nodes, provisioning stuck; see "Provisioning stalled" at the end). The fix in use: no API watch cache and no streaming watch-list. The operator chart already sets its client side (`kubernetesClient.watchList: false`). The server side:

```bash
A=/var/snap/microk8s/current/args/kube-apiserver
cp $A $A.bak.$(date +%Y%m%d%H%M%S)
if grep -q '^--feature-gates=' $A; then
  grep -q 'WatchList=false' $A || sed -i -E 's/^--feature-gates="?([^"]*)"?$/--feature-gates=\1,WatchList=false/' $A
else
  echo '--feature-gates=WatchList=false' >> $A
fi
sed -i '/^--watch-cache=/d' $A; echo '--watch-cache=false' >> $A
snap restart microk8s.daemon-kubelite
microk8s status --wait-ready
grep -E '^--(feature-gates|watch-cache)=' $A
```

Do the next node only when `kubectl get nodes` shows all nodes Ready again. Without the watch cache, the API reads dqlite directly: a little more datastore load, in exchange for watches that never go stale.

If kubelite doesn't come back after a later Kubernetes upgrade and `journalctl -u snap.microk8s.daemon-kubelite | grep -i "feature gate"` complains about `WatchList`, remove `WatchList=false` from that line (the gate no longer exists) and restart.

---

## 5. Addons and charts

**node1:**

```bash
microk8s enable dns ingress cert-manager helm3
kubectl -n ingress rollout status ds/traefik
kubectl -n cert-manager rollout status deploy/cert-manager-webhook
```

Addons are cluster-wide: enable them once, on node1 only. Don't enable `hostpath-storage` (single-node data) or `metallb`. Longhorn is the storage, and step 7 handles the public address.

**Your workstation**, from the repo root: copy the charts to node1:

```bash
NODE1=198.51.100.11      # node1 public IP
tar czf - compute/charts | ssh root@$NODE1 'mkdir -p /root/odoo-saas-platform && tar xzf - -C /root/odoo-saas-platform'
```

**Check (node1):**

```bash
kubectl get pods -A | grep -vE 'Running|Completed'          # only the header line
ls $REPO/compute/charts/vendor                              # includes longhorn and prometheus
ls $REPO/compute/charts/odoo-operator/crds                  # saas.odoo.example.com_odooinstances.yaml
```

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
  --set defaultSettings.nodeDownPodDeletionPolicy=delete-both-statefulset-and-deployment-pod \
  --set defaultSettings.nodeDrainPolicy=block-for-eviction-if-contains-last-replica \
  --set defaultSettings.storageOverProvisioningPercentage=200
kubectl -n longhorn-system rollout status deploy/longhorn-driver-deployer --timeout=10m
kubectl -n longhorn-system rollout status ds/longhorn-csi-plugin --timeout=10m
```

- `kubeletRootDir`: MicroK8s's kubelet path. Without it, volumes never mount.
- `defaultClassReplicaCount` / `defaultReplicaCount`: copies per volume, from `REPLICAS` (see the Overview for the trade-off).
- `nodeDownPodDeletionPolicy`: when a node dies, the tenant restarts elsewhere without manual help (if its volume has a healthy replica).
- `nodeDrainPolicy=block-for-eviction-if-contains-last-replica`: with one replica, `kubectl drain` first copies a volume's only replica to another node, so maintenance never strands data. Drains take longer (they wait for the copy).
- `storageOverProvisioningPercentage=200`: new Managed tenants use one PVC for PostgreSQL and Odoo, sized to the whole package; legacy tenants keep separate volumes until migrated (`STORAGE-COST-FIX.md`). 200% is kept for existing thin-provisioned volumes; it is not a second storage allowance. Watch real disk use (act at 70%).

**Check:** a test volume is written, with `REPLICAS` copies on different nodes, then deleted:

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

### 6.1 Changing the replica count later (node1)

New volumes: set `REPLICAS` in `cluster.env` (every node) and run the step 6 install again. Existing volumes keep their count until you change each one:

```bash
. /root/cluster.env
for v in $(kubectl -n longhorn-system get volumes.longhorn.io -o name); do
  kubectl -n longhorn-system patch $v --type=merge -p "{\"spec\":{\"numberOfReplicas\":$REPLICAS}}"
done
kubectl -n longhorn-system get volumes.longhorn.io \
  -o custom-columns=NAME:.metadata.name,REPLICAS:.spec.numberOfReplicas,ROBUSTNESS:.status.robustness
# wait until every attached volume is "healthy" (rebuilding copies takes time and disk)
```

`REPLICAS` can't exceed the number of schedulable nodes (replicas go on different nodes). Check free disk first: each extra replica is a full copy.

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
No LB? Add one `A` record per node's public IP, and set `LB_IP` to node1's public IP. Browsers then retry another IP when a node is down, but slowly; an LB is the real HA path.

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

The name `letsencrypt-prod` goes into the cluster record (step 13, `TLS_ISSUER`).

---

## 9. Odoo operator (node1)

The operator isolates each tenant: its own namespace, the `restricted` Pod Security level, and a default-deny NetworkPolicy. The values below select plain Ingress (class `traefik`) and allow only Traefik (namespace `ingress`, label `app.kubernetes.io/name=traefik`) to reach tenants. The chart's default (`gateway-api`) needs Gateway API CRDs that MicroK8s doesn't have.

```bash
. /root/cluster.env; cd $REPO
cat > $REPO/operator-values-prod.yaml <<'EOF'
networking:
  provider: ingress
  ingressClassName: traefik
  gateway:
    namespace: ingress
    podSelector:
      app.kubernetes.io/name: traefik
# imagePullSecrets: [{name: regcred}]   # once the operator images are private
EOF
# CRDs first, server-side: Helm installs CRDs once but never upgrades them.
kubectl apply --server-side --force-conflicts -f compute/charts/odoo-operator/crds/
helm upgrade --install odoo-operator compute/charts/odoo-operator \
  -n odoo-system --create-namespace -f operator-values-prod.yaml
kubectl -n odoo-system rollout status deploy/odoo-operator
```

**Check:**

```bash
kubectl get crd odooinstances.saas.odoo.example.com                  # exists
kubectl -n odoo-system get pods -o wide                              # 1 Running (a single replica, by design)
kubectl -n odoo-system get deploy odoo-operator \
  -o jsonpath='{.spec.strategy.type} {.spec.template.spec.containers[0].image}{"\n"}'
                                                                     # Recreate docker.io/moutazmuhammad/odoo-saas-operator:0.1.28
kubectl -n odoo-system logs deploy/odoo-operator --tail=20 | grep -i "starting workers"
```

The chart runs exactly one operator (`replicas: 1`, `Recreate`, `--leader-elect=false`): an upgrade stops the old pod before the new one starts, so two operators never reconcile the same tenant. There is no replica setting to raise. Keep `operator-values-prod.yaml`: every operator upgrade uses it.

---

## 10. Prometheus (node1)

The control plane reads tenant CPU/RAM usage through the Kubernetes API proxy. Prometheus is never exposed publicly. The values run only the Prometheus server (15-day retention, 8 Gi volume) and kube-state-metrics.

```bash
. /root/cluster.env; cd $REPO
helm upgrade --install prometheus compute/charts/vendor/prometheus \
  -n monitoring --create-namespace -f compute/charts/monitoring/prometheus-values.yaml
kubectl -n monitoring rollout status deploy/prometheus-server --timeout=5m
kubectl -n monitoring get pvc      # Bound, on longhorn
```

The cluster record uses namespace `monitoring` and service `prometheus-server:80` (step 13 sets them).

---

## 11. Image registry (only if customers deploy Git repos)

Use a private registry with a fixed token: GHCR, Docker Hub (private), Harbor, or GCP Artifact Registry. Not the in-cluster test registry (single-node, plain HTTP), and not AWS ECR, whose tokens expire every 12 hours. Put it in the cluster's *Image Builds* tab: host, push host, path prefix (your org), and a push/pull-only token. Leave plain-HTTP off.

Builds use temporary pod storage (up to about 24 GiB ephemeral per build, `STORAGE-COST-FIX.md`): keep that much free on the node OS disks.

---

## 12. API access for the control plane

### 12.1 API address

The control plane talks to one address, `API_HOST:16443`. For it to survive the loss of node1, `API_HOST` must reach every control-plane node:

- **Best:** a TCP load balancer on 16443 → all three nodes (passthrough, health check TCP 16443), with `API_HOST` = its IP or a DNS name for it. Allow the LB on 16443 in the firewall.
- **Simpler:** a DNS name, e.g. `k8s-api.example.com`, with one `A` record per node's public IP. The client tries the next IP when one doesn't answer, but a dead node can slow or fail a call, so the cluster may flap to *Unreachable* until you remove the dead node's record.
- **Test only:** node1's public IP. No API failover.

### 12.2 Add that address to the API certificate (every node)

```bash
. /root/cluster.env
T=/var/snap/microk8s/current/certs/csr.conf.template
if [[ $API_HOST =~ ^[0-9.]+$ ]]; then SAN="IP.99 = $API_HOST"; else SAN="DNS.99 = $API_HOST"; fi
grep -qF "$SAN" $T || sed -i "s/^#MOREIPS/$SAN\n#MOREIPS/" $T
microk8s refresh-certs --cert server.crt
openssl x509 -in /var/snap/microk8s/current/certs/server.crt -noout -ext subjectAltName | tr ',' '\n' | grep -F "$API_HOST"
```

Do it on all three nodes: each serves its own certificate.

### 12.3 Create the control plane's token and kubeconfig (node1)

Safe to run again, for example to rebuild the file:

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

Don't use `microk8s config` instead: it's the permanent admin certificate. This token can be revoked by deleting its Secret. The control plane needs at least: create/update tenants, list the operator Deployment across namespaces, and read Leases in `kube-system` (its health check); `cluster-admin` covers all of it.

| Error | Fix |
|---|---|
| `https://:16443` / `ServerName or InsecureSkipVerify` | `API_HOST` is empty: do step 2.2, then run 12.3 again. |
| `certificate is valid for …, not <API_HOST>` | Do 12.2 on every node. |
| Timeout on `:16443` | The firewall: allow 16443 from that machine (step 1). |
| `Unauthorized` | The token was recreated: run 12.3 again and redo step 13. |

### 12.4 Staff cluster terminal (node1, optional)

The **Terminal** button on a *Kubernetes Clusters* form opens `kubectl` and `helm` on the cluster in the browser. It runs in a toolbox pod that the control plane starts in the `saas-toolbox` namespace, as the `saas-toolbox` ServiceAccount. The control plane never grants permissions itself; you decide here what that ServiceAccount may do:

```bash
kubectl create namespace saas-toolbox --dry-run=client -o yaml | kubectl apply -f -
kubectl -n saas-toolbox create serviceaccount saas-toolbox --dry-run=client -o yaml | kubectl apply -f -
kubectl create clusterrolebinding saas-toolbox --clusterrole=cluster-admin \
  --serviceaccount=saas-toolbox:saas-toolbox --dry-run=client -o yaml | kubectl apply -f -
```

- **Read-only instead:** use `--clusterrole=view`. The terminal can then list, describe and read logs, but not change anything.
- **Who can open it:** users with *SaaS Terminal → Cluster Shell* on the Users form. Every session is logged on the cluster record.
- **The pod:** `alpine/k8s` (kubectl and helm). It stops on its own after 8 hours and starts again on the next open.
- **Turn it off:** `kubectl delete clusterrolebinding saas-toolbox`. To remove everything, also run `kubectl delete namespace saas-toolbox`.

---

## 13. Register the cluster in the control plane

The control plane needs three records:

- a **Region**: the location customers pick at checkout;
- a **Kubernetes Cluster** inside it (`saas.server`): the kubeconfig, TLS issuer and Prometheus. A region can have several clusters, and new instances go to the least-loaded healthy one;
- a **Base domain** (`saas.based.domain`): the tenant URLs. Its wildcard DNS points at one cluster, so instances on that domain are placed on that cluster.

The script below creates or updates all three. It's safe to run again. Its health check needs the operator from step 9 running.

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
NODE_PUBLIC_IP = '198.51.100.11'      # node1 public IP (must be unique across clusters)
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

**Check:** the output must show `health ok`.

| `health unreachable` with … | Fix |
|---|---|
| a timeout / connection error | The SaaS server can't reach `API_HOST:16443`: firewall (step 1) or DNS (12.1). |
| `Odoo operator is missing` / `unavailable` | Do step 9; wait for the operator pod to be Running. |
| `has no recent leader heartbeat` | A controller is stalled: see "Provisioning stalled" at the end. |
| `same public IP as server …` | Another record already uses that IP: one cluster = one record. Reuse its name. |

Then repeat 13.2 and 13.3.

**Notes:**
- For a test catalog (Odoo versions, the "Odoo Hosting" product, plans), install the **SaaS Demo Catalog** app (`saas_demo_data`) from Apps.
- A second cluster in the same region: repeat 12.3 on that cluster, then 13.2 and 13.3 with a new `CLUSTER_NAME` and that cluster's own `BASE_DOMAIN` (its wildcard DNS points at its own load balancer).
- Still to set in the backend (`SAAS-SERVER-SETUP.md`, step 8): backup storage (step 14), a payment provider, mail, and the registry (step 11) if you use it.

---

## 14. Backups

**14.1 Tenant backups (required).** With `REPLICAS=1`, they are the only copy of a volume that sits on a lost disk.

1. Settings → SaaS Manager → backup storage: the S3 bucket (provider, keys, bucket, region/endpoint).
2. The key gets access to **that bucket only**.
3. Turn on bucket versioning or object lock, if the provider has it.
4. Turn on scheduled backups on the plans.

**14.2 Cluster state (recommended, node1).** Keep a copy of the dqlite datastore and the cluster certificates off the cluster, e.g. weekly and before every upgrade:

```bash
D=/root/cluster-backup-$(date +%F); mkdir -p $D
microk8s dbctl backup -o $D/dqlite.tar.gz
tar czf $D/certs.tar.gz -C /var/snap/microk8s/current certs args
ls -lh $D        # copy this folder off the node (it holds cluster secrets: store it encrypted)
```

This is for disaster recovery of the Kubernetes objects (`microk8s dbctl restore`). Tenant data lives in the S3 backups, not here.

---

## 15. Tests before go-live

**15.1 A test tenant.** Order one instance from the portal (or the backend) on `apps.example.com`. Then, on node1:

```bash
kubectl get odooinstances -A                                   # PHASE Ready, READY True
SUB=<sub>; NS=odoo-tenant-odoo-$SUB
kubectl -n $NS get pods -o wide        # odoo-… 2/2 (web + cron), postgresql-0 1/1, on the same node
kubectl -n $NS get pvc                 # odoo-filestore Bound, storageclass longhorn (one shared data volume)
kubectl -n $NS get certificate         # odoo-tls READY True
curl -s "https://$SUB.apps.example.com/web/health?db_server_status=1"   # "status": "pass", "db_server_status": true
```

In the backend, the instance shows **Online**. When something is stuck, `kubectl describe odooinstance -A | less` shows the reason in its conditions (image pull, database, route/TLS, update Job).

**15.2 Tenants on every node.** Order 3 test instances. `kubectl get pods -A -o wide | grep odoo-` should show them on different nodes, and every `https://<sub>.apps.example.com/web/login` returns 200. If one node's tenant times out, the step 9 labels are wrong or the LB doesn't target that node.

**15.3 A node dies.** Power off node2 from the console (not node1, so you can still watch from it).
- `kubectl get nodes` still works (3 control-plane nodes); `microk8s status` still says `high-availability: yes` with one voter missing.
- The SaaS backend still shows the cluster healthy (API failover through `API_HOST`).
- Tenants on the other nodes keep answering.
- node2's tenants restart elsewhere within about 5–10 minutes **if** their volumes have a replica on another node. With `REPLICAS=1`, a tenant whose only replica is on node2 stays down until node2 is back: this is expected under the low-cost policy.
- Power node2 back on: it rejoins, its tenants' volumes come back, and (with `REPLICAS` ≥ 2) Longhorn rebuilds missing copies.

**15.4 Zero-downtime update.** Poll a tenant while you trigger an update (e.g. a module update or plan change) from the control plane. Every response must be 200:

```bash
while true; do curl -s -o /dev/null -w "%{http_code} " https://<sub>.apps.example.com/web/login; sleep 1; done
```

The replacement pod starts on the **same** node as the old one (it shares the RWO volume), so that node needs room for both pods for a moment. A rollout stuck `Pending` means the node is full.

**15.5 Node maintenance.** `kubectl drain node3 --ignore-daemonsets --delete-emptydir-data` completes (with `REPLICAS=1` it waits while Longhorn copies the last replicas away), node3's tenants come back on other nodes, then `kubectl uncordon node3`.

**15.6 Backup and restore:** back up a test tenant, restore it, and log in.

**15.7 Security:**
- From an outside machine, `nc -zv -w3 <node-public-ip> 16443` must fail (unless that machine is allowed), and so must ports 10250, 25000 and 19001.
- SSH password login must be refused.

---

## 16. Operations

**Upgrade Kubernetes:** one minor version at a time, one node at a time. Back up first (14.2). For each node:

```bash
kubectl drain node1 --ignore-daemonsets --delete-emptydir-data   # from any node
snap refresh microk8s --channel=1.36/stable                       # on node1 (an explicit refresh ignores the hold)
microk8s status --wait-ready                                      # on node1
kubectl uncordon node1
kubectl get nodes                                                 # all Ready, node1 on the new version
kubectl -n longhorn-system get volumes.longhorn.io -o custom-columns=NAME:.metadata.name,STATE:.status.state,ROBUSTNESS:.status.robustness
# wait: all nodes Ready, attached volumes healthy, `microk8s status` HA yes; then the next node
```

Check step 4.1's settings survived (`grep -E '^--(feature-gates|watch-cache)=' /var/snap/microk8s/current/args/kube-apiserver`). Before the jump, check that the Longhorn version supports the new Kubernetes (`kubeVersion` in its `Chart.yaml`).

**OS updates and reboots:** drain → `apt full-upgrade` → reboot → uncordon, one node at a time. Never have two control-plane nodes down at once.

**Upgrade the operator** (copy the charts first, as in step 5):

```bash
. /root/cluster.env; cd $REPO
kubectl apply --server-side --force-conflicts -f compute/charts/odoo-operator/crds/
helm upgrade odoo-operator compute/charts/odoo-operator -n odoo-system -f operator-values-prod.yaml
kubectl -n odoo-system rollout status deploy/odoo-operator
```

With `Recreate`, provisioning pauses for the few seconds between the old pod stopping and the new one starting. Running tenants are not restarted by an operator upgrade unless the new version changes their pods.

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

**A node is down for a while (it will come back):** do nothing to the cluster. The other two voters keep the API. Tenants with a replica elsewhere move automatically; with `REPLICAS=1`, tenants whose volume is only on that node wait for it. Find them:

```bash
kubectl -n longhorn-system get volumes.longhorn.io \
  -o custom-columns=NAME:.metadata.name,PVC:.status.kubernetesStatus.pvcName,NS:.status.kubernetesStatus.namespace,ROBUSTNESS:.status.robustness \
  | grep -E 'faulted|unknown'
```

**A node is lost for good:**
1. On a healthy node: `microk8s remove-node <name> --force`. Check `microk8s status`: two datastore voters left. Remove its IP from the LB, the tenant DNS (if round robin) and `API_HOST` records.
2. Tenants whose volume was `faulted` (only replica on the lost disk): restore each from its latest S3 backup in the control plane. Their data since that backup is lost. With `REPLICAS` ≥ 2 this step is empty.
3. In Longhorn, remove the dead node (UI → Node → Delete) so it stops counting its disk.
4. Build a new server with steps 2–3, using the same name and IP if possible. If the IP changes, update `cluster.env` everywhere and run step 2.4 on every node.
5. Join it (step 4, without `--worker`), redo 4.1 and 12.2 on it, and add it back to the LB and DNS. `microk8s status` must show 3 datastore master nodes again.

**Lost quorum (two control-plane nodes down at once):** the API stops; running tenants keep serving but nothing can change. Bring the nodes back if you can: quorum returns by itself. If they are gone for good, follow the MicroK8s "Recover from lost quorum" procedure (https://microk8s.io/docs/restore-quorum) on the survivor, using the 14.2 backup if needed. Don't run `microk8s reset` on a node holding data.

**Add capacity (worker nodes):**
1. Add it to `cluster.env` on every node (`NODES`, `PRIV`, `PUB`), and run step 2.4 on the existing nodes.
2. Build the new node with steps 2–3.
3. Join it using the `--worker` line from `microk8s add-node`. Keep exactly 3 control-plane nodes (odd voters; more voters add latency, not safety at this size).
4. Run step 7.1 again (the Traefik externalIPs), and add the node to the LB.

**Watch:** `kubectl get nodes`, `microk8s status`, `kubectl get odooinstances -A`, `kubectl get certificates -A`, Longhorn volume robustness, and disk space (act at 70%).

**Revoke the control plane's access:** `kubectl -n kube-system delete secret saas-control-plane-token`. Then run 12.3 and 13 again.

---

## Appendix: managed Kubernetes (GKE / EKS / AKS / DOKS)

| Step | Change |
|---|---|
| 1–4 | Create a cluster with 3+ nodes in one zone (on GKE: Standard, not Autopilot). The provider runs the API with HA. Skip 4.1. |
| 5 | Install an ingress controller and cert-manager with Helm (vendored under `compute/charts/vendor/`). |
| 6 | Skip it. Use the cloud disks: `standard-rwo` / `gp3` / `managed-csi` / `do-block-storage`. |
| 7 | Skip the patches. The ingress Service gets a cloud LB (EKS: use a CNAME). |
| 9 | Set `ingressClassName` and the NetworkPolicy labels for your ingress controller. |
| 12 | Skip 12.1–12.2. Restrict the API to the SaaS server's IP. 12.3 is required (with `server` from your kubeconfig). |
| 14.2, 16 | Skip the MicroK8s parts; the provider handles control-plane backups and upgrades. |

---

## Final checklist

- [ ] 3 control-plane nodes Ready with private INTERNAL-IPs; `microk8s status` shows `high-availability: yes` and 3 datastore master nodes on private IPs
- [ ] Step 4.1 settings on every node
- [ ] Firewall attached: 16443 only from the SaaS server, admin (and API LB); 10250/25000/19001 not public
- [ ] SSH keys only; `microk8s` snap held; clocks synced
- [ ] Longhorn is the default StorageClass; test volume passed; replica count chosen on purpose (`REPLICAS`) and the trade-off accepted
- [ ] `*.apps.example.com` resolves to the LB; `letsencrypt-prod` READY
- [ ] Operator: 1 pod Running, `Recreate`, image 0.1.28; Prometheus PVC Bound
- [ ] `API_HOST` reaches every control-plane node and is in every node's certificate
- [ ] Step 13 shows `health ok`; kubeconfig copies deleted
- [ ] Tenant backups to S3 scheduled; a restore tested; a cluster-state backup (14.2) stored off the cluster
- [ ] Tests 15.1–15.7 passed

---

## Provisioning stalled before any tenant pods exist

Check the operator lease renewal time, tenant resources, and node service logs. Pod `Running` and node `Ready` values can be stale when Kubernetes watches stop receiving datastore updates; confirm fresh leases and resource changes as well.

```bash
kubectl -n kube-system get lease kube-scheduler kube-controller-manager \
  -o custom-columns=NAME:.metadata.name,HOLDER:.spec.holderIdentity,RENEWED:.spec.renewTime   # renewed within the last minute
grep -c node-ip /var/snap/microk8s/current/args/kubelet                                     # 1, on every node
journalctl -u snap.microk8s.daemon-kubelite --since -15min | tail -50
```

The October 5, 2026 test-cluster incident had an invalid duplicate `--node-ip` on node1, stale API cache reads, and stalled datastore watches. The existing `odoo-ensan` request resumed after correcting the private IP, backing up and restarting the datastore watch services, and restarting Kubernetes services/operator with compatible list/watch settings. No tenant resource or database was deleted.

For this MicroK8s cluster, the API-server configuration retains the existing feature gates plus `WatchList=false` and uses `--watch-cache=false` (step 4.1). The operator uses `KUBE_FEATURE_WatchListClient=false` (chart value `kubernetesClient.watchList: false`). These settings are a mitigation for the observed watch/cache failure; disabling the server cache increases direct datastore reads. Back up the args files and datastore before changing them, apply changes one node at a time, and verify controller leases, new resource events, operator rollout, and tenant readiness. Do not edit datastore contents or reset the cluster to recover provisioning.

The control plane now checks cluster controllers every minute and before provisioning. It requires an available operator deployment and, when leader election is enabled, an operator lease renewed within 120 seconds (the chart disables leader election, so no operator lease is needed). Existing scheduler/controller-manager leases must also be fresh; absent control-plane leases are allowed for managed Kubernetes. The kubeconfig must permit listing operator deployments across namespaces and reading coordination leases. A reachability-only API response is insufficient.

Deployment waits recheck controller health every 30 seconds and tolerate one transient failure. Two consecutive failures stop that attempt with an infrastructure error, allowing the durable queue to retry with backoff. A Failed OdooInstance stops the wait immediately. Cluster health transitions use the existing infrastructure alert mechanism, exclude unhealthy clusters from new allocations, and flag pending provisioning for retry after recovery. Automatic retries remain bounded; an exhausted deployment requires operator intervention. These safeguards detect the observed stall; they do not automatically restart the cluster or guarantee that Kubernetes cannot fail.
