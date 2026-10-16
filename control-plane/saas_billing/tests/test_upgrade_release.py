"""C6.2: an undelivered paid upgrade is released, never captured. Released
automatically at T2 for any reason (C6.D11), by staff, before the card
authorization lapses, or when the authorization is lost outside the
platform; parts already in effect stay in place (C6.D7). Run against Odoo's
demo provider in manual-capture mode (see _CaptureCase)."""
import json
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests.common import tagged

from .test_capture_on_delivery import _CaptureCase

# What a release may ask of the cluster: reads and the package patch only.
SAFE_CALLS = {'cluster_facts', 'require_cluster_ready', 'package_rollout', 'delivery_status',
              'check_public_url', 'set_package', 'package_in_effect'}
RELEASED_TYPE = 'saas_billing.mail_act_upgrade_released'


@tagged('post_install', '-at_install')
class TestUpgradeRelease(_CaptureCase):

    def setUp(self):
        super().setUp()
        self.old_res = self.inst._k8s_plan_resources()
        self.driver.package_in_effect.return_value = {
            'odoo': False, 'database': False, 'volume_gi': 10}

    # ------------------------------------------------------------ helpers
    def _stuck_upgrade(self):
        """A funded upgrade whose rollout cannot finish (no room)."""
        invoice = self._upgrade()
        self.driver.package_rollout.return_value = (
            'blocked', 'unschedulable', '0/2 nodes are available: Insufficient cpu')
        self._run('_do_deliver_change')
        self._run('_do_verify_package')
        self.assertEqual(self.inst.package_status, 'blocked')
        self.assertFalse(invoice.saas_delivered_at)
        return invoice

    def _release_jobs(self, reason=None):
        jobs = self._jobs('_do_release_upgrade')
        if reason:
            jobs = jobs.filtered(lambda j: j.idempotency_key.startswith('upgrade-release:%s:' % reason))
        return jobs

    def _run_release(self, reason='deadline'):
        job = self._release_jobs(reason).filtered(lambda j: j.state == 'pending')
        self.assertEqual(len(job), 1, "one %s release queued" % reason)
        job.write({'run_after': fields.Datetime.now() - timedelta(seconds=1)})
        self.env.flush_all()
        self.Job._run_one(job.id)
        self.env.invalidate_all()
        self.assertEqual(job.state, 'done', job.error)

    def _last_package(self):
        return self.driver.set_package.call_args.args[1]

    def _unsafe_calls(self):
        return {c[0] for c in self.driver.method_calls} - SAFE_CALLS

    def _activities(self, xmlid):
        return self.env['mail.activity'].sudo().search([
            ('res_model', '=', 'saas.instance'), ('res_id', '=', self.inst.id),
            ('activity_type_id', '=', self.env.ref(xmlid).id)])

    # ------------------------------------------------------------ tests
    def test_t2_release_is_queued_at_the_deadline(self):
        invoice = self._upgrade()
        job = self._release_jobs('deadline')
        self.assertEqual(len(job), 1)
        self.assertEqual(job.run_after, self.inst.upgrade_deadline_at)
        self.inst._advance_change(invoice)
        self.env['saas.instance']._cron_apply_paid_pending_changes()
        self.assertEqual(len(self._release_jobs()), 1, "never doubled")

    def test_unschedulable_at_t2_is_released_and_nothing_is_charged(self):
        self.inst._grant_wallet_credit(5.0, origin='manual', reason='test')
        wallet = self.inst._wallet()
        before = wallet.balance
        invoice = self._stuck_upgrade()
        tx = invoice.transaction_ids
        self._run_release()
        self.assertEqual(tx.state, 'cancel', "authorization voided")
        self.assertEqual(invoice.state, 'cancel')
        self.assertTrue(invoice.saas_released_at)
        self.assertAlmostEqual(wallet.balance, before, 2, msg="wallet hold returned")
        self.assertFalse(tx.payment_id, "nothing captured")
        self.assertEqual(self.inst.plan_id, self.small, "billing never switched")
        self.assertFalse(self.inst.pending_plan_id)
        self.assertEqual(self.inst.upgrade_customer_status, 'released')
        status = self.inst._upgrade_status()
        self.assertFalse(status['charged'])
        self.assertFalse(status['refunded'])
        # Withdrawn to the paid package; nothing was in effect, nothing kept.
        self.assertEqual(self._last_package(), self.old_res)
        self.assertFalse(self.inst.package_retained)
        self.assertIn("We couldn't complete your upgrade in time. You haven't been charged. "
                      "Your current plan is unchanged. You can try again.", self._bodies())
        self.assertFalse(self._unsafe_calls(), "the service was never stopped or restarted")
        # Handed to the on-call user (C6.5), and the T2 fallback check adds nothing.
        self.assertEqual(len(self._activities(RELEASED_TYPE)), 1)
        self.assertIn('upgrade_released', self.alerts)
        self.inst._do_escalate_upgrade(invoice.id, 'deadline')
        self.assertFalse(self._activities('saas_billing.mail_act_upgrade_past_deadline'))

    def test_withdrawn_package_stays_withdrawn(self):
        self._stuck_upgrade()
        self._run_release()
        # Capacity arrives later (a new node): the old package is what verifies.
        self.driver.package_rollout.return_value = ('applied', None, '')
        self.inst._verify_package()
        self.assertEqual(self.inst.package_status, 'in_sync')
        self.assertEqual(json.loads(self.inst.package_applied), self.old_res)
        self.assertEqual(self.inst._package_plan(), self.small)
        self.assertNotIn('Upgrade complete', self._bodies())

    def test_parts_in_effect_stay_in_place_and_count_in_capacity(self):
        self._stuck_upgrade()
        new_res = self.inst._k8s_plan_resources()   # the funded package
        self.driver.package_in_effect.return_value = {
            'odoo': False, 'database': True, 'volume_gi': 20}
        server = self.fx['server']
        server.invalidate_recordset(['allocated_cpu'])
        cpu_before = server.allocated_cpu
        self._run_release()
        sent = self._last_package()
        for key in ('db_cpu_limit', 'db_cpu_request', 'db_mem_limit', 'db_mem_request'):
            self.assertEqual(sent[key], new_res[key], "the resized database is never downsized")
        for key in ('cpu_limit', 'mem_limit', 'workers'):
            self.assertEqual(sent[key], self.old_res[key], "the Odoo rollout is withdrawn")
        self.assertEqual(sent['filestore_size'], '20Gi', "a grown volume never shrinks")
        new_pkg, old_pkg = self.big._package(storage_gb=20), self.small._package(storage_gb=10)
        self.assertEqual(self.inst.package_retained_cpu_m, new_pkg['db_cpu_m'] - old_pkg['db_cpu_m'])
        server.invalidate_recordset(['allocated_cpu'])
        self.assertAlmostEqual(server.allocated_cpu - cpu_before,
                               self.inst.package_retained_cpu_m / 1000.0, 3)
        # Verifying the fallback is not an "upgrade complete".
        self.driver.package_rollout.return_value = ('applied', None, '')
        self.inst._verify_package()
        self.assertEqual(self.inst.package_status, 'in_sync')
        self.assertNotIn('Upgrade complete', self._bodies())
        # The next package change replaces the kept parts.
        self.inst.plan_id = self.big
        self.assertFalse(self.inst._package_retained_overlay())
        self.inst._package_applied_ok(self.inst._package_key(self.inst._k8s_plan_resources()))
        self.assertFalse(self.inst.package_retained)
        self.assertEqual(self.inst.package_retained_cpu_m, 0)

    def test_a_delivered_upgrade_is_captured_not_released(self):
        invoice = self._stuck_upgrade()
        self.driver.package_rollout.return_value = ('applied', None, '')   # capacity freed
        self._run_release()
        self.assertTrue(invoice.saas_delivered_at)
        self.assertFalse(invoice.saas_released_at)
        self.assertEqual(invoice.transaction_ids.state, 'authorized')
        self._run('_do_capture_change')
        self.assertEqual(invoice.transaction_ids.state, 'done')

    def test_staff_release_now(self):
        invoice = self._stuck_upgrade()
        clerk = self.env['res.users'].sudo().create({
            'name': 'Clerk', 'login': 'clerk-c62',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])]})
        with self.assertRaises(AccessError):
            self.inst.with_user(clerk).action_release_upgrade()
        self.inst.action_release_upgrade()
        self._run_release('staff')
        self.assertTrue(invoice.saas_released_at)
        self.assertEqual(invoice.transaction_ids.state, 'cancel')
        self.assertIn("Your upgrade was cancelled. You haven't been charged.", self._bodies())
        self.assertFalse(self._activities(RELEASED_TYPE), "staff did it: no hand-over needed")
        self._run_release('deadline')   # the T2 job later finds nothing to do
        self.assertEqual(len(invoice.transaction_ids), 1)

    def test_authorization_voided_outside_stops_the_upgrade(self):
        invoice = self._stuck_upgrade()
        tx = invoice.transaction_ids
        tx._set_canceled(state_message='voided at the provider')
        self._run_release('authorization_lost')
        self.assertTrue(invoice.saas_released_at)
        self.assertEqual(invoice.state, 'cancel')
        self.assertEqual(self._last_package(), self.old_res)
        self.assertIn('Your payment authorization for the upgrade was cancelled', self._bodies())

    def test_own_void_is_not_a_lost_authorization(self):
        self._stuck_upgrade()
        self._run_release()
        self.assertFalse(self._release_jobs('authorization_lost'))

    def test_repeats_and_a_crash_give_one_void_and_one_wallet_return(self):
        self.inst._grant_wallet_credit(5.0, origin='manual', reason='test')
        wallet = self.inst._wallet()
        before = wallet.balance
        invoice = self._stuck_upgrade()
        Inst = type(self.inst)
        with patch.object(Inst, '_withdraw_package', side_effect=RuntimeError('worker killed')):
            with self.assertRaises(RuntimeError), self.env.cr.savepoint():
                self.inst._do_release_upgrade(invoice.id, 'deadline')
        self.env.invalidate_all()
        self.assertFalse(invoice.saas_released_at, "a crash releases nothing")
        self.assertEqual(invoice.transaction_ids.state, 'authorized')
        self._run_release()
        self.inst._do_release_upgrade(invoice.id, 'deadline')
        self.inst._do_release_upgrade(invoice.id, 'staff')
        self.assertEqual(invoice.transaction_ids.mapped('state'), ['cancel'])
        self.assertAlmostEqual(wallet.balance, before, 2)
        refunds = self.env['saas.wallet.transaction'].sudo().search_count([
            ('move_id', '=', invoice.id), ('kind', '=', 'refund')])
        self.assertEqual(refunds, 1)
        self.assertEqual(self._bodies().count("We couldn't complete your upgrade"), 1)

    def test_money_already_taken_goes_back_to_the_wallet_once(self):
        self.provider.capture_manually = False
        self.inst._grant_wallet_credit(5.0, origin='manual', reason='test')
        wallet = self.inst._wallet()
        before = wallet.balance
        invoice = self._stuck_upgrade()
        held = round(before - wallet.balance, 2)
        self.assertIn(invoice.payment_state, ('paid', 'in_payment'))
        paid = invoice.amount_total
        self.assertGreater(paid, 0)
        self._run_release()
        self.assertTrue(invoice.saas_released_at)
        self.assertAlmostEqual(wallet.balance, before + paid, 2,
                               msg="the wallet part and the card part are back")
        self.assertTrue(self.inst._upgrade_status()['refunded'])
        self.assertIn('%.2f %s is back in your wallet' % (held + paid, invoice.currency_id.name),
                      self._bodies())
        self.inst._do_release_upgrade(invoice.id, 'staff')
        self.assertAlmostEqual(wallet.balance, before + paid, 2, msg="never twice")
        self.assertEqual(self.inst.plan_id, self.small)

    def test_wallet_covered_upgrade_is_given_back(self):
        self.inst.saas_payment_method_id = False
        self.method.active = False
        self.inst._grant_wallet_credit(1000.0, origin='manual', reason='test')
        wallet = self.inst._wallet()
        before = wallet.balance
        invoice = self._stuck_upgrade()
        self.assertEqual(invoice.amount_total, 0)
        self.assertLess(wallet.balance, before)
        self._run_release()
        self.assertAlmostEqual(wallet.balance, before, 2)
        self.assertTrue(invoice.saas_released_at)

    def test_t2_beyond_the_authorization_is_released_before_it_lapses(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'saas_master.upgrade_deadline_minutes', str(200 * 60))
        invoice = self._upgrade()
        self.assertFalse(self._release_jobs('deadline'))
        job = self._release_jobs('capture_deadline')
        self.assertEqual(job.run_after, invoice.saas_capture_deadline - timedelta(hours=1))
