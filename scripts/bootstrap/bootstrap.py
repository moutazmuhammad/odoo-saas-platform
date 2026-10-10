#!/usr/bin/env python3
"""Local entrypoints for Ubuntu SaaS, MicroK8s nodes and existing DOKS clusters."""
import argparse
import base64
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('local_deploy', ROOT / 'scripts/deploy/local.py')
local = importlib.util.module_from_spec(spec)
spec.loader.exec_module(local)
Error = local.DeployError


def command(args, *, env=None, capture=False, data=None, cwd=ROOT):
    # Credentials travel through pipes/private files, never argv or log output.
    args = list(map(str, args))
    if not capture:
        print('+ ' + shlex.join(args), flush=True)
    return subprocess.run(args, cwd=cwd, env=env, input=data, check=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def private(path, data):
    path = Path(path).expanduser()
    path = path.parent.resolve() / path.name
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Refuse symlink destinations, including existing user-managed credentials.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags, 0o600), 'wb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
    return path


def valid_name(value, label='name'):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,50}', value):
        raise Error(f'Invalid {label}: use lowercase letters, digits and hyphens.')
    return value


def validate(kind, cfg):
    if not re.fullmatch(r'(?=.{1,253}$)[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?', cfg['domain']) or '.' not in cfg['domain']:
        raise Error('A public DNS domain is required.')
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', cfg['email']):
        raise Error('An ACME contact email is required.')
    if kind == 'saas':
        if not cfg.get('admin_login'):
            raise Error('Set admin_login for the initial administrator account.')
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,50}', cfg.get('database', 'saas')):
            raise Error('Invalid PostgreSQL database name.')
        if not 1 <= int(cfg.get('workers', 2)) <= 64:
            raise Error('workers must be between 1 and 64.')
        for key in ('admin_password_file', 'deploy_public_key'):
            if not local.local_path(cfg[key]).is_file():
                raise Error(f'Missing {key}.')
        if len(local.local_path(cfg['admin_password_file']).read_text().strip()) < 16:
            raise Error('Admin password must contain at least 16 characters.')
        local.ssh_options(cfg['ssh'])
        return
    valid_name(cfg['name'])
    if not cfg.get('output'):
        raise Error('Set a private output path for the SaaS kubeconfig.')
    if kind == 'doks':
        if not cfg.get('context') or not local.local_path(cfg['kubeconfig']).is_file():
            raise Error('Set an existing kubeconfig and an explicit DOKS context.')
    else:
        nodes = cfg['nodes']
        if not nodes or len({n['name'] for n in nodes}) != len(nodes):
            raise Error('Provide one or more nodes with unique names.')
        if len({n['private_ip'] for n in nodes}) != len(nodes):
            raise Error('Node private IPs must be unique.')
        for node in nodes:
            valid_name(node['name'], 'node name')
            ipaddress.IPv4Address(node['private_ip'])
            local.ssh_options(node)
        ipaddress.IPv4Address(cfg['ingress_ip'])
        if cfg.get('storage_class') != 'longhorn':
            raise Error('MicroK8s bootstrap uses storage_class=longhorn.')
        if not re.fullmatch(r'[a-zA-Z0-9.-]+', cfg['api_host']):
            raise Error('Invalid api_host.')
        if not re.fullmatch(r'\d+\.\d+/stable', cfg.get('channel', '1.35/stable')):
            raise Error('Use a pinned MicroK8s stable channel, for example 1.35/stable.')
        if not 1 <= int(cfg.get('storage_replicas', min(3, len(nodes)))) <= min(3, len(nodes)):
            raise Error('storage_replicas must be between 1 and min(3, node count).')
    if cfg.get('register_saas'):
        local.ssh_options(cfg['register_saas']['ssh'])
    if cfg.get('operator_values') and not local.local_path(cfg['operator_values']).is_file():
        raise Error('operator_values file does not exist.')
    if cfg.get('dockerconfigjson'):
        auth = json.loads(local.local_path(cfg['dockerconfigjson']).read_text())
        if not auth.get('auths'):
            raise Error('dockerconfigjson must contain registry auths.')


def ssh(server, remote, *, data=None, capture=False):
    options, port, address = local.ssh_options(server)
    return command(['ssh', *options, '-p', port, address, remote], data=data, capture=capture)


