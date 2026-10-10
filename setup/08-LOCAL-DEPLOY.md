# Deploy directly from this local machine

`scripts/deploy/local.sh` builds the current local files and deploys directly over SSH or through the Kubernetes API. GitHub Actions, a commit and a push are not required. The existing server deployment helper provides the database/filestore backup, addon upgrade, service restart and HTTP health check. Cluster deployments reuse the existing operator helper for CRDs and Helm readiness checks.

For a fresh Ubuntu SaaS VM, one/multiple MicroK8s VMs or an existing DOKS cluster that still needs tenant infrastructure, start with [the three bootstrap scripts](09-BOOTSTRAP.md).

## Deploy to DOKS from this machine

Use this procedure for the existing DigitalOcean cluster. It updates the platform operator and backup/restore tooling. Initial cluster creation, ingress, storage and TLS are covered in [03b-DOKS-CLUSTER-SETUP.md](03b-DOKS-CLUSTER-SETUP.md).

### 1. Open the checkout and review the destination

```bash
cd ~/Documents/odoo-saas-platform
```

The local config already exists at `.local-deploy/config.json`. Its DOKS entry must identify:

| Setting | This machine |
| --- | --- |
| Cluster alias used by the script | `doks` |
| Kubeconfig | `~/Downloads/node-kubeconfig.yaml` |
| Kubernetes context | `do-fra1-node` |
| Operator Helm release | `odoo-operator` |
| Operator namespace | `odoo-system` |
| Local operator overrides | `.local-deploy/doks-values.yaml` |
| Image registry prefix | `docker.io/moutazmuhammad` — verify your namespace |

`doks` is the script's local alias; `do-fra1-node` is the context inside the kubeconfig. They are different settings. Edit the JSON when either changes. An existing config is kept by `init`; do not rerun `init` to replace it.

Verify access using the same explicit kubeconfig and context the script uses:

```bash
kubectl --kubeconfig "$HOME/Downloads/node-kubeconfig.yaml" \
  --context do-fra1-node get nodes

helm --kubeconfig "$HOME/Downloads/node-kubeconfig.yaml" \
  --kube-context do-fra1-node list -n odoo-system
```

The release/namespace in the JSON should match the existing installation. The generated `doks-values.yaml` contains `{}`, preserving the installed Helm values. Put deliberate overrides there, including any private registry pull-secret settings; do not replace the installed configuration with an unrelated example.

On a fresh checkout only, create the local config with:

```bash
./scripts/deploy/local.sh init --cluster doks \
  --kubeconfig ~/Downloads/node-kubeconfig.yaml --context do-fra1-node
```

### 2. Prepare Docker and registry access

```bash
docker info
docker buildx version
docker login docker.io
```

Log in as an account allowed to push to the namespace in `images.prefix`. The script publishes these repositories when their respective inputs change:

```text
docker.io/moutazmuhammad/odoo-saas-operator:local-<release-id>
docker.io/moutazmuhammad/odoo-saas-backup-tool:local-<release-id>
```

`images.platforms` defaults to `auto`: the script reads the selected clusters' node architectures and builds only those platforms. An amd64-only DOKS cluster therefore builds `linux/amd64`. You can explicitly set `linux/amd64,linux/arm64` for images shared across both architectures. Check the nodes and your builder with:

```bash
kubectl --kubeconfig "$HOME/Downloads/node-kubeconfig.yaml" \
  --context do-fra1-node get nodes -L kubernetes.io/arch
docker buildx inspect
```

If an old config still contains both platforms, set `images.platforms` to `auto`. An `exec /bin/sh: exec format error` during an ARM64 `RUN` step means the builder cannot execute that architecture. The operator uses Go cross-compilation and runs its compiler on the builder's native CPU; the backup image still needs a native builder or emulation for each requested platform. See [Docker multi-platform builds](https://docs.docker.com/build/building/multi-platform/).

After updating the script/Dockerfile, retry the failed deployment normally:

```bash
./scripts/deploy/local.sh deploy --cluster doks
```

Failed builds do not advance deployment receipts, so the retry rebuilds and pushes the required images before updating Helm.

