# CI/CD: build, publish and deploy

The repository includes `.github/workflows/ci.yml` and `.github/workflows/cd.yml`.

**Temporary fast deployment mode:** pushes to `main` deploy directly through CD. Automatic tests, typechecks, lint and security scans are disabled. CI remains available through **Actions → CI → Run workflow**; it does not gate deployment. Builds, deployment readiness checks, backups and HTTP health checks remain enabled. Existing CI runs can be stopped with **Cancel workflow** on their Actions page.

For step-by-step setup through the GitHub UI, including the DigitalOcean cluster and `main.eagle-tech.info` server commands, start with [07-CICD-UI-WALKTHROUGH.md](07-CICD-UI-WALKTHROUGH.md).
Complete the server setup in [02](02-SAAS-SERVER-SETUP.md) and cluster setup in [03a](03a-MICROK8S-CLUSTER-SETUP.md) or [03b](03b-DOKS-CLUSTER-SETUP.md) first. CI needs no deployment secrets. CD needs the configuration below; adding workflow files alone does not connect production.

## 1. How delivery works

1. A push to **main** starts CD immediately. Manual CD also deploys directly. Neither waits for CI. PRs do not run automatic tests in this temporary mode.
2. CD compares the target commit with the last fully delivered commit. A successful `delivered-<SHA>` artifact records that baseline, including compatibility with earlier CI-triggered deliveries. Failed/partial deployments do not advance it, so a later push retries outstanding changes. The first delivery, missing/expired baseline, or non-ancestor history rebuilds everything. A superseded queued commit skips delivery.
3. The selected frontend release and container images build **in parallel**. The frontend uses Vite directly, without tests or TypeScript checking, and is packaged with the control-plane addons in a `saas-release` artifact in the same CD run.
4. Selected images are published for **linux/amd64 and linux/arm64**, with BuildKit caching, SBOM and provenance. No vulnerability scan runs in CD during this temporary mode. Tags are unique: `sha-<40-character SHA>-<run ID>-<attempt>`; images are never published as `latest`.
5. Each configured Kubernetes cluster gets CRDs explicitly applied and a Helm upgrade with readiness checks and automatic Helm rollback on failure. Backup-only changes update the backup and restore image flags without rebuilding the operator.
6. The SaaS deploy waits for its package and required cluster deployments, checks that the installed root-owned deployment script matches the release source, uploads the built artifact, creates an isolated Python environment, drains/stops both services, backs up the database/filestore/config, upgrades all four addons, switches code/environment symlinks, restarts both services and checks HTTP health.
7. Only complete success records the delivery baseline. The production concurrency lock never cancels a running deployment. GitHub may replace pending runs; cumulative comparison carries their changes forward.

| Changed paths | Published/deployed |
|---|---|
| `compute/operator/**`, `compute/charts/**`, `compute/examples/**` | Operator image and operator chart on all configured clusters |
| `compute/tools/backup-tool/**` | Backup image; operator Helm settings for backup and restore |
| `control-plane/**`, `frontend/**`, `scripts/generate-customer-docs.py` | SaaS addons + freshly built SPA as one release |
| `.github/workflows/**`, `scripts/ci/**`, `scripts/deploy/**` | All owned components |
| Documentation only | No build or production changes |

