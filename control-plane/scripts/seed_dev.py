"""Seed the local dev DB (saas_dev) with realistic fake data to exercise the
whole platform WITHOUT any real provisioning (seed-only, mock hosts).

Idempotent: safe to re-run — every record is search-before-create by a natural
key. Run with:

    odoo-bin shell -c <conf> -d saas_dev --no-http < scripts/seed_dev.py

Everything is created with sudo. Instances are written straight into their
target lifecycle states; saas.instance.create()/write() never trigger a real
deploy (that only happens via action_deploy()), so this is inert.
"""
import base64
import os
from datetime import timedelta
from odoo import fields

E = env  # provided by odoo shell
CUR = E.company.currency_id
today = fields.Date.today()
now = fields.Datetime.now()

# SSH key stored on the (mock) seed server record. Path is overridable; if the
# file is missing we generate a throwaway ed25519 key so the seed is
# self-contained and portable (no real host is contacted in seed-only mode).
KEY_PATH = os.environ.get(
    'SAAS_SEED_KEY', os.path.expanduser('~/.saas-seed-key'))
if not os.path.exists(KEY_PATH):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey)
    from cryptography.hazmat.primitives import serialization
    pem = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption())
    with open(KEY_PATH, 'wb') as fh:
        fh.write(pem)
    os.chmod(KEY_PATH, 0o600)
    print('generated throwaway seed SSH key at', KEY_PATH)


def upsert(model, domain, vals):
    """Search by domain; write-or-create. Returns the (single) record."""
    rec = E[model].sudo().search(domain, limit=1)
    if rec:
        rec.write(vals)
    else:
        rec = E[model].sudo().create(vals)
    return rec


print('=' * 60)
print('SEED START')

# ----------------------------------------------------------------- config
# Pricing rates (single source of truth is ir.config_parameter). Sensible
# defaults so tiers price realistically; cost-floor rates left at 0 (no floor).
icp = E['ir.config_parameter'].sudo()
icp.set_param('saas_master.hosting_worker_price', '10.0')
icp.set_param('saas_master.hosting_storage_price_per_gb', '0.3')
icp.set_param('saas_master.worker_price', '15.0')
icp.set_param('saas_master.storage_price_per_gb', '0.5')
icp.set_param('saas_master.snapshot_price_per_gb', '2.0')

# ----------------------------------------------------------------- ssh key
key_b64 = base64.b64encode(open(KEY_PATH, 'rb').read()).decode()
sshkey = upsert('saas.ssh.key.pair', [('name', '=', 'seed-ed25519')], {
    'name': 'seed-ed25519', 'type': 'ed25519',
    'private_key_file': key_b64, 'private_key_file_name': 'seed_key',
})
print('sshkey', sshkey.id)

# ----------------------------------------------------------------- regions
region_default = E['saas.region'].sudo().search([('code', '=', 'default')], limit=1)
region_fra = upsert('saas.region', [('code', '=', 'eu-fra')], {
    'name': 'EU · Frankfurt', 'code': 'eu-fra', 'price_multiplier': 1.0,
    'is_recommended': True, 'sequence': 2,
})
region_us = upsert('saas.region', [('code', '=', 'us-east')], {
    'name': 'US · East', 'code': 'us-east', 'price_multiplier': 1.15,
    'sequence': 3,
})
print('regions', region_default.id, region_fra.id, region_us.id)

# ----------------------------------------------------------------- domains
dom_fra = upsert('saas.based.domain', [('name', '=', 'apps.local.test')], {
    'name': 'apps.local.test', 'region_id': region_fra.id})
dom_us = upsert('saas.based.domain', [('name', '=', 'us.local.test')], {
    'name': 'us.local.test', 'region_id': region_us.id})
print('domains', dom_fra.id, dom_us.id)

# ----------------------------------------------------------------- versions
ver18 = upsert('saas.odoo.version', [('name', '=', '18.0')], {
    'name': '18.0', 'docker_image': 'odoo', 'docker_image_tag': '18.0',
    'nginx_template': 'new', 'is_hosting_version': True})
ver17 = upsert('saas.odoo.version', [('name', '=', '17.0')], {
    'name': '17.0', 'docker_image': 'odoo', 'docker_image_tag': '17.0',
    'nginx_template': 'new', 'is_hosting_version': True})
print('versions', ver18.id, ver17.id)

