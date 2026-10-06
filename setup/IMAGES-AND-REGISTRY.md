# Images and registries

Which container images the platform runs, which ones we build (and how), and how to move to a **private registry**.

Every statement cites the code it comes from. Paths are from the repo root. Line numbers are from commit `8c10c89`.

---

## 1. Every image, at a glance

| Image | Built from | Who builds it | Pulled from (today) | Tag comes from | Runs in |
|---|---|---|---|---|---|
| **Operator** `docker.io/moutazmuhammad/odoo-saas-operator` | `compute/operator/Dockerfile` | You, by hand (`make docker-build docker-push`) | Docker Hub, public | `compute/charts/odoo-operator/values.yaml` (`image.tag`) | `odoo-system/odoo-operator` Deployment |
| **Backup tool** `docker.io/moutazmuhammad/odoo-saas-backup-tool` | `compute/tools/backup-tool/Dockerfile` | You, by hand (`docker build`) | Docker Hub, public | `DefaultBackupToolImage` / `DefaultRestoreToolImage` constants, or the chart's `backup.toolImage` / `restore.toolImage` | Backup CronJob + restore Job in each tenant namespace |
| **Odoo base** (per version) `docker.io/library/odoo:<ver>` | Third-party (official Odoo image) | Nobody, pulled | Docker Hub, public | `saas.odoo.version` fields `docker_image` + `docker_image_tag` | Tenant pod (`odoo`, `render-config`, cron, shell), init/update Jobs, build `inspect` step |
| **Tenant image** `<registry_host>/<registry_prefix>/tenant-<sub>:<ver>-b<build id>` | `control-plane/saas_core/templates/build/Dockerfile.jinja` | The control plane, at runtime, as a Job inside the cluster | The cluster's registry (cluster form, **Image Builds** tab) | `saas_instance_build.py:197` | Same pods as the Odoo base, for tenants with Git repos |
| PostgreSQL `docker.io/library/postgres:<16>` | Third-party | Pulled | Docker Hub | `spec.database.version` (default `"16"`); repository from the chart's `images.postgresRepository` | Tenant `postgresql` StatefulSet |
| CNPG Postgres `ghcr.io/cloudnative-pg/postgresql:<ver>` | Third-party | Pulled | GHCR | chart `images.cnpgPostgresRepository` | Only with `spec.database.mode: CloudNativePG` (the control plane never sets it) |
| BuildKit `moby/buildkit:v0.16.0-rootless` | Third-party | Pulled | Docker Hub | cluster field `builder_image` | Build Job `build` container (`odoo-builds` ns) |
| Git `alpine/git:v2.45.2` | Third-party | Pulled | Docker Hub | cluster field `git_image` | Build Job `fetch` init container |
| Toolbox `alpine/k8s:1.35.6` | Third-party | Pulled | Docker Hub | cluster field `toolbox_image` | Staff cluster terminal pod (`saas-toolbox` ns) |
| Traefik, cert-manager, Prometheus (+ config-reloader), Longhorn | Third-party, charts vendored in `compute/charts/vendor/` | Pulled | docker.io / quay.io | each chart's `values.yaml` / `appVersion` | `ingress`, `cert-manager`, `monitoring`, `longhorn-system` |
| Test registry `registry:2.8.3` | Third-party | Pulled | Docker Hub | `compute/examples/test-registry/registry.yaml:46` | Test clusters only |

Sources: `compute/charts/odoo-operator/values.yaml:3-7`, `compute/operator/internal/resources/backup.go:25`, `restore.go:23`, `control-plane/saas_core/data/saas_odoo_version_data.xml:5-6`, `control-plane/saas_core/models/saas_instance_build.py:140-161`, `compute/operator/internal/resources/database.go:87`, `cnpg.go:55`, `control-plane/saas_core/models/saas_server.py:108-115`, `control-plane/saas_core/drivers/kubernetes_driver.py:250`.

