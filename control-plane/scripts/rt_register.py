"""Real-test registration: wire up control-plane records to provision a tenant
on the live test server (165.245.245.196). Run via:  odoo-bin shell -d saas_dev < this
No secrets are stored here — the private key is read from disk at runtime.
"""
import base64

KEY_PATH = '/home/moutaz/.ssh/saas_cp_ed25519'
SERVER_IP = '165.245.245.196'
HOSTKEY = 'SHA256:zEkqyqrjlQShNw2EDFKdgPCtzhhL0fz9T8WfHZoQDJ4'

E = env  # provided by odoo shell

# 1) SSH key pair (private key as base64 binary)
key_b64 = base64.b64encode(open(KEY_PATH, 'rb').read()).decode()
sshkey = E['saas.ssh.key.pair'].sudo().search([('name', '=', 'cp-ed25519')], limit=1)
vals = {'name': 'cp-ed25519', 'type': 'ed25519',
        'private_key_file': key_b64, 'private_key_file_name': 'saas_cp_ed25519'}
sshkey = sshkey and (sshkey.write(vals) or sshkey) or E['saas.ssh.key.pair'].sudo().create(vals)
print('sshkey', sshkey.id)

# 2) Region
region = E['saas.region'].sudo().search([('code', '=', 'fra1')], limit=1)
if not region:
    # don't touch is_default/is_recommended — a seeded default region already exists
    region = E['saas.region'].sudo().create({'name': 'Frankfurt', 'code': 'fra1'})
print('region', region.id)

# 3) Server (docker host + db server, same box)
server = E['saas.server'].sudo().search([('name', '=', 'rt-fra1')], limit=1)
svals = {'name': 'rt-fra1', 'is_docker_host': True, 'is_db_server': True,
         'region_id': region.id, 'ip_v4': SERVER_IP, 'ssh_key_pair_id': sshkey.id,
         'ssh_user': 'root', 'ssh_port': 22, 'ssh_connect_using': 'public_ip',
         'expected_host_key_fingerprint': HOSTKEY,
         'docker_base_path': '/home/odoo', 'psql_port': 5432}
server = server and (server.write(svals) or server) or E['saas.server'].sudo().create(svals)
print('server', server.id)

# 4) Odoo version (official odoo:18.0)
ver = E['saas.odoo.version'].sudo().search([('name', '=', '18.0')], limit=1)
vvals = {'name': '18.0', 'docker_image': 'odoo', 'docker_image_tag': '18.0',
         'nginx_template': 'new', 'is_hosting_version': True}
ver = ver and (ver.write(vvals) or ver) or E['saas.odoo.version'].sudo().create(vvals)
print('version', ver.id)

# 5) Based domain
dom = E['saas.based.domain'].sudo().search([('name', '=', 'odoo.odex.sa')], limit=1)
dvals = {'name': 'odoo.odex.sa', 'region_id': region.id}
dom = dom and (dom.write(dvals) or dom) or E['saas.based.domain'].sudo().create(dvals)
print('domain', dom.id)

# 6) Product + plan
prod = E['saas.product'].sudo().search([('is_hosting', '=', True)], limit=1)
if not prod:
    prod = E['saas.product'].sudo().create({'name': 'RT Hosting', 'is_hosting': True, 'is_published': True})
plan = E['saas.plan'].sudo().search([('name', '=', 'RT Plan')], limit=1)
if not plan:
    plan = E['saas.plan'].sudo().create({
        'name': 'RT Plan', 'is_custom': True, 'workers': 2, 'storage_limit': 10,
        'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 200.0,
        'currency_id': E.company.currency_id.id, 'saas_product_ids': [(6, 0, [prod.id])]})
print('product/plan', prod.id, plan.id)

# 7) Partner
partner = E['res.partner'].sudo().search([('name', '=', 'RT Customer')], limit=1) \
    or E['res.partner'].sudo().create({'name': 'RT Customer'})

# 8) Instance (production, draft)
inst = E['saas.instance'].sudo().search([('subdomain', '=', 'rt1')], limit=1)
if not inst:
    inst = E['saas.instance'].sudo().create({
        'subdomain': 'rt1', 'domain_id': dom.id, 'partner_id': partner.id,
        'saas_product_id': prod.id, 'plan_id': plan.id, 'billing_period': 'monthly',
        'environment': 'production', 'region_id': region.id, 'odoo_version_id': ver.id,
        'docker_server_id': server.id, 'db_server_id': server.id, 'state': 'draft'})
print('instance', inst.id, inst.subdomain, inst.state)

E.cr.commit()
print('COMMITTED')
try:
    inst._validate_deploy_fields()
    print('VALIDATE_OK')
except Exception as e:
    print('VALIDATE_ERR:', e)