# ----------------------------------------------------------------- servers
srv_fra = upsert('saas.server', [('name', '=', 'fra-host-1')], {
    'name': 'fra-host-1', 'is_docker_host': True, 'is_db_server': True,
    'is_proxy_server': True,
    'region_id': region_fra.id, 'ip_v4': '10.0.0.11',
    'ssh_key_pair_id': sshkey.id, 'ssh_user': 'root', 'ssh_port': 22,
    'ssh_connect_using': 'public_ip', 'docker_base_path': '/home/odoo',
    'psql_port': 5432, 'health_state': 'ok', 'allow_overcommit': True})
srv_us = upsert('saas.server', [('name', '=', 'us-host-1')], {
    'name': 'us-host-1', 'is_docker_host': True, 'is_db_server': True,
    'is_proxy_server': True,
    'region_id': region_us.id, 'ip_v4': '10.0.0.21',
    'ssh_key_pair_id': sshkey.id, 'ssh_user': 'root', 'ssh_port': 22,
    'ssh_connect_using': 'public_ip', 'docker_base_path': '/home/odoo',
    'psql_port': 5432, 'health_state': 'ok'})
print('servers', srv_fra.id, srv_us.id)

# ----------------------------------------------------------------- products
prod_hosting = upsert('saas.product', [('is_hosting', '=', True)], {
    'name': 'Odoo Hosting', 'is_hosting': True, 'is_published': True,
    'subtitle': 'Bring your own code — we run it.',
    'odoo_version_id': ver18.id, 'sequence': 1})
prod_pharma = upsert('saas.product', [('name', '=', 'Pharmacy Management')], {
    'name': 'Pharmacy Management', 'is_hosting': False, 'is_published': True,
    'subtitle': 'Ready-made pharmacy suite — POS, stock, expiry tracking.',
    'backup_bucket_path': 'pharmacy/seed/snapshot.zip', 'sequence': 10})
prod_clinic = upsert('saas.product', [('name', '=', 'Clinic & EMR')], {
    'name': 'Clinic & EMR', 'is_hosting': False, 'is_published': True,
    'subtitle': 'Appointments, patient records, billing.',
    'backup_bucket_path': 'clinic/seed/snapshot.zip', 'sequence': 11})
print('products', prod_hosting.id, prod_pharma.id, prod_clinic.id)


def make_plan(name, vals, products):
    v = dict(vals)
    v['name'] = name
    v['currency_id'] = CUR.id
    v['saas_product_ids'] = [(6, 0, [p.id for p in products])]
    return upsert('saas.plan', [('name', '=', name)], v)


# hosting tiers
plan_h_trial = make_plan('Hosting Free Trial', {
    'is_trial_plan': True, 'price': 0.0, 'yearly_price': 0.0,
    'workers': 1, 'storage_limit': 5, 'cpu_limit': 1.0, 'ram_limit': '1g',
    'sequence': 1}, [prod_hosting])
plan_h_starter = make_plan('Starter', {
    'is_public_tier': True, 'workers': 1, 'storage_limit': 10,
    'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 200.0,
    'sequence': 10}, [prod_hosting])
plan_h_pro = make_plan('Professional', {
    'is_public_tier': True, 'is_recommended': True, 'badge': 'Popular',
    'workers': 2, 'storage_limit': 20, 'cpu_limit': 2.0, 'ram_limit': '2g',
    'price': 40.0, 'yearly_price': 400.0, 'sequence': 20}, [prod_hosting])
plan_h_biz = make_plan('Business', {
    'is_public_tier': True, 'workers': 4, 'storage_limit': 40,
    'cpu_limit': 4.0, 'ram_limit': '4g', 'price': 80.0, 'yearly_price': 800.0,
    'sequence': 30}, [prod_hosting])
plan_h_custom = make_plan('Custom (Enterprise)', {
    'is_custom': True, 'workers': 8, 'storage_limit': 100, 'cpu_limit': 8.0,
    'ram_limit': '8g', 'price': 160.0, 'yearly_price': 1600.0,
    'sequence': 40}, [prod_hosting])

# service plans (pharmacy)
plan_p_trial = make_plan('Pharmacy Free Trial', {
    'is_trial_plan': True, 'price': 0.0, 'yearly_price': 0.0,
    'workers': 1, 'storage_limit': 5, 'cpu_limit': 1.0, 'ram_limit': '1g',
    'sequence': 1}, [prod_pharma])
