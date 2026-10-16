"""C6.5: a paid upgrade that misses its target is owned by a named on-call
user: one activity at T1 (emailed, due at T2), replaced by a deadline
activity and a critical alert at T2, closed by itself on delivery or
release, and never repeated."""
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

from .test_upgrade_gate import _GatePatches, _mk_instance, _setup_fixture

DELAYED = 'saas_billing.mail_act_upgrade_delayed'
PAST_DEADLINE = 'saas_billing.mail_act_upgrade_past_deadline'


@tagged('post_install', '-at_install')
class TestUpgradeEscalation(_GatePatches, TransactionCase):

    def setUp(self):
        super().setUp()
        self.fx = _setup_fixture(self.env, 'e')
        self.inst = _mk_instance(self.env, self.fx, 'esctest')
        self.start_gate_patches(self.inst)
        self.inst._update_container_resources()
        self.inst._verify_package()
        self.Job = self.env['saas.job'].sudo()
        for name in ('commit', 'rollback'):
            p = patch.object(self.env.cr, name, lambda: None)
            p.start()
            self.addCleanup(p.stop)
        self.oncall = self.env['res.users'].sudo().create({
            'name': 'On Call', 'login': 'oncall-c65', 'email': 'oncall-c65@example.com',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])]})
        self.icp = self.env['ir.config_parameter'].sudo()
        self.icp.set_param('saas_master.upgrade_oncall_user_id', str(self.oncall.id))
        # A zero-total (wallet-covered) upgrade: funds secured at once.
        self.invoice = self._invoice(0.0)
        self.inst.write({'pending_plan_id': self.fx['big'].id,
                         'pending_change_invoice_id': self.invoice.id})

    # ------------------------------------------------------------ helpers
    def _invoice(self, amount):
        invoice = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice', 'partner_id': self.fx['partner'].id,
            'invoice_line_ids': [(0, 0, {'name': 'up', 'quantity': 1, 'price_unit': amount})]})
        invoice.action_post()
        return invoice

    def _fund(self):
        self.inst._advance_change(self.invoice)
        # Undelivered and stuck, as the escalation sees a blocked rollout.
        self.inst.write({'package_status': 'blocked', 'package_block_reason': 'unschedulable',
                         'package_error': '0/2 nodes are available: Insufficient cpu'})

    def _jobs(self, stage, invoice=None):
        key = 'upgrade-escalate:%s:inv%s' % (stage, (invoice or self.invoice).id)
        return self.Job.search([('idempotency_key', '=', key)])

    def _run_stage(self, stage):
        job = self._jobs(stage).filtered(lambda j: j.state == 'pending')
        self.assertEqual(len(job), 1, "one %s check queued" % stage)
        job.write({'run_after': fields.Datetime.now() - timedelta(seconds=1)})
        self.env.flush_all()
        self.Job._run_one(job.id)
        self.env.invalidate_all()
        self.assertEqual(job.state, 'done')

    def _run(self, method):
        for job in self.Job.search([('model', '=', 'saas.instance'), ('res_id', '=', self.inst.id),
                                    ('method', '=', method), ('state', '=', 'pending')]):
            job.write({'run_after': fields.Datetime.now() - timedelta(seconds=1)})
            self.env.flush_all()
            self.Job._run_one(job.id)
        self.env.invalidate_all()

    def _activities(self, inst=None):
        inst = inst or self.inst
        return self.env['mail.activity'].sudo().search([
            ('res_model', '=', 'saas.instance'), ('res_id', '=', inst.id)])

    def _assignment_emails(self, user):
        return self.env['mail.message'].sudo().search_count([
            ('model', '=', 'saas.instance'), ('res_id', '=', self.inst.id),
            ('message_type', '=', 'user_notification'),
            ('partner_ids', 'in', user.partner_id.ids)])

    def _events(self):
        return [event for event, _message in self.alerts]

    # ------------------------------------------------------------ tests
    def test_funding_queues_t1_and_t2_checks_at_their_times(self):
        self._fund()
        self.assertEqual(self._jobs('target').run_after, self.inst.upgrade_target_at)
        self.assertEqual(self._jobs('deadline').run_after,
                         self.inst.upgrade_deadline_at + timedelta(minutes=2),
                         "just after the T2 release, as its fallback")
        self.inst._advance_change(self.invoice)
        self.env['saas.instance']._cron_apply_paid_pending_changes()
        self.assertEqual(len(self._jobs('target')), 1)
        self.assertEqual(len(self._jobs('deadline')), 1)

    def test_t1_assigns_one_actionable_activity_to_the_oncall_user(self):
        self._fund()
        self._run_stage('target')
        activity = self._activities()
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity.activity_type_id, self.env.ref(DELAYED))
        self.assertEqual(activity.user_id, self.oncall)
        self.assertEqual(activity.date_deadline,
                         fields.Date.context_today(self.inst, self.inst.upgrade_deadline_at))
        note = str(activity.note)
        for text in ('unschedulable', 'Insufficient cpu', 'vCPU', self.fx['server'].name,
                     self.invoice.name, 'Deadline (T2)', 'autoscaler'):
            self.assertIn(text, note)
        self.assertEqual(self._assignment_emails(self.oncall), 1, "the assignee is notified once")
        self.assertEqual(self.env['mail.notification'].sudo().search_count([
            ('res_partner_id', '=', self.oncall.partner_id.id),
            ('notification_type', '=', 'email')]), 1, "by email")
        self.assertIn('upgrade_delayed', self._events())
        self.assertNotIn('oncall_missing', self._events())
        self.assertEqual(self.invoice.saas_escalation, 'target')

    def test_repeats_add_nothing(self):
        self._fund()
        self._run_stage('target')
        self.inst._do_escalate_upgrade(self.invoice.id, 'target')
        self.env['saas.instance']._cron_apply_paid_pending_changes()
        self.assertEqual(len(self._jobs('target')), 1, "a finished check is never queued again")
        self.assertEqual(len(self._activities()), 1)
        self.assertEqual(self._assignment_emails(self.oncall), 1)
        self.assertEqual(self._events().count('upgrade_delayed'), 1)

    def test_t2_replaces_it_with_a_deadline_activity_and_a_critical_alert(self):
        self._fund()
        self._run_stage('target')
        self._run_stage('deadline')
        activity = self._activities()
        self.assertEqual(len(activity), 1, "one open activity per paid change")
        self.assertEqual(activity.activity_type_id, self.env.ref(PAST_DEADLINE))
        self.assertEqual(activity.user_id, self.oncall)
        self.assertEqual(self._assignment_emails(self.oncall), 2)
        self.assertEqual(self._events().count('upgrade_past_deadline'), 1)
        done = self.env['mail.message'].sudo().search_count([
            ('model', '=', 'saas.instance'), ('res_id', '=', self.inst.id),
            ('mail_activity_type_id', '=', self.env.ref(DELAYED).id),
            ('subtype_id', '=', self.env.ref('mail.mt_activities').id)])
        self.assertEqual(done, 1, "the T1 activity was marked done, not deleted silently")
        self.inst._do_escalate_upgrade(self.invoice.id, 'deadline')
        self.inst._do_escalate_upgrade(self.invoice.id, 'target')
        self.assertEqual(len(self._activities()), 1)

    def test_a_missed_t1_check_does_not_block_t2(self):
        self._fund()
        self._run_stage('deadline')
        self.assertEqual(self._activities().activity_type_id, self.env.ref(PAST_DEADLINE))
        self._run_stage('target')
        self.assertEqual(len(self._activities()), 1, "a late T1 check adds nothing after T2")

    def test_no_oncall_user_goes_to_the_administrator_with_an_alert(self):
        admin = self.env.ref('base.user_admin')
        portal = self.env['res.users'].sudo().create({
            'name': 'Portal', 'login': 'portal-c65', 'email': 'portal-c65@example.com',
            'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])]})
        for value in (False, str(portal.id), 'nobody'):
            self.icp.set_param('saas_master.upgrade_oncall_user_id', value)
            self.assertEqual(self.env['saas.instance']._upgrade_oncall_user(), (admin, False), value)
        self.icp.set_param('saas_master.upgrade_oncall_user_id', False)
        self._fund()
        self._run_stage('target')
        self.assertEqual(self._activities().user_id, admin)
        self.assertEqual(self._assignment_emails(admin), 1)
        self.assertIn('oncall_missing', self._events())

    def test_delivery_closes_the_escalation(self):
        self._fund()
        self._run_stage('target')
        self.inst.write({'package_status': 'applying', 'package_block_reason': False})
        self._run('_do_deliver_change')
        self._run('_do_verify_package')
        self.assertTrue(self.invoice.saas_delivered_at)
        self.assertFalse(self._activities(), "closed by itself on delivery")
        self._run_stage('deadline')
        self.assertFalse(self._activities(), "nothing to escalate once delivered")
        self.assertNotIn('upgrade_past_deadline', self._events())

    def test_release_closes_the_escalation(self):
        invoice = self._invoice(5.0)
        now = fields.Datetime.now()
        invoice.write({'saas_funds_secured_at': now, 'saas_capture_mode': 'manual'})
        self.inst.write({'pending_change_invoice_id': invoice.id, 'upgrade_invoice_id': invoice.id,
                         'upgrade_authorized_at': now,
                         'upgrade_target_at': now, 'upgrade_deadline_at': now + timedelta(minutes=20)})
        self.inst._do_escalate_upgrade(invoice.id, 'target')
        self.assertEqual(len(self._activities()), 1)
        self.assertTrue(self.inst._release_change_funds(invoice))
        self.assertFalse(self._activities(), "closed by itself on release")

    def test_a_crashed_escalation_leaves_nothing_and_runs_again(self):
        self._fund()

        def crash(rec, event, message, **kw):
            raise RuntimeError('worker killed')
        with patch.object(type(self.env['saas.alert']), '_notify', crash):
            with self.assertRaises(RuntimeError), self.env.cr.savepoint():
                self.inst._do_escalate_upgrade(self.invoice.id, 'target')
        self.env.invalidate_all()
        self.assertFalse(self._activities())
        self.assertFalse(self.invoice.saas_escalation)
        self._run_stage('target')
        self.assertEqual(len(self._activities()), 1)

    def test_backend_filter_lists_escalated_open_upgrades(self):
        action_domain = [('upgrade_invoice_id.saas_escalation', '!=', False),
                         ('upgrade_invoice_id.saas_delivered_at', '=', False),
                         ('upgrade_invoice_id.saas_released_at', '=', False)]
        Inst = self.env['saas.instance'].sudo()
        self._fund()
        self.assertNotIn(self.inst, Inst.search(action_domain))
        self._run_stage('target')
        self.assertIn(self.inst, Inst.search(action_domain))
        self.inst.write({'package_status': 'applying', 'package_block_reason': False})
        self._run('_do_deliver_change')
        self._run('_do_verify_package')
        self.assertNotIn(self.inst, Inst.search(action_domain))
