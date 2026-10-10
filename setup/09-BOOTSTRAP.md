# Bootstrap a SaaS VM or a client cluster

Run these three entrypoints from your workstation, in the repository root:

| Script | Target | Installs / verifies |
| --- | --- | --- |
| `scripts/bootstrap/saas.sh` | Fresh Ubuntu **24.04 LTS** VM | PostgreSQL, Odoo 18 + current local SaaS/SPA, systemd web/jobs, nginx HTTPS, deployment user/helper |
| `scripts/bootstrap/microk8s.sh` | One or more Ubuntu **24.04 LTS** VMs | MicroK8s, private-network node joins, Longhorn, Traefik, cert-manager, operator, Prometheus, readiness checks |
| `scripts/bootstrap/doks.sh` | An existing DigitalOcean Kubernetes cluster | DO storage check, Traefik/DO Load Balancer, cert-manager, operator, Prometheus, readiness checks |

They share Python helpers in `scripts/bootstrap/`. `plan` validates local configuration and prints scope without contacting the servers. `apply` performs the installation. No Git push is required. DOKS must already exist; this script does not create a DigitalOcean account, VPC or cluster. Ubuntu 22.04 and other releases are not supported by this first version.

These scripts prepare new infrastructure. For later code changes use [local deployment](08-LOCAL-DEPLOY.md); its receipts deploy only changed components. Existing unmanaged SaaS/MicroK8s installations are rejected instead of reset. Reusing the same bootstrap configuration resumes its own partial installation. Helm operations wait for readiness and enable rollback on failure (`--atomic` on Helm 3, `--rollback-on-failure` on Helm 4); a failure stops further steps. Successful earlier stages stay installed for a retry.

## Workstation prerequisites

Install Python 3, OpenSSH (`ssh`, `scp`), Git, and verified SSH host keys for the Ubuntu targets. SSH uses your configured private key, strict host-key checking and noninteractive authentication. The remote account needs root or passwordless `sudo`; VMs need Python 3 already installed. Bootstrap changes system packages/services on these dedicated VMs.

SaaS also needs Node/npm locally to build the current SPA. Cluster setup needs `kubectl` and Helm 3.14+ or Helm 4 locally. Docker is only needed for subsequent image builds through `local.sh`; bootstrap uses the image tags shipped in the operator chart unless you supply overrides.

Keep configuration, password files and generated kubeconfigs under the gitignored `.local-deploy/`. Paths in JSON resolve from the repository root, not the configuration file's directory. Keep that convention when running commands. Copy an example and edit all example domains, addresses, SSH keys and contexts before `apply`.

## 1. Deploy SaaS to an Ubuntu VM

Create a dedicated Ubuntu 24.04 VM. Point `saas.example.com` at its public IPv4 address. Open public TCP 80/443 and your administrative SSH port. The script keeps PostgreSQL and Odoo on local interfaces and does not change your firewall rules.

```bash
mkdir -p .local-deploy
chmod 700 .local-deploy
cp scripts/bootstrap/examples/saas.json .local-deploy/bootstrap-saas.json
chmod 600 .local-deploy/bootstrap-saas.json

# Generate a key for future local/CD deployments, if you do not have one.
ssh-keygen -t ed25519 -f "$HOME/.ssh/saas-deploy"

# Generate the initial application administrator password without printing it.
python3 - <<'PY'
import os, secrets
fd = os.open('.local-deploy/admin-password', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    stream.write(secrets.token_urlsafe(32) + '\n')
PY
```

Edit `.local-deploy/bootstrap-saas.json`: set `ssh` to the VM's administrative SSH account/key, `domain`, ACME `email`, `admin_login`, and `deploy_public_key` to `~/.ssh/saas-deploy.pub`. `admin_password_file` refers to the generated file. The public deployment key is uploaded; the private key stays on your workstation. The database defaults to `saas`. `workers` defaults to 2; choose capacity for the VM and concurrent users.

```bash
./scripts/bootstrap/saas.sh plan --config .local-deploy/bootstrap-saas.json
./scripts/bootstrap/saas.sh apply --config .local-deploy/bootstrap-saas.json
```

The script builds your current local SPA, uploads the current local control-plane code, installs Odoo 18 and Python requirements, initializes the four platform addons without demo data, sets the initial admin login/password, and freezes `web.base.url` to the HTTPS domain. It generates the master password and `saas_secret_key` once in `/etc/odoo/saas.conf`. Back up that file together with PostgreSQL and `/opt/saas/data`; encrypted credentials depend on the same key.