If all target nodes are `amd64` and you use a single-architecture builder, set `images.platforms` to `linux/amd64`. For a mixed cluster, keep both architectures and use a builder that supports both. DOKS must be able to pull the published images; for private repositories, configure the operator/platform pull secrets as described in [04-IMAGES-AND-REGISTRY.md](04-IMAGES-AND-REGISTRY.md).

### 3. Preview, then deploy only DOKS

```bash
# Local preview: no build, image push or Kubernetes API calls.
./scripts/deploy/local.sh plan --cluster doks

# Build/push required images and update this cluster.
./scripts/deploy/local.sh deploy --cluster doks
```

The preview should identify `doks (context: do-fra1-node)`. `--cluster doks` selects the cluster only, so this command does not deploy the SaaS server. Running `./scripts/deploy/local.sh` with no arguments only displays the plan; include `deploy` to execute it.

The first successful local deployment has no local baseline, so it builds/pushes both platform images and applies the operator chart. Later runs select changes from the last successful local receipts, including uncommitted edits and deletions. The local script currently groups operator source, operator chart and operator values together: changing any of those rebuilds the operator image. Backup-only changes rebuild just the backup image. The GitHub CD planner separately supports chart-only updates without image builds.

The execution order is:

1. Verify the explicit cluster context, access, chart values and Docker availability.
2. Build and push every selected image with a unique `local-...` tag.
3. Apply operator CRDs, then update Helm with the selected image tags.
4. Wait for Helm and the operator deployment rollout to succeed.
5. Save local success receipts under `.local-deploy/state/`.

A build or push failure stops before any chart update. A failed deployment keeps its previous receipt; after fixing the reported issue, rerun the same command. No commit, push or GitHub Actions run is needed.

To force both images and the operator release to redeploy on **DOKS only**:

```bash
./scripts/deploy/local.sh deploy --cluster doks --full
```

### 4. Verify the deployed operator

```bash
kubectl --kubeconfig "$HOME/Downloads/node-kubeconfig.yaml" \
  --context do-fra1-node -n odoo-system rollout status \
  deployment/odoo-operator --timeout=300s

kubectl --kubeconfig "$HOME/Downloads/node-kubeconfig.yaml" \
  --context do-fra1-node -n odoo-system get deployment odoo-operator \
  -o jsonpath='{.spec.template.spec.containers[*].image}{"\n"}'

helm --kubeconfig "$HOME/Downloads/node-kubeconfig.yaml" \
  --kube-context do-fra1-node get values odoo-operator -n odoo-system
```

The operator image should match the new tag printed during the build when the operator was selected. Helm values record the configured backup/restore image tag; verify a subsequent tenant backup/restore operation through the platform when that image changes.

### Troubleshooting DOKS deployment

| Error | Next step |
| --- | --- |
| `error: write /dev/stdout: bad file descriptor` during kubeconfig extraction | Use the updated `scripts/deploy/local.py`, which captures `kubectl` output through a pipe before writing the private temporary kubeconfig. Rerun the same deployment command. This failure happens before image builds or cluster updates. |
| Context missing or kubeconfig file missing | Update `clusters[].kubeconfig` and `clusters[].context` in `.local-deploy/config.json`; confirm the context with `kubectl --kubeconfig "$HOME/Downloads/node-kubeconfig.yaml" config get-contexts`. |
| `Unauthorized`, `Forbidden`, or cluster connection failure | Refresh the DOKS kubeconfig from DigitalOcean and verify API access with the explicit `get nodes` command above. Deployment credentials need permission to manage the operator release and its cluster-scoped CRDs/RBAC. |
| Registry push denied | Run `docker login` against the configured registry and check access to `images.prefix`. |
| Docker daemon unavailable or unsupported build platform | Check `docker info`, the selected Buildx builder and `images.platforms`. |
| `ImagePullBackOff` after publishing | Check the image name/tag and the cluster's private registry pull secrets. |
| Helm/rollout timeout | Inspect `kubectl --kubeconfig "$HOME/Downloads/node-kubeconfig.yaml" --context do-fra1-node -n odoo-system get pods` and the failing pod's events/logs, fix the issue, then rerun deployment. |
| A required update is skipped after a deployment from another machine or GitHub | Run `deploy --cluster doks --full` to replace that external deployment with the local version. Local and GitHub receipts are separate. |