plan_p_pro = make_plan('Pharmacy Professional', {
    'is_public_tier': True, 'workers': 2, 'storage_limit': 20,
    'cpu_limit': 2.0, 'ram_limit': '2g', 'price': 99.0, 'yearly_price': 990.0,
    'sequence': 10}, [prod_pharma])
plan_p_biz = make_plan('Pharmacy Business', {
    'is_public_tier': True, 'is_recommended': True, 'workers': 4,
    'storage_limit': 40, 'cpu_limit': 4.0, 'ram_limit': '4g',
    'price': 199.0, 'yearly_price': 1990.0, 'sequence': 20}, [prod_pharma])

# service plans (clinic)
plan_c_pro = make_plan('Clinic Professional', {
    'is_public_tier': True, 'workers': 2, 'storage_limit': 20,
    'cpu_limit': 2.0, 'ram_limit': '2g', 'price': 149.0,
    'yearly_price': 1490.0, 'sequence': 10}, [prod_clinic])
print('plans created')

# support plans (seeded by module; give the paid ones real prices)
sp_free = E.ref('saas_core.saas_support_plan_free')
sp_std = E.ref('saas_core.saas_support_plan_standard')
sp_pro = E.ref('saas_core.saas_support_plan_pro')
sp_ent = E.ref('saas_core.saas_support_plan_enterprise')
sp_std.sudo().write({'monthly_price': 29.0})
sp_pro.sudo().write({'monthly_price': 99.0})
sp_ent.sudo().write({'monthly_price': 299.0})

# ----------------------------------------------------------------- clients
portal_group = E.ref('base.group_portal')


def make_client(name, email, wallet_amount):
    partner = E['res.partner'].sudo().search(
        [('email', '=', email)], limit=1)
    if not partner:
        partner = E['res.partner'].sudo().create({
            'name': name, 'email': email, 'is_company': True,
            'customer_rank': 1})
    user = E['res.users'].sudo().search([('login', '=', email)], limit=1)
    if not user:
        user = E['res.users'].sudo().create({
            'name': name, 'login': email, 'email': email,
            'password': 'demo1234', 'partner_id': partner.id,
            'groups_id': [(6, 0, [portal_group.id])]})
    if wallet_amount:
        wallet = E['saas.wallet']._for_partner(partner)
        if wallet._live_balance() < wallet_amount:
            wallet._credit(wallet_amount - wallet._live_balance(),
                           'seed_topup', reason='Seed funds')
    return partner


acme = make_client('Acme Corp', 'acme@example.com', 500.0)
globex = make_client('Globex Inc', 'globex@example.com', 150.0)
initech = make_client('Initech LLC', 'initech@example.com', 0.0)
umbrella = make_client('Umbrella Co', 'umbrella@example.com', 1000.0)
print('clients', acme.id, globex.id, initech.id, umbrella.id)


# ----------------------------------------------------------------- instances
def make_instance(subdomain, partner, state, **kw):
    vals = {
        'subdomain': subdomain, 'partner_id': partner.id,
        'domain_id': kw.get('domain', dom_fra).id,
        'region_id': kw.get('region', region_fra).id,
        'odoo_version_id': kw.get('version', ver18).id,
        'docker_server_id': kw.get('server', srv_fra).id,
        'db_server_id': kw.get('server', srv_fra).id,
        'billing_period': kw.get('billing', 'monthly'),
        'environment': kw.get('environment', 'production'),
        'state': state,
    }
    if kw.get('product'):
        vals['saas_product_id'] = kw['product'].id
    if kw.get('plan'):
        vals['plan_id'] = kw['plan'].id
    if kw.get('support'):
        vals['support_plan_id'] = kw['support'].id
    if kw.get('parent'):
        vals['parent_id'] = kw['parent'].id
    for f in ('is_trial', 'daily_backup_enabled'):
        if f in kw:
            vals[f] = kw[f]
    inst = E['saas.instance'].sudo().search(
        [('subdomain', '=', subdomain)], limit=1)
    if inst:
        inst.write(vals)
    else:
        inst = E['saas.instance'].sudo().create(vals)
    return inst


# 1) Acme — live hosting project (production) + staging + dev children
acme_prod = make_instance('acme-erp', acme, 'running',
                          product=prod_hosting, plan=plan_h_biz,
                          support=sp_pro, daily_backup_enabled=True)
