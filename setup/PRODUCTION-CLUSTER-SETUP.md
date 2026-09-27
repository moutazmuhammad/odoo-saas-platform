# Production cluster setup

This guide builds a production Kubernetes cluster for tenant Odoo instances, step by step. It covers high availability, traffic, storage, security, backups, the tests to run before go-live, and day-to-day operations.

- **Other guides in this folder:**
  - `SAAS-SERVER-SETUP.md` — the control-plane server.
  - `MICROK8S-CLUSTER-SETUP.md` — a single-node test cluster, plus an explanation of what each piece is for.
  - `RUN-GUIDE.txt` — the dev environment.
- **Where commands run:** on a node, as a sudo user in the `microk8s` group, unless the step says otherwise. `kubectl` means `microk8s kubectl` and `helm` means `microk8s helm3`:

  ```bash
  alias kubectl='microk8s kubectl'; alias helm='microk8s helm3'
  ```

- **Example values**, which you replace with your own:

  | Thing | Example |
  |---|---|
  | Nodes (private IPs) | `node1` 10.0.0.11, `node2` 10.0.0.12, `node3` 10.0.0.13 |
  | Load balancer public IP | `203.0.113.50` |
  | Tenant domain | `*.apps.example.com` |
  | Kubernetes API name | `k8s-api.example.com` |
  | Control-plane server IP | `198.51.100.20` |

---

## The design in one page

```
                    customers
                        │ https
                        ▼
        DNS  *.apps.example.com → 203.0.113.50
                        │
             ┌──────────▼──────────┐
             │ Cloud load balancer │   TCP 80/443, health-checks every node
             └───┬───────┬───────┬─┘
                 ▼       ▼       ▼                 private network 10.0.0.0/24
             ┌──────┐ ┌──────┐ ┌──────┐
             │node1 │ │node2 │ │node3 │   each node: Kubernetes control plane + tenants
             │      │ │      │ │      │   Traefik (ingress) + cert-manager (TLS)
             │ disk │ │ disk │ │ disk │   Longhorn: every volume copied to 3 nodes
             └──────┘ └──────┘ └──────┘
                 ▲       ▲       ▲
                 └── k8s-api.example.com:16443 (3 A records), open only to the control plane
                        ▲
                 control plane (SaaS server) ──▶ tenant backups ──▶ S3 bucket
```

