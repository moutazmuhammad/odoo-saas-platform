#!/usr/bin/env python3
"""Root-side node setup. Invoked by microk8s.sh over verified SSH."""
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def run(*args, capture=False):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE if capture else None).stdout


def setup(cfg):
    if os.geteuid() != 0:
        raise RuntimeError('Root is required.')
    release = Path('/etc/os-release').read_text()
    if 'ID=ubuntu\n' not in release or 'VERSION_ID="24.04"' not in release:
        raise RuntimeError('Supported Ubuntu version: 24.04 LTS.')
    marker = Path('/etc/saas-microk8s.json')
    node = cfg['node']
    identity = {'cluster': cfg['name'], 'node': node['name'], 'private_ip': node['private_ip'], 'channel': cfg.get('channel', '1.35/stable'), 'api_host': cfg['api_host']}
    if marker.exists() and json.loads(marker.read_text()) != identity:
        raise RuntimeError('Node belongs to a different bootstrap configuration. Refusing to reconfigure it.')
    installed = Path('/snap/microk8s/current').exists()
    if installed and not marker.exists():
        raise RuntimeError('Existing unmanaged MicroK8s installation. Refusing to adopt or reset it.')
    addresses = json.loads(run('ip', '-j', '-4', 'address', 'show', capture=True))
    if not any(a['local'] == node['private_ip'] for interface in addresses for a in interface['addr_info']):
        raise RuntimeError('Configured private IP is not assigned to this VM.')
    ready = Path('/etc/saas-microk8s-ready')
    if installed and ready.exists():
        run('microk8s', 'status', '--wait-ready', '--timeout', '600')
        return
    # Record ownership before installation so an interrupted install can resume.
    marker.write_text(json.dumps(identity))
    marker.chmod(0o600)
    os.environ['DEBIAN_FRONTEND'] = 'noninteractive'
    run('apt-get', 'update')
    run('apt-get', 'install', '-y', 'snapd', 'open-iscsi', 'nfs-common', 'curl', 'ca-certificates')
    run('systemctl', 'enable', '--now', 'iscsid')
    run('modprobe', 'iscsi_tcp')
    run('modprobe', 'dm_crypt')
    Path('/etc/modules-load.d/saas-longhorn.conf').write_text('iscsi_tcp\ndm_crypt\n')
    if Path('/lib/systemd/system/multipathd.service').exists():
        run('systemctl', 'disable', '--now', 'multipathd.service', 'multipathd.socket')
    run('hostnamectl', 'set-hostname', node['name'])
    hosts = Path('/etc/hosts')
    existing = hosts.read_text()
    existing = re.sub(r'\n# BEGIN SAAS NODES\n.*?# END SAAS NODES\n?', '\n', existing, flags=re.S)
    # Remove cloud-init's loopback mapping for these Kubernetes node names.
    names = {n['name'] for n in cfg['nodes']}
    lines = []
    for line in existing.splitlines():
        fields = line.split()
        if line.startswith('127.') and names.intersection(fields[1:]):
            fields = [fields[0], *(field for field in fields[1:] if field not in names)]
            if len(fields) > 1:
                lines.append(' '.join(fields))
        else:
            lines.append(line)
    existing = '\n'.join(lines)
    hosts.write_text(existing + '\n# BEGIN SAAS NODES\n' + ''.join(n['private_ip'] + ' ' + n['name'] + '\n' for n in cfg['nodes']) + '# END SAAS NODES\n')
    Path('/var/lib/longhorn').mkdir(exist_ok=True)
    if not installed:
        run('snap', 'install', 'microk8s', '--classic', '--channel=' + identity['channel'])
    run('snap', 'refresh', '--hold', 'microk8s')
    kubelet = Path('/var/snap/microk8s/current/args/kubelet')
    kubelet.write_text('\n'.join(line for line in kubelet.read_text().splitlines() if not line.startswith('--node-ip=')) + '\n--node-ip=' + node['private_ip'] + '\n')
    # Add the SaaS-facing API address to the certificate without disabling TLS.
    template = Path('/var/snap/microk8s/current/certs/csr.conf.template')
    text = template.read_text()
    try:
        ipaddress.IPv4Address(cfg['api_host'])
        san = 'IP.99 = ' + cfg['api_host']
    except ipaddress.AddressValueError:
        san = 'DNS.99 = ' + cfg['api_host']
    if '[ alt_names ]' not in text:
        raise RuntimeError('MicroK8s certificate template has no alt_names section.')
    text = re.sub(r'^(?:IP|DNS)\.99\s*=.*\n?', '', text, flags=re.M)
    text = text.replace('[ alt_names ]', '[ alt_names ]\n' + san)
    template.write_text(text)
    run('microk8s', 'refresh-certs', '--cert', 'server.crt')
    run('snap', 'restart', 'microk8s')
    run('microk8s', 'status', '--wait-ready', '--timeout', '600')
    ready.write_text('ready\n')


if __name__ == '__main__':
    setup(json.loads((Path(sys.argv[1]) / 'config.json').read_text()))
