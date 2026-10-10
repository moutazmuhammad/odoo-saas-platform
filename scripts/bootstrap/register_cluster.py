#!/usr/bin/env python3
"""Optional registration from the SaaS VM; commit only after a healthy probe."""
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile


def register(directory):
    cfg = json.loads((directory / 'config.json').read_text())
    if os.geteuid() != 0:
        raise RuntimeError('Root is required for registration.')
    user = pwd.getpwnam('odoo')
    fd, name = tempfile.mkstemp(prefix='cluster-registration-', suffix='.json', dir='/opt/saas')
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchown(stream.fileno(), user.pw_uid, user.pw_gid)
            json.dump(cfg, stream)
        # Nothing secret is embedded into code or command arguments.
        script = "import json\nc = json.load(open(" + repr(name) + "))\n" + '''
def upsert(model, domain, values):
    record = env[model].sudo().with_context(active_test=False).search(domain, limit=1)
    if record:
        record.write(values)
    else:
        record = env[model].sudo().create(values)
    return record

region_code = c.get('region_code', 'default')
region = env['saas.region'].sudo().search([('code', '=', region_code)], limit=1)
if not region:
    region = env['saas.region'].sudo().create({'code': region_code, 'name': c.get('region_name', region_code)})
domain = env['saas.based.domain'].sudo().search([('name', '=', c['domain'])], limit=1)
if domain and domain.proxy_server_id and domain.proxy_server_id.name != c['name']:
    raise RuntimeError('Base domain is linked to another cluster; refusing to reassign it.')
values = {
    'name': c['name'], 'compute_driver': 'kubernetes', 'region_id': region.id,
    'kubeconfig_file': c['kubeconfig_data'], 'kubeconfig_file_name': c['name'] + '.json',
    'tls_cluster_issuer': 'letsencrypt-prod', 'prometheus_namespace': 'monitoring',
    'prometheus_service': 'prometheus-server:80', 'ip_v4': c['ingress_ip'],
    'private_ip_v4': c['nodes'][0]['private_ip'] if c.get('nodes') else False,
}
registry = c.get('register_saas', {}).get('registry', {})
for field in ('registry_host', 'registry_push_host', 'registry_prefix', 'registry_username', 'registry_password', 'registry_insecure'):
    if field in registry:
        values[field] = registry[field]
server = upsert('saas.server', [('name', '=', c['name'])], values)
ok, error = server._probe_reachable(timeout=30)
server._update_health(ok, error)
if not ok:
    env.cr.rollback()
    raise RuntimeError('SaaS cannot reach a healthy operator: ' + str(error))
upsert('saas.based.domain', [('name', '=', c['domain'])], {'name': c['domain'], 'proxy_server_id': server.id})
env.cr.commit()
print('SaaS cluster registration and health verified.')
'''
        registration = cfg['register_saas']
        subprocess.run(['runuser', '-u', 'odoo', '--', '/opt/saas/venv/bin/python',
                        '/opt/saas/odoo18/odoo-bin', 'shell', '-c', '/etc/odoo/saas.conf',
                        '-d', registration.get('database', 'saas'), '--no-http'], input=script.encode(), check=True)
    finally:
        Path(name).unlink(missing_ok=True)


if __name__ == '__main__':
    register(Path(sys.argv[1]))
