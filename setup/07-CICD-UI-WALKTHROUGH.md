# CI/CD setup using the GitHub UI

Follow this guide one step at a time. Secrets are entered through the GitHub website; GitHub CLI is not needed. For build behavior, backups, and rollback, see [06-CICD.md](06-CICD.md).

This walkthrough uses the setup confirmed during configuration:

| Item | Value |
|---|---|
| Kubernetes provider | DigitalOcean |
| Existing operator release / namespace | `odoo-operator` / `odoo-system` |
| Cluster GitHub environment | `production-cluster` |
| SaaS website | `https://main.eagle-tech.info` |
| SaaS database | `saas` |
| SSH deployment user | `saas-deploy` |
| SSH port | `22` |
| Services | `saas-odoo`, `saas-jobs` |
| Server layout | `/opt/saas/{platform,odoo18,venv,data}` |

Replace these values when setting up another server. Commands marked **computer** run on your administrator computer. Commands marked **server** run on the SaaS server. The server commands below use `sudo`; when logged in as root, it is optional.

## 1. Understand where each setting goes

A GitHub Environment is a named collection of deployment secrets. Its name does not need to match your actual DigitalOcean cluster name.

Create these three environments under **Repository → Settings → Environments → New environment**:

| Environment | Secrets inside it |
|---|---|
| `production-images` | `REGISTRY_USERNAME`, `REGISTRY_TOKEN` |
| `production-cluster` | `KUBECONFIG_B64`, `OPERATOR_VALUES_YAML` |
| `production-saas` | `SAAS_SSH_HOST`, `SAAS_SSH_USER`, `SAAS_SSH_PRIVATE_KEY`, `SAAS_SSH_KNOWN_HOSTS` |

For each secret, open its environment, select **Add environment secret**, enter the exact name and value, then save. Keep deployment branches restricted to `main`. Required reviewers are optional if you want deployment to run automatically.

`DEPLOY_CLUSTERS` tells the workflow which cluster environments to select. With `["production-cluster"]`, the workflow selects that environment and reads its `KUBECONFIG_B64` and `OPERATOR_VALUES_YAML`. The kubeconfig determines the actual Kubernetes cluster.

## 2. Add Docker Hub secrets

Open the `production-images` environment and add:

| Name | Value |
|---|---|
| `REGISTRY_USERNAME` | Your Docker Hub username |
| `REGISTRY_TOKEN` | A Docker Hub personal access token with Read & Write access |

Create the token in your Docker account's personal access token settings. Create the `odoo-saas-operator` and `odoo-saas-backup-tool` repositories under your Docker Hub namespace. If they are private, configure Kubernetes image pull credentials as described in [04-IMAGES-AND-REGISTRY.md](04-IMAGES-AND-REGISTRY.md).

## 3. Add the DigitalOcean kubeconfig secret

In DigitalOcean, open **Kubernetes → your cluster** and download its kubeconfig.

**Computer:** replace the filename with the downloaded filename:

```bash
base64 -w0 ~/Downloads/your-cluster-kubeconfig.yaml
```

Copy the complete output into the `production-cluster` environment as `KUBECONFIG_B64`. Base64 does not encrypt the credentials; keep the output private. The cluster API must be reachable by the Actions runner, and the kubeconfig must work noninteractively.

## 4. Add the existing operator configuration

**Computer:** inspect the installed operator values with that kubeconfig:

```bash
helm --kubeconfig ~/Downloads/your-cluster-kubeconfig.yaml \
  get values odoo-operator -n odoo-system -o yaml
```

For this cluster, the confirmed values are:

```yaml
networking:
  gateway:
    namespace: ingress
    podSelector:
      app.kubernetes.io/name: traefik
  ingressClassName: traefik
  provider: ingress
```

Paste this YAML into `OPERATOR_VALUES_YAML` inside `production-cluster`.

For another cluster, use its actual values. Remove pipeline-managed `image.tag`, `images.backupTool`, `backup.toolImage`, and `restore.toolImage` overrides before uploading. Keep other configuration such as networking and image pull secret names.

## 5. Add repository variables

Open **Settings → Secrets and variables → Actions → Variables → New repository variable**. Add each separately:

