# C3.7 crash-safety harness, step 1 — run inside `odoo-bin shell -d saas_tmp_*`.
# Queues the five operations through their real entry points (payment hook,
# actions) and commits; the harness worker then runs them.
assert env.cr.dbname.startswith('saas_tmp_'), env.cr.dbname

# Only queue: in production the dedicated worker is up, so nothing runs in
# the enqueuing process. Without this, this shell would run the jobs in its
# own threads and kill them when it exits.
Job = type(env['saas.job'])
Job._spawn_worker = lambda self: None
Job._spawn_worker_after_commit = lambda self: None

icp = env['ir.config_parameter'].sudo()
icp.set_param('saas_master.hosting_worker_price', '10.0')
icp.set_param('saas_master.hosting_storage_price_per_gb', '0.3')

product = env['saas.product'].sudo().search([('is_hosting', '=', True)], limit=1) \
    or env['saas.product'].sudo().create(
        {'name': 'Crash Hosting', 'is_hosting': True, 'is_published': True})


def mk_plan(name, workers, price):
    return env['saas.plan'].sudo().create({
        'name': name, 'is_custom': True, 'workers': workers, 'storage_limit': 20,
        'cpu_limit': 1.0, 'ram_limit': '1g', 'price': price, 'yearly_price': price * 10,
        'currency_id': env.company.currency_id.id,
        'saas_product_ids': [(6, 0, [product.id])]})


small, big = mk_plan('Crash Small', 2, 50.0), mk_plan('Crash Big', 4, 90.0)
partner = env['res.partner'].sudo().create({'name': 'Crash Customer'})
domain = env['saas.based.domain'].sudo().create({'name': 'crash.example.com'})
server = env['saas.server'].sudo().create({'name': 'crash-srv', 'compute_driver': 'kubernetes'})
version = env['saas.odoo.version'].sudo().search(
    [('docker_image', '!=', False), ('docker_image_tag', '!=', False)], limit=1) \
    or env['saas.odoo.version'].sudo().create(
        {'name': 'Crash 18', 'docker_image': 'odoo', 'docker_image_tag': '18.0'})


def mk(sub, state):
    return env['saas.instance'].sudo().create({
        'subdomain': sub, 'domain_id': domain.id, 'partner_id': partner.id,
        'saas_product_id': product.id, 'plan_id': small.id,
        'docker_server_id': server.id, 'odoo_version_id': version.id,
        'billing_period': 'monthly', 'environment': 'production',
        'region_id': False, 'state': state})


def pay(invoice):
    invoice.write({'payment_state': 'paid'})
    invoice._saas_check_instance_payment()


# A — paid order: payment queues the post-payment deploy (C3.1).
deploy = mk('crash-deploy', 'draft')
deploy.action_confirm_and_bill()
pay(deploy.sale_order_id.invoice_ids)

# B — paid plan change (C3.2).
change = mk('crash-change', 'running')
change_inv = env['account.move'].sudo().create({
    'move_type': 'out_invoice', 'partner_id': partner.id,
    'invoice_line_ids': [(0, 0, {'name': 'upgrade', 'quantity': 1, 'price_unit': 40.0})]})
change_inv.action_post()
change.write({'pending_plan_id': big.id, 'pending_billing_period': 'monthly',
              'pending_change_invoice_id': change_inv.id})
pay(change_inv)

# C — suspend (C3.3).
suspend = mk('crash-suspend', 'running')
suspend.action_suspend()

# D — cancel (C3.3), with a final snapshot left by an earlier cancel.
cancel = mk('crash-cancel', 'running')
env['saas.instance.backup'].sudo().create({
    'instance_id': cancel.id, 'name': 'old-final', 'state': 'done',
    'is_full_instance': True, 'format': 'operator', 'source': 'final',
    'bucket_path': 'backups/crash-cancel/old-final'})
cancel.action_cancel()

# E — a funded upgrade past its target (C6.5): the T1 escalation is due now.
from datetime import timedelta
from odoo import fields
oncall = env['res.users'].sudo().create({
    'name': 'Crash On Call', 'login': 'crash-oncall', 'email': 'crash-oncall@example.com',
    'groups_id': [(6, 0, [env.ref('base.group_user').id])]})
icp.set_param('saas_master.upgrade_oncall_user_id', str(oncall.id))
escalate = mk('crash-escalate', 'running')
esc_inv = env['account.move'].sudo().create({
    'move_type': 'out_invoice', 'partner_id': partner.id,
    'invoice_line_ids': [(0, 0, {'name': 'upgrade', 'quantity': 1, 'price_unit': 40.0})]})
esc_inv.action_post()
now = fields.Datetime.now()
esc_inv.write({'saas_funds_secured_at': now, 'saas_capture_mode': 'manual'})
escalate.write({'pending_plan_id': big.id, 'pending_billing_period': 'monthly',
                'pending_change_invoice_id': esc_inv.id, 'upgrade_invoice_id': esc_inv.id,
                'upgrade_authorized_at': now - timedelta(minutes=11),
                'upgrade_target_at': now - timedelta(minutes=1),
                'upgrade_deadline_at': now + timedelta(hours=1)})
escalate._schedule_upgrade_escalation(esc_inv)
# Only the T1 check is under test; the T2 one would wait an hour.
env['saas.job'].sudo().search([('idempotency_key', '=', 'upgrade-escalate:deadline:inv%s' % esc_inv.id)]) \
    .write({'state': 'cancelled'})

env.cr.commit()
for inst in (deploy, change, suspend, cancel, escalate):
    print('SETUP %s state=%s op=%s' % (inst.subdomain, inst.state, inst.pending_operation))
print('SETUP jobs=%s' % env['saas.job'].sudo().search_count([('state', '=', 'pending')]))
