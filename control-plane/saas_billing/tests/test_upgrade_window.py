"""C6.4: a paid upgrade has a bounded, plainly stated wait. T1 (target) and
T2 (deadline) are set when its funds are secured; retries run fast inside
that window and stop after T2 unless the case is escalated (C6.5)."""
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

from .test_upgrade_gate import _GatePatches, _mk_instance, _setup_fixture

T1, T2 = 10, 30      # C6.D11 defaults (minutes)
INFRA = ('cluster', 'node', 'pod', 'kubernetes', 'operator', 'namespace')


@tagged('post_install', '-at_install')
class TestUpgradeWindow(_GatePatches, TransactionCase):

    def setUp(self):
        super().setUp()
        self.fx = _setup_fixture(self.env, 'w')
        self.inst = _mk_instance(self.env, self.fx, 'wintest')
        self.start_gate_patches(self.inst)
        self.inst._update_container_resources()
        self.inst._verify_package()
        self.Job = self.env['saas.job'].sudo()
        for target, name, fn in ((self.env.cr, 'commit', lambda: None),
                                 (self.env.cr, 'rollback', lambda: None)):
            p = patch.object(target, name, fn)
            p.start()
            self.addCleanup(p.stop)
        # A zero-total (wallet-covered) upgrade: funds secured at once.
        self.invoice = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice', 'partner_id': self.fx['partner'].id,
            'invoice_line_ids': [(0, 0, {'name': 'up', 'quantity': 1, 'price_unit': 0.0})]})
        self.invoice.action_post()
        self.inst.write({'pending_plan_id': self.fx['big'].id,
                         'pending_change_invoice_id': self.invoice.id})

    def _fund(self):
        self.inst._advance_change(self.invoice)
        return self.inst.upgrade_authorized_at

    def _jobs(self, method):
        return self.Job.search([('model', '=', 'saas.instance'), ('res_id', '=', self.inst.id),
                                ('method', '=', method)], order='id')

    def _run(self, method):
        job = self._jobs(method).filtered(lambda j: j.state == 'pending')
        job.write({'run_after': fields.Datetime.now() - timedelta(seconds=1)})
        self.env.flush_all()
        for j in job:
            self.Job._run_one(j.id)
        self.env.invalidate_all()

    def test_window_is_set_when_funds_are_secured(self):
        at = self._fund()
        self.assertEqual(self.inst.upgrade_invoice_id, self.invoice)
        self.assertEqual(self.inst.upgrade_target_at, at + timedelta(minutes=T1))
        self.assertEqual(self.inst.upgrade_deadline_at, at + timedelta(minutes=T2))
        self.assertEqual(self.inst.upgrade_customer_status, 'in_progress')

    def test_window_follows_settings_and_ignores_bad_values(self):
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('saas_master.upgrade_target_minutes', '5')
        icp.set_param('saas_master.upgrade_deadline_minutes', '30')
        self.assertEqual(self.inst._upgrade_window_minutes(), (5, 30))
        icp.set_param('saas_master.upgrade_target_minutes', 'soon')
        icp.set_param('saas_master.upgrade_deadline_minutes', '-1')
        self.assertEqual(self.inst._upgrade_window_minutes(), (T1, T2))
        icp.set_param('saas_master.upgrade_target_minutes', '200')
        self.assertEqual(self.inst._upgrade_window_minutes(), (200, 200), "T2 never before T1")

    def test_status_follows_the_clock(self):
        at = self._fund()
        status = self.inst._upgrade_status_at
        self.assertEqual(status(at + timedelta(minutes=T1 - 1)), 'in_progress')
        self.assertEqual(status(at + timedelta(minutes=T1)), 'delayed')
        self.assertEqual(status(at + timedelta(minutes=T2 - 1)), 'delayed')
        self.assertEqual(status(at + timedelta(minutes=T2)), 'action_available')

    def test_healthy_upgrade_completes_before_t1_without_retry_waits(self):
        self._fund()
        self._run('_do_deliver_change')
        self._run('_do_verify_package')
        self.assertEqual(self.inst.upgrade_customer_status, 'complete')
        self.assertLess(self.invoice.saas_delivered_at, self.inst.upgrade_target_at)
        self.assertFalse(self._jobs('_do_converge_package'), "no retry was needed")

    def test_fast_retries_inside_the_window(self):
        at = self._fund()
        plan = self.inst._package_retry_plan
        self.assertEqual(plan(1, at), (at + timedelta(minutes=1), True))
        self.assertEqual(plan(2, at), (at + timedelta(minutes=2), True))
        self.assertEqual(plan(3, at), (at + timedelta(minutes=T1 / 4), True), "a quarter of T1 apart at most")
        near_t1 = at + timedelta(minutes=T1 - 1)
        self.assertEqual(plan(5, near_t1), (self.inst.upgrade_target_at, True), "never after T1")
        near_t2 = at + timedelta(minutes=T2 - 2)
        self.assertEqual(plan(9, near_t2), (self.inst.upgrade_deadline_at, True), "never after T2")
        self.assertEqual(plan(9, at + timedelta(minutes=T2 + 1)), (False, False),
                         "no retry after T2: the upgrade is released (C6.2)")

    def test_a_fault_cleared_at_half_t1_is_retried_before_t1(self):
        at = self._fund()
        now, attempts = at, 0
        while now < at + timedelta(minutes=T1 / 2):   # still failing
            attempts += 1
            now, _scheduled = self.inst._package_retry_plan(attempts, now)
        self.assertLess(now, self.inst.upgrade_target_at, "the next retry, after the fault, is before T1")

    def test_failed_apply_is_retried_at_its_exact_time(self):
        self._fund()
        self.driver.set_package.side_effect = RuntimeError('admission webhook denied')
        self._run('_do_deliver_change')
        self.assertEqual(self.inst.package_status, 'pending')
        job = self._jobs('_do_converge_package')
        self.assertEqual(len(job), 1, "scheduled by the failure itself, not the next reconcile")
        self.assertEqual(job.run_after, self.inst.package_next_retry)
        self.assertLessEqual(job.run_after, fields.Datetime.now() + timedelta(minutes=1, seconds=5))
        self.env['saas.instance']._converge_packages()
        self.assertEqual(len(self._jobs('_do_converge_package')), 1, "the cron never doubles it")

    def test_no_retry_after_t2_without_escalation(self):
        at = self._fund()
        self.inst.write({'upgrade_target_at': at - timedelta(hours=2),
                         'upgrade_deadline_at': at - timedelta(hours=1)})
        self.driver.set_package.side_effect = RuntimeError('admission webhook denied')
        self._run('_do_deliver_change')
        self.assertFalse(self.inst.package_next_retry)
        self.assertFalse(self._jobs('_do_converge_package'))
        self.assertIn('no further retry until it is escalated', self.inst.provisioning_log)
        self.assertEqual(self.inst.upgrade_customer_status, 'action_available')

    def test_status_payload_is_in_customer_terms(self):
        self._fund()
        payload = self.inst._upgrade_status()
        self.assertEqual(set(payload), {'status', 'target_at', 'deadline_at', 'completed_at',
                                        'released_at', 'charged', 'refunded', 'can_cancel'})
        self.assertFalse(payload['can_cancel'], "released automatically at T2 (C6.D11)")
        text = repr(payload).lower()
        for word in INFRA:
            self.assertNotIn(word, text)

    def test_released_upgrade_says_released(self):
        invoice = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice', 'partner_id': self.fx['partner'].id,
            'invoice_line_ids': [(0, 0, {'name': 'up', 'quantity': 1, 'price_unit': 5.0})]})
        invoice.action_post()
        now = fields.Datetime.now()
        invoice.sudo().write({'saas_funds_secured_at': now, 'saas_capture_mode': 'manual'})
        self.inst.write({'pending_change_invoice_id': invoice.id, 'upgrade_invoice_id': invoice.id,
                         'upgrade_target_at': now + timedelta(minutes=T1),
                         'upgrade_deadline_at': now + timedelta(minutes=T2)})
        self.assertEqual(self.inst.upgrade_customer_status, 'in_progress')
        self.assertTrue(self.inst._release_change_funds(invoice))
        self.inst.invalidate_recordset(['upgrade_customer_status'])
        self.assertEqual(self.inst.upgrade_customer_status, 'released')
        payload = self.inst._upgrade_status()
        self.assertTrue(payload['released_at'])
        self.assertFalse(payload['charged'])
        self.assertFalse(payload['refunded'])