def remote_python(server, script, cfg, *, source=None):
    """Private staging; no privileged execution until all files are uploaded."""
    directory = ssh(server, 'umask 077; mktemp -d /tmp/saas-bootstrap.XXXXXXXX', capture=True).decode().strip()
    if not re.fullmatch(r'/tmp/saas-bootstrap\.[A-Za-z0-9]+', directory):
        raise Error('Unexpected remote staging path.')
    try:
        with tempfile.TemporaryDirectory(prefix='saas-bootstrap-upload-') as temporary:
            work = Path(temporary)
            private(work / 'config.json', json.dumps(cfg).encode())
            options, port, address = local.ssh_options(server)
            files = [HERE / script, work / 'config.json']
            if source:
                files.append(source)
            command(['scp', *options, '-P', port, *files, f'{address}:{directory}/'])
        ssh(server, 'sudo -n python3 ' + shlex.quote(directory + '/' + script) + ' ' + shlex.quote(directory))
    finally:
        ssh(server, 'rm -rf -- ' + shlex.quote(directory))


def saas(cfg, work):
    local.require('ssh', 'scp', 'npm', 'node')
    frozen = work / 'source'
    local.snapshot(frozen, local.source_files(ROOT))
    frontend = frozen / 'frontend/veltnex'
    command(['npm', 'ci', '--no-audit', '--no-fund'], cwd=frontend)
    env = dict(os.environ)
    env.pop('VITE_STANDALONE', None)
    command(['npx', '--no-install', 'vite', 'build'], cwd=frontend, env=env)
    if not (frozen / 'control-plane/saas_website/static/spa/index.html').is_file():
        raise Error('SPA build did not produce index.html.')
    archive = work / 'source.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        tar.add(frozen / 'control-plane', arcname='control-plane')
        tar.add(frozen / 'scripts/deploy/saas-deploy.sh', arcname='saas-deploy.sh')
    payload = dict(cfg, admin_password=local.local_path(cfg['admin_password_file']).read_text().strip(),
                   deploy_key=local.local_path(cfg['deploy_public_key']).read_text().strip())
    if not re.fullmatch(r'(ssh-ed25519|ssh-rsa|ecdsa-sha2-\S+) [A-Za-z0-9+/=]+(?: [^\r\n]*)?', payload['deploy_key']):
        raise Error('deploy_public_key must contain one OpenSSH public key.')
    remote_python(cfg['ssh'], 'ubuntu_saas.py', payload, source=archive)
    print(f"SaaS verified: https://{cfg['domain']}; subsequent changes: scripts/deploy/local.sh deploy --target saas")


def microk8s(cfg, work):
    local.require('ssh', 'scp', 'kubectl', 'helm')
    # Check every node before installing the first one.
    for node in cfg['nodes']:
        ssh(node, "sudo -n python3 -c 'import os; assert os.geteuid() == 0'; . /etc/os-release; test \"$ID:$VERSION_ID\" = ubuntu:24.04")
    for node in cfg['nodes']:
        remote_python(node, 'ubuntu_microk8s.py', dict(cfg, node=node))
    first = cfg['nodes'][0]
    for node in cfg['nodes'][1:]:
        known = json.loads(ssh(first, 'sudo -n microk8s kubectl get nodes -o json', capture=True))
        if any(n['metadata']['name'] == node['name'] for n in known['items']):
            continue
        lines = ssh(first, 'sudo -n microk8s add-node --token-ttl -1', capture=True).decode().splitlines()
        match = next((line.split()[2] for line in lines if line.startswith('microk8s join ' + first['private_ip'] + ':')), None)
        if not match or not re.fullmatch(r'[0-9.:]+/[a-fA-F0-9]+/[a-fA-F0-9]+', match):
            raise Error('MicroK8s did not return a private-network join command.')
        # Tokens are passed through stdin, never printed by command logging.
        ssh(node, 'read -r join_address; sudo -n microk8s join "$join_address"', data=(match + '\n').encode())
    ssh(first, 'sudo -n microk8s enable dns; sudo -n microk8s kubectl -n kube-system set env daemonset/calico-node IP_AUTODETECTION_METHOD=kubernetes-internal-ip')
    # kubectl normalizes the YAML locally without logging the admin credentials.
    admin = private(work / 'microk8s-admin.yaml', ssh(first, 'sudo -n microk8s config', capture=True))
    data = json.loads(command(['kubectl', '--kubeconfig', admin, 'config', 'view', '--raw', '-o', 'json'], capture=True))
    data['clusters'][0]['cluster']['server'] = 'https://' + cfg['api_host'] + ':16443'
    private(admin, json.dumps(data).encode())
    return admin


