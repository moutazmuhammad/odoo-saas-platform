# Tenant cluster setup on MicroK8s

This guide turns a fresh Ubuntu server into a Kubernetes cluster that the SaaS control plane can deploy customer Odoo instances onto. Each tenant becomes an `OdooInstance` resource. The operator turns it into a namespace with Postgres, Odoo, an Ingress with a Let's Encrypt certificate, and backups.

It is written for one node, like the current test cluster. Section 9 covers extra nodes.

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

- `hostpath-storage` provides the default StorageClass for tenant volumes, and is fine for a single node. On several nodes, use a network StorageClass instead (see section 9).
- `ingress` installs Traefik in namespace `ingress`, with IngressClass `traefik`.

## 2. Route ports 80/443 to the ingress

Without a cloud load balancer, the Traefik Service stays `<pending>`. Give it the node's IP, then mark it ready in its status. The operator waits for that status before it reports a tenant's route as ready.

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

You will enter this name (`letsencrypt-prod`) on the Region in the control plane.

## 4. The Odoo operator

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
  - `docker.io/moutazmuhammad/odoo-saas-operator:0.1.5`
  - `docker.io/moutazmuhammad/odoo-saas-backup-tool:0.1.0`, used by the backup and restore Jobs and compiled in as the default.

  With a private registry, set `imagePullSecrets` in the values.
- **Upgrading the operator** later means re-running the two commands above: CRDs first, then `helm upgrade`.

## 5. Prometheus (tenant CPU/RAM usage and the customer dashboard)

```bash
helm upgrade --install prometheus compute/charts/vendor/prometheus \
  -n monitoring --create-namespace \
  -f compute/charts/monitoring/prometheus-values.yaml
kubectl -n monitoring rollout status deploy/prometheus-server
```

- It runs only the Prometheus server (15-day retention, 8 Gi volume) and kube-state-metrics.
- The control plane reads it through the Kubernetes API proxy, so nothing is exposed publicly.
- Leave the Region's Monitoring fields at their defaults: `monitoring` and `prometheus-server:80`.

## 6. Image registry for customer Git repositories (optional)

Only needed if customers deploy their own Git repos. Each push builds an image inside the cluster. Any OCI registry works: GHCR, Docker Hub, Harbor, ECR. For a single test node, an in-cluster registry is enough:

```bash
sudo apt-get install -y apache2-utils
kubectl create namespace container-registry
kubectl -n container-registry create secret generic registry-auth \
  --from-literal=htpasswd="$(htpasswd -nbB saasbuild '<choose-a-password>')"
kubectl apply -f compute/examples/test-registry/registry.yaml
kubectl -n container-registry rollout status deploy/registry
```

For this registry, fill in the Region's "Tenant Image Builds" fields as follows:

| Field | Value |
|---|---|
| Registry Host | `localhost:32000` (the host nodes pull from) |
| Registry Push Host | `registry.container-registry.svc.cluster.local:5000` |
| Username / Password | `saasbuild` / the password chosen above |
| Plain-HTTP Registry | on |

## 7. Kubeconfig for the control plane

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

This file is an admin credential for the whole cluster. Upload it in the control plane (a Kubeconfig record, stored encrypted), then delete the local copy. Allow `16443` only from the control-plane server.

## 8. Verify end to end

After registering the cluster in the control plane (see `SAAS-SERVER-SETUP.md`, step 8) and deploying a first instance:

```bash
kubectl get odooinstances -A                          # PHASE Ready, READY True
kubectl -n odoo-tenant-odoo-<sub> get pods            # odoo-*, postgresql-0 Running
curl -sI https://<sub>.apps.example.com/web/login     # HTTP/2 200
echo | openssl s_client -connect <sub>.apps.example.com:443 \
  -servername <sub>.apps.example.com 2>/dev/null | openssl x509 -noout -issuer
                                                      # Let's Encrypt
```

When something is stuck, run `kubectl describe odooinstance odoo-<sub>`. The conditions show the reason: image pull, database, route/TLS, or the update Job.

## 9. More nodes / production notes

- **Joining nodes:**
  - On the first node, `microk8s add-node` prints a join command; run it on the new node. Open `25000/tcp` between nodes.
  - With 3 nodes, `microk8s` runs HA (ha-cluster is on by default).
- **Storage:**
  - `hostpath-storage` is node-local.
  - For several nodes, install a network StorageClass (NFS, Longhorn, Ceph, or the cloud's CSI) and make it the default.
  - Tenants on a higher compute tier (2+ replicas) need ReadWriteMany.
  - A rolling update of a ReadWriteOnce tenant can hang if the new pod is scheduled on another node (PLAN.txt 3.7).
- **Backups:** tenant backups need an S3/GCS bucket configured in the control plane. The backup Job runs in each tenant namespace.
- **Upgrades:** `sudo snap refresh microk8s --channel=<next>/stable`, one minor version at a time, one node at a time.
- **Credentials:** keep the kubeconfig and the registry password only in the control plane. Rotate them if they were ever shared.
