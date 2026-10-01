# Tenant cluster setup on MicroK8s

This guide turns a fresh Ubuntu server into a Kubernetes cluster that the SaaS control plane can deploy customer Odoo instances onto. Each tenant becomes an `OdooInstance` resource. The operator turns it into a namespace with Postgres, Odoo, an Ingress with a Let's Encrypt certificate, and backups.

> **For production, use `PRODUCTION-CLUSTER-SETUP.md` instead.** This guide is for test and small single-node clusters.

Sections 1–8 set up one node, like the current test cluster. Section 9 adds a second node, and section 10 covers three or more. Section 11 explains what changes on a managed cluster (GKE, EKS, AKS, DigitalOcean, …).

## How it fits together

The control plane (the Odoo SaaS server) never logs into the node. It talks only to the **Kubernetes API**, using a kubeconfig, and it creates one `OdooInstance` per tenant. Everything else happens inside the cluster.

```
customer browser ──https──▶ DNS *.apps.example.com ──▶ ingress controller (Traefik) ──▶ tenant Odoo pod
                                                            ▲ TLS cert from cert-manager
control plane ──kubeconfig──▶ Kubernetes API ──▶ operator ──▶ namespace: Odoo + Postgres + volumes + Ingress
                     └──(API proxy)──▶ Prometheus  (CPU/RAM charts)
```

So any cluster, MicroK8s or cloud, must provide these seven things. Every step below delivers one of them:

| # | The cluster must have | Why the platform needs it | MicroK8s step |
|---|---|---|---|
| 1 | A Kubernetes API the control plane can reach | Every create, update and delete goes through it | 1, 7 |
| 2 | A **default StorageClass** | Each tenant's Postgres data and Odoo filestore live on volumes. Without it, pods stay `Pending` | 1 |
| 3 | An **ingress controller with a public address** | It routes `<sub>.apps.example.com` to the right tenant. The operator waits until the Ingress has an address before it marks the tenant ready | 1, 2 |
| 4 | **cert-manager + a ClusterIssuer** | It issues the Let's Encrypt HTTPS certificate for each tenant automatically | 1, 3 |
| 5 | The **Odoo operator** and its CRDs | It turns an `OdooInstance` into the real pods, volumes and routes | 4 |
| 6 | **Prometheus** | Tenant CPU/RAM usage and the customer dashboard | 5 |
| 7 | An image registry (optional) | Only for customers who deploy their own Git repos: each push builds an image that must be stored somewhere | 6 |

Plus one thing outside the cluster: **wildcard DNS** pointing at the ingress address.

## 0. What you need

| Item | Notes |
|---|---|
| Server | Ubuntu 22.04/24.04, 4+ vCPU, 8+ GB RAM, 80+ GB disk. 2 vCPU / 4 GB works for a few small tenants. |
| Public IP | For example `203.0.113.10`. |
| Wildcard DNS | `*.apps.example.com` → the public IP (an `A` record). Without a domain, `<ip-with-dashes>.nip.io` works, e.g. `203-0-113-10.nip.io`. |
| Firewall | Open `80`, `443` to everyone. Open `16443` (Kubernetes API) only to the control-plane server. Open `22` for you. |
| A checkout of this repo | On the node, or on a workstation with the kubeconfig (step 7). |

Commands below run on the node as `root` or a sudo user in the `microk8s` group. `kubectl` means `microk8s kubectl` and `helm` means `microk8s helm3`; set aliases if you like:

```bash
alias kubectl='microk8s kubectl'; alias helm='microk8s helm3'
```

## 1. Install MicroK8s

```bash
sudo snap install microk8s --classic --channel=1.35/stable   # the tested version
sudo usermod -aG microk8s $USER && newgrp microk8s
microk8s status --wait-ready
microk8s enable dns hostpath-storage ingress cert-manager helm3
kubectl get nodes          # STATUS Ready
```

