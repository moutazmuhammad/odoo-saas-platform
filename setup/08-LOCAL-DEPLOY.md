# Deploy directly from this local machine

`scripts/deploy/local.sh` builds the current local files and deploys directly over SSH or through the Kubernetes API. GitHub Actions, a commit and a push are not required. The existing server deployment helper provides the database/filestore backup, addon upgrade, service restart and HTTP health check. Cluster deployments reuse the existing operator helper for CRDs and Helm readiness checks.

## Commands

Run from anywhere; the script locates this checkout itself:

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
./scripts/deploy/local.sh deploy --cluster microk8s

# Force a full deployment of selected targets:
./scripts/deploy/local.sh deploy --target all --full

# Preview the exact target selection:
./scripts/deploy/local.sh deploy --target saas --dry-run
```

**بالعربي:** عدّل إعدادات الجهاز في `.local-deploy/config.json`، وشغّل `plan` لمراجعة الوجهات. بعدها `deploy` ينشر الأجزاء المتغيّرة تلقائيًا. `--target saas` للسيرفر فقط، و`--cluster microk8s` للـ cluster فقط. أول نشر محلي بيكون كامل للوجهات المختارة لأن مفيش سجل نجاح محلي لسه. السكربت بينشر الملفات المحلية، بما فيها التغييرات اللي لسه ما اتعملهاش commit.

## Machine configuration

`.local-deploy/` is ignored by Git. Keep its config, values and receipts on this machine. `init` refuses to overwrite an existing config. The generated config uses the server documented in this repository and the locally available `microk8s` context:

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
      "name": "microk8s",
      "enabled": true,
      "context": "microk8s",
      "kubeconfig": "~/.kube/config",
      "values": ".local-deploy/microk8s-values.yaml",
      "namespace": "odoo-system",
      "release": "odoo-operator",
      "releases": []
    }
  ]
}
```

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