| Area | Choice | What it gives you |
|---|---|---|
| **HA** | 3 identical MicroK8s nodes, all running the control plane | The cluster keeps working when any **one** node dies. 2 nodes can't do this; 3 is the minimum. |
| **Traffic** | One cloud TCP load balancer → all 3 nodes | Customers never depend on a single node. TLS ends inside the cluster (cert-manager + Let's Encrypt). |
| **Storage** | Longhorn, 3 copies of every volume | A tenant's database and files survive the loss of a node, and the tenant restarts on another node. |
| **API** | `k8s-api.example.com` → 3 nodes, firewalled | The control plane can still manage the cluster when a node is down. Only the control plane can reach it. |
| **Security** | Private network, cloud firewall, SSH keys only, revocable token for the control plane, tenant isolation | Details in steps 1, 2, 9 and 12. |
| **Backups** | Nightly tenant backups to an S3 bucket at another provider/region | Protection against the loss of the whole cluster. |

Why MicroK8s and not GKE/EKS? This works on any provider, including cheap VPS providers, and it is what the platform is tested on. If you'd rather use a managed cloud, see the appendix. It is less work, but costs more.

---

## Read first: what isn't production-ready yet

These gaps are in the platform, not in this guide. Fix them, or accept them knowingly, before real customers arrive.

1. **Rolling updates across nodes (PLAN.txt 3.7).** A tenant's filestore volume attaches to one node at a time. If the updated pod starts on a different node, the update can hang, and zero downtime isn't guaranteed. Test 15.3 checks this. It's a blocker until it passes.
2. **Operator images are public** on Docker Hub (`moutazmuhammad/*`). Move them to a private registry before go-live.
3. **Tenants can only send traffic out on HTTPS (443) and DNS.** The tenant NetworkPolicy blocks SMTP ports 25/465/587. So tenant Odoo can't use a normal SMTP server; it needs a mail provider reachable over HTTPS, or a policy change.
4. **Let's Encrypt limits** a domain to about 50 new certificates per week. More than ~50 new tenants a week under `apps.example.com` would hit that. The fix, a wildcard certificate via DNS-01, isn't built yet.
5. **Not yet run on this platform:** steps 6 (Longhorn), 7 (load balancer), 9 (NetworkPolicy labels) and 12 (API failover) are standard setups, but untested here. Section 15 is where you prove them.

---

## 0. Shopping list

| Item | Recommended | Why |
|---|---|---|
| 3 servers, same size, same datacenter | Ubuntu 24.04. Start at 8 vCPU / 32 GB RAM each (minimum 4 vCPU / 16 GB). | 3 nodes give HA. The same size means any node can take over another's load. The same datacenter keeps the latency low that Kubernetes and Longhorn need. |
| One extra data disk per server | SSD, 200+ GB, the same size on all 3 | Longhorn keeps a copy of **every** volume on every node, so each disk needs room for *all* tenant data plus ~30%. A separate disk keeps a full disk from killing the OS. |
| Private network | The provider's VPC / private networking | Node-to-node traffic (storage copies, pod traffic) never goes over the internet. |
| Cloud load balancer | TCP, ports 80 and 443 | A single public entry point that routes around a dead node. |
| Cloud firewall | The provider's firewall, attached to all 3 servers | It blocks everything you don't explicitly open. |
| A domain | `*.apps.example.com` and `k8s-api.example.com` | Tenant URLs and the API name. |
| S3 bucket | At a **different** provider or region | Tenant backups must survive the loss of the whole cluster. |
| Email address | For Let's Encrypt | Expiry warnings. |

---

## 1. Cloud firewall

**Why:** everything is closed by default, and each opening has a reason. Use the provider's firewall rather than `ufw` on the nodes: `ufw` and the Kubernetes network (Calico) get in each other's way.

Inbound rules, applied to all 3 servers:

| Port | From | Reason |
|---|---|---|
| `22/tcp` | **Your admin IP only** | SSH |
| `80/tcp`, `443/tcp` | The load balancer (or everyone, if the provider can't restrict it to the LB) | Customer traffic |
| `16443/tcp` | Control-plane server `198.51.100.20` + your admin IP | Kubernetes API |
| Everything | The private network `10.0.0.0/24` | Node-to-node: Kubernetes, Calico, Longhorn |
| Everything else | — | **Denied** |

Outbound: allow all. Nodes pull images, reach Let's Encrypt, and send backups to S3.

---

## 2. Prepare each server (all 3)

**Why:** a clean, patched, key-only OS, with the tools Longhorn needs installed before Kubernetes arrives.

```bash
# Updates and packages. open-iscsi + nfs-common are needed by Longhorn.
sudo apt update && sudo apt -y full-upgrade
sudo apt -y install unattended-upgrades open-iscsi nfs-common
sudo systemctl enable --now iscsid unattended-upgrades

# multipathd grabs Longhorn's disks and breaks them (a known Longhorn issue).
sudo systemctl disable --now multipathd multipathd.socket 2>/dev/null || true

# Hostname: node1 / node2 / node3, one per server.
sudo hostnamectl set-hostname node1

# Every node must resolve every other node by name.
echo "10.0.0.11 node1
10.0.0.12 node2
10.0.0.13 node3" | sudo tee -a /etc/hosts
```

**SSH: keys only.** First make sure your key login works. Then:

```bash
sudo sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication no/; s/^#\?PermitRootLogin .*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
sudo systemctl restart ssh
```

**Data disk for Longhorn.** The device name is an example; check yours with `lsblk`.

```bash
sudo mkfs.ext4 /dev/sdb
sudo mkdir -p /var/lib/longhorn
echo '/dev/sdb /var/lib/longhorn ext4 defaults,nofail 0 2' | sudo tee -a /etc/fstab
sudo mount -a && df -h /var/lib/longhorn
```

**Check:** `ping node2` works from node1 (and the same for every pair), and `systemctl is-active iscsid` says `active`.

---

## 3. Install MicroK8s (all 3)

**Why:** the same version on every node. The *hold* stops snap from upgrading Kubernetes by itself in the middle of the day. You upgrade by hand, one node at a time (section 16).

```bash
sudo snap install microk8s --classic --channel=1.35/stable
sudo snap refresh --hold microk8s
sudo usermod -aG microk8s $USER && newgrp microk8s

# Make Kubernetes use the PRIVATE IP for this node (10.0.0.12 on node2, etc.).
echo '--node-ip=10.0.0.11' | sudo tee -a /var/snap/microk8s/current/args/kubelet
sudo snap restart microk8s
microk8s status --wait-ready
```

---

## 4. Join the 3 nodes into one HA cluster

**Why:** with 3 control-plane nodes, MicroK8s turns on HA by itself. The cluster's database is kept on all 3, and any one node can die.

On **node1**, create a token (run it once per node you're adding; each token works once):

```bash
microk8s add-node
# prints: microk8s join 10.0.0.11:25000/<token>/<hash>
```

On **node2**, run the printed line **without** `--worker`. Use the 10.0.0.x address. Then do the same for **node3** with a new token from node1.

```bash
microk8s join 10.0.0.11:25000/<token>/<hash>
```

Make the pod network use the private IPs too (once, on any node):

```bash
kubectl -n kube-system set env daemonset/calico-node IP_AUTODETECTION_METHOD=kubernetes-internal-ip
```

**Check:**

```bash
microk8s status | grep high-availability     # high-availability: yes
kubectl get nodes -o wide                    # 3 × Ready, INTERNAL-IP = 10.0.0.x
kubectl -n kube-system get pods -o wide | grep calico-node   # 3 × Running
```

---

## 5. Addons (once, on any node)

**Why:** DNS for the cluster, the ingress controller (Traefik), cert-manager for TLS, and Helm for installing the rest. Addons are cluster-wide, so enable them once.

```bash
microk8s enable dns ingress cert-manager helm3
```

**Don't enable `hostpath-storage`.** It keeps data on a single node, which is exactly what production must avoid. Longhorn replaces it in the next step.

**Check:** `kubectl get pods -A` shows everything `Running`, and `kubectl get sc` shows **no** StorageClass yet.

---

## 6. Storage: Longhorn

**Why:** each tenant's Postgres data and Odoo files live on volumes. Longhorn keeps 3 copies of every volume, one per node. When a node dies, the tenant starts on another node with its data intact.

**6.1 Vendor the chart** (project rule: open-source charts live in the repo). On your workstation, pick the latest stable Longhorn version that supports Kubernetes 1.35:

```bash
helm repo add longhorn https://charts.longhorn.io && helm repo update
helm search repo longhorn/longhorn --versions | head -5
helm pull longhorn/longhorn --version <VERSION> --untar -d compute/charts/vendor/
git add compute/charts/vendor/longhorn && git commit -m "chore(charts): vendor Longhorn <VERSION>"
```

**6.2 Install** (on a node, from the repo checkout):

```bash
helm upgrade --install longhorn compute/charts/vendor/longhorn \
  -n longhorn-system --create-namespace \
  --set csi.kubeletRootDir=/var/snap/microk8s/common/var/lib/kubelet \
  --set persistence.defaultClass=true \
  --set persistence.defaultClassReplicaCount=3 \
  --set defaultSettings.defaultReplicaCount=3 \
  --set defaultSettings.defaultDataPath=/var/lib/longhorn \
  --set defaultSettings.nodeDownPodDeletionPolicy=delete-both-statefulset-and-deployment-pod
kubectl -n longhorn-system rollout status deploy/longhorn-driver-deployer
```

What the settings do:
- `kubeletRootDir` — MicroK8s keeps kubelet files in a non-standard place. Without this setting, volumes never mount.
- `ReplicaCount=3` — one copy on each node.
- `nodeDownPodDeletionPolicy` — when a node dies, Longhorn frees the tenant's volume so it can start elsewhere. Without it, the tenant stays stuck until someone steps in.

**6.3 Check:**

```bash
kubectl get sc                         # longhorn (default)
kubectl -n longhorn-system get pods    # all Running
# A test volume: create it, confirm it's Bound, delete it.
kubectl create -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: longhorn-test, namespace: default}
spec: {accessModes: [ReadWriteOnce], resources: {requests: {storage: 1Gi}}}
EOF
kubectl run lh-test --image=busybox --restart=Never --overrides='{"spec":{"volumes":[{"name":"v","persistentVolumeClaim":{"claimName":"longhorn-test"}}],"containers":[{"name":"c","image":"busybox","command":["sh","-c","echo ok > /v/t && cat /v/t"],"volumeMounts":[{"name":"v","mountPath":"/v"}]}]}}'
sleep 30; kubectl logs lh-test         # ok
kubectl delete pod lh-test; kubectl delete pvc longhorn-test
```

**Longhorn UI:** don't expose it publicly. Open it through a tunnel when you need it:
`kubectl -n longhorn-system port-forward svc/longhorn-frontend 8080:80`, then browse http://localhost:8080.

---

## 7. Traffic: load balancer → Traefik

**Why:** customers reach one address, the load balancer. It sends each request to any healthy node, and Traefik on that node forwards it to the right tenant, even if the tenant runs on another node.

**7.1 Traefik accepts traffic on every node.** It listens on the nodes' private IPs, and the Service is marked with the LB address. The operator waits for that address before it marks a tenant ready.

```bash
LB_IP=203.0.113.50
kubectl -n ingress patch svc traefik --type=merge \
  -p '{"spec":{"externalIPs":["10.0.0.11","10.0.0.12","10.0.0.13"]}}'
kubectl -n ingress patch svc traefik --subresource=status --type=merge \
  -p "{\"status\":{\"loadBalancer\":{\"ingress\":[{\"ip\":\"$LB_IP\"}]}}}"
```

Traefik itself must run on more than one node:

```bash
kubectl -n ingress get pods -o wide    # one per node? Good (DaemonSet).
# If it's a Deployment with 1 pod:
kubectl -n ingress scale deploy traefik --replicas=3
```

**7.2 Create the load balancer** in the provider console:
- **Forwarding:** TCP 80 → 80 and TCP 443 → 443 (TCP *passthrough*, **no** TLS on the LB, because cert-manager handles certificates).
- **Targets:** node1, node2 and node3, on their private IPs.
- **Health check:** TCP port 80.

**7.3 DNS:** `*.apps.example.com` — `A` record → `203.0.113.50`.

**Check:** `curl -sI http://anything.apps.example.com` answers (404 is fine: no tenant yet). Then stop Traefik on one node, or power the node off, and the curl still answers.

> **No load balancer at your provider?** Point `*.apps.example.com` at the 3 nodes' public IPs (3 `A` records), put those public IPs in `externalIPs`, and put one of them in the status patch. Browsers retry another IP when one fails, but less cleanly than a load balancer.

---

## 8. TLS: Let's Encrypt

**Why:** every tenant gets a real HTTPS certificate automatically, and it renews by itself.

```bash
cat <<'EOF' | kubectl apply -f -
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    email: ops@example.com
    server: https://acme-v02.api.letsencrypt.org/directory
    privateKeySecretRef: {name: letsencrypt-prod-account-key}
    solvers:
      - http01: {ingress: {ingressClassName: traefik}}
EOF
kubectl get clusterissuer letsencrypt-prod    # READY True
```

---

## 9. The Odoo operator

**Why:** it turns each `OdooInstance` from the control plane into the tenant's namespace, Postgres, Odoo, volumes, route and backups.

**Security built into the operator** (no action needed):
- Each tenant gets its own namespace, running under the `restricted` Pod Security level.
- A default-deny NetworkPolicy blocks all traffic between tenants.
- The Odoo pods have no access to the Kubernetes API.

The one thing you must set is **which pods are allowed to send traffic to tenants**: the ingress controller. Find Traefik's labels:

```bash
kubectl -n ingress get pods --show-labels     # e.g. app.kubernetes.io/name=traefik
```

Write the production values (put the label you found under `podSelector`):

```bash
cat > operator-values-prod.yaml <<'EOF'
replicaCount: 2          # two copies of the operator; one works, one waits
leaderElection: true
networking:
  provider: ingress
  ingressClassName: traefik
  gateway:               # who may reach tenant pods (the NetworkPolicy)
    namespace: ingress
    podSelector:
      app.kubernetes.io/name: traefik
# imagePullSecrets: [{name: regcred}]   # once the operator images are private
EOF

kubectl apply --server-side --force-conflicts -f compute/charts/odoo-operator/crds/
helm upgrade --install odoo-operator compute/charts/odoo-operator \
  -n odoo-system --create-namespace -f operator-values-prod.yaml
kubectl -n odoo-system rollout status deploy/odoo-operator
```

Keep `operator-values-prod.yaml` safe (or commit it under `compute/examples/`). Every future operator upgrade uses it.

---

## 10. Prometheus

**Why:** the control plane reads tenant CPU/RAM usage from it, for the charts and for billing. It's reached only through the Kubernetes API, never publicly.

```bash
helm upgrade --install prometheus compute/charts/vendor/prometheus \
  -n monitoring --create-namespace \
  -f compute/charts/monitoring/prometheus-values.yaml
kubectl -n monitoring rollout status deploy/prometheus-server
kubectl -n monitoring get pvc          # Bound, on longhorn
```

---

## 11. Image registry (only if customers deploy their Git repos)

**Why:** each push to a customer's repo builds an image that must be stored somewhere private.

- **Production:** use a real private registry with a fixed token: GHCR, Docker Hub (private), Harbor, or GCP Artifact Registry. Don't use the in-cluster test registry: its data lives on one volume, and it runs over plain HTTP. Don't use AWS ECR either: its passwords expire every 12 hours.
- **Region fields in the control plane:**
  - *Registry Host* and *Registry Push Host*: both the registry address (e.g. `ghcr.io`).
  - *Registry Path Prefix*: your org.
  - *Username / Token*: a token that can **only** push and pull (not an admin token).
  - *Plain-HTTP*: **off**.

---

## 12. Kubernetes API access for the control plane

**Why:** the control plane manages everything through the API. The API must survive a dead node, and only the control plane should be able to use it.

**12.1 One name for all 3 nodes.** DNS: `k8s-api.example.com` gets 3 `A` records, one per node's public IP. The client tries the next IP when one fails.

**12.2 Add that name to the API certificate (on each of the 3 nodes):**

```bash
sudo sed -i 's/^#MOREIPS/DNS.99 = k8s-api.example.com\n#MOREIPS/' \
  /var/snap/microk8s/current/certs/csr.conf.template
sudo microk8s refresh-certs --cert server.crt
```

**12.3 A dedicated, revocable credential.** Don't give the control plane `microk8s config`, which is the permanent admin certificate. Create a ServiceAccount token instead. It can be revoked at any time by deleting its Secret.

```bash
kubectl -n kube-system create serviceaccount saas-control-plane
kubectl create clusterrolebinding saas-control-plane \
  --clusterrole=cluster-admin --serviceaccount=kube-system:saas-control-plane
kubectl -n kube-system apply -f - <<'EOF'
apiVersion: v1
kind: Secret
metadata:
  name: saas-control-plane-token
  annotations: {kubernetes.io/service-account.name: saas-control-plane}
type: kubernetes.io/service-account-token
EOF

CA=$(kubectl -n kube-system get secret saas-control-plane-token -o jsonpath='{.data.ca\.crt}')
TOKEN=$(kubectl -n kube-system get secret saas-control-plane-token -o jsonpath='{.data.token}' | base64 -d)
cat > kubeconfig-prod <<EOF
apiVersion: v1
kind: Config
clusters: [{name: prod, cluster: {server: "https://k8s-api.example.com:16443", certificate-authority-data: $CA}}]
users: [{name: saas, user: {token: $TOKEN}}]
contexts: [{name: prod, context: {cluster: prod, user: saas}}]
current-context: prod
EOF
```

**Check, from the control-plane server:** `kubectl --kubeconfig kubeconfig-prod get nodes` → 3 nodes. Then upload the file to the control plane (step 13) and **delete every local copy**.

---

## 13. Register the cluster in the control plane

In the backend (`SAAS-SERVER-SETUP.md`, step 8):

1. **Kubeconfig:** a new record; upload `kubeconfig-prod`.
2. **Region:**

   | Field | Value |
   |---|---|
   | Kubeconfig | the record above |
   | Ingress Host / Port | `203.0.113.50` / `80` |
   | Native Kubernetes Ingress TLS | **on** |
   | TLS ClusterIssuer | `letsencrypt-prod` |
   | Prometheus Namespace / Service | `monitoring` / `prometheus-server:80` (defaults) |
   | Tenant Image Builds | from step 11, if used |

3. **Server:** compute driver *Kubernetes*, this region, address `203.0.113.50`.
4. **Based domain:** `apps.example.com`, with this region and server.

---

## 14. Tenant backups

**Why:** Longhorn protects against losing a *node*. Backups protect against losing the *whole cluster*, human mistakes, and bad upgrades.

1. Settings → SaaS Manager → backup storage: the S3 bucket from the shopping list (provider, keys, bucket, region/endpoint).
2. Give the bucket's key access to **that bucket only**.
3. In the bucket, turn on versioning or object lock if the provider has it, so a deleted backup can be recovered.
4. Enable scheduled backups on the plans.

---

## 15. Tests before go-live (all must pass)

**15.1 Tenants on every node.** Order 3 test instances.

```bash
kubectl get pods -A -o wide | grep -E 'odoo-|postgresql'    # spread across node1..3
curl -sI https://<each-sub>.apps.example.com/web/login       # HTTP/2 200 for all 3
```

If a tenant on one node times out while the others work, the NetworkPolicy labels in step 9 are wrong.

**15.2 A node dies.** Power off the node that hosts one test tenant (from the provider console, not a clean shutdown).
- `kubectl get nodes` still works through `k8s-api.example.com`, and shows the node `NotReady`.
- The URLs of tenants on the other nodes keep answering.
- The tenant from the dead node comes back on another node within ~5–10 minutes, with its data. That's a restart, not zero downtime.
- Power the node back on, and it rejoins by itself. Longhorn then re-copies the volumes: watch for `healthy` in the UI.

**15.3 Zero-downtime update** (the PLAN.txt 3.7 blocker). While polling a tenant every second:

```bash
while true; do curl -s -o /dev/null -w "%{http_code} " https://<sub>.apps.example.com/web/login; sleep 1; done
```

trigger an update from the control plane (e.g. change the plan or redeploy). Every response must be `200`, and the update must finish. Repeat until the new pod has landed on a **different** node at least once.

**15.4 Backup and restore:** back up a test tenant, restore it, and log in.

**15.5 Security:**
- From an outside machine (not the control plane), `nc -zv <node-public-ip> 16443` must fail, and so must SSH from a non-admin IP.
- Password SSH login must be refused.

---

## 16. Day-to-day operations

**Upgrading Kubernetes.** One minor version at a time, one node at a time, and wait until the cluster is fully healthy before moving to the next node:

```bash
kubectl drain node1 --ignore-daemonsets --delete-emptydir-data
sudo snap refresh microk8s --channel=1.36/stable     # on node1
kubectl uncordon node1
# Wait: all nodes Ready, and every Longhorn volume healthy. Then node2, then node3.
```

**OS updates and reboots:** security patches install by themselves. For a kernel update reboot: drain → reboot → uncordon, one node at a time, as above.

**Upgrading the operator:** CRDs first, then Helm, using the same values file:

```bash
kubectl apply --server-side --force-conflicts -f compute/charts/odoo-operator/crds/
helm upgrade odoo-operator compute/charts/odoo-operator -n odoo-system -f operator-values-prod.yaml
```

**Upgrading Longhorn:** vendor the new chart version (6.1), read its upgrade notes, one minor version at a time, then `helm upgrade` with the same `--set` flags.

**Replacing a dead node:**
1. On a healthy node: `microk8s remove-node node3 --force`.
2. Prepare a new server (steps 2–3), with the same name and private IP if you can.
3. Join it (step 4) and add it to the load balancer.
4. Longhorn copies the data onto it by itself.

**Adding capacity:** prepare the servers the same way and join them with `--worker`. Keep exactly 3 control-plane nodes. Add each new node to the load balancer and the firewall.

**Watch regularly:**
- Longhorn disk usage: the UI, or `df -h /var/lib/longhorn` on each node. Act at 70%.
- `kubectl get nodes`.
- `kubectl get odooinstances -A` (anything not `Ready`).
- `kubectl get certificates -A` (anything not `True`).

**Credentials:**
- To revoke the control plane's access: `kubectl -n kube-system delete secret saas-control-plane-token`. Then create a new one (12.3) and upload it.
- Rotate the registry and S3 keys yearly, and immediately if they were ever shared.

---

## Appendix: using a managed cloud (GKE / EKS / AKS / DOKS) instead

The cloud runs the HA control plane, the nodes and the disks for you. So the steps change like this:

| Step | On a managed cloud |
|---|---|
| 1–4 (servers, MicroK8s, HA) | Create a cluster with a node pool of 3+ nodes, in **one zone**, with private nodes. On GKE, use a *Standard* cluster, not Autopilot. |
| 5 (addons) | Install an ingress controller (e.g. ingress-nginx) and cert-manager with Helm (vendor both charts). |
| 6 (Longhorn) | Skip. Use the cloud's disks: GKE `standard-rwo`, EKS `gp3` (install the EBS CSI add-on and make it the default), AKS `managed-csi`, DOKS `do-block-storage`. |
| 7 (traffic) | Skip the patches. The ingress controller's Service gets a cloud load balancer automatically. EKS gives a hostname, so use a `CNAME` for `*.apps.example.com`. |
| 8, 9, 10, 13–15 | The same, with `ingressClassName` and the NetworkPolicy labels set for your ingress controller. |
| 12 (API) | Skip 12.1 and 12.2. Restrict the API endpoint to the control-plane IP (*authorized networks*). 12.3 is **required**: a cloud kubeconfig needs a cloud login that the control plane doesn't have. |

---

## Final checklist

- [ ] 3 nodes Ready, `high-availability: yes`, INTERNAL-IPs private
- [ ] Firewall: 22 from admin only, 16443 from control plane only, 80/443 open, nothing else
- [ ] SSH keys only; MicroK8s snap on hold; unattended-upgrades on
- [ ] `longhorn (default)` StorageClass, 3 replicas, the only StorageClass
- [ ] Load balancer → 3 nodes; `*.apps.example.com` → LB
- [ ] `letsencrypt-prod` READY
- [ ] Operator running with 2 replicas; NetworkPolicy labels match Traefik
- [ ] Prometheus Bound on Longhorn
- [ ] ServiceAccount kubeconfig via `k8s-api.example.com` uploaded; local copies deleted
- [ ] Backups to an off-site S3 bucket; a restore tested
- [ ] Tests 15.1–15.5 passed
- [ ] The "not production-ready yet" list is fixed or knowingly accepted