## Deploy the SaaS server from this machine

### 1. Review configuration and SSH access

From the repository root:

```bash
cd ~/Documents/odoo-saas-platform
```

The `saas` object in `.local-deploy/config.json` should contain:

```json
{
  "enabled": true,
  "host": "main.eagle-tech.info",
  "user": "saas-deploy",
  "port": 22,
  "key": "~/saas-ci",
  "known_hosts": "~/.ssh/known_hosts"
}
```

This is a section of the existing config, not a replacement for the entire JSON file. Verify the hostname, SSH port, key and trusted host-key file for your server. The script uses noninteractive SSH with strict host-key checking.

Check SSH and the installed deployment permission without deploying:

```bash
ssh -i "$HOME/saas-ci" -o IdentitiesOnly=yes -o BatchMode=yes \
  -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$HOME/.ssh/known_hosts" \
  -p 22 saas-deploy@main.eagle-tech.info \
  'sudo -n -l /usr/local/sbin/saas-deploy'
```

The local machine needs Python 3, Git, Bash, Node/npm, SSH and scp. The server must already have the native Odoo installation, `saas-odoo` and `saas-jobs` services, root-owned `/usr/local/sbin/saas-deploy`, `/etc/saas-deploy.conf` and its sudo rule. Complete [06-CICD.md — server preparation](06-CICD.md#3-prepare-the-saas-server-once) before using this deployment path. The installed helper must match the local `scripts/deploy/saas-deploy.sh`; a checksum mismatch stops the script before maintenance.

### 2. Preview and deploy SaaS only

```bash
# Preview changed SaaS inputs without contacting the server.
./scripts/deploy/local.sh plan --target saas

# Build the frontend and deploy the SaaS release.
./scripts/deploy/local.sh deploy --target saas
```

`--target saas` excludes DOKS and other client clusters. It does not need Docker, Helm, kubectl or a registry login. It packages both the Odoo addons and freshly built frontend in one SaaS release, including local changes that have not been committed or pushed.

The first local deployment sends the complete SaaS release. Later runs skip deployment when its inputs match the last successful local receipt. To force a SaaS deployment:

```bash
./scripts/deploy/local.sh deploy --target saas --full
```

### 3. What runs during deployment

1. Check SSH access, sudo permission and the installed server helper's checksum.
2. Install frontend build dependencies and build the SPA in a temporary copy of the local source.
3. Upload the packaged control plane and SPA to the server.
4. Prepare a separate Python environment for the release.
5. Stop `saas-jobs` and `saas-odoo`, then back up the database, filestore and Odoo configuration under `/var/backups/saas/<release-id>/`.
6. Upgrade `saas_core`, `saas_billing`, `saas_website` and `saas_iam`, then switch the platform/environment symlinks to the new release.
7. Start both services and check the configured `SAAS_HEALTH_URL`.
8. Record the server's current release and the successful local receipt.

**SaaS is unavailable while the services are stopped for backup and addon upgrades.** A build or preflight failure happens before that maintenance period. A failure during backup, migration or service startup may leave services stopped; inspect the deployment output and server logs before retrying. Code symlink rollback alone does not undo a database migration.

### 4. Verify and troubleshoot

The script waits for the configured HTTP health check before reporting success. For this server, check the documented public endpoint and open the portal:

```bash
curl --fail --show-error https://main.eagle-tech.info/web/health
```

On the server, using an administrator session:

```bash
sudo systemctl status saas-odoo saas-jobs --no-pager
sudo journalctl -u saas-odoo -u saas-jobs -n 100 --no-pager
sudo cat /opt/saas/current-release
```

| Error | Next step |
| --- | --- |
| SSH key/known_hosts missing or SSH authentication fails | Correct the paths/user/port in the local config, verify the server host key and repeat the SSH check above. |
| Installed server helper differs | Have an administrator install the reviewed local helper as root-owned `/usr/local/sbin/saas-deploy`, as described in the server preparation guide, then retry. |
| `sudo` permission denied | Correct the deployment user's sudo rule on the server. |
| Frontend build fails | Fix the reported frontend/dependency issue; the server has not entered maintenance yet. |
| Backup or addon upgrade fails | Inspect the server services/logs and the printed backup/release directories. Recover using the appropriate database and filestore backup if needed before retrying. |
| Health check fails | Inspect both services, application logs, reverse proxy and `SAAS_HEALTH_URL`; a successful upload alone does not establish a healthy release. |
| SaaS shows `SKIP` after an external deployment | Use `deploy --target saas --full` when you intend to replace the external release with this local source. |

## Commands

The examples below run from the repository root. From another directory, use the full path to `scripts/deploy/local.sh`; the script locates the checkout itself:

```bash
# Already initialized in this workspace. For a new checkout, run once:
./scripts/deploy/local.sh init

# Show destinations and changed inputs, with no remote calls or builds:
./scripts/deploy/local.sh plan

# Deploy changes to the destinations that need them:
./scripts/deploy/local.sh deploy

# Only the SaaS server, including the current webhook fixes:
./scripts/deploy/local.sh deploy --target saas

# Only the named client cluster (--cluster implies --target cluster):
./scripts/deploy/local.sh deploy --cluster doks

# Force a full deployment of selected targets:
./scripts/deploy/local.sh deploy --target all --full

# Preview the exact target selection:
./scripts/deploy/local.sh deploy --target saas --dry-run
```

**بالعربي:** عدّل إعدادات الجهاز في `.local-deploy/config.json`، وشغّل `plan` لمراجعة الوجهات. بعدها `deploy` ينشر الأجزاء المتغيّرة تلقائيًا. `--target saas` للسيرفر فقط، و`--cluster doks` للـ cluster فقط. أول نشر محلي بيكون كامل للوجهات المختارة لأن مفيش سجل نجاح محلي لسه. السكربت بينشر الملفات المحلية، بما فيها التغييرات اللي لسه ما اتعملهاش commit.

## Machine configuration

`.local-deploy/` is ignored by Git. Keep its config, values and receipts on this machine. `init` refuses to overwrite an existing config. The generated config uses the server documented in this repository and the DOKS kubeconfig downloaded on this machine (`do-fra1-node`):

```json
{
  "saas": {
    "enabled": true,
    "host": "main.eagle-tech.info",
    "user": "saas-deploy",
    "port": 22,
    "key": "~/saas-ci",
    "known_hosts": "~/.ssh/known_hosts"
  },
  "images": {
    "prefix": "docker.io/moutazmuhammad",
    "platforms": "linux/amd64,linux/arm64"
  },
  "clusters": [
    {
      "name": "doks",
      "enabled": true,
      "context": "do-fra1-node",
      "kubeconfig": "~/Downloads/node-kubeconfig.yaml",
      "values": ".local-deploy/doks-values.yaml",
      "namespace": "odoo-system",
      "release": "odoo-operator",
      "releases": []
    }
  ]
}
```

For a different kubeconfig/context on a fresh checkout, initialize explicitly:

```bash
./scripts/deploy/local.sh init --cluster doks \
  --kubeconfig ~/Downloads/node-kubeconfig.yaml --context do-fra1-node
```

For this existing workspace, the config has already been updated to DOKS. The script selects that explicit context without changing `~/.kube/config` or its MicroK8s current context. An existing config is never overwritten by `init`; edit it to change destinations.

The registry prefix is a starting value from the setup guide; verify your actual registry namespace. Set a destination's `enabled` to `false` to exclude it. Add another cluster object with its own name, context, kubeconfig and values for each client cluster. `--cluster` can be repeated. A custom config is selected with `--config /path/config.json`; relative configured paths resolve against the repository root, and `~`/environment variables are expanded.

The generated operator values file contains `{}`. This preserves the installed release's values through the existing `--reset-then-reuse-values` deployment path. Put intentional cluster configuration changes in this file. For a new cluster, complete its networking, registry and pull-secret configuration using the cluster setup guide before deployment. Changing this values file triggers an operator/chart update.

Requirements:

- Python 3, Git and Bash on this Linux machine.
- SaaS: Node/npm, SSH/scp, the existing deploy key and a verified SSH host key. The server needs the setup from [06-CICD.md](06-CICD.md), including the root-owned `/usr/local/sbin/saas-deploy`, its sudo rule, services and backup paths. The helper's checksum must match `scripts/deploy/saas-deploy.sh`; a mismatch stops deployment before maintenance.
- Cluster: kubectl, Helm and reachable credentials for the explicitly configured context. The script creates a temporary, flattened kubeconfig for that context; it does not change your default context.
- Image changes: Docker with Buildx and a running daemon. Log in once with `docker login` for the configured registry. Its credentials remain in Docker's credential store. The Buildx builder must support the selected architectures; for a single-architecture cluster you may set `platforms` to `linux/amd64` or `linux/arm64`.

## Change routing and recovery

| Input | Deployment |
| --- | --- |
| `control-plane/**`, `frontend/**` | SaaS release, freshly built SPA, Odoo addon upgrade |
| `compute/operator/**`, `compute/charts/odoo-operator/**`, operator values | Operator image/chart and CRDs |
| `compute/tools/backup-tool/**` | Backup image, installed through the operator chart's backup/restore settings |
| `scripts/deploy/**` | All configured owned components |
| Extra configured Helm chart/values | That Helm release |

The script fingerprints the **actual content and modes**, rather than relying on a Git commit ID. It includes tracked files and nonignored untracked files, detects deletions, and freezes them into a temporary build directory. Ignored files are excluded. The old generated SPA is excluded and built afresh. Frontend build dependencies are installed in that temporary directory, leaving your working frontend untouched. Changing the registry/platform configuration rebuilds the platform images.

The first local deployment sends all selected configured components. Thereafter, successful receipts under `.local-deploy/state/` determine which inputs changed. GitHub deployment receipts are separate; a deployment through GitHub or another machine is not automatically recognized here. Use `--full` after an external deployment to restore the reviewed local version. Keep local and GitHub deployments from running simultaneously against the same cluster; the SaaS server helper already has its own deployment lock.

All selected destinations are preflighted, and all required images/SPA artifacts are built before applying remote workload updates. Images have unique `local-...` tags. Cluster updates precede SaaS, so a cluster failure blocks the server update. A successful component gets its own receipt only after deployment checks pass; a failed component keeps its old receipt and is selected on the next run. A local lock prevents overlapping runs using the same config directory. If deployment stops after a database migration begins, follow the installed server helper's logged backup/recovery instructions.

This publishes the platform operator and backup images. Customer application images continue through the tenant build pipeline; this script does not rebuild every customer's application. Public platform images require no pull credentials; private images need working pull secrets on each cluster, as in [04-IMAGES-AND-REGISTRY.md](04-IMAGES-AND-REGISTRY.md).

## Other cluster Helm releases

Vendor infrastructure is not implicitly installed. To deploy a chart such as Traefik, add its existing release name, namespace, chart and reviewed values to the cluster's `releases` list:

```json
{
  "name": "traefik",
  "namespace": "ingress",
  "chart": "compute/charts/vendor/traefik",
  "values": ["compute/examples/doks/traefik-values.yaml"]
}
```

The chart must live under `compute/charts/`. Changes in that chart or its values select that release. Extra releases use Helm lint, an explicit CRD apply when the chart has `crds/`, and `helm upgrade --install --atomic --wait`. Keep existing release names and namespaces; supply values appropriate to the target cluster. Files outside configured deployment inputs, such as setup docs or unconfigured vendor charts, do not trigger a rollout.

## Local verification

```bash
python3 -m unittest discover -s scripts/ci -p 'test_*.py'
bash -n scripts/deploy/local.sh scripts/deploy/operator.sh scripts/deploy/saas-deploy.sh
./scripts/deploy/local.sh plan
```

These tests mock remote commands. A successful local test or plan is not a successful live deployment; the script reports live success only after the remote deployment checks pass.