The control plane runs natively with systemd; it does not need a new Docker image. Official Odoo/PostgreSQL, BuildKit and vendor images are upstream images and are not rebuilt by this repository. Customer tenant images continue to build from their connected Git repositories through the runtime pipeline in [04](04-IMAGES-AND-REGISTRY.md#3-tenant-image-pipeline-customer-git-repos). A platform push does not rebuild every customer's image or change their pinned image. Vendor infrastructure releases (Traefik, storage, monitoring, cert-manager) stay under the cluster setup guides; CD deploys the operator chart when its files change.

## 2. GitHub variables and environments

Open **Settings → Secrets and variables → Actions**. Add these as **repository variables**, so every cluster job sees the same registry:

| Variable | Example / purpose |
|---|---|
| `IMAGE_REGISTRY` | `docker.io` (default); alternatively `ghcr.io` or your registry hostname |
| `IMAGE_NAMESPACE` | `moutazmuhammad` or your registry organization/path; required |
| `DEPLOY_CLUSTERS` | JSON array of GitHub environment names: `["production-doks", "production-microk8s"]`; required and nonempty |

**Odoo validation:** disabled automatically along with the other tests. Run CI manually when you want the full validation suite, including all four production addons. The previous `RUN_ODOO_TESTS` opt-in is unused. Manual failed runs retain logs and coverage in the `odoo-test-diagnostics` artifact.

Create these environments in **Settings → Environments**. Restrict deployment branches to `main`. Required reviewers are optional; leave them unset for fully automatic deployment.

### `production-images` environment secrets

| Secret | Value |
|---|---|
| `REGISTRY_USERNAME` | Registry account with push access to the two image repositories |
| `REGISTRY_TOKEN` | Access token with push access; use a scoped token |

Create repositories `<namespace>/odoo-saas-operator` and `<namespace>/odoo-saas-backup-tool` at your registry. For private images, set up cluster pull secrets following [04 section 4](04-IMAGES-AND-REGISTRY.md). Registry push credentials are not automatically installed as pull credentials.

### Each cluster environment listed in `DEPLOY_CLUSTERS`

| Secret | Value |
|---|---|
| `KUBECONFIG_B64` | Base64-encoded kubeconfig, self-contained, with reachable API endpoint and sufficient deployment access |
| `OPERATOR_VALUES_YAML` | Complete cluster-specific operator Helm values YAML, preserving the configuration from your cluster setup |

Optional environment variables: `OPERATOR_NAMESPACE` (default `odoo-system`), `OPERATOR_RELEASE` (default `odoo-operator`). Keep these equal to the existing release.

Generate the kubeconfig value on an administrator machine:

```bash
kubectl config view --raw --flatten --minify | base64 -w0
```

Store its output directly as a GitHub secret. Do not commit it. For MicroK8s, use its exported kubeconfig with a remotely reachable endpoint; hosted runners cannot reach private cluster IPs without a configured network route. Use a runner with appropriate connectivity if production is private.

The deployment identity needs access to apply cluster-scoped CRDs, ClusterRoles/ClusterRoleBindings, and manage the operator release's namespace resources and Helm Secrets. Use an administrator-provisioned deployment identity appropriate to these operations. Kubeconfigs with local file references or interactive cloud login plugins need to be flattened/replaced by working noninteractive credentials.

Example `OPERATOR_VALUES_YAML` for an existing ingress-mode cluster (use your real settings):

```yaml
networking:
  provider: ingress
  ingressClassName: traefik
imagePullSecrets:
  - name: platform-registry
platformPullSecret: platform-registry
supportedOdooVersions: ["17.0", "18.0", "19.0", "20.0"]
```

For public images omit the pull-secret fields. For Gateway API keep your existing gateway settings instead. The pipeline loads chart defaults, reuses saved release values, then applies this YAML and newly built image overrides. Avoid fixing `image.tag`, `images.backupTool`, `backup.toolImage`, or `restore.toolImage` in environment YAML: those would overwrite a previously deployed tag when only another component changes. Existing per-instance backup/restore image overrides continue to take precedence; remove or update them if they must follow the platform default.

### `production-saas` environment secrets

| Secret | Value |
|---|---|
| `SAAS_SSH_HOST` | SaaS server DNS name or IPv4 address |
| `SAAS_SSH_USER` | `saas-deploy`, created below |
| `SAAS_SSH_PRIVATE_KEY` | Dedicated SSH private key, including the BEGIN/END lines |
| `SAAS_SSH_KNOWN_HOSTS` | Verified server host-key line(s) in OpenSSH known_hosts format |

Optional environment variable: `SAAS_SSH_PORT`, default `22`.

Generate a dedicated key on your administrator machine with `ssh-keygen -t ed25519 -f saas-ci -C github-saas-deploy`. Put `saas-ci` in `SAAS_SSH_PRIVATE_KEY` and install `saas-ci.pub` on the server below. Get the host key from the server console or compare its fingerprint over a trusted channel before saving the known_hosts entry. For a custom SSH port the entry uses `[host]:port`. The workflow enforces host-key verification.

Database passwords, the Fernet key and storage/payment credentials remain in the existing server configuration/database. They do not belong in GitHub for this workflow.

## 3. Prepare the SaaS server once

These commands assume the exact layout and service names from [02](02-SAAS-SERVER-SETUP.md): `/opt/saas`, user `odoo`, Python 3.12, PostgreSQL, `saas-odoo` and `saas-jobs`. Adapt and review the root-owned deployment script first if your layout differs.

On the server, from a trusted checkout of this repository:

```bash
sudo apt-get install -y curl util-linux
sudo useradd --create-home --shell /bin/bash saas-deploy
sudo usermod -aG saas-deploy odoo
sudo install -d -o saas-deploy -g saas-deploy -m 2750 /var/lib/saas-deploy/incoming
sudo chown root:root /opt/saas
sudo chmod 755 /opt/saas
sudo install -d -o root -g root -m 755 /opt/saas/releases
sudo install -d -o root -g root -m 700 /var/backups/saas
sudo install -o root -g root -m 755 scripts/deploy/saas-deploy.sh /usr/local/sbin/saas-deploy
sudo install -d -o saas-deploy -g saas-deploy -m 700 /home/saas-deploy/.ssh
# Replace /tmp/saas-ci.pub with the PUBLIC key generated above.
sudo install -o saas-deploy -g saas-deploy -m 600 /tmp/saas-ci.pub /home/saas-deploy/.ssh/authorized_keys
```

Create `/etc/saas-deploy.conf`, owned by root and mode `600`:

```bash
SAAS_DB=saas
SAAS_HEALTH_URL=https://saas.example.com/web/health
```

Use the actual public health URL; it must return a successful HTTP response. The deploy script backs up local PostgreSQL through `postgres` peer authentication, matching guide 02. Remote PostgreSQL requires adapting its backup/restore commands and credentials before enabling CD.

Create `/etc/sudoers.d/saas-deploy` using `sudo visudo -f /etc/sudoers.d/saas-deploy`:

```sudoers
saas-deploy ALL=(root) NOPASSWD: /usr/local/sbin/saas-deploy *
```

The script validates its single release-ID argument; the SSH user cannot choose a command or arbitrary archive path. Keep the script and configuration writable only by root. **When `scripts/deploy/saas-deploy.sh` changes, reinstall the reviewed script on the server before running the new delivery.** The workflow deliberately invokes this installed script, rather than granting sudo execution of an uploaded script. CD compares its SHA-256 checksum with the tested script and fails before maintenance if they differ. Reinstall the script whenever it changes; this review fixes creation of the new virtualenv directory so it is writable by `odoo`.

Verify services already work and that `/opt/saas/data/filestore/saas` exists. Make sure `/opt/saas`, `/opt/saas/releases`, and the symlink locations are root-owned; `odoo` owns only release contents/data/logs. Allow the runner to reach SSH and the server to reach Python package repositories. Each release creates a fresh virtualenv using the already installed `/opt/saas/odoo18` source, which the pipeline does not update.

The first successful release moves existing real `platform`/`venv` directories to `.pre-cicd-<release ID>` siblings and replaces them with symlinks. Subsequent releases switch those symlinks. Existing systemd units and `/etc/odoo/saas.conf` keep using the stable paths in guide 02.

## 4. First run and routine use

1. Add repository variables, environments and secrets; prepare the server and cluster pull credentials.
2. Merge/push these files to `main`. CD starts immediately; the first delivery selects every owned component.
3. Inspect Actions → CD: plan → frontend packaging and image publishing in parallel → cluster deployment → SaaS deployment → receipt. CI runs only when explicitly requested manually.
4. Check the portal, both systemd services, operator rollout and a tenant backup/restore smoke test.
5. Future pushes deploy changed components. To rebuild everything, choose **Actions → CD → Run workflow → main**, keeping `full=true`. Set `full=false` to use the cumulative diff. Manual CD runs on other branches skip delivery.

The server-side SaaS release ID is `<run ID>-<attempt>-<SHA>`. Re-running a failed workflow creates a new attempt and release directory. Do not retry a failed database upgrade blindly: inspect the error and maintenance state first.

Changing the production branch requires updating the CD push branch and event guards, concurrency group, branch lookup/baseline query, and environment branch restrictions together. Adding a new image build context requires adding its change selection and image matrix entry in `scripts/ci/changes.py` / `cd.yml`.

## 5. Failures, backups and rollback

**Operator:** Helm uses `--atomic --wait --timeout 10m`. Failed upgrades roll back the release, but CRD changes are separate and are not automatically rolled back. CRDs are applied without forcing ownership conflicts; resolve a conflict deliberately if an older manual apply owns a field. A backup-tool release changes defaults for scheduled backups and future restore jobs; already running Jobs finish with their existing images.

```bash
helm history odoo-operator -n odoo-system
helm rollback odoo-operator <revision> -n odoo-system --wait
kubectl -n odoo-system rollout status deployment/odoo-operator
```

**SaaS:** every upgrade saves `/var/backups/saas/<release ID>/{database.dump,filestore.tar.gz,saas.conf,previous-platform,previous-venv}`. An error after maintenance begins may leave services stopped. Inspect `/opt/saas/log/odoo.log` and the Actions error. Code rollback alone cannot undo schema/data migrations. To restore the previous release, stop both services, restore the database and filestore backup together, keep the encryption key, restore the paths recorded in `previous-platform` and `previous-venv`, then start both services and verify health. For the first migration these paths refer to the directories moved to `.pre-cicd-<release ID>`; use those moved directories.

Backups here are local pre-upgrade recovery points. Arrange scheduled off-server copies of these backups and the configuration as described in guide 02. Retain older release directories/virtualenvs and images until rollback is no longer needed; clean them and local backups according to your retention policy. No automatic cleanup deletes recovery data.

## 6. Optional local verification and builds

These commands are for manually requested verification; they are not executed by direct CD. To restore automatic validation, restore CI push/PR triggers and make CD depend on successful CI again.

```bash
python3 -m unittest discover -s scripts/ci -p 'test_*.py'
python3 compute/tools/backup-tool/test_backup_restore.py
bash -n scripts/deploy/saas-deploy.sh scripts/deploy/operator.sh
shellcheck scripts/deploy/*.sh
actionlint
helm lint compute/charts/odoo-operator
helm lint compute/charts/odoo-instance --set-string image.tag=ci-test --set-string domain.hostname=ci.example.com
```

Frontend: Node 22, `cd frontend/veltnex && npm ci && npm run test:coverage && npm run build`. Output goes directly into `control-plane/saas_website/static/spa`. Operator: `cd compute/operator && make test`, then `make docker-build IMG=<registry>/<namespace>/odoo-saas-operator:<unique-tag>`. Backup tool: `docker build -t <registry>/<namespace>/odoo-saas-backup-tool:<unique-tag> compute/tools/backup-tool`. See [05](05-DEVELOPMENT.md) for the full local Odoo environment.

GitHub reference: [reusing workflows](https://docs.github.com/en/actions/how-tos/sharing-automations/reuse-workflows), [deployment environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments), [workflow artifacts](https://docs.github.com/en/actions/tutorials/store-and-share-data).

## 7. Example: obtain and upload secrets

Run these examples on your administrator machine from the repository checkout, with GitHub CLI installed. Replace example domains/usernames. Create the environments from section 2 first. `gh secret set` accepts file content through stdin or prompts for a value, so credentials need not appear in command history. See [GitHub CLI secret set](https://cli.github.com/manual/gh_secret_set).

```bash
gh auth login
# Public configuration: these are repository variables, not secrets.
gh variable set IMAGE_REGISTRY --body docker.io
gh variable set IMAGE_NAMESPACE --body YOUR_DOCKERHUB_USERNAME
gh variable set DEPLOY_CLUSTERS --body '["production-doks"]'
```

**Docker Hub:** `REGISTRY_USERNAME` is your Docker ID. Create a personal access token in your Docker account's access-token settings with repository read/write permissions, then paste it into the interactive prompt below. See [Docker access tokens](https://docs.docker.com/security/access-tokens/).

```bash
gh secret set REGISTRY_USERNAME --env production-images --body YOUR_DOCKERHUB_USERNAME
gh secret set REGISTRY_TOKEN --env production-images
```

**Kubernetes:** select the production context on your administrator machine and upload its portable kubeconfig. Check the context before running this. Its API endpoint must be reachable by the Actions runner and its credentials must work without interactive login. The [kubectl reference](https://kubernetes.io/docs/reference/generated/kubectl/kubectl-commands) describes `--raw`, `--flatten`, and `--minify`.

```bash
kubectl config current-context
kubectl config view --raw --flatten --minify | base64 -w0 | \
  gh secret set KUBECONFIG_B64 --env production-doks
```

For MicroK8s, use `sudo microk8s config` on the cluster server to obtain the kubeconfig, then flatten/encode it after verifying its server endpoint. Use a separate environment for each cluster.

`OPERATOR_VALUES_YAML` comes from your cluster's operator configuration. Use your reviewed deployment values file, with the pipeline-managed image tag/backup/restore settings removed as described in section 2:

```bash
gh secret set OPERATOR_VALUES_YAML --env production-doks < /path/to/operator-values.yaml
```

To inspect the values currently saved by Helm, use `helm get values odoo-operator -n odoo-system -o yaml`; review them before uploading, particularly any image overrides. Do not use `--all`, which also includes chart defaults.

**SaaS SSH:** the host is the server's DNS name or IPv4 address; the user is `saas-deploy` after completing section 3. Generate a dedicated key on the administrator machine. This workflow requires a key without a passphrase because it connects noninteractively.

```bash
umask 077
ssh-keygen -t ed25519 -N '' -f /tmp/saas-ci -C github-saas-deploy
gh secret set SAAS_SSH_HOST --env production-saas --body saas.example.com
gh secret set SAAS_SSH_USER --env production-saas --body saas-deploy
gh secret set SAAS_SSH_PRIVATE_KEY --env production-saas < /tmp/saas-ci
# Transfer the PUBLIC key using your existing administrator SSH account.
scp /tmp/saas-ci.pub admin@saas.example.com:/tmp/saas-ci.pub
```

On the server, install that public key as shown in section 3. The private key stays on the administrator machine/GitHub; do not copy it onto the server.

For `SAAS_SSH_KNOWN_HOSTS`, obtain the server's public host key through its trusted console. For default port 22, run this **on the server**, substituting the exact hostname saved in `SAAS_SSH_HOST`:

```bash
awk '{print "saas.example.com " $1 " " $2}' /etc/ssh/ssh_host_ed25519_key.pub
```

Copy the resulting single line into the interactive GitHub prompt **on your administrator machine**:

```bash
gh secret set SAAS_SSH_KNOWN_HOSTS --env production-saas
```

For a custom port, use `[saas.example.com]:2222` as the hostname in that line and set the environment variable with `gh variable set SAAS_SSH_PORT --env production-saas --body 2222`. Uploading the private key alone is not enough: server preparation, the public authorized key and the trusted host-key entry are all required.

List configured names without revealing secret values:

```bash
gh secret list --env production-images
gh secret list --env production-doks
gh secret list --env production-saas
```
