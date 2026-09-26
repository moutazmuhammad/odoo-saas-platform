"""In-cluster tenant image builds (customer Git repos) for KubernetesDriver.

A build is one Kubernetes Job in the shared ``odoo-builds`` namespace:

1. ``fetch`` (git image): clone each repo at the exact commit.
2. ``inspect`` (the Odoo base image, for Python): find each repo's addons
   directory and module versions, merge Python requirements.
3. ``build`` (rootless BuildKit): build the tenant Dockerfile and push it.

The build pod never gets Kubernetes API credentials; it reports back by
printing marker lines (``SAAS_BUILD_DIGEST`` / ``SAAS_BUILD_RESULT``) that
``build_status`` reads from its log. Clone URLs (which embed access tokens)
and registry credentials travel in a per-build Secret owned by the Job, so
they're garbage-collected with it. Egress is limited to DNS and public
HTTP(S) — no cluster-internal or cloud-metadata addresses — by a
NetworkPolicy on the namespace.

Deploying a finished build is ``deploy_image``: the image, addons paths and
a module-update request go onto the OdooInstance CR, and the operator runs
``odoo -u`` against the new image before rolling any serving pod to it
(the previous image keeps serving until then — zero downtime).
"""
from __future__ import annotations

import base64
import json
import os
import re
from typing import Optional

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

BUILD_NAMESPACE = 'odoo-builds'
_BUILD_LABEL = 'saas.odoo.example.com/build'
_TENANT_PULL_SECRET = 'tenant-registry'
_DIGEST_MARKER = 'SAAS_BUILD_DIGEST '
_RESULT_MARKER = 'SAAS_BUILD_RESULT '
_TEMPLATES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'templates', 'build')
# Never reachable from a build: cluster/pod/service ranges and link-local
# (cloud metadata) — the build runs untrusted customer code (pip installs).
_BLOCKED_CIDRS = ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
                  '169.254.0.0/16', '100.64.0.0/10']
_SVC_HOST_RE = re.compile(r'^[a-z0-9-]+\.([a-z0-9-]+)\.svc(\.[a-z0-9.-]+)?(:\d+)?$')


def _template(name):
    with open(os.path.join(_TEMPLATES, name)) as fh:
        return fh.read()


def docker_config_json(host, username, password):
    auth = base64.b64encode(('%s:%s' % (username, password)).encode()).decode()
    return json.dumps({'auths': {host: {'auth': auth}}})


def read_pod_log(core, pod_name, namespace, container, tail=None):
    """A pod's log as text. Reads the raw response: for non-JSON bodies this
    client version returns ``str(bytes)`` ("b'...\\n...'") from
    read_namespaced_pod_log, which mangles newlines."""
    resp = core.read_namespaced_pod_log(
        pod_name, namespace, container=container,
        tail_lines=int(tail) if tail else None, _preload_content=False)
    data = resp.data if hasattr(resp, 'data') else resp
    if isinstance(data, bytes):
        return data.decode('utf-8', 'replace')
    return data or ''


def parse_build_log(log):
    """(digest, result dict) from a build container's log; (None, None)
    for whatever marker is missing."""
    digest, result = None, None
    for line in (log or '').splitlines():
        if line.startswith(_DIGEST_MARKER):
            digest = line[len(_DIGEST_MARKER):].strip() or None
        elif line.startswith(_RESULT_MARKER):
            try:
                result = json.loads(line[len(_RESULT_MARKER):])
            except ValueError:
                result = None
    return digest, result