**We build exactly three images:** the operator, the backup tool (both by hand) and the tenant images (automatically, per Git push). The Odoo image itself is the official one, unchanged.

### Removed leftovers

The old Docker-host files (`control-plane/saas_core/docker/`: `Dockerfile.odoo-light`, `Dockerfile.odoo-base` and their build/provision scripts; the `docker-compose.yml`, `Dockerfile.tenant`, `odoo.conf` and nginx templates) were deleted. Nothing on the Kubernetes path used them. Only `control-plane/saas_core/templates/build/` remains: the tenant build pipeline below.

---

## 2. Building our images by hand

Run from the repo root. Bump the tag every time: tags are never overwritten in production (`IfNotPresent` pull policy everywhere, `backup.go:129`, `restore.go:164`).

### 2.1 Operator

Multi-stage: `golang:1.27-bookworm` builds a static binary, runtime is `gcr.io/distroless/static:nonroot` (`compute/operator/Dockerfile`).

```bash
cd compute/operator
make docker-build docker-push IMG=docker.io/moutazmuhammad/odoo-saas-operator:0.1.29
```

`docker-build` / `docker-push` are plain `docker build -t $IMG .` / `docker push $IMG` (`Makefile:64-70`). Log in first (`docker login`).

Then change the tag in **all** of these:

| File | What |
|---|---|
| `compute/charts/odoo-operator/values.yaml:5` | `image.tag` (this is what installs use) |
| `compute/charts/odoo-operator/Chart.yaml` | `appVersion` (and bump `version` for a chart change) |
| `compute/operator/Makefile:2` | `IMG ?=` default |
| `compute/operator/config/manager/manager.yaml:43` | kustomize manifest (not used by the setup guides) |
| `setup/*.md` | the version mentioned in the guides |

Roll out (same as the setup guides):

```bash
kubectl apply --server-side --force-conflicts -f compute/charts/odoo-operator/crds/
helm upgrade odoo-operator compute/charts/odoo-operator -n odoo-system \
  -f compute/examples/doks/operator-values.yaml --wait
```

`make deploy IMG=...` (from `compute/operator`) runs the same `helm upgrade --install` with that image; apply the CRDs first.

### 2.2 Backup tool

`postgres:16-bookworm` + `postgresql-client-16`, `rclone`, `python3`, `run-backup.sh`, `run-restore.sh`, `lib-objectstorage.sh`; runs as uid 100:101 (`compute/tools/backup-tool/Dockerfile`). One image serves both the backup and restore Jobs.

```bash
docker build -t docker.io/moutazmuhammad/odoo-saas-backup-tool:0.1.6 compute/tools/backup-tool
docker push docker.io/moutazmuhammad/odoo-saas-backup-tool:0.1.6
```

Then either:

- **compiled-in default** (needs a new operator image): change `DefaultBackupToolImage` (`compute/operator/internal/resources/backup.go:25`) **and** `DefaultRestoreToolImage` (`restore.go:23`), rebuild the operator (2.1); or
- **no operator rebuild**: set the chart values, which become `--backup-tool-image` / `--restore-tool-image` flags (`compute/charts/odoo-operator/templates/deployment.yaml:54-58`, `compute/operator/cmd/main.go:91-92`):

```bash
helm upgrade odoo-operator compute/charts/odoo-operator -n odoo-system \
  -f compute/examples/doks/operator-values.yaml \
  --set backup.toolImage=docker.io/moutazmuhammad/odoo-saas-backup-tool:0.1.6 \
  --set restore.toolImage=docker.io/moutazmuhammad/odoo-saas-backup-tool:0.1.6 --wait
```

Also update `setup/MICROK8S-CLUSTER-SETUP.md:126`.

### 2.3 Odoo versions (no build)

