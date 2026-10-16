# C3.7 crash-safety harness, last step — run inside `odoo-bin shell -d saas_tmp_*`
# with EFFECT_LOG set. Prints one PASS/FAIL line per check and a RESULT line.
import os

assert env.cr.dbname.startswith('saas_tmp_'), env.cr.dbname
Instance = env['saas.instance'].sudo()
Job = env['saas.job'].sudo()
effects = open(os.environ['EFFECT_LOG']).read().split('\n')
failures = []


def check(label, ok, detail=''):
    print('%s %s %s' % ('PASS' if ok else 'FAIL', label, detail))
    if not ok:
        failures.append(label)


def inst(sub):
    return Instance.search([('subdomain', '=', sub)])


def jobs(rec, method):
    return Job.search([('model', '=', 'saas.instance'), ('res_id', '=', rec.id),
                       ('method', '=', method)])


def starts(effect, sub):
    return sum(1 for line in effects if line.endswith(' start %s %s' % (effect, sub)))


from odoo import fields
due = [('state', 'in', ('pending', 'running')), ('run_after', '<=', fields.Datetime.now())]
check('queue drained', not Job.search_count(due))
check('no failed job', not Job.search_count([('state', '=', 'failed')]),
      Job.search([('state', '=', 'failed')]).mapped('error'))

# A — paid order deployed exactly once; its invoice has its effect.
a = inst('crash-deploy')
check('deploy: running', a.state == 'running', a.state)
check('deploy: one post-payment job, done',
      jobs(a, '_do_deploy_after_payment').mapped('state') == ['done'])
check('deploy: deploy job done', jobs(a, '_do_deploy').mapped('state') == ['done'])
check('deploy: killed once, re-run once', starts('deploy', a.subdomain) == 2,
      starts('deploy', a.subdomain))
check('deploy: invoice paid', set(a.sale_order_id.invoice_ids.mapped('payment_state'))
      <= {'paid', 'in_payment'})

# B — paid plan change applied exactly once.
b = inst('crash-change')
check('change: on the new plan', b.plan_id.name == 'Crash Big', b.plan_id.name)
check('change: nothing pending', not b.pending_plan_id and not b.pending_change_invoice_id)
check('change: delivered first (C6.10), one job, done',
      jobs(b, '_do_deliver_change').mapped('state') == ['done'])
check('change: billing switched by one job, done',
      jobs(b, '_do_apply_paid_plan_change').mapped('state') == ['done'])
audits = env['saas.audit.log'].sudo().search_count([
    ('res_id', '=', b.id), ('model', '=', 'saas.instance'), ('action', '=', 'instance_scale')])
check('change: one audit entry', audits == 1, audits)
upgraded = env['mail.message'].sudo().search_count([
    ('model', '=', 'saas.instance'), ('res_id', '=', b.id),
    ('body', 'ilike', 'Payment complete')])
check('change: one confirmation message', upgraded == 1, upgraded)
# delivery killed once + re-run once, then the billing switch's re-apply
check('change: killed once, re-run once', starts('resize', b.subdomain) == 3,
      starts('resize', b.subdomain))

# C — suspend completed once.
c = inst('crash-suspend')
check('suspend: suspended', c.state == 'suspended' and not c.pending_operation, c.state)
check('suspend: one job, done', jobs(c, '_do_suspend').mapped('state') == ['done'])
check('suspend: killed once, re-run once', starts('stop', c.subdomain) == 2,
      starts('stop', c.subdomain))

# D — cancel completed once, keeping the snapshot it took before the kill.
d = inst('crash-cancel')
check('cancel: cancelled', d.state in ('cancelled', 'cancelled_by_client'), d.state)
check('cancel: one job, done', jobs(d, '_do_delete_instance').mapped('state') == ['done'])
check('cancel: one final snapshot taken', starts('snapshot', d.subdomain) == 1,
      starts('snapshot', d.subdomain))
backups = env['saas.instance.backup'].sudo().search([('instance_id', '=', d.id)])
check('cancel: only the fresh final snapshot kept',
      len(backups) == 1 and backups.name != 'old-final', backups.mapped('name'))
check('cancel: killed once, re-run once', starts('destroy', d.subdomain) == 2,
      starts('destroy', d.subdomain))

# E — the T1 escalation killed after its activity was created: exactly one
# activity, one assignment email, once the re-run finished (C6.5).
e = inst('crash-escalate')
acts = env['mail.activity'].sudo().search([('res_model', '=', 'saas.instance'), ('res_id', '=', e.id)])
oncall = env['res.users'].sudo().search([('login', '=', 'crash-oncall')])
check('escalate: one activity, to the on-call user',
      len(acts) == 1 and acts.user_id == oncall
      and acts.activity_type_id == env.ref('saas_billing.mail_act_upgrade_delayed'),
      acts.mapped('user_id.login'))
emails = env['mail.message'].sudo().search_count([
    ('model', '=', 'saas.instance'), ('res_id', '=', e.id),
    ('message_type', '=', 'user_notification'), ('partner_ids', 'in', oncall.partner_id.ids)])
check('escalate: one assignment email', emails == 1, emails)
check('escalate: one T1 job, done', jobs(e, '_do_escalate_upgrade').filtered(
    lambda j: j.state != 'cancelled').mapped('state') == ['done'])
check('escalate: stage recorded', e.upgrade_invoice_id.saas_escalation == 'target',
      e.upgrade_invoice_id.saas_escalation)
check('escalate: killed once, re-run once', starts('escalate', e.subdomain) == 2,
      starts('escalate', e.subdomain))

# F — the T2 release killed mid-withdrawal: released once (C6.2).
f = inst('crash-release')
inv = env['account.move'].sudo().search([('partner_id', '=', f.partner_id.id),
                                         ('invoice_line_ids.price_unit', '=', -10.0)], limit=1)
check('release: invoice cancelled and released', inv.state == 'cancel' and bool(inv.saas_released_at),
      inv.state)
check('release: plan unchanged, nothing pending', f.plan_id.name == 'Crash Small'
      and not f.pending_plan_id, f.plan_id.name)
refunds = env['saas.wallet.transaction'].sudo().search_count([('move_id', '=', inv.id), ('kind', '=', 'refund')])
check('release: one wallet return', refunds == 1, refunds)
check('release: one release job, done', jobs(f, '_do_release_upgrade').mapped('state') == ['done'])
told = env['mail.message'].sudo().search_count([
    ('model', '=', 'saas.instance'), ('res_id', '=', f.id),
    ('body', 'ilike', "couldn't complete your upgrade")])
check('release: customer told once', told == 1, told)
released = env['mail.activity'].sudo().search_count([
    ('res_model', '=', 'saas.instance'), ('res_id', '=', f.id),
    ('activity_type_id', '=', env.ref('saas_billing.mail_act_upgrade_released').id)])
check('release: one hand-over activity', released == 1, released)
check('release: killed once, re-run once', starts('resize', f.subdomain) == 2,
      starts('resize', f.subdomain))

print('RESULT %s (%d failed)' % ('PASS' if not failures else 'FAIL', len(failures)))