**Why:** MicroK8s is a bare Kubernetes. The addons add the pieces from the table above: `dns` (pods find each other by name), `hostpath-storage` (#2), `ingress` (#3), `cert-manager` (#4), `helm3` (installs steps 4–5).

- `hostpath-storage` provides the default StorageClass for tenant volumes, and is fine for a single node. On several nodes, use a network StorageClass instead (see section 9).
- `ingress` installs Traefik in namespace `ingress`, with IngressClass `traefik`.

## 2. Route ports 80/443 to the ingress

**Why:** on a cloud, a `LoadBalancer` Service gets a public address automatically. A plain server has no such thing, so the Traefik Service stays `<pending>`. Nothing would receive traffic on 80/443, and the operator would wait forever (requirement #3).

Give the Service the node's IP, then mark it ready in its status. The operator waits for that status before it reports a tenant's route as ready.

```bash
IP=203.0.113.10
kubectl -n ingress patch svc traefik --type=merge \
  -p "{\"spec\":{\"externalIPs\":[\"$IP\"]}}"
kubectl -n ingress patch svc traefik --subresource=status --type=merge \
  -p "{\"status\":{\"loadBalancer\":{\"ingress\":[{\"ip\":\"$IP\"}]}}}"
curl -sI http://$IP | head -1        # any HTTP answer (404 is fine)
```

> **Do not** `microk8s enable metallb` with the node's own IP as the pool. It fights the kernel's ARP replies and breaks SSH to the node.

## 3. TLS: Let's Encrypt ClusterIssuer

**Why:** cert-manager is installed, but it doesn't know *where* to get certificates. The ClusterIssuer tells it: Let's Encrypt, proven by an HTTP challenge served through Traefik. After that, every tenant Ingress gets its certificate with no manual work (requirement #4).

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

You will enter this name (`letsencrypt-prod`) on the cluster in the control plane (*Kubernetes Clusters*).

## 4. The Odoo operator

**Why:** this is our own software, the part that actually runs tenants (requirement #5). The CRDs teach Kubernetes the new resource type `OdooInstance`. The operator watches those resources and creates each tenant's namespace, Postgres, Odoo, volumes, Ingress and backup Jobs. The values file tells it which ingress controller this cluster uses.

```bash
git clone https://github.com/moutazmuhammad/odoo-saas-platform.git && cd odoo-saas-platform

# CRDs first — Helm installs them once but never upgrades them.
kubectl apply --server-side --force-conflicts -f compute/charts/odoo-operator/crds/

helm upgrade --install odoo-operator compute/charts/odoo-operator \
  -n odoo-system --create-namespace \
  -f compute/examples/test-cluster/operator-values.yaml

kubectl -n odoo-system rollout status deploy/odoo-operator
kubectl -n odoo-system logs deploy/odoo-operator --tail=5   # "Starting workers"
```

- `operator-values.yaml` selects `networking.provider: ingress` with class `traefik`. Use the chart default `gateway-api` only when the Gateway API CRDs and a Gateway exist.
- Images come from Docker Hub, public for now:
  - `docker.io/moutazmuhammad/odoo-saas-operator:0.1.8`
  - `docker.io/moutazmuhammad/odoo-saas-backup-tool:0.1.0`, used by the backup and restore Jobs and compiled in as the default.

  With a private registry, set `imagePullSecrets` in the values.
- **Upgrading the operator** later means re-running the two commands above: CRDs first, then `helm upgrade`.

## 5. Prometheus (tenant CPU/RAM usage and the customer dashboard)

**Why:** the control plane shows usage charts and bills compute. It gets those numbers by querying Prometheus, which collects them from the cluster (requirement #6).

```bash
helm upgrade --install prometheus compute/charts/vendor/prometheus \
  -n monitoring --create-namespace \
  -f compute/charts/monitoring/prometheus-values.yaml
kubectl -n monitoring rollout status deploy/prometheus-server
```

- It runs only the Prometheus server (15-day retention, 8 Gi volume) and kube-state-metrics.
- The control plane reads it through the Kubernetes API proxy, so nothing is exposed publicly.
- Leave the cluster's Monitoring fields at their defaults: `monitoring` and `prometheus-server:80`.

## 6. Image registry for customer Git repositories (optional)

**Why:** a customer's Git repo is built into a Docker image inside the cluster. The image has to be pushed somewhere the nodes can pull it from (requirement #7). Skip this step if no customer uses Git repos.

Any OCI registry with a fixed password works: GHCR, Docker Hub, Harbor (not ECR, see section 11). For a single test node, an in-cluster registry is enough:

```bash
sudo apt-get install -y apache2-utils
kubectl create namespace container-registry
kubectl -n container-registry create secret generic registry-auth \
  --from-literal=htpasswd="$(htpasswd -nbB saasbuild '<choose-a-password>')"
kubectl apply -f compute/examples/test-registry/registry.yaml
kubectl -n container-registry rollout status deploy/registry
```

For this registry, fill in the cluster's "Image Builds" tab as follows:

| Field | Value |
|---|---|
| Registry Host | `localhost:32000` (the host nodes pull from) |
| Registry Push Host | `registry.container-registry.svc.cluster.local:5000` |
| Username / Password | `saasbuild` / the password chosen above |
| Plain-HTTP Registry | on |

## 7. Kubeconfig for the control plane

**Why:** the kubeconfig is the address plus the credentials of the Kubernetes API (requirement #1). The control plane stores it on the cluster record and uses it for everything.

```bash
microk8s config > kubeconfig-<cluster-name>
grep server: kubeconfig-<cluster-name>     # must be https://<PUBLIC-IP>:16443
```

If `server:` shows a private IP:
1. Edit it to the public IP.
2. Add the public IP to the API certificate, then check again from the control-plane server with `kubectl --kubeconfig ... get nodes`:

```bash
sudo sed -i 's/^#MOREIPS/IP.99 = 203.0.113.10\n#MOREIPS/' \
  /var/snap/microk8s/current/certs/csr.conf.template
sudo microk8s refresh-certs --cert server.crt
```

This file is an admin credential for the whole cluster. Upload it in the control plane (*Kubernetes Clusters*, stored encrypted), then delete the local copy. Allow `16443` only from the control-plane server.

## 8. Verify end to end

**Why:** this proves the whole chain works for a real tenant: operator → Postgres and Odoo pods → Ingress → DNS → certificate.

After registering the cluster in the control plane (see `SAAS-SERVER-SETUP.md`, step 8.7) and deploying a first instance:

```bash
kubectl get odooinstances -A                          # PHASE Ready, READY True
kubectl -n odoo-tenant-odoo-<sub> get pods            # odoo-*, postgresql-0 Running
curl -sI https://<sub>.apps.example.com/web/login     # HTTP/2 200
echo | openssl s_client -connect <sub>.apps.example.com:443 \
  -servername <sub>.apps.example.com 2>/dev/null | openssl x509 -noout -issuer
                                                      # Let's Encrypt
```

When something is stuck, run `kubectl describe odooinstance odoo-<sub>`. The conditions show the reason: image pull, database, route/TLS, or the update Job.

## 9. Adding a second node

Do sections 1–8 on the first node (`node1`) first. The second node (`node2`) joins as a **worker**: it runs tenant pods but not the Kubernetes API or its database.

> **Two nodes are not HA.** MicroK8s needs 3 control-plane nodes for HA. With 2, `node1` still holds the API and the datastore. If `node1` goes down, the control plane can't reach the cluster, and the ingress IP is gone too. A second node adds capacity, not failover. For failover, see section 10.

### 9.1 Prepare node2

- Same Ubuntu version and the **same MicroK8s channel** as node1.
- A unique hostname. Each node must resolve the other's hostname. Without internal DNS, add both to `/etc/hosts` on **both** nodes:

  ```bash
  echo "10.0.0.11 node1
  10.0.0.12 node2" | sudo tee -a /etc/hosts
  ```

- Firewall between the nodes. Use the private network if there is one, and don't open these ports to the internet:

  | Port | From → to | Used for |
  |---|---|---|
  | `25000/tcp` | node2 → node1 | Join / cluster agent |
  | `16443/tcp` | node2 → node1 | Kubernetes API |
  | `10250/tcp` | node1 → node2 | Kubelet (logs, exec, metrics) |
  | `4789/udp` | both ways | Pod network (Calico VXLAN) |

### 9.2 Install MicroK8s on node2 and join

On node2, install only. **Don't enable any addons**: they are cluster-wide and already on node1.

```bash
sudo snap install microk8s --classic --channel=1.35/stable
sudo usermod -aG microk8s $USER && newgrp microk8s
microk8s status --wait-ready
```

On node1, create a join token. It is single-use and expires:

```bash
microk8s add-node
# prints e.g.:  microk8s join 10.0.0.11:25000/<token>/<hash> --worker
```

On node2, run the printed line **with `--worker`**. Use node1's private IP if it has one.

```bash
microk8s join 10.0.0.11:25000/<token>/<hash> --worker
```

### 9.3 Verify (on node1)

A worker has no local API, so run `kubectl` on node1:

```bash
kubectl get nodes -o wide                       # node1 and node2 Ready
kubectl -n kube-system get pods -o wide | grep node2   # calico-node Running on node2
kubectl run nettest --rm -it --restart=Never --image=busybox \
  --overrides='{"spec":{"nodeName":"node2"}}' -- nslookup kubernetes.default
                                                # resolves → pod network + DNS work on node2
```

If `calico-node` on node2 stays `0/1`, `4789/udp` is blocked between the nodes.

### 9.4 Ingress

Nothing changes. The wildcard DNS and the Traefik `externalIPs` still point at node1. kube-proxy on node1 forwards traffic to the Traefik pod, even when that pod runs on node2.

Optionally, also send traffic through node2. Add node2's public IP to the Traefik Service (both patches from section 2, with both IPs in each list), add a second `A` record for `*.apps.example.com`, and open `80/443` on node2. This spreads the load, but it is still not failover: if node1 dies, the API is gone anyway.

### 9.5 Storage — check this before putting tenants on node2

`hostpath-storage` keeps each volume on the disk of **one** node. A tenant is safe only if its pod always runs on the node that holds its data. Check that the StorageClass binds late and that the volumes are pinned:

```bash
kubectl get sc microk8s-hostpath -o jsonpath='{.volumeBindingMode}{"\n"}'
                                   # want: WaitForFirstConsumer
kubectl get pv -o custom-columns=NAME:.metadata.name,NODE:.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0]
                                   # want: a node name on every PV
```

- **Both OK:** each new tenant's volumes are created on the node where its pod first runs, and the pod stays there for good. Volumes that existed before the join stay on node1. This is fine for a test or small setup. But if a node dies, its tenants stay down until it comes back. You can only move them by backup and restore.
- **Either missing** (`Immediate`, or PVs with no node): **don't** schedule tenants on node2. A pod could start on node2 with an empty directory while its data sits on node1. Keep node2 out of tenant scheduling with `kubectl cordon node2` until real storage is in place.

For real multi-node use, install a network StorageClass and make it the default. Options: Longhorn, NFS, Ceph, or the cloud provider's CSI driver. Vendor its Helm chart under `compute/charts/vendor/`, like Prometheus. With network storage, a tenant's pod can move to either node.

Other points:

- **Tenants with 2+ replicas** need `spec.storage.filestore.accessMode: ReadWriteMany`, and the operator refuses anything else. That requires an RWX-capable class, such as Longhorn RWX or NFS. hostpath can't do it.
- **Rolling updates with network RWO volumes:** if the new pod lands on the other node, it can't attach the volume and the rollout hangs (PLAN.txt 3.7, still open). Test an update on each node before you rely on it.
- **Shared volumes:** Prometheus (section 5) and the test registry (section 6) also use hostpath volumes, so they stay on the node they started on. The registry is still reachable from both nodes at `localhost:32000` through its NodePort.

### 9.6 Control plane side

Nothing to change. The kubeconfig still points at `node1:16443`, and the cluster record stays the same. New tenants are scheduled on either node automatically.

### 9.7 Removing node2

```bash
kubectl drain node2 --ignore-daemonsets --delete-emptydir-data   # on node1
microk8s leave                                                   # on node2
microk8s remove-node node2                                       # on node1
```

With hostpath storage, move node2's tenants off first (backup and restore). Otherwise draining leaves them `Pending`, with their data still on node2.

## 10. Three or more nodes / production notes

- **HA:** join nodes 2 and 3 **without** `--worker`. With 3 control-plane nodes, MicroK8s turns on HA automatically (`microk8s status` shows `high-availability: yes`), and the cluster survives the loss of one. Any extra nodes beyond that can join with `--worker`. For the control plane to survive losing node1, put a load balancer or DNS name in front of the API servers, use it in the kubeconfig, and add it to the certificate (section 7). Do the same for ingress: a load balancer, or an `A` record per node.
- **Storage:** network storage is required (see 9.5).
- **Backups:** tenant backups need an S3/GCS bucket configured in the control plane. The backup Job runs in each tenant namespace.
- **Upgrades:** `sudo snap refresh microk8s --channel=<next>/stable`, one minor version at a time, one node at a time (drain → refresh → uncordon).
- **Credentials:** keep the kubeconfig and the registry password only in the control plane. Rotate them if they were ever shared.

## 11. Other clusters: GKE, EKS, AKS, DigitalOcean, …

The platform doesn't care which Kubernetes it runs on. It needs the same seven things from the table at the top. What changes is **who provides each one**: on MicroK8s you add everything by hand, while a managed cloud gives you some of it for free and needs a few extra steps elsewhere.

### What changes, step by step

| Step | MicroK8s | Managed cloud (GKE / EKS / AKS / DOKS) |
|---|---|---|
| 1. Cluster + nodes | `snap install`, one server | Create it in the console or CLI (`gcloud container clusters create`, `eksctl create cluster`, `az aks create`, `doctl kubernetes cluster create`). Nodes are a *node pool*. Run `kubectl`/`helm` from your workstation, not on a node. |
| Storage (#2) | `hostpath-storage`, node-local | Built in, backed by cloud disks: GKE `standard-rwo`, AKS `managed-csi`, DOKS `do-block-storage`. **EKS:** install the *EBS CSI driver* add-on and mark `gp3` as the default. Check with `kubectl get sc` (one must say `(default)`). |
| Ingress (#3) | `ingress` addon (Traefik) | Nothing bundled that we use. Install an ingress controller with Helm, e.g. ingress-nginx or Traefik. Its `LoadBalancer` Service gets a **real cloud load balancer automatically**. |
| Step 2 (externalIPs patch) | Needed | **Skip it.** The cloud load balancer fills in the address itself. |
| DNS | `A` record → node IP | `A` record → the load balancer IP. **EKS** gives a *hostname*, not an IP, so use a `CNAME` for `*.apps.example.com`. |
| cert-manager (#4) | Addon | Install it with Helm (vendor the chart under `compute/charts/vendor/`). The ClusterIssuer in step 3 is the same, with `ingressClassName` changed to your controller (e.g. `nginx`). |
| Operator (#5) | Step 4 | Same commands. Change `ingressClassName` in the values file to your controller. |
| Prometheus (#6) | Step 5 | Same. |
| Registry (#7) | In-cluster, plain HTTP | Use a real registry: Docker Hub, GHCR, Harbor, or GCP Artifact Registry. Turn **Plain-HTTP off**, and set Registry Host and Push Host to the same value. **Avoid ECR for now**: its passwords expire every 12 hours, and the cluster stores a fixed password. |
| Kubeconfig (#1) | `microk8s config`, works as is | **Must be rebuilt** (see below). |
| API firewall | Open `16443` only to the control plane | Restrict the API endpoint to the control-plane server's IP: GKE *authorized networks*, EKS *public access CIDRs*, AKS *authorized IP ranges*. |
| 2+ nodes, HA | Sections 9–10 | Built in: the cloud runs the API with HA. Just set the node count or autoscaling. |

### The kubeconfig on a cloud

A kubeconfig downloaded from GKE, EKS or AKS doesn't hold a password. It runs a helper program (`gke-gcloud-auth-plugin`, `aws eks get-token`, `kubelogin`) that logs in with *your* cloud account. The control plane has neither the program nor your account, so that file won't work there.

Instead, create a ServiceAccount in the cluster and a kubeconfig that uses its token:

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

SERVER=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
CA=$(kubectl -n kube-system get secret saas-control-plane-token -o jsonpath='{.data.ca\.crt}')
TOKEN=$(kubectl -n kube-system get secret saas-control-plane-token -o jsonpath='{.data.token}' | base64 -d)
cat > kubeconfig-<cluster-name> <<EOF
apiVersion: v1
kind: Config
clusters: [{name: c, cluster: {server: $SERVER, certificate-authority-data: $CA}}]
users: [{name: saas, user: {token: $TOKEN}}]
contexts: [{name: c, context: {cluster: c, user: saas}}]
current-context: c
EOF
kubectl --kubeconfig kubeconfig-<cluster-name> get nodes   # must work without any cloud login
```

Upload that file to the control plane as in step 7. Deleting the Secret revokes it.

### Cloud gotchas

- **Zones:** a cloud disk lives in **one zone**. If the node pool spans several zones, a tenant's pod can't move to a node in another zone, and a rolling update can hang there (PLAN.txt 3.7). Start with a **single-zone** node pool, or use regional disks (GKE `standard-rwo-regional`, AKS ZRS).
- **Tenants with 2+ replicas** need a ReadWriteMany class: GKE Filestore CSI, EKS EFS CSI, AKS `azurefile-csi`. DOKS has none, so use NFS or Longhorn there.
- **GKE Autopilot** restricts what pods may do and bills per pod. Use a **Standard** cluster.
- **Cost:** each cloud load balancer is billed. We need only **one**, for the ingress controller. Never give tenants their own `LoadBalancer` Service.
- **Gateway API:** GKE has it built in, and the operator supports it (`networking.provider: gateway-api`). The tested path is `ingress`, though, so start with that.

### Everything else

Sections 3 (ClusterIssuer), 4 (operator), 5 (Prometheus) and 8 (verify) are the same on every cluster. On a cloud, the whole setup is: **create the cluster → install an ingress controller → install cert-manager → steps 3, 4, 5 → wildcard DNS → ServiceAccount kubeconfig → register the cluster**.