It installs `saas-odoo` and `saas-jobs`, obtains a real Let's Encrypt certificate, and verifies both services and HTTPS before success. A missing DNS record or closed port 80 stops certificate setup. Fix DNS/firewall and rerun the same command. On a completed installation, rerunning uses the existing deployment helper with database/filestore backups and retains the admin password/encryption key. Use `local.sh` for routine changes; it avoids reinstalling packages.

Set the `saas` entry in `.local-deploy/config.json` to the new domain, user `saas-deploy`, private key `~/.ssh/saas-deploy`, and verified known-hosts file. Then:

```bash
./scripts/deploy/local.sh plan --target saas
./scripts/deploy/local.sh deploy --target saas
```

Configure real SMTP, object storage/backups, payment providers, products/plans and supported tenant images in the SaaS UI before accepting paid orders. Bootstrap does not install the demo catalog or demo payment provider. PDF invoice rendering additionally needs a patched-Qt wkhtmltopdf package suitable for your VM architecture; see [the SaaS server guide](02-SAAS-SERVER-SETUP.md).

## 2. Prepare one or multiple MicroK8s VMs

Use dedicated Ubuntu 24.04 VMs with unique names and reachable private IPv4 addresses on the same private network. Open node-to-node networking as described in [the MicroK8s guide](03a-MICROK8S-CLUSTER-SETUP.md#1-firewall-provider-console), including TCP 16443/25000/19001/10250 and Calico UDP 4789. Allow SaaS/admin access to API TCP 16443. Public HTTP/HTTPS must reach the node host ports 80/443, directly or through a TCP load balancer. The script does not create a provider load balancer or edit provider firewalls.

```bash
cp scripts/bootstrap/examples/microk8s.json .local-deploy/bootstrap-microk8s.json
chmod 600 .local-deploy/bootstrap-microk8s.json
```

Edit `nodes`: one entry per VM, with its actual hostname, `private_ip`, SSH `host`, account/key and port. The script sets the hostname; use dedicated nodes. Keep `name` stable as the cluster identity. Set:

| Setting | Meaning |
| --- | --- |
| `channel` | Pinned MicroK8s channel, initially `1.35/stable`; snap auto-refresh is held |
| `api_host` | IPv4/DNS address reachable from SaaS and this workstation; added to API TLS certificates |
| `ingress_ip` | Public IPv4 of the TCP load balancer, or the first node for a single-node cluster |
| `domain` | Base domain for tenants, e.g. `apps.example.com`; wildcard DNS points to `ingress_ip` |
| `storage_class` | `longhorn` |
| `storage_replicas` | 1 for one node, usually 3 for three or more nodes; cannot exceed node count |
| `output` | Private portable kubeconfig output path |

For three VMs, add two more node entries and set `storage_replicas` to 3. All nodes join as control-plane nodes over the first node's private IP. One node works without HA; two nodes do not provide the three-voter HA topology. Use three or more for HA. The script does not format disks: Longhorn stores data under `/var/lib/longhorn`, so mount any dedicated data filesystem there **before** running setup.

```bash
./scripts/bootstrap/microk8s.sh plan --config .local-deploy/bootstrap-microk8s.json
./scripts/bootstrap/microk8s.sh apply --config .local-deploy/bootstrap-microk8s.json
```

It prepares iSCSI/NFS dependencies, sets private node IPs, joins nodes, enables DNS, and installs the vendored Longhorn/Traefik/cert-manager/operator/Prometheus charts. Traefik runs on every node using host ports 80/443 and publishes the configured ingress address. API certificate validation stays enabled. Longhorn becomes the default storage class for tenant PVCs and is used explicitly for monitoring and the readiness PVC.

An existing MicroK8s installation without this script's ownership marker is rejected. Changing an owned node's cluster identity/private IP/channel/API address is also rejected; this bootstrap is not a cluster migration or Kubernetes upgrade tool. Adding new node entries with the same cluster configuration is supported.

## 3. Prepare an existing DOKS cluster

Download the cluster's kubeconfig and get the exact context name:

```bash
kubectl --kubeconfig "$HOME/Downloads/production-kubeconfig.yaml" config get-contexts
cp scripts/bootstrap/examples/doks.json .local-deploy/bootstrap-doks.json
chmod 600 .local-deploy/bootstrap-doks.json
```

Edit `kubeconfig`, `context`, cluster `name`, tenant `domain`, ACME `email`, region and `output`. The script verifies that the selected nodes identify DigitalOcean as their provider before installing charts. `do-block-storage` must already exist and be the only default storage class, so tenant PVCs use the intended provisioner. It installs the vendored Traefik chart with a DigitalOcean LoadBalancer service, then prints its public IP. Point `*.apps.example.com` to that IP. On a first installation you can set DNS while the script waits for its TLS readiness check; if the check times out, finish DNS and rerun.

```bash
./scripts/bootstrap/doks.sh plan --config .local-deploy/bootstrap-doks.json
./scripts/bootstrap/doks.sh apply --config .local-deploy/bootstrap-doks.json
```

For this workstation's existing cluster, set `kubeconfig` to `~/Downloads/node-kubeconfig.yaml` and `context` to `do-fra1-node`. Keep its current base domain and deliberate operator overrides. Bootstrap provisions cluster infrastructure; later operator/backup code changes use:

```bash
./scripts/deploy/local.sh deploy --cluster doks
```

For private platform images, set optional `dockerconfigjson` to a local Docker authentication JSON file. Bootstrap creates `odoo-system/platform-registry`, configures operator pull credentials and tenant pull-secret replication. Never commit this file. Optional `operator_values` supplies extra Helm values, including image overrides; it takes precedence over generated defaults, so retain the ingress provider/class and pull-secret settings. Subsequent `local.sh` builds and pushes new operator/backup tags before Helm updates, with `images.platforms: "auto"` selecting the actual node architectures.

## Cluster readiness and SaaS registration

Both cluster scripts require Ready nodes, successful Helm releases/operator rollout, a temporary PVC write/read plus in-cluster DNS lookup, and a real certificate for `bootstrap-check.<domain>` issued through Traefik's HTTP-01 routing. The readiness Job/PVC are deleted after the storage check. The `saas-bootstrap/bootstrap-tls` certificate/secret remain for renewal and reuse across retries. A failed check stops the command; it does not report the cluster as ready.

After those checks, the script exports a dedicated service-account kubeconfig with permissions matching the platform's existing cluster-admin integration. The output has mode `0600` and contains a long-lived credential: keep it private. The exported credential is tested against the API/CRD and does not depend on `doctl`, local admin certificates or exec auth plugins.

To register automatically, add this optional block to the cluster JSON **before** running `apply`:

```json
{
  "register_saas": {
    "ssh": {
      "host": "saas.example.com", "user": "root", "port": 22,
      "key": "~/.ssh/id_ed25519", "known_hosts": "~/.ssh/known_hosts"
    },
    "database": "saas",
    "registry": {
      "registry_host": "docker.io",
      "registry_prefix": "your-namespace",
      "registry_username": "your-user",
      "registry_password": "your-registry-token"
    }
  }
}
```

`registry` is optional; it supplies the SaaS image-build credentials, separate from Kubernetes pull credentials. This SSH account needs administrative sudo on the SaaS VM; the restricted `saas-deploy` account only runs code deployments and cannot register clusters. The SaaS VM must use this repository's native `/opt/saas` layout.

Registration upserts the region and named Kubernetes cluster, uploads its kubeconfig, configures `letsencrypt-prod`, `monitoring/prometheus-server:80`, and links the base domain. It probes the cluster **from the SaaS server** and commits only if healthy. It refuses to move a base domain that already belongs to a different named cluster. API/firewall failure rolls back the registration transaction.

Without `register_saas`, import the generated kubeconfig manually under **SaaS > Kubernetes Clusters**, choose the region, set issuer `letsencrypt-prod` and monitoring service `prometheus-server:80` in namespace `monitoring`, then link the base domain. Tenant PVCs use the cluster's default storage class. The script clearly reports that this step remains.

Infrastructure checks do not simulate a paid order. Finish the SaaS catalog, tenant image/registry settings and object-storage configuration, then run a real tenant provision, GitHub webhook delivery and backup/restore test using [the setup guides](03b-DOKS-CLUSTER-SETUP.md). Bootstrap does not change the GitHub webhook verification implementation.

Implementation references: [Docker cross-compilation](https://docs.docker.com/build/building/multi-platform/), [MicroK8s commands](https://canonical.com/microk8s/docs/command-reference), [MicroK8s clustering](https://canonical.com/microk8s/docs/clustering), [Longhorn Helm installation](https://longhorn.io/docs/1.12.1/deploy/install/install-with-helm/).