| Name | Value |
|---|---|
| `IMAGE_REGISTRY` | `docker.io` |
| `IMAGE_NAMESPACE` | Your Docker Hub username or organization |
| `DEPLOY_CLUSTERS` | `["production-cluster"]` |

Automatic tests are temporarily disabled. Pushes to `main` deploy directly; no extra variable is needed. CI, including the Odoo suite, remains available through **Actions → CI → Run workflow**. The previous `RUN_ODOO_TESTS` variable is unused. Cancel any already-running CI manually on its Actions page.

These are repository variables, rather than secrets inside an environment. Additional clusters require additional environments, each containing its own two cluster secrets, and their names added to this JSON list.

Optional environment variables, only if you need different defaults:

| Environment | Variable | Default |
|---|---|---|
| `production-cluster` | `OPERATOR_NAMESPACE` | `odoo-system` |
| `production-cluster` | `OPERATOR_RELEASE` | `odoo-operator` |
| `production-saas` | `SAAS_SSH_PORT` | `22` |

## 6. Add SaaS host and user

Inside `production-saas`, add:

| Name | Value for this setup |
|---|---|
| `SAAS_SSH_HOST` | `main.eagle-tech.info` |
| `SAAS_SSH_USER` | `saas-deploy` |

Use the hostname alone for SSH, without `https://` or a URL path.

**Server:** if the deployment user does not already exist, create it:

```bash
sudo useradd --create-home --shell /bin/bash saas-deploy
```

Skip creation if `id saas-deploy` already succeeds.

## 7. Generate and save the SSH private key

**Computer:** generate a dedicated key without a passphrase for the noninteractive workflow. If `~/saas-ci` already exists, reuse the key you configured rather than overwriting it.

```bash
umask 077
ssh-keygen -t ed25519 -N '' -f ~/saas-ci -C github-saas-deploy
cat ~/saas-ci
```

Paste the entire private-key output, including its BEGIN and END lines, into `SAAS_SSH_PRIVATE_KEY` inside `production-saas`. Do not share the private key in chat or copy it to the server.

## 8. Install the public key on the server

**Computer:** display the public key:

```bash
cat ~/saas-ci.pub
```

Copy that single line. Connect to the server using your existing administrator account.

**Server:**

```bash
sudo install -d -o saas-deploy -g saas-deploy -m 700 /home/saas-deploy/.ssh
sudo nano /home/saas-deploy/.ssh/authorized_keys
```

Paste the public key on its own line, preserving any existing authorized keys. Save, then run:

```bash
sudo chown saas-deploy:saas-deploy /home/saas-deploy/.ssh/authorized_keys
sudo chmod 600 /home/saas-deploy/.ssh/authorized_keys
```

## 9. Add the SSH host key

**Server, through a trusted console/session:**

```bash
awk '{print "main.eagle-tech.info " $1 " " $2}' /etc/ssh/ssh_host_ed25519_key.pub
```

Copy that output into `SAAS_SSH_KNOWN_HOSTS` inside `production-saas`. The hostname in the line must exactly match `SAAS_SSH_HOST`.

For a custom SSH port, use `[main.eagle-tech.info]:PORT` in the host-key line and set `SAAS_SSH_PORT` in that environment.

## 10. Prepare deployment directories and install the script

**Server:** verify the existing layout and services:

```bash
ls -ld /opt/saas /opt/saas/platform /opt/saas/odoo18 /opt/saas/venv
systemctl is-active saas-odoo saas-jobs
id saas-deploy
```

Both services should report `active`. Prepare the directories and permissions:

```bash
sudo apt-get install -y curl util-linux
sudo usermod -aG saas-deploy odoo
sudo chown root:root /opt/saas
sudo chmod 755 /opt/saas
sudo install -d -o saas-deploy -g saas-deploy -m 2750 /var/lib/saas-deploy/incoming
sudo install -d -o root -g root -m 755 /opt/saas/releases
sudo install -d -o root -g root -m 700 /var/backups/saas
```

The live checkout did not contain the new deployment script during this setup. Copy the reviewed local script without pulling changes into the running application.

**Computer, from the repository root:** replace `ADMIN_USER` with your existing SSH administrator username (`root` was used for this setup):

```bash
scp scripts/deploy/saas-deploy.sh ADMIN_USER@main.eagle-tech.info:/tmp/saas-deploy.sh
```

**Server:**

