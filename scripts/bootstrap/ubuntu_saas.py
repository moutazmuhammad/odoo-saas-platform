#!/usr/bin/env python3
"""Root-side Ubuntu 24.04 installation; keep secrets and database on reruns."""
import base64
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import subprocess
import sys
import tarfile
import time

ROOT = Path('/opt/saas')


def run(*args, data=None, capture=False, odoo=False):
    if odoo:
        args = ('runuser', '-u', 'odoo', '--', *args)
    return subprocess.run(args, input=data, check=True, stdout=subprocess.PIPE if capture else None).stdout


def write(path, text, mode=0o644, owner=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    if owner:
        user = pwd.getpwnam(owner)
        os.chown(path, user.pw_uid, user.pw_gid)


def service(name, command, extra=''):
    write('/etc/systemd/system/' + name + '.service', f'''[Unit]
Description=SaaS {name}
After=network.target postgresql.service
Requires=postgresql.service
[Service]
User=odoo
Group=odoo
ExecStart={command}
Restart=on-failure
RestartSec=5
{extra}
[Install]
WantedBy=multi-user.target
''')


def install(directory):
    cfg = json.loads((directory / 'config.json').read_text())
    if os.geteuid() != 0:
        raise RuntimeError('Root is required.')
    release = Path('/etc/os-release').read_text()
    if 'ID=ubuntu\n' not in release or 'VERSION_ID="24.04"' not in release:
        raise RuntimeError('Supported Ubuntu version: 24.04 LTS.')
    marker = Path('/etc/saas-bootstrap.json')
    identity = {'domain': cfg['domain'], 'database': cfg.get('database', 'saas')}
    if marker.exists() and json.loads(marker.read_text()) != identity:
        raise RuntimeError('Existing bootstrap domain/database differ. Refusing to overwrite.')
    fresh = not Path('/opt/saas/current-release').exists()
    if not marker.exists() and (ROOT.exists() or Path('/etc/odoo/saas.conf').exists()):
        raise RuntimeError('Existing unmanaged SaaS installation. Use local.sh for deployments.')
    os.environ['DEBIAN_FRONTEND'] = 'noninteractive'
    run('apt-get', 'update')
    run('apt-get', 'install', '-y', 'git', 'python3.12', 'python3.12-venv', 'python3.12-dev', 'build-essential',
        'libpq-dev', 'libldap2-dev', 'libsasl2-dev', 'libxml2-dev', 'libxslt1-dev', 'libjpeg-dev', 'zlib1g-dev',
        'libffi-dev', 'libssl-dev', 'liblcms2-dev', 'libopenjp2-7-dev', 'libtiff-dev',
        'postgresql', 'nginx', 'certbot', 'python3-certbot-nginx', 'curl')
    run('systemctl', 'enable', '--now', 'postgresql')
    # Retain ownership on partial setup; don't initialize an unrelated database.
    if not marker.exists():
        exists = run('runuser', '-u', 'postgres', '--', 'psql', '-tAc',
                     "SELECT 1 FROM pg_database WHERE datname='" + identity['database'] + "'", capture=True).strip()
        if exists:
            raise RuntimeError('Database already exists without a bootstrap marker.')
        write(marker, json.dumps(identity), 0o600)
    try:
        pwd.getpwnam('odoo')
    except KeyError:
        run('useradd', '-m', '-d', str(ROOT), '-s', '/bin/bash', 'odoo')
    run('install', '-d', '-o', 'odoo', '-g', 'odoo', ROOT, ROOT / 'data', ROOT / 'log', ROOT / 'releases')
    role = run('runuser', '-u', 'postgres', '--', 'psql', '-tAc', "SELECT 1 FROM pg_roles WHERE rolname='odoo'", capture=True).strip()
    if not role:
        run('runuser', '-u', 'postgres', '--', 'createuser', '--createdb', 'odoo')
    if not (ROOT / 'odoo18/odoo-bin').exists():
        run('git', 'clone', '--depth', '1', '--branch', '18.0', 'https://github.com/odoo/odoo.git', ROOT / 'odoo18', odoo=True)
    identifier = f'{time.time_ns()}-{os.getpid()}-' + __import__('hashlib').sha256((directory / 'source.tar.gz').read_bytes()).hexdigest()[:40]
    release_dir = ROOT / 'releases' / identifier
    run('install', '-d', '-o', 'odoo', '-g', 'odoo', release_dir / 'platform')
    # The archive was generated from the local snapshot; extract as unprivileged odoo.
    run('install', '-o', 'odoo', '-g', 'odoo', '-m', '600', directory / 'source.tar.gz', ROOT / 'source.tar.gz')
    run('tar', '-xzf', ROOT / 'source.tar.gz', '-C', release_dir / 'platform', '--no-same-owner', '--no-same-permissions', odoo=True)
    (ROOT / 'source.tar.gz').unlink()
    helper = release_dir / 'platform/saas-deploy.sh'
    run('install', '-o', 'root', '-g', 'root', '-m', '755', helper, '/usr/local/sbin/saas-deploy')
    try:
        pwd.getpwnam('saas-deploy')
    except KeyError:
        run('useradd', '-m', '-s', '/bin/bash', 'saas-deploy')
    run('install', '-d', '-o', 'saas-deploy', '-g', 'saas-deploy', '-m', '700', '/home/saas-deploy/.ssh', '/var/lib/saas-deploy/incoming')
    # Do not remove an existing CI key when the operator adds a local deploy key.
    keys = Path('/home/saas-deploy/.ssh/authorized_keys')
    current_keys = keys.read_text().splitlines() if keys.exists() else []
    entry = 'restrict ' + cfg['deploy_key']
    if entry not in current_keys:
        current_keys.append(entry)
    write(keys, '\n'.join(current_keys) + '\n', 0o600, 'saas-deploy')
    write('/etc/sudoers.d/saas-deploy', 'saas-deploy ALL=(root) NOPASSWD: /usr/local/sbin/saas-deploy\n', 0o440)
    run('visudo', '-cf', '/etc/sudoers.d/saas-deploy')
    write('/etc/saas-deploy.conf', f"SAAS_DB={identity['database']}\nSAAS_HEALTH_URL=http://127.0.0.1:8069/web/login\n", 0o600)
    if not fresh:
        archive = Path('/var/lib/saas-deploy/incoming') / (identifier + '.tar.gz')
        # The update helper expects only control-plane/ in its archive.
        with tarfile.open(archive, 'w:gz') as tar:
            tar.add(release_dir / 'platform/control-plane', arcname='control-plane')
        shutil.rmtree(release_dir)
        run('/usr/local/sbin/saas-deploy', identifier)
        run('curl', '--fail', '--silent', '--show-error', '--max-time', '30', 'https://' + cfg['domain'] + '/web/login', capture=True)
        return
    run('python3.12', '-m', 'venv', release_dir / 'venv', odoo=True)
    pip = release_dir / 'venv/bin/pip'
    run(pip, 'install', '-r', ROOT / 'odoo18/requirements.txt', odoo=True)
    run(pip, 'install', '-r', release_dir / 'platform/control-plane/requirements.txt', odoo=True)
    for name in ('platform', 'venv'):
        target = ROOT / name
        if target.is_symlink():
            target.unlink()
        target.symlink_to(release_dir / name)
    configuration = Path('/etc/odoo/saas.conf')
    if not configuration.exists():
        key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        write(configuration, f'''[options]
admin_passwd = {secrets.token_urlsafe(48)}
db_host = False
db_user = odoo
db_name = {identity['database']}
dbfilter = ^{identity['database']}$
list_db = False
data_dir = /opt/saas/data
addons_path = /opt/saas/odoo18/addons,/opt/saas/platform/control-plane
http_interface = 127.0.0.1
http_port = 8069
gevent_port = 8072
workers = {int(cfg.get('workers', 2))}
max_cron_threads = 1
proxy_mode = True
logfile = /opt/saas/log/odoo.log
saas_secret_key = {key}
''', 0o640)
        os.chown(configuration, 0, pwd.getpwnam('odoo').pw_gid)
    python = ROOT / 'venv/bin/python'
    odoo = ROOT / 'odoo18/odoo-bin'
    db = identity['database']
    # Retry safely after partial initialization: -i updates installed modules too.
    run(python, odoo, '-c', configuration, '-d', db, '-i', 'saas_core,saas_billing,saas_website,saas_iam',
        '--without-demo=all', '--stop-after-init', '--no-http', odoo=True)
    # Use stdin for the initial admin password; never include it in process argv.
    settings_path = release_dir / 'admin-setup.json'
    write(settings_path, json.dumps({'login': cfg['admin_login'], 'password': cfg['admin_password'], 'url': 'https://' + cfg['domain']}), 0o600, 'odoo')
    script = f"import json\nc = json.load(open({str(settings_path)!r}))\nenv.ref('base.user_admin').write({{'login': c['login'], 'password': c['password']}})\nenv['ir.config_parameter'].sudo().set_param('web.base.url', c['url'])\nenv['ir.config_parameter'].sudo().set_param('web.base.url.freeze', 'True')\nenv.cr.commit()\n"
    try:
        run(python, odoo, 'shell', '-c', configuration, '-d', db, '--no-http', data=script.encode(), odoo=True)
    finally:
        settings_path.unlink(missing_ok=True)
    base = '/opt/saas/venv/bin/python /opt/saas/odoo18/odoo-bin'
    service('saas-odoo', base + ' -c /etc/odoo/saas.conf', 'KillMode=mixed')
    service('saas-jobs', base + f' --addons-path=/opt/saas/odoo18/addons,/opt/saas/platform/control-plane saas-jobs -c /etc/odoo/saas.conf -d {db}',
            'Environment=SAAS_JOB_THREADS=4\nTimeoutStopSec=900')
    domain = cfg['domain']
    write('/etc/nginx/sites-available/saas', f'''server {{
    listen 80;
    server_name {domain};
    client_max_body_size 200m;
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    location /websocket {{
        proxy_pass http://127.0.0.1:8072;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
    location / {{ proxy_pass http://127.0.0.1:8069; proxy_buffering off; }}
}}
''')
    enabled = Path('/etc/nginx/sites-enabled/saas')
    if not enabled.exists():
        enabled.symlink_to('/etc/nginx/sites-available/saas')
    run('nginx', '-t')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'saas-odoo', 'saas-jobs', 'nginx')
    run('systemctl', 'restart', 'saas-odoo', 'saas-jobs')
    run('systemctl', 'reload', 'nginx')
    run('curl', '--fail', '--silent', '--show-error', '--retry', '30', '--retry-delay', '5', '--retry-all-errors',
        '--max-time', '10', 'http://127.0.0.1:8069/web/login', capture=True)
    run('certbot', '--nginx', '--non-interactive', '--agree-tos', '--redirect', '--email', cfg['email'], '-d', domain)
    run('systemctl', 'is-active', '--quiet', 'saas-odoo', 'saas-jobs')
    run('curl', '--fail', '--silent', '--show-error', '--max-time', '30', 'https://' + domain + '/web/login', capture=True)
    write(ROOT / 'current-release', identifier + '\n', 0o644)
    print('SaaS services and HTTPS verified.')


if __name__ == '__main__':
    install(Path(sys.argv[1]))