class Cluster:
    def __init__(self, config, env, work):
        self.cfg, self.env, self.work = config, env, work

    def kubectl(self, *args, capture=False, data=None):
        return command(['kubectl', *args], env=self.env, capture=capture, data=data)

    def apply(self, *objects):
        self.kubectl('apply', '-f', '-', data=json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': objects}).encode())

    def helm(self, name, chart, namespace, values=None, extra=()):
        args = []
        if values is not None:
            path = private(self.work / (name + '-values.json'), json.dumps(values).encode())
            args += ['-f', path]
        readiness = local.helm_wait_flags(command(['helm', 'upgrade', '--help'], capture=True))
        command(['helm', 'upgrade', '--install', name, ROOT / chart, '--namespace', namespace,
                 '--create-namespace', *args, *extra, *readiness, '--timeout', '15m'], env=self.env)


def issuer_manifest(email):
    return {'apiVersion': 'cert-manager.io/v1', 'kind': 'ClusterIssuer',
            'metadata': {'name': 'letsencrypt-prod'}, 'spec': {'acme': {
                'server': 'https://acme-v02.api.letsencrypt.org/directory', 'email': email,
                'privateKeySecretRef': {'name': 'letsencrypt-prod-account-key'},
                'solvers': [{'http01': {'ingress': {'ingressClassName': 'traefik'}}}]}}}


def account_manifests():
    return [
        {'apiVersion': 'v1', 'kind': 'ServiceAccount', 'metadata': {'name': 'saas-control-plane', 'namespace': 'kube-system'}},
        {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'ClusterRoleBinding',
         'metadata': {'name': 'saas-control-plane'},
         'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'ClusterRole', 'name': 'cluster-admin'},
         'subjects': [{'kind': 'ServiceAccount', 'name': 'saas-control-plane', 'namespace': 'kube-system'}]},
        {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'kubernetes.io/service-account-token',
         'metadata': {'name': 'saas-control-plane-token', 'namespace': 'kube-system',
                      'annotations': {'kubernetes.io/service-account.name': 'saas-control-plane'}}},
    ]


def export_kubeconfig(cluster):
    cluster.apply(*account_manifests())
    cluster.kubectl('wait', '--for=jsonpath={.data.token}', 'secret/saas-control-plane-token',
                    '-n', 'kube-system', '--timeout=120s')
    secret = json.loads(cluster.kubectl('get', 'secret', 'saas-control-plane-token', '-n', 'kube-system', '-o', 'json', capture=True))
    source = json.loads(cluster.kubectl('config', 'view', '--minify', '--flatten', '--raw', '-o', 'json', capture=True))
    endpoint = source['clusters'][0]['cluster']['server']
    ca = secret['data']['ca.crt']
    # Never export a provider's short-lived admin certificate or exec auth plugin.
    name = cluster.cfg['name']
    return {'apiVersion': 'v1', 'kind': 'Config', 'current-context': name,
            'clusters': [{'name': name, 'cluster': {'server': endpoint, 'certificate-authority-data': ca}}],
            'users': [{'name': 'saas-control-plane', 'user': {'token': base64.b64decode(secret['data']['token']).decode()}}],
            'contexts': [{'name': name, 'context': {'cluster': name, 'user': 'saas-control-plane'}}]}


def smoke(cluster):
    namespace = 'saas-bootstrap'
    cluster.apply({'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': namespace}})
    name = 'storage-' + uuid.uuid4().hex[:10]
    pvc = {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim', 'metadata': {'name': name, 'namespace': namespace},
           'spec': {'storageClassName': cluster.cfg['storage_class'], 'accessModes': ['ReadWriteOnce'],
                    'resources': {'requests': {'storage': '1Gi'}}}}
    job = {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': {'name': name, 'namespace': namespace},
           'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': 600, 'template': {'spec': {
               'restartPolicy': 'Never', 'containers': [{'name': 'check', 'image': 'busybox:1.37',
                   'command': ['sh', '-ec', 'nslookup kubernetes.default.svc.cluster.local; echo storage-ok > /data/test; test "$(cat /data/test)" = storage-ok'],
                   'volumeMounts': [{'name': 'data', 'mountPath': '/data'}]}],
               'volumes': [{'name': 'data', 'persistentVolumeClaim': {'claimName': name}}]}}}}
    try:
        cluster.apply(pvc, job)
        cluster.kubectl('wait', '--for=condition=Complete', 'job/' + name, '-n', namespace, '--timeout=10m')
    finally:
        cluster.kubectl('delete', 'job,pvc', name, '-n', namespace, '--ignore-not-found', '--wait=false')
    # Keep this certificate between runs to avoid repeated ACME issuance.
    cluster.apply({'apiVersion': 'cert-manager.io/v1', 'kind': 'Certificate',
                   'metadata': {'name': 'bootstrap-tls', 'namespace': namespace},
                   'spec': {'secretName': 'bootstrap-tls', 'dnsNames': ['bootstrap-check.' + cluster.cfg['domain']],
                            'issuerRef': {'name': 'letsencrypt-prod', 'kind': 'ClusterIssuer'}}})
    print('Verifying wildcard DNS, public HTTP routing and ACME issuance...', flush=True)
    cluster.kubectl('wait', '--for=condition=Ready', 'certificate/bootstrap-tls', '-n', namespace, '--timeout=10m')