```bash
sudo install -o root -g root -m 755 /tmp/saas-deploy.sh /usr/local/sbin/saas-deploy
```

Repeat this installation whenever the reviewed deployment script changes. The workflow calls this root-owned installed script and verifies that its SHA-256 checksum matches the release version before uploading the release. An outdated script blocks deployment before services are stopped.

## 11. Create server configuration and sudo permission

**Server:** confirm the database name without displaying database passwords:

```bash
awk '/^[[:space:]]*db_name[[:space:]]*=/' /etc/odoo/saas.conf
```

The confirmed output was `db_name = saas`. The filename ends in `.conf`, not `.con`.

Create the configuration for this server:

```bash
sudo tee /etc/saas-deploy.conf >/dev/null <<'EOF'
SAAS_DB=saas
SAAS_HEALTH_URL=https://main.eagle-tech.info/web/health
EOF
sudo chown root:root /etc/saas-deploy.conf
sudo chmod 600 /etc/saas-deploy.conf
```

Create the sudo rule with `sudo visudo -f /etc/sudoers.d/saas-deploy` and enter:

```sudoers
saas-deploy ALL=(root) NOPASSWD: /usr/local/sbin/saas-deploy *
```

Validate its permissions and syntax:

```bash
sudo chown root:root /etc/sudoers.d/saas-deploy
sudo chmod 440 /etc/sudoers.d/saas-deploy
sudo visudo -cf /etc/sudoers.d/saas-deploy
curl --fail --show-error https://main.eagle-tech.info/web/health
```

Confirmed results during setup:

```text
/etc/sudoers.d/saas-deploy: parsed OK
{"status": "pass"}
```

The deployment script assumes local PostgreSQL and `/opt/saas/data/filestore/saas`. Confirm this directory exists before the first deployment:

```bash
sudo ls -ld /opt/saas/data/filestore/saas
```

## 12. Verify SSH access and run the first deployment

**Computer:**

```bash
ssh -i ~/saas-ci -o IdentitiesOnly=yes \
  saas-deploy@main.eagle-tech.info \
  'sudo -n -l'
```

Verify the server fingerprint if prompted. The output should list permission to run `/usr/local/sbin/saas-deploy`. This checks SSH authentication and sudo permission without deploying.

Once this succeeds:

1. Ensure the CI/CD files are committed and pushed to `main`.
2. Open **GitHub → Actions → CD** and inspect the push-triggered run, or select **Run workflow → main**, keeping `full=true` for the first full delivery.
3. Watch Actions → CD: frontend packaging and image publishing run in parallel, followed by cluster deployment, SaaS deployment, and the final delivery receipt. Push and manual CD runs bypass tests, lint, typechecks and security scans in the current temporary mode.
4. Verify the portal, both SaaS services, operator rollout, and a tenant backup/restore smoke test.

CI has no automatic push/PR trigger in the current mode. Production deployments finish once started; queued superseded commits skip delivery. CI results do not gate CD.

Before the first release, arrange the backup retention and off-server copies described in [06-CICD.md](06-CICD.md). SaaS deployment stops both services during the backup and addon upgrade, then starts them again.

## Configuration status from this walkthrough

Based on the reported setup, GitHub secrets/variables were entered, the deployment script was installed, the sudo rule passed validation, and the public health endpoint passed. GitHub values have not been independently inspected. The SSH/sudo check was subsequently confirmed from the administrator computer: authentication as `saas-deploy` succeeded and `sudo -n -l` listed `(root) NOPASSWD: /usr/local/sbin/saas-deploy *`. A successful end-to-end Actions deployment still needs confirmation before production delivery can be considered verified.

### Verification attempt from the development workspace

Local checks passed: nine delivery tests, shell syntax checks, workflow YAML parsing, Helm lint for both charts, and `git diff --check`.

Remote verification could not complete from this workspace: `~/saas-ci` is not available here, the SSH command could not resolve the server hostname, and GitHub CLI is not installed. The browser tool also could not fetch the server health endpoint or repository Actions page. These access limitations do not establish a server failure.

At this check, the new CD workflow and setup documentation were still uncommitted locally. A successful production workflow run has not been verified. The administrator subsequently completed the SSH/sudo check from the computer holding the key successfully. Next, commit/push the reviewed CI/CD files and inspect the first Actions run as described in step 12.