Each **Odoo Version** record (`saas.odoo.version`) names its image: `docker_image` (repository) + `docker_image_tag` (`models/saas_odoo_version.py:14-22`). The shipped record is `docker.io/library/odoo` / `20.0` (`data/saas_odoo_version_data.xml`, `noupdate="1"`, so edit it in the UI, not the XML). A tenant can't be deployed without both fields (`models/saas_instance.py:3755-3758`). The operator also accepts only versions listed in `supportedOdooVersions` (`compute/charts/odoo-operator/values.yaml:70`).

Tags `latest`, `main`, `master`, `edge`, `dev`, `nightly` are rejected (`compute/operator/internal/controller/validate.go:49,134-140`) unless `allowMutableTags: true`. A pinned tag (or a dated one like `18.0-20261001`) is safer than `18.0`, which Odoo moves nightly.

---

## 3. Tenant image pipeline (customer Git repos)

What happens when a customer pushes to a connected repo, or presses Redeploy (`models/saas_instance_build.py`, `drivers/k8s_builds.py`):

1. **Queue.** `action_build_and_deploy` checks that the cluster has `registry_host` (else: "no container registry is configured") and queues `_job_start_build` (`saas_instance_build.py:56-90, 140-150`).
2. **No repos?** The tenant just runs the plain version image; nothing is built (`saas_instance_build.py:189-193`).
3. **Job.** One Job `build-<id>-<sub>` in namespace `odoo-builds` (`k8s_builds.py:224-273`): `backoffLimit: 0`, 30-minute deadline, deleted 1 h after it ends, no ServiceAccount token. A NetworkPolicy allows only DNS and public 80/443 (no cluster or metadata IPs), plus the in-cluster registry if the push host is a `*.svc` name (`k8s_builds.py:104-141`). A per-build Secret holds the clone URLs (with tokens) and `config.json`; a ConfigMap holds the Dockerfile and scripts. Both are owned by the Job and go with it.
4. **`fetch`** init container, image = `git_image` (default `alpine/git:v2.45.2`): `templates/build/fetch.sh` shallow-clones every repo at the exact commit into `/workspace/addons/<dir>` (two at a time), verifies a pinned SHA, deletes `.git`.
5. **`inspect`** init container, image = **the Odoo base image** (for its Python): `templates/build/inspect.py` finds each repo's addons folder and module versions, merges the repos' root `requirements.txt` into `/workspace/requirements.txt`, writes `/workspace/meta/result.json`.
6. **`build`** container, image = `builder_image` (default `moby/buildkit:v0.16.0-rootless`), uid 1000, seccomp/AppArmor `Unconfined` (rootless BuildKit requirement), 20 GiB scratch `emptyDir`. `templates/build/build.sh` runs `buildctl-daemonless.sh build ... --opt no-cache --output type=image,name=$IMAGE_REF,push=true`. **No cache** is imported or exported (`k8s_builds.py:158-161`).
7. **The Dockerfile** (`templates/build/Dockerfile.jinja`):
   ```dockerfile
   FROM <docker_image>:<docker_image_tag>     # the version's official Odoo image
   USER root
   COPY requirements.txt /tmp/tenant-requirements.txt
   RUN pip3 install --no-cache-dir --break-system-packages -r ... (only if not empty)
   COPY --chown=odoo:odoo addons /opt/tenant-addons
   USER odoo
   ```
   `/opt/tenant-addons`, not `/mnt/extra-addons`, because the official image declares the latter a `VOLUME`.