class ImageBuildMixin:
    """Mixed into KubernetesDriver (needs its _client/_core_api/_batch_api/
    _custom_api/_cr_name/_namespace_for helpers)."""

    def _networking_api(self):
        return k8s_client.NetworkingV1Api(self._client())

    # ------------------------------------------------------------------
    # build namespace
    # ------------------------------------------------------------------
    def _ensure_build_namespace(self, registry_push_host=None):
        core = self._core_api()
        try:
            core.read_namespace(BUILD_NAMESPACE)
        except ApiException as e:
            if e.status != 404:
                raise RuntimeError('reading namespace %s failed: %s' % (BUILD_NAMESPACE, e)) from e
            core.create_namespace(k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(
                name=BUILD_NAMESPACE,
                labels={'app.kubernetes.io/managed-by': 'saas-control-plane'})))

        egress = [
            {'to': [{'namespaceSelector': {}, 'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}],
             'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}]},
            {'to': [{'ipBlock': {'cidr': '0.0.0.0/0', 'except': _BLOCKED_CIDRS}}],
             'ports': [{'protocol': 'TCP', 'port': 443}, {'protocol': 'TCP', 'port': 80}]},
        ]
        m = _SVC_HOST_RE.match((registry_push_host or '').lower())
        if m:
            # An in-cluster registry (e.g. "registry.container-registry.svc:5000").
            rule = {'to': [{'namespaceSelector': {'matchLabels': {
                'kubernetes.io/metadata.name': m.group(1)}}}]}
            if m.group(3):
                rule['ports'] = [{'protocol': 'TCP', 'port': int(m.group(3)[1:])}]
            egress.append(rule)
        policy = {
            'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
            'metadata': {'name': 'build-egress', 'namespace': BUILD_NAMESPACE},
            'spec': {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress'],
                     'ingress': [], 'egress': egress},
        }
        net = self._networking_api()
        try:
            net.replace_namespaced_network_policy('build-egress', BUILD_NAMESPACE, policy)
        except ApiException as e:
            if e.status != 404:
                raise RuntimeError('updating build NetworkPolicy failed: %s' % e) from e
            net.create_namespaced_network_policy(BUILD_NAMESPACE, policy)

    # ------------------------------------------------------------------
    # start / status / cleanup
    # ------------------------------------------------------------------
    def start_image_build(self, *, name: str, repos: list, dockerfile: str,
                          requirements: str, base_image: str, image_ref: str,
                          builder_image: str, git_image: str,
                          registry_host: Optional[str] = None,
                          registry_username: Optional[str] = None,
                          registry_password: Optional[str] = None,
                          registry_insecure: bool = False,
                          registry_push_host: Optional[str] = None,
                          deadline_seconds: int = 1800) -> str:
        """Create the build Job ``name`` and return it. ``repos`` is a list
        of ``{'url', 'ref', 'branch', 'dir'}`` (url may embed a token).
        ``image_ref`` is the full push reference (push host)."""
        self._ensure_build_namespace(registry_push_host)
        core = self._core_api()
        labels = {_BUILD_LABEL: name, 'app.kubernetes.io/managed-by': 'saas-control-plane'}

        secret_data = {'REPO_URL_%d' % i: r['url'] for i, r in enumerate(repos)}
        if registry_username:
            secret_data['config.json'] = docker_config_json(
                registry_push_host or registry_host, registry_username, registry_password or '')
        # Idempotent: a retried job step (e.g. after a DB serialization
        # failure rolled back the first attempt's bookkeeping) must reuse
        # what the first attempt already created in the cluster.
        self._create_or_replace(
            core.create_namespaced_secret, core.replace_namespaced_secret,
            name, k8s_client.V1Secret(
                metadata=k8s_client.V1ObjectMeta(name=name, labels=labels),
                string_data=secret_data))
        self._create_or_replace(
            core.create_namespaced_config_map, core.replace_namespaced_config_map,
            name, k8s_client.V1ConfigMap(
                metadata=k8s_client.V1ObjectMeta(name=name, labels=labels),
                data={
                    'Dockerfile': dockerfile,
                    'requirements.txt': requirements or '',
                    'fetch.sh': _template('fetch.sh'),
                    'inspect.py': _template('inspect.py'),
                    'build.sh': _template('build.sh'),
                }))

        fetch_env = [{'name': 'REPO_COUNT', 'value': str(len(repos))}]
        for i, r in enumerate(repos):
            fetch_env += [
                {'name': 'REPO_URL_%d' % i, 'valueFrom': {'secretKeyRef': {'name': name, 'key': 'REPO_URL_%d' % i}}},
                {'name': 'REPO_REF_%d' % i, 'value': r.get('ref') or r['branch']},
                {'name': 'REPO_BRANCH_%d' % i, 'value': r['branch']},
                {'name': 'REPO_DIR_%d' % i, 'value': r['dir']},
            ]
        mounts = [{'name': 'workspace', 'mountPath': '/workspace'},
                  {'name': 'files', 'mountPath': '/files', 'readOnly': True}]
        build_mounts = mounts + [{'name': 'buildkit', 'mountPath': '/home/user/.local/share/buildkit'}]
        volumes = [
            {'name': 'workspace', 'emptyDir': {}},
            {'name': 'files', 'configMap': {'name': name}},
            {'name': 'buildkit', 'emptyDir': {}},
        ]
        build_env = [
            {'name': 'IMAGE_REF', 'value': image_ref},
            {'name': 'REGISTRY_INSECURE', 'value': '1' if registry_insecure else ''},
            {'name': 'BUILDKITD_FLAGS', 'value': '--oci-worker-no-process-sandbox'},
        ]
        if registry_username:
            volumes.append({'name': 'docker-config', 'secret': {
                'secretName': name, 'items': [{'key': 'config.json', 'path': 'config.json'}]}})
            build_mounts.append({'name': 'docker-config', 'mountPath': '/home/user/.docker', 'readOnly': True})

        job = {
            'apiVersion': 'batch/v1', 'kind': 'Job',
            'metadata': {'name': name, 'namespace': BUILD_NAMESPACE, 'labels': labels},
            'spec': {
                'backoffLimit': 0,
                'activeDeadlineSeconds': int(deadline_seconds),
                'ttlSecondsAfterFinished': 3600,
                'template': {
                    'metadata': {'labels': labels},
                    'spec': {
                        'restartPolicy': 'Never',
                        'automountServiceAccountToken': False,
                        'enableServiceLinks': False,
                        'initContainers': [
                            {'name': 'fetch', 'image': git_image,
                             'command': ['sh', '/files/fetch.sh'],
                             'env': fetch_env, 'volumeMounts': mounts,
                             'resources': {'limits': {'cpu': '1', 'memory': '512Mi'}}},
                            {'name': 'inspect', 'image': base_image,
                             'command': ['python3', '/files/inspect.py'],
                             'env': [{'name': 'REPO_DIRS', 'value': json.dumps([r['dir'] for r in repos])}],
                             'volumeMounts': mounts,
                             'resources': {'limits': {'cpu': '500m', 'memory': '256Mi'}}},
                        ],
                        'containers': [{
                            'name': 'build', 'image': builder_image,
                            'command': ['sh', '/files/build.sh'],
                            'env': build_env, 'volumeMounts': build_mounts,
                            'securityContext': {
                                # Rootless BuildKit's documented requirements.
                                'runAsUser': 1000, 'runAsGroup': 1000,
                                'seccompProfile': {'type': 'Unconfined'},
                                'appArmorProfile': {'type': 'Unconfined'},
                            },
                            'resources': {
                                'requests': {'cpu': '500m', 'memory': '1Gi'},
                                'limits': {'cpu': '2', 'memory': '4Gi'},
                            },
                        }],
                        'volumes': volumes,
                    },
                },
            },
        }
        batch = self._batch_api()
        try:
            created = batch.create_namespaced_job(BUILD_NAMESPACE, job)
        except ApiException as e:
            if e.status != 409:
                raise RuntimeError('creating build Job %s failed: %s' % (name, e)) from e
            created = batch.read_namespaced_job(name, BUILD_NAMESPACE)
        owner = [{'apiVersion': 'batch/v1', 'kind': 'Job', 'name': name,
                  'uid': created.metadata.uid, 'blockOwnerDeletion': False}]
        patch = {'metadata': {'ownerReferences': owner}}
        core.patch_namespaced_secret(name, BUILD_NAMESPACE, patch)
        core.patch_namespaced_config_map(name, BUILD_NAMESPACE, patch)
        return name

    @staticmethod
    def _create_or_replace(create, replace, name, body):
        try:
            create(BUILD_NAMESPACE, body)
        except ApiException as e:
            if e.status != 409:
                raise
            replace(name, BUILD_NAMESPACE, body)

    def build_status(self, name: str) -> dict:
        """``{'state': 'running'|'succeeded'|'failed', 'digest', 'result',
        'log'}`` for build Job ``name`` (``log`` is a tail, for display)."""
        try:
            job = self._batch_api().read_namespaced_job(name, BUILD_NAMESPACE)
        except ApiException as e:
            if e.status == 404:
                return {'state': 'failed', 'log': 'build Job %s no longer exists' % name}
            raise RuntimeError('reading build Job %s failed: %s' % (name, e)) from e
        status = job.status
        if status.succeeded:
            log = self._build_pod_log(name, 'build')
            digest, result = parse_build_log(log)
            if not digest or result is None:
                return {'state': 'failed', 'log': 'build finished without a result:\n' + log[-4000:]}
            return {'state': 'succeeded', 'digest': digest, 'result': result, 'log': log[-4000:]}
        failed = status.failed or any(
            c.type == 'Failed' and c.status == 'True' for c in (status.conditions or []))
        if failed:
            reason = '; '.join(
                '%s: %s' % (c.reason, c.message) for c in (status.conditions or [])
                if c.type == 'Failed' and c.status == 'True')
            return {'state': 'failed', 'log': (reason + '\n' + self._failed_step_log(name)).strip()}
        return {'state': 'running'}

    def _build_pod(self, name):
        pods = self._core_api().list_namespaced_pod(
            BUILD_NAMESPACE, label_selector='job-name=%s' % name).items
        return pods[0] if pods else None

    def _build_pod_log(self, name, container, tail=400):
        pod = self._build_pod(name)
        if pod is None:
            return ''
        try:
            return read_pod_log(self._core_api(), pod.metadata.name, BUILD_NAMESPACE, container, tail)
        except ApiException:
            return ''

    def _failed_step_log(self, name):
        """Log tail of the first build step that exited non-zero."""
        pod = self._build_pod(name)
        if pod is None:
            return ''
        statuses = list(pod.status.init_container_statuses or []) + list(pod.status.container_statuses or [])
        for cs in statuses:
            term = cs.state.terminated if cs.state else None
            if term and term.exit_code:
                return '[%s exited %s]\n%s' % (cs.name, term.exit_code, self._build_pod_log(name, cs.name, 80))
        return ''

    def cleanup_build(self, name: str) -> None:
        """Delete build Job ``name`` (its Secret/ConfigMap/pod follow via
        owner references)."""
        try:
            self._batch_api().delete_namespaced_job(
                name, BUILD_NAMESPACE, propagation_policy='Background')
        except ApiException as e:
            if e.status != 404:
                raise RuntimeError('deleting build Job %s failed: %s' % (name, e)) from e

    # ------------------------------------------------------------------
    # deploy a finished build
    # ------------------------------------------------------------------
    def deploy_image(self, handle, *, repository: str, tag: str,
                     addons_paths: list, update_token: str, modules: list,
                     databases: Optional[list] = None,
                     registry_host: Optional[str] = None,
                     registry_username: Optional[str] = None,
                     registry_password: Optional[str] = None) -> None:
        """Point the instance at a built image. The operator upgrades
        ``modules`` against it first — in its own database plus
        ``databases`` (a hosting instance's customer databases) — then
        rolls the pods (see module doc)."""
        image = {'repository': repository, 'tag': tag}
        if registry_username:
            namespace = handle.instance_path or self._namespace_for(handle)
            self._upsert_pull_secret(namespace, registry_host, registry_username, registry_password or '')
            image['pullSecretRefs'] = [{'name': _TENANT_PULL_SECRET}]
        else:
            image['pullSecretRefs'] = None  # merge-patch: drop a previous one
        patch = {'spec': {
            'image': image,
            'addonsPaths': list(addons_paths) or None,
            'update': {'token': update_token, 'modules': list(modules),
                       'databases': list(databases or []) or None},
        }}
        try:
            self._custom_api().patch_cluster_custom_object(
                'saas.odoo.example.com', 'v1alpha1', 'odooinstances', self._cr_name(handle), patch)
        except ApiException as e:
            raise RuntimeError('deploying image to %s failed: %s' % (self._cr_name(handle), e)) from e

    def _upsert_pull_secret(self, namespace, host, username, password):
        body = k8s_client.V1Secret(
            metadata=k8s_client.V1ObjectMeta(name=_TENANT_PULL_SECRET),
            type='kubernetes.io/dockerconfigjson',
            string_data={'.dockerconfigjson': docker_config_json(host, username, password)})
        core = self._core_api()
        try:
            core.replace_namespaced_secret(_TENANT_PULL_SECRET, namespace, body)
        except ApiException as e:
            if e.status != 404:
                raise RuntimeError('updating pull Secret in %s failed: %s' % (namespace, e)) from e
            core.create_namespaced_secret(namespace, body)

    def update_status(self, handle) -> dict:
        """Where the operator is with the instance's spec.update:
        ``{'applied_token', 'state': 'applied'|'running'|'failed'|'unknown',
        'message', 'phase', 'observed_image'}``."""
        cr = self._get_cr(self._cr_name(handle)) or {}
        status = cr.get('status') or {}
        token = ((cr.get('spec') or {}).get('update') or {}).get('token')
        cond = next((c for c in status.get('conditions') or [] if c.get('type') == 'UpdateReady'), None)
        # A condition written for an older spec (e.g. the previous update's
        # failure, before the operator has seen this token) says nothing
        # about the current one: the update is still on its way.
        generation = (cr.get('metadata') or {}).get('generation') or 0
        stale = bool(cond) and (cond.get('observedGeneration') or 0) < generation
        state = 'unknown'
        if token and status.get('appliedUpdateToken') == token:
            state = 'applied'
        elif stale:
            state, cond = 'running', None
        elif cond and cond.get('reason') == 'UpdateFailed':
            state = 'failed'
        elif cond:
            state = 'running'
        return {
            'applied_token': status.get('appliedUpdateToken'),
            'state': state,
            'message': (cond or {}).get('message') or '',
            'phase': status.get('phase'),
            'observed_image': status.get('observedImage') or '',
        }

    def update_job_log(self, handle, tail=200) -> str:
        """Log tail of the instance's most recent module-update Job pod."""
        namespace = handle.instance_path or self._namespace_for(handle)
        try:
            pods = self._core_api().list_namespaced_pod(
                namespace, label_selector='app.kubernetes.io/component=update').items
        except ApiException:
            return ''
        if not pods:
            return ''
        pod = sorted(pods, key=lambda p: p.metadata.creation_timestamp)[-1]
        try:
            return read_pod_log(self._core_api(), pod.metadata.name, namespace, 'update-modules', tail)
        except ApiException:
            return ''
