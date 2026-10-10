#!/usr/bin/env python3
"""Deploy a frozen local worktree directly to SaaS and configured clusters."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / '.local-deploy/config.json'
SOURCE_ROOTS = ('control-plane/', 'frontend/', 'compute/', 'scripts/deploy/')
COMMON = ('scripts/deploy/',)


class DeployError(Exception):
    pass


def run(args, *, cwd=ROOT, env=None, capture=False, output=None):
    # Arguments are passed directly, never interpolated into a local shell.
    if not capture:
        print('+ ' + shlex.join(map(str, args)), flush=True)
    result = subprocess.run(list(map(str, args)), cwd=cwd, env=env,
                            stdout=output if output else (subprocess.PIPE if capture else None),
                            check=True)
    return result.stdout if capture else None


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def local_path(value):
    path = Path(os.path.expandvars(value)).expanduser()
    return path if path.is_absolute() else ROOT / path


def source_files(root=ROOT):
    names = subprocess.check_output(
        ['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=root)
    files = {}
    for raw in names.split(b'\0'):
        if not raw:
            continue
        name = os.fsdecode(raw)
        if not name.startswith(SOURCE_ROOTS) and name != 'scripts/generate-customer-docs.py':
            continue
        path = root / name
        # Generated SPA is rebuilt in isolation; ignored local files aren't shipped.
        if name.startswith('control-plane/saas_website/static/spa/'):
            continue
        if path.is_symlink():
            raise DeployError(f'Symlink source is unsupported: {name}')
        if path.is_file():
            files[name] = path
    return files


def snapshot(destination, files):
    fingerprints = {}
    for name, source in files.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        fingerprints[name] = digest(target.read_bytes() + str(target.stat().st_mode & 0o777).encode())
    return fingerprints


def scoped(files, prefixes):
    return {name: value for name, value in files.items() if name.startswith(tuple(prefixes) + COMMON)}


def delta(previous, current):
    return sorted(name for name in previous.keys() | current.keys()
                  if previous.get(name) != current.get(name))


def receipt_path(state, identity, component):
    key = digest(json.dumps(identity, sort_keys=True).encode())[:20]
    return state / key / (component + '.json')


def unit(state, identity, component, current, full):
    receipt = receipt_path(state, identity, component)
    old = read_json(receipt)
    changed = delta(old['files'], current) if old else sorted(current)
    return {'component': component, 'receipt': receipt, 'files': current,
            'changed': changed, 'needed': full or old is None or bool(changed),
            'first': old is None}


def make_plan(config, files, state, target='auto', clusters=(), full=False):
    plan = {'saas': None, 'clusters': []}
    server = config.get('saas')
    if server and server.get('enabled', True) and target in ('auto', 'saas', 'all'):
        plan['saas'] = unit(state, server, 'saas', scoped(files, ('control-plane/', 'frontend/', 'scripts/generate-customer-docs.py')), full)
        plan['saas']['destination'] = f"{server.get('user', 'saas-deploy')}@{server['host']}:{server.get('port', 22)}"
    configured = config.get('clusters', [])
    names = [c['name'] for c in configured]
    if len(names) != len(set(names)):
        raise DeployError('Cluster names must be unique.')
    if set(clusters) - set(names):
        raise DeployError('Unknown cluster: ' + ', '.join(sorted(set(clusters) - set(names))))
    if target in ('auto', 'cluster', 'all'):
        for cluster in configured:
            if not cluster.get('enabled', True) or (clusters and cluster['name'] not in clusters):
                continue
            if not re.fullmatch(r'[A-Za-z0-9_-]+', cluster['name']):
                raise DeployError('Invalid cluster name.')
            item = {'config': cluster, 'releases': []}
            values = local_path(cluster['values'])
            operator = scoped(files, ('compute/operator/', 'compute/charts/odoo-operator/'))
            operator['@operator-values'] = digest(values.read_bytes())
            operator['@image-config'] = digest(json.dumps(config.get('images', {}), sort_keys=True).encode())
            item['operator'] = unit(state, cluster, 'operator', operator, full)
            backup = scoped(files, ('compute/tools/backup-tool/',))
            backup['@image-config'] = operator['@image-config']
            item['backup'] = unit(state, cluster, 'backup', backup, full)
            seen = set()
            for release in cluster.get('releases', []):
                if not re.fullmatch(r'[a-z0-9][a-z0-9.-]*', release['name']) or release['name'] in seen:
                    raise DeployError('Invalid or duplicate extra Helm release name.')
                seen.add(release['name'])
                chart = release['chart'].rstrip('/')
                if not chart.startswith('compute/charts/') or '..' in Path(chart).parts:
                    raise DeployError('Extra charts must be repository paths under compute/charts/.')
                inputs = scoped(files, (chart + '/',))
                for index, value in enumerate(release.get('values', [])):
                    inputs[f'@values-{index}'] = digest(local_path(value).read_bytes())
                inputs['@release-config'] = digest(json.dumps(release, sort_keys=True).encode())
                item['releases'].append((release, unit(state, cluster, 'helm-' + release['name'], inputs, full)))
            plan['clusters'].append(item)
    if target == 'saas' and not plan['saas']:
        raise DeployError('No enabled SaaS server configured.')
    if (target == 'cluster' or clusters) and not plan['clusters']:
        raise DeployError('No enabled matching cluster configured.')
    return plan


def show_plan(plan):
    units = []
    if plan['saas']:
        units.append(('SaaS server ' + plan['saas']['destination'], plan['saas']))
    for cluster in plan['clusters']:
        name = f"{cluster['config']['name']} (context: {cluster['config'].get('context', 'MISSING')})"
        units.extend([(f'{name}: operator/chart', cluster['operator']),
                      (f'{name}: backup image', cluster['backup'])])
        units.extend((f"{name}: {release['name']}", entry) for release, entry in cluster['releases'])
    for name, entry in units:
        reason = 'first local deployment' if entry['first'] else f"{len(entry['changed'])} changed inputs"
        print(f"{'DEPLOY' if entry['needed'] else 'SKIP  '} {name} ({reason})")
        for path in entry['changed'][:12]:
            print('       ' + path)
        if len(entry['changed']) > 12:
            print(f"       ... and {len(entry['changed']) - 12} more")
    if not units:
        raise DeployError('No deployment targets configured.')
    return any(entry['needed'] for _, entry in units)


def require(*commands):
    for command in commands:
        if not shutil.which(command):
            raise DeployError(f'Required command not installed: {command}')


def ssh_options(server):
    host = server['host']
    user = server.get('user', 'saas-deploy')
    port = str(server.get('port', 22))
    if not re.fullmatch(r'[A-Za-z0-9.-]+', host) or not re.fullmatch(r'[A-Za-z0-9_-]+', user):
        raise DeployError('Invalid SSH host/user.')
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise DeployError('Invalid SSH port.')
    key = local_path(server['key'])
    known = local_path(server.get('known_hosts', '~/.ssh/known_hosts'))
    if not key.is_file() or not known.is_file():
        raise DeployError('SSH key or verified known_hosts file is missing.')
    options = ['-i', str(key), '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
               '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={known}',
               '-o', 'ConnectTimeout=15']
    return options, port, f'{user}@{host}'


def preflight(plan, config, frozen, work):
    prepared = {}
    if plan['saas'] and plan['saas']['needed']:
        require('ssh', 'scp', 'npm', 'node')
        server = config['saas']
        options, port, address = ssh_options(server)
        expected = digest((frozen / 'scripts/deploy/saas-deploy.sh').read_bytes())
        installed = run(['ssh', *options, '-p', port, address,
                         'sha256sum /usr/local/sbin/saas-deploy'], capture=True).decode().split()[0]
        if expected != installed:
            raise DeployError('Installed /usr/local/sbin/saas-deploy differs. Install the reviewed '
                              'scripts/deploy/saas-deploy.sh as root before deploying.')
        run(['ssh', *options, '-p', port, address,
             'sudo -n -l /usr/local/sbin/saas-deploy >/dev/null'])
    need_images = False
    for item in plan['clusters']:
        if not (item['operator']['needed'] or item['backup']['needed'] or
                any(u['needed'] for _, u in item['releases'])):
            continue
        require('kubectl', 'helm', 'bash')
        cluster = item['config']
        context = cluster.get('context')
        if not context:
            raise DeployError(f"Explicit Kubernetes context required for {cluster['name']}.")
        kubeconfig = work / (digest(cluster['name'].encode())[:12] + '.kubeconfig')
        with kubeconfig.open('wb') as output:
            run(['kubectl', '--kubeconfig', local_path(cluster['kubeconfig']),
                 '--context', context, 'config', 'view', '--minify', '--flatten', '--raw'],
                output=output)
        kubeconfig.chmod(0o600)
        env = dict(os.environ, KUBECONFIG=str(kubeconfig))
        run(['kubectl', 'cluster-info'], env=env)
        values = work / (kubeconfig.stem + '-values.yaml')
        shutil.copyfile(local_path(cluster['values']), values)
        if digest(values.read_bytes()) != item['operator']['files']['@operator-values']:
            raise DeployError('Operator values changed during planning; retry.')
        release_values = {}
        prepared[cluster['name']] = (env, values, release_values)
        if item['operator']['needed'] or item['backup']['needed']:
            run(['helm', 'lint', frozen / 'compute/charts/odoo-operator', '-f', values], env=env)
        for release, entry in item['releases']:
            if entry['needed']:
                chart = frozen / release['chart']
                if not (chart / 'Chart.yaml').is_file():
                    raise DeployError(f'Missing chart: {chart}')
                args = []
                for index, value in enumerate(release.get('values', [])):
                    copy = work / f"{digest((cluster['name'] + release['name']).encode())[:12]}-{index}.yaml"
                    shutil.copyfile(local_path(value), copy)
                    if digest(copy.read_bytes()) != entry['files'][f'@values-{index}']:
                        raise DeployError('Helm values changed during planning; retry.')
                    args += ['-f', str(copy)]
                release_values[release['name']] = args
                run(['helm', 'lint', chart, *args], env=env)
        need_images |= item['operator']['needed'] or item['backup']['needed']
    if need_images:
        require('docker')
        registry = config.get('images', {}).get('prefix', '')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]*', registry):
            raise DeployError('Set images.prefix to registry/namespace (no password).')
        run(['docker', 'buildx', 'version'])
        run(['docker', 'info'], capture=True)
    return prepared


def mark(entry, release_id):
    save_json(entry['receipt'], {'files': entry['files'], 'release': release_id,
                                'deployed_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})


def deploy(plan, config, frozen, work, prepared, release_id):
    images = {}
    for component, context, repository in (
            ('operator', 'compute/operator', 'odoo-saas-operator'),
            ('backup', 'compute/tools/backup-tool', 'odoo-saas-backup-tool')):
        if any(item[component]['needed'] for item in plan['clusters']):
            image = f"{config['images']['prefix'].rstrip('/')}/{repository}:local-{release_id}"
            run(['docker', 'buildx', 'build', '--pull', '--platform',
                 config['images'].get('platforms', 'linux/amd64,linux/arm64'),
                 '--tag', image, '--push', frozen / context])
            images[component] = image
    archive = None
    if plan['saas'] and plan['saas']['needed']:
        frontend = frozen / 'frontend/veltnex'
        run(['npm', 'ci', '--no-audit', '--no-fund'], cwd=frontend)
        env = dict(os.environ)
        env.pop('VITE_STANDALONE', None)
        run(['npx', '--no-install', 'vite', 'build'], cwd=frontend, env=env)
        if not (frozen / 'control-plane/saas_website/static/spa/index.html').is_file():
            raise DeployError('Frontend build did not produce the Odoo SPA.')
        archive = work / (release_id + '.tar.gz')
        with tarfile.open(archive, 'w:gz') as tar:
            tar.add(frozen / 'control-plane', arcname='control-plane')
    # Build everything before updating remote workloads. Cluster failures block SaaS.
    for item in plan['clusters']:
        cluster = item['config']
        if cluster['name'] not in prepared:
            continue
        base_env, values, release_values = prepared[cluster['name']]
        if item['operator']['needed'] or item['backup']['needed']:
            env = dict(base_env, OPERATOR_VALUES_FILE=str(values),
                       OPERATOR_NAMESPACE=cluster.get('namespace', 'odoo-system'),
                       OPERATOR_RELEASE=cluster.get('release', 'odoo-operator'))
            # Clear inherited overrides so backup-only deployment preserves the operator image.
            env.pop('OPERATOR_IMAGE', None)
            env.pop('BACKUP_IMAGE', None)
            if item['operator']['needed']:
                env['OPERATOR_IMAGE'] = images['operator']
            if item['backup']['needed']:
                env['BACKUP_IMAGE'] = images['backup']
            run(['bash', 'scripts/deploy/operator.sh'], cwd=frozen, env=env)
            mark(item['operator'], release_id)
            mark(item['backup'], release_id)
        for release, entry in item['releases']:
            if not entry['needed']:
                continue
            chart = frozen / release['chart']
            args = release_values[release['name']]
            if (chart / 'crds').is_dir():
                run(['kubectl', 'apply', '--server-side', '--field-manager=saas-local-deploy',
                     '-f', chart / 'crds'], env=base_env)
            run(['helm', 'upgrade', '--install', release['name'], chart,
                 '--namespace', release['namespace'], '--create-namespace',
                 '--reset-then-reuse-values', *args, '--atomic', '--wait',
                 '--timeout', '10m', '--history-max', '20'], env=base_env)
            mark(entry, release_id)
    if archive:
        options, port, address = ssh_options(config['saas'])
        remote = f'/var/lib/saas-deploy/incoming/{release_id}.tar.gz'
        # Upload under a temporary name, then atomically publish the complete archive.
        run(['scp', *options, '-P', port, archive, f'{address}:{remote}.part'])
        run(['ssh', *options, '-p', port, address,
             f'mv {remote}.part {remote} && sudo -n /usr/local/sbin/saas-deploy {release_id}'])
        mark(plan['saas'], release_id)


def initialize(path):
    if path.exists():
        raise DeployError(f'Config already exists: {path}')
    config = {
        'saas': {'enabled': True, 'host': 'main.eagle-tech.info', 'user': 'saas-deploy',
                 'port': 22, 'key': '~/saas-ci', 'known_hosts': '~/.ssh/known_hosts'},
        'images': {'prefix': 'docker.io/moutazmuhammad', 'platforms': 'linux/amd64,linux/arm64'},
        'clusters': [{'name': 'microk8s', 'enabled': True, 'context': 'microk8s',
                      'kubeconfig': '~/.kube/config',
                      'values': str(path.parent / 'microk8s-values.yaml'),
                      'namespace': 'odoo-system', 'release': 'odoo-operator', 'releases': []}],
    }
    save_json(path, config)
    values = path.parent / 'microk8s-values.yaml'
    if not values.exists():
        values.write_text('{}\n')  # Keep installed Helm values; don't guess cluster settings.
        values.chmod(0o600)
    print(f'Created {path}. Review destinations and registry, then run plan.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', choices=('init', 'plan', 'deploy'), default='plan')
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--target', choices=('auto', 'saas', 'cluster', 'all'), default='auto')
    parser.add_argument('--cluster', action='append', default=[], help='Cluster name; repeat for multiple clusters')
    parser.add_argument('--full', action='store_true', help='Deploy selected targets even if inputs are unchanged')
    parser.add_argument('--dry-run', action='store_true', help='Plan only; no build, upload or remote commands')
    args = parser.parse_args(argv)
    if args.cluster and args.target == 'saas':
        raise DeployError('--cluster cannot be combined with --target saas.')
    if args.cluster and args.target == 'auto':
        args.target = 'cluster'
    config_path = args.config.expanduser().resolve()
    if args.command == 'init':
        initialize(config_path)
        return
    config = read_json(config_path)
    if not config:
        raise DeployError(f'Config missing: run {sys.argv[0]} init first.')
    state = config_path.parent / 'state'
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / 'deploy.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise DeployError('Another local deployment is running.') from error
        with tempfile.TemporaryDirectory(prefix='saas-local-') as directory:
            work = Path(directory)
            frozen = work / 'source'
            frozen.mkdir()
            files = snapshot(frozen, source_files())
            plan = make_plan(config, files, state, args.target, args.cluster, args.full)
            needed = show_plan(plan)
            if args.command == 'plan' or args.dry_run or not needed:
                return
            prepared = preflight(plan, config, frozen, work)
            head = run(['git', 'rev-parse', 'HEAD'], capture=True).decode().strip()
            release_id = f'{time.time_ns()}-{uuid.uuid4().int % 1000000000}-{head}'
            deploy(plan, config, frozen, work, prepared, release_id)
            print('Deployment complete. Successful target receipts saved locally.')


if __name__ == '__main__':
    try:
        main()
    except (DeployError, subprocess.CalledProcessError, OSError, ValueError, KeyError) as error:
        print(f'Deployment stopped: {error}', file=sys.stderr)
        sys.exit(1)