acme_stg = make_instance('acme-erp-staging', acme, 'running',
                         product=prod_hosting, plan=plan_h_starter,
                         environment='staging', parent=acme_prod)
acme_dev = make_instance('acme-erp-dev', acme, 'running',
                         product=prod_hosting, plan=plan_h_starter,
                         environment='development', parent=acme_prod)

# 2) Acme — a second hosting project currently provisioning
make_instance('acme-shop', acme, 'provisioning',
              product=prod_hosting, plan=plan_h_pro, support=sp_std)

# 3) Globex — hosting order awaiting payment (incomplete checkout)
make_instance('globex', globex, 'pending_payment',
              product=prod_hosting, plan=plan_h_pro)

# 4) Globex — pharmacy SERVICE running on a free trial
make_instance('globex-pharma', globex, 'running',
              product=prod_pharma, plan=plan_p_trial, is_trial=True,
              support=sp_free)

# 5) Initech — hosting production, suspended (e.g. non-payment)
make_instance('initech', initech, 'suspended',
              product=prod_hosting, plan=plan_h_starter, support=sp_free)

# 6) Initech — an old hosting project, cancelled by client
make_instance('initech-legacy', initech, 'cancelled_by_client',
              product=prod_hosting, plan=plan_h_starter)

# 7) Umbrella — hosting production, stopped
make_instance('umbrella', umbrella, 'stopped', region=region_us,
              server=srv_us, domain=dom_us,
              product=prod_hosting, plan=plan_h_pro, support=sp_ent,
              billing='yearly')

# 8) Umbrella — clinic SERVICE, running (paid)
make_instance('umbrella-clinic', umbrella, 'running',
              product=prod_clinic, plan=plan_c_pro, support=sp_pro)

# 9) A failed provision, to exercise the error/retry UI
make_instance('failed-demo', initech, 'failed',
              product=prod_hosting, plan=plan_h_starter)

print('instances created')

# ----------------------------------------------------------------- history
# Metrics (last 24h, hourly) + a couple of backups for the live Acme prod box.
if not E['saas.instance.metric'].sudo().search_count(
        [('instance_id', '=', acme_prod.id)]):
    metrics = []
    for h in range(24, 0, -1):
        ts = now - timedelta(hours=h)
        cpu = 20 + (h % 7) * 6
        ram = 35 + (h % 5) * 7
        stor = 4200 + (24 - h) * 55
        metrics.append({
            'instance_id': acme_prod.id, 'ts': ts,
            'cpu_pct': cpu, 'ram_pct': ram,
            'storage_mb': stor, 'storage_pct': stor / (40 * 1024) * 100})
    E['saas.instance.metric'].sudo().create(metrics)

for i in range(3):
    name = 'acme-erp-daily-%d' % i
    if not E['saas.instance.backup'].sudo().search_count([('name', '=', name)]):
        E['saas.instance.backup'].sudo().create({
            'instance_id': acme_prod.id, 'name': name,
            'db_name': 'acme_erp', 'state': 'done',
            'size_mb': 512 + i * 40, 'is_full_instance': True,
            'format': 'restic' if 'format' in E['saas.instance.backup']._fields else False,
            'bucket_path': 'acme/daily/%d.restic' % i})

# ----------------------------------------------------------------- payments
# Enable the Demo payment provider (module payment_demo) if present, so paid
# checkout can be completed with a SIMULATED card locally — no real gateway.
demo = E['payment.provider'].sudo().search([('code', '=', 'demo')], limit=1)
if demo:
    demo.write({'state': 'test', 'is_published': True})
    print('demo payment provider enabled (state=test) — paid checkout works')
else:
    print('payment_demo not installed — paid checkout needs a provider; '
          'trials still work with no payment')

E.cr.commit()
print('COMMITTED')
print('=' * 60)
print('SUMMARY')
print('  regions :', E['saas.region'].sudo().search_count([]))
print('  servers :', E['saas.server'].sudo().search_count([]))
print('  products:', E['saas.product'].sudo().search_count([]))
print('  plans   :', E['saas.plan'].sudo().search_count([]))
print('  clients :', E['res.partner'].sudo().search_count([('customer_rank', '>', 0)]))
print('  instances by state:')
for st, _lbl in E['saas.instance']._fields['state'].selection:
    n = E['saas.instance'].sudo().search_count([('state', '=', st)])
    if n:
        print('     %-20s %d' % (st, n))
print('SEED DONE')