8. **Push.** Ref = `<registry_push_host or registry_host>/<registry_prefix>/tenant-<subdomain>:<base tag>-b<build id>` (`saas_instance_build.py:150-161, 197, 216`). The log prints `SAAS_BUILD_DIGEST` / `SAAS_BUILD_RESULT`, which the control plane reads (`k8s_builds.py:79-91`).
9. **Deploy.** `deploy_image` (`k8s_builds.py:362-389`) writes the `tenant-registry` pull Secret into the tenant namespace and patches the OdooInstance: `spec.image` (pull host + tag, `pullSecretRefs: [tenant-registry]`), `spec.addonsPaths`, `spec.update` (token + changed modules).
10. **Operator update gate.** The operator runs an `odoo -u` Job against the new image first; only if it succeeds does it roll the web pod. If it fails, the old image keeps serving (`compute/operator/internal/resources/update_job.go:42-47`). The control plane waits until `status.observedImage` equals the new ref (`saas_instance_build.py:323`).

### Registry auth

| Where | Mechanism |
|---|---|
| Build push and `FROM` | `config.json` in the per-build Secret, mounted at `/home/user/.docker`, with credentials for the push host **and** the pull host (`registry_host`) when they differ. Docker Hub also gets the legacy key `https://index.docker.io/v1/`, which BuildKit uses. |
| Build pod pulls (git, Odoo base for `inspect`, BuildKit) | A per-build `kubernetes.io/dockerconfigjson` Secret `<job>-pull` for `registry_host`, listed in the build pod's `imagePullSecrets`. It is owned by the Job and deleted with it. |
| Tenant pull | Secret `tenant-registry` in `odoo-tenant-<name>`, written by the control plane for **every** tenant whenever the cluster has registry credentials: on create (retried from `health()` until the namespace exists, and again on any `ErrImagePull`/`ImagePullBackOff`) and on every image change, including a rollback to the plain version image. `spec.image.pullSecretRefs: [tenant-registry]`. |
| Platform pull | Operator flag `--platform-pull-secret` (chart `platformPullSecret`): a dockerconfigjson Secret in `odoo-system` that the operator copies into each tenant namespace as `odoo-platform-pull`. |
| Pod use | The operator lists `pullSecretRefs` + `odoo-platform-pull` on **every** tenant pod: web Deployment, init/update Jobs, backup CronJob and final-backup Job, restore Job, the Managed PostgreSQL StatefulSet and CNPG `spec.imagePullSecrets`. |
| Staff terminal | With registry credentials, Secret `saas-toolbox-registry` in `saas-toolbox`, listed on the toolbox pod. Image: cluster field `toolbox_image`. |
| Plain HTTP | `registry_insecure` adds `http = true` to `buildkitd.toml` and `registry.insecure=true` to the push |

No username on the cluster form means no Secrets and no `pullSecretRefs`: the registry must then allow anonymous push and pull, as Docker Hub public does.

---

## 4. Using a private registry

### 4.0 Credentials per provider

| Registry | Host | Login user | Token |
|---|---|---|---|
| Docker Hub (private repos) | `docker.io` | Docker Hub user | Personal access token: *Read & Write* for pushing, *Read-only* for pull-only Secrets |
| GHCR | `ghcr.io` | GitHub user | Classic PAT: `write:packages` (push), `read:packages` (pull). New packages are private by default. |
| DigitalOcean (DOCR) | `registry.digitalocean.com` | the token itself (any non-empty user works) | DO API token with registry read/write; read-only for pulls |

Create a pull Secret anywhere you need one:

```bash
# Docker Hub / GHCR / any registry
kubectl -n <ns> create secret docker-registry regcred \
  --docker-server=<host> --docker-username=<user> --docker-password=<token>

# DOCR: generate the Secret manifest with doctl
doctl registry kubernetes-manifest --namespace <ns> --name regcred | kubectl apply -f -
```