def prepare_cluster(kind, cfg, admin, work):
    local.require('kubectl', 'helm')
    raw = command(['kubectl', '--kubeconfig', admin, *(['--context', cfg['context']] if kind == 'doks' else []),
                   'config', 'view', '--minify', '--flatten', '--raw'], capture=True)
    kube = private(work / 'cluster-admin.yaml', raw)
    cluster = Cluster(cfg, dict(os.environ, KUBECONFIG=str(kube)), work)
    cluster.kubectl('wait', '--for=condition=Ready', 'nodes', '--all', '--timeout=10m')
    if kind == 'doks':
        nodes = json.loads(cluster.kubectl('get', 'nodes', '-o', 'json', capture=True))
        if not nodes['items'] or not all(n['spec'].get('providerID', '').startswith('digitalocean://') for n in nodes['items']):
            raise Error('Selected context does not identify a DigitalOcean Kubernetes cluster.')
        classes = json.loads(cluster.kubectl('get', 'storageclass', '-o', 'json', capture=True))
        defaults = [item['metadata']['name'] for item in classes['items']
                    if item['metadata'].get('annotations', {}).get('storageclass.kubernetes.io/is-default-class') == 'true']
        if defaults != [cfg['storage_class']]:
            raise Error('The configured storage_class must be the only default StorageClass; fix this before bootstrap.')
        cluster.helm('traefik', 'compute/charts/vendor/traefik', 'ingress', extra=('-f', ROOT / 'compute/examples/doks/traefik-values.yaml'))
        cluster.kubectl('wait', '--for=jsonpath={.status.loadBalancer.ingress[0].ip}', 'service/traefik', '-n', 'ingress', '--timeout=10m')
        svc = json.loads(cluster.kubectl('get', 'service', 'traefik', '-n', 'ingress', '-o', 'json', capture=True))
        cfg['ingress_ip'] = svc['status']['loadBalancer']['ingress'][0]['ip']
    else:
        replicas = int(cfg.get('storage_replicas', min(3, len(cfg['nodes']))))
        cluster.helm('longhorn', 'compute/charts/vendor/longhorn', 'longhorn-system', {
            'csi': {'kubeletRootDir': '/var/snap/microk8s/common/var/lib/kubelet'},
            'persistence': {'defaultClass': True, 'defaultClassReplicaCount': replicas},
            'defaultSettings': {'defaultReplicaCount': replicas, 'defaultDataPath': '/var/lib/longhorn',
                                'nodeDownPodDeletionPolicy': 'delete-both-statefulset-and-deployment-pod',
                                'nodeDrainPolicy': 'block-for-eviction-if-contains-last-replica'}})
        cluster.kubectl('rollout', 'status', 'ds/longhorn-csi-plugin', '-n', 'longhorn-system', '--timeout=10m')
        cluster.helm('traefik', 'compute/charts/vendor/traefik', 'ingress', {
            'deployment': {'kind': 'DaemonSet'},
            'ingressClass': {'enabled': True, 'isDefaultClass': False, 'name': 'traefik'},
            'providers': {'kubernetesIngress': {'publishedService': {'enabled': False}, 'ingressEndpoint': {'ip': cfg['ingress_ip']}}},
            'ports': {'web': {'port': 8000, 'hostPort': 80}, 'websecure': {'port': 8443, 'hostPort': 443}},
            'service': {'type': 'ClusterIP'}})
    print(f"Point *.{cfg['domain']} to {cfg['ingress_ip']} before the TLS check completes.", flush=True)
    cluster.helm('cert-manager', 'compute/charts/vendor/cert-manager', 'cert-manager', {'crds': {'enabled': True}})
    cluster.apply(issuer_manifest(cfg['email']))
    cluster.kubectl('wait', '--for=condition=Ready', 'clusterissuer/letsencrypt-prod', '--timeout=3m')
    extra = ['-f', ROOT / 'compute/charts/monitoring/prometheus-values.yaml',
             '--set-string', 'server.persistentVolume.storageClass=' + cfg['storage_class']]
    cluster.helm('prometheus', 'compute/charts/vendor/prometheus', 'monitoring', extra=extra)
    cluster.kubectl('apply', '--server-side', '--field-manager=saas-bootstrap', '-f', ROOT / 'compute/charts/odoo-operator/crds')
    values = {'networking': {'provider': 'ingress', 'ingressClassName': 'traefik',
                             'gateway': {'namespace': 'ingress', 'podSelector': {'app.kubernetes.io/name': 'traefik'}}}}
    if cfg.get('dockerconfigjson'):
        cluster.apply({'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': 'odoo-system'}},
                      {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'platform-registry', 'namespace': 'odoo-system'},
                       'type': 'kubernetes.io/dockerconfigjson', 'data': {'.dockerconfigjson': base64.b64encode(local.local_path(cfg['dockerconfigjson']).read_bytes()).decode()}})
        values.update(platformPullSecret='platform-registry', imagePullSecrets=[{'name': 'platform-registry'}])
    cluster.helm('odoo-operator', 'compute/charts/odoo-operator', 'odoo-system', values,
                 extra=('-f', local.local_path(cfg['operator_values'])) if cfg.get('operator_values') else ())
    cluster.kubectl('rollout', 'status', 'deployment/odoo-operator', '-n', 'odoo-system', '--timeout=5m')
    smoke(cluster)
    output = private(cfg['output'], json.dumps(export_kubeconfig(cluster), indent=2).encode())
    # Verify the exact credential delivered to SaaS before reporting success.
    command(['kubectl', '--kubeconfig', output, 'get', 'odooinstances', '--all-namespaces'], capture=True)
    if cfg.get('register_saas'):
        payload = dict(cfg, kubeconfig_data=base64.b64encode(output.read_bytes()).decode())
        remote_python(cfg['register_saas']['ssh'], 'register_cluster.py', payload)
    print(f"Cluster infrastructure verified. SaaS kubeconfig: {output}")
    if not cfg.get('register_saas'):
        print('Upload this kubeconfig in SaaS > Kubernetes Clusters and link the base domain; see setup/09-BOOTSTRAP.md.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=('saas', 'microk8s', 'doks'))
    parser.add_argument('action', choices=('plan', 'apply'))
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args(argv)
    cfg = json.loads(args.config.expanduser().read_text())
    validate(args.kind, cfg)
    print(f"{args.action.upper()} {args.kind}: domain={cfg['domain']}")
    if args.action == 'plan':
        if args.kind == 'saas':
            print('Ubuntu 24.04: packages, PostgreSQL, Odoo 18, local SPA/source, systemd, nginx, HTTPS, deploy user.')
        else:
            print('Prepare nodes (MicroK8s only), storage, ingress, TLS issuer, monitoring, operator, storage/DNS/TLS checks, portable kubeconfig.')
        return
    with tempfile.TemporaryDirectory(prefix='saas-bootstrap-') as temporary:
        work = Path(temporary)
        if args.kind == 'saas':
            saas(cfg, work)
        else:
            admin = microk8s(cfg, work) if args.kind == 'microk8s' else local.local_path(cfg['kubeconfig'])
            prepare_cluster(args.kind, cfg, admin, work)


if __name__ == '__main__':
    try:
        main()
    except (Error, subprocess.CalledProcessError, KeyError, ValueError, OSError) as exc:
        # Do not stringify payloads / subprocess stdout, which can contain tokens.
        print(f'Bootstrap stopped: {exc}', file=sys.stderr)
        sys.exit(1)