For Docker Hub, use `--docker-server=https://index.docker.io/v1/` in the Secret (the key `docker.io` alone isn't always matched by the kubelet; the control plane writes both, see `k8s_builds.py:60-62`).

### 4.1 Re-tag and push our public images to the private registry

```bash
REG=registry.digitalocean.com/my-registry     # or ghcr.io/my-org, docker.io/my-org
for img in odoo-saas-operator:0.1.29 odoo-saas-backup-tool:0.1.5; do
  docker pull docker.io/moutazmuhammad/$img
  docker tag  docker.io/moutazmuhammad/$img $REG/$img
  docker push $REG/$img
done
# Odoo per version (only if you also host the base privately, see 4.5)
# Optional mirrors: postgres:16, moby/buildkit:v0.16.0-rootless, alpine/git:v2.45.2, alpine/k8s:1.35.6 (4.6)
docker pull docker.io/library/odoo:20.0 && docker tag docker.io/library/odoo:20.0 $REG/odoo:20.0 && docker push $REG/odoo:20.0
```

`crane copy <src> <dst>` / `skopeo copy` do the same without a local Docker and keep multi-arch manifests.

### 4.2 Tenant images (supported today)

Cluster form → **Image Builds** tab (`saas_server.py:80-115`, view `saas_server_views.xml:71-82`):

| Field | Value |
|---|---|
| Registry Host | `registry.digitalocean.com` / `ghcr.io` / `docker.io` |
| Registry Path Prefix | registry name / org / user |
| Registry Username / Password | see 4.0 (read **and write**: the same pair pushes and pulls) |
| Registry Push Host | empty unless pushing to a different address (in-cluster registry) |
| Plain-HTTP | off |

Nothing else. The next build pushes there; `deploy_image` creates `tenant-registry` in the tenant namespace and the Deployment references it through `spec.image.pullSecretRefs` → `imagePullSecrets` (section 3). Existing tenants switch on their next build/redeploy.

On Docker Hub, make sure the `tenant-*` repositories are **private** (a free plan creates public ones).

### 4.3 Operator image

The chart already supports pull secrets (`values.yaml:7`, `templates/deployment.yaml:32-35`):

```bash
kubectl create namespace odoo-system --dry-run=client -o yaml | kubectl apply -f -
kubectl -n odoo-system create secret docker-registry regcred \
  --docker-server=<host> --docker-username=<user> --docker-password=<read-only token>

helm upgrade --install odoo-operator compute/charts/odoo-operator -n odoo-system \
  -f compute/examples/doks/operator-values.yaml \
  --set image.repository=<host>/<path>/odoo-saas-operator \
  --set 'imagePullSecrets[0].name=regcred' --wait
```

Put the same keys into your values file so later upgrades keep them.

### 4.4 Platform images: backup tool, PostgreSQL, Odoo base

One pull Secret in `odoo-system` covers every platform image the operator runs in tenant namespaces:

```bash
kubectl -n odoo-system create secret docker-registry platform-pull \
  --docker-server=<host> --docker-username=<user> --docker-password=<read-only token>
```

Then, in your operator values file (e.g. `compute/examples/doks/operator-values.yaml`):

```yaml
platformPullSecret: platform-pull
images:
  backupTool: <host>/<path>/odoo-saas-backup-tool:0.1.5
  postgresRepository: <host>/<path>/postgres            # tag stays spec.database.version
  cnpgPostgresRepository: <host>/<path>/cnpg-postgresql # only if you use CloudNativePG
```

```bash
helm upgrade odoo-operator compute/charts/odoo-operator -n odoo-system -f <values file> --wait
```

The operator copies `platform-pull` into each tenant namespace as `odoo-platform-pull` on its next reconcile. If the source Secret is missing, tenants keep running and show the `PlatformPullSecretMissing` condition and a Warning event.

**Turning it on changes every tenant's pod template**, so all web Deployments roll (rolling, zero downtime, but they need node headroom). PostgreSQL StatefulSets use `OnDelete` and don't restart.

**Odoo base images:** set the version's `docker_image` (Odoo Versions menu) to the private copy. New tenants pull it with `odoo-platform-pull`, and with `tenant-registry` too when it's on the same registry as the tenant images. Builds pull it through the per-build pull Secret and BuildKit's `config.json`, so keep the base on the cluster's `registry_host`.

### 4.5 One registry for everything (recommended)

Use one private registry, one read/write token on the cluster form (tenant images, builds, toolbox), and one read-only token as `platform-pull` (operator side). Mirror every image from 4.1 into it, then:

1. Cluster form, **Image Builds** tab: registry fields (4.2), plus `builder_image`, `git_image` and `toolbox_image` pointing at the mirrors.
2. The operator values from 4.3 and 4.4.
3. Odoo Versions: `docker_image` = the mirrored Odoo repository.
4. Optional, against Docker Hub rate limits: the vendored charts' own image settings (4.6).

### 4.6 Third-party images (optional mirroring)

| Image | How to redirect |
|---|---|
| BuildKit, Git | Cluster fields `builder_image` / `git_image`; pulled with the per-build pull Secret |
| Toolbox | Cluster field `toolbox_image`; pulled with `saas-toolbox-registry` |
| PostgreSQL / CNPG | Chart `images.postgresRepository` / `images.cnpgPostgresRepository`; pulled with `odoo-platform-pull` |
| Traefik, cert-manager, Prometheus, Longhorn | Their vendored chart values: `image.registry/repository/tag` and `deployment.imagePullSecrets` (Traefik), `imageRegistry`/`imageNamespace` and `global.imagePullSecrets` (cert-manager), `server.image` / `configmapReload.prometheus.image` and `imagePullSecrets` (Prometheus), `global.imagePullSecrets` (Longhorn) |

Mirroring mostly protects against Docker Hub's anonymous pull rate limit.

### 4.7 Verify

```bash
# Anything failing to pull, cluster-wide
kubectl get pods -A | grep -E 'ErrImagePull|ImagePullBackOff|InvalidImageName'
kubectl -n <ns> describe pod <pod> | sed -n '/Events/,$p'   # "pull access denied" / "401 Unauthorized"

# Which pods carry which pull secrets
kubectl -n odoo-system get deploy odoo-operator -o jsonpath='{.spec.template.spec.imagePullSecrets}'
kubectl -n odoo-tenant-<name> get deploy -o jsonpath='{..imagePullSecrets}'
kubectl -n odoo-tenant-<name> get sa odoo -o jsonpath='{.imagePullSecrets}'
kubectl -n odoo-tenant-<name> get secret tenant-registry odoo-platform-pull

# The Secret really decodes to the right host/user
kubectl -n odoo-tenant-<name> get secret tenant-registry \
  -o jsonpath='{.data.\.dockerconfigjson}' | base64 -d | jq '.auths | keys'

# Exercise the risky paths
kubectl -n odoo-tenant-<name> create job --from=cronjob/<backup cronjob> pulltest   # backup tool pulls?
kubectl -n odoo-builds get pods; kubectl -n odoo-builds logs <build pod> -c build  # push OK?
```

Checklist:

- [ ] Operator pod Running with the private image (`kubectl -n odoo-system get pod -o wide`, `describe` shows the new image)
- [ ] A Git build pushes, and the tenant rolls to `<host>/<prefix>/tenant-<sub>:...-b<id>`
- [ ] A manual backup Job pulls the backup tool
- [ ] A new tenant **without** repos starts from the private Odoo base
- [ ] No tenant shows the `PlatformPullSecretMissing` condition (`kubectl get odooinstances -A -o yaml | grep -c PlatformPullSecretMissing` is 0)
- [ ] Docker Hub `tenant-*` repos are private (`docker logout && docker pull <tenant image>` must fail)

---

## 5. Requirements

A fully private setup needs control plane `saas_core` 18.0.58.4.0 or later, and operator 0.1.29 / chart 0.4.16 or later. Older versions could not pass pull secrets to backup/restore Jobs, plain-image tenants, build pods or PostgreSQL.

Existing tenants get `pullSecretRefs` on their next image change, and the operator-side `odoo-platform-pull` on their next reconcile.
