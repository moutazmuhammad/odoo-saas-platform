"""C6.10: authorize at purchase, capture on verified delivery. Run against
Odoo's demo payment provider in manual-capture mode with a saved demo card,
so authorization, capture, void and post-processing are Odoo's own."""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from .test_upgrade_gate import _mk_instance, _setup_fixture


@tagged('post_install', '-at_install')
class TestCaptureOnDelivery(TransactionCase):

    def setUp(self):
        super().setUp()
        env = self.env
        if not env['ir.module.module'].search_count(
                [('name', '=', 'payment_demo'), ('state', '=', 'installed')]):
            self.skipTest('payment_demo is not installed in this database')
        self.provider = env.ref('payment.payment_provider_demo').sudo()
        journal = self.provider.journal_id or env['account.journal'].search(
            [('type', '=', 'bank'), ('company_id', '=', env.company.id)], limit=1)
        self.provider.write({'state': 'test', 'capture_manually': True, 'journal_id': journal.id})
        self.fx = _setup_fixture(env, 'cap')
        self.small, self.big = self.fx['small'], self.fx['big']
        self.inst = _mk_instance(env, self.fx, 'captest')
        token = env['payment.token'].sudo().create({
            'provider_id': self.provider.id,
            'payment_method_id': env.ref('payment_demo.payment_method_demo').id,
            'partner_id': self.fx['partner'].id, 'provider_ref': 'demo-captest',
            'payment_details': '4242', 'demo_simulated_state': 'done'})
        self.method = env['saas.payment.method'].sudo().create({
            'partner_id': self.fx['partner'].id, 'provider_id': self.provider.id,
            'token_id': token.id, 'is_default': True, 'display_label': 'Demo 4242'})
        self.inst.saas_payment_method_id = self.method
        self.Job = env['saas.job'].sudo()
        self.driver = MagicMock()
        self.driver.cluster_facts.return_value = {
            'node_max_cpu_m': 16000, 'node_max_mem_mi': 65536, 'volume_expansion': True}
        self.driver.package_rollout.return_value = ('applied', None, '')
        self.driver.delivery_status.return_value = {'ready': True, 'failure': None, 'detail': ''}
        self.driver.check_public_url.return_value = (True, '')
        self.alerts = []
        for target, attr, value in (
                (type(self.inst), '_compute_driver', lambda rec, connection=None: self.driver),
                (type(self.inst), '_compute_handle', lambda rec: rec.subdomain),
                (type(env['saas.alert']), '_notify',
                 lambda rec, event, message, **kw: self.alerts.append(event)),
                (type(self.Job), '_spawn_worker', lambda rec: None),
                (type(self.Job), '_spawn_worker_after_commit', lambda rec: None),
                (env.cr, 'commit', lambda: None),
                (env.cr, 'rollback', lambda: None)):
            p = patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        env.cr.cache.pop(type(self.inst)._GATE_PROBE_CACHE, None)
        self.inst._update_container_resources()
        self.inst._verify_package()

    # ------------------------------------------------------------ helpers
    def _jobs(self, method):
        return self.Job.search([('model', '=', 'saas.instance'), ('res_id', '=', self.inst.id),
                                ('method', '=', method)], order='id')

    def _run(self, method):
        job = self._jobs(method).filtered(lambda j: j.state == 'pending')
        self.assertTrue(job, 'expected a pending %s job' % method)
        job.write({'run_after': fields.Datetime.now() - timedelta(seconds=1)})
        self.env.flush_all()
        for j in job:
            self.Job._run_one(j.id)
        self.env.invalidate_all()

    def _bodies(self):
        return ' '.join(self.inst.message_ids.mapped('body'))

    def _upgrade(self):
        self.inst.action_request_plan_change(self.big.id)
        return self.inst.pending_change_invoice_id

    def _deliver(self):
        self._run('_do_deliver_change')
        self._run('_do_verify_package')

    # ------------------------------------------------------------ tests
    def test_authorized_then_delivered_then_captured_once(self):
        invoice = self._upgrade()
        tx = invoice.transaction_ids
        self.assertEqual(tx.state, 'authorized', "saved card authorized, not charged")
        self.assertEqual(invoice.saas_capture_mode, 'manual')
        self.assertTrue(invoice.saas_funds_secured_at)
        self.assertAlmostEqual(
            (invoice.saas_capture_deadline - invoice.saas_funds_secured_at).total_seconds() / 3600,
            self.inst._CAPTURE_WINDOW_HOURS, delta=0.01)
        self.assertNotIn(invoice.payment_state, ('paid', 'in_payment'))
        self.assertEqual(self.inst.plan_id, self.small, "billing unchanged before delivery")
        self.assertEqual(self.inst._package_plan(), self.big, "the cluster gets the paid plan")
        self.assertIn("you'll be charged when it's complete", self._bodies())

        self._run('_do_deliver_change')
        sent = self.driver.set_package.call_args.args[1]
        self.assertEqual(sent['workers'], self.big._package()['workers'])
        self.assertEqual(self.inst.package_status, 'applying')
        self.assertEqual(tx.state, 'authorized', "nothing captured before verified delivery")
        self.assertFalse(self._jobs('_do_capture_change'))

        self._run('_do_verify_package')
        self.assertTrue(invoice.saas_delivered_at)
        self.assertEqual(tx.state, 'authorized')
        self._run('_do_capture_change')
        self.assertEqual(tx.state, 'done')
        self.assertIn(invoice.payment_state, ('paid', 'in_payment'))
        self.assertEqual(self.inst.plan_id, self.small, "billing switches in its own step")
        self._run('_do_apply_paid_plan_change')
        self.assertEqual(self.inst.plan_id, self.big)
        self.assertFalse(self.inst.pending_plan_id)
        self.assertIn('Payment complete', self._bodies())
        self.assertEqual(self.inst.package_status, 'in_sync')

        # A repeated notice or a re-run capture never captures twice.
        self.inst._advance_change(invoice)
        self.inst._do_capture_change(invoice.id)
        self.assertEqual(len(invoice.transaction_ids), 1)
        self.assertEqual(len(invoice.transaction_ids.payment_id), 1)

    def test_two_authorizations_are_never_both_captured(self):
        invoice = self._upgrade()
        with patch.object(type(self.env['saas.instance']), '_gate_collection',
                          lambda model, ids: None):
            self.env['saas.payment.gateway'].charge(self.method, invoice)   # a duplicate attempt
        self.assertEqual(invoice.transaction_ids.mapped('state'), ['authorized', 'authorized'])
        self._deliver()
        self._run('_do_capture_change')
        states = sorted(invoice.transaction_ids.mapped('state'))
        self.assertEqual(states, ['cancel', 'done'], "one captured, the other voided")
        self.assertEqual(len(invoice.transaction_ids.payment_id), 1)

    def test_duplicate_authorization_notice_queues_one_delivery(self):
        invoice = self._upgrade()
        self.inst._advance_change(invoice)
        self.inst._advance_change(invoice)
        self.assertEqual(len(self._jobs('_do_deliver_change')), 1)

    def test_wallet_covered_upgrade_holds_then_switches_on_delivery(self):
        self.inst.saas_payment_method_id = False
        self.method.active = False
        self.inst._grant_wallet_credit(1000.0, origin='manual', reason='test')
        invoice = self._upgrade()
        self.assertEqual(invoice.amount_total, 0)
        self.assertEqual(invoice.saas_capture_mode, 'immediate')
        self.assertFalse(invoice.transaction_ids)
        self.assertEqual(self.inst.plan_id, self.small)
        self._deliver()
        self._run('_do_apply_paid_plan_change')
        self.assertEqual(self.inst.plan_id, self.big)

    def test_provider_without_manual_capture_charges_at_once_and_flags_it(self):
        self.provider.capture_manually = False
        invoice = self._upgrade()
        tx = invoice.transaction_ids
        self.assertEqual(tx.state, 'done')
        tx._post_process()                       # Odoo's post-processing cron
        self.assertEqual(invoice.saas_capture_mode, 'immediate')
        self.assertEqual(self.inst.plan_id, self.small, "still waits for delivery")
        self._deliver()
        self._run('_do_apply_paid_plan_change')
        self.assertEqual(self.inst.plan_id, self.big)
        self.assertEqual(len(invoice.transaction_ids), 1, "no second charge")

    def test_failed_capture_keeps_the_service_and_dunning_applies(self):
        invoice = self._upgrade()
        self._deliver()
        with patch.object(type(invoice.transaction_ids), '_send_capture_request',
                          side_effect=RuntimeError('provider down')):
            self._run('_do_capture_change')
        self.assertIn('capture_failed', self.alerts)
        self.assertNotIn(invoice.payment_state, ('paid', 'in_payment'))
        self.assertEqual(self.inst.package_status, 'in_sync', "the service is untouched")
        self.assertEqual(self.inst._package_plan(), self.big)
        self.assertFalse(self.inst._is_optional_invoice(invoice), "now a normal invoice")

    def test_other_authorized_payments_are_captured_at_once(self):
        invoice = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice', 'partner_id': self.fx['partner'].id,
            'invoice_line_ids': [(0, 0, {'name': 'renewal', 'quantity': 1, 'price_unit': 9.0})]})
        invoice.action_post()
        state, _msg = self.env['saas.payment.gateway'].charge(self.method, invoice)
        self.assertEqual(state, 'done')
        self.assertEqual(invoice.transaction_ids.state, 'done')

    def test_release_voids_and_returns_the_wallet(self):
        self.inst._grant_wallet_credit(5.0, origin='manual', reason='test')
        wallet = self.inst._wallet()
        before = wallet.balance
        invoice = self._upgrade()
        self.assertLess(wallet.balance, before, "wallet part held on the invoice")
        tx = invoice.transaction_ids
        self.assertTrue(self.inst._release_change_funds(invoice))
        self.assertEqual(tx.state, 'cancel')
        self.assertEqual(invoice.state, 'cancel')
        self.assertAlmostEqual(wallet.balance, before, 2)
        self.assertFalse(self.inst.pending_plan_id)
        self.assertTrue(invoice.saas_released_at)
        self.assertTrue(self.inst._release_change_funds(invoice), "idempotent")

    def test_delivered_change_is_never_released(self):
        invoice = self._upgrade()
        self._deliver()
        with self.assertRaises(UserError):
            self.inst._release_change_funds(invoice)

    def test_charged_fallback_needs_a_refund_not_a_void(self):
        self.provider.capture_manually = False
        invoice = self._upgrade()
        invoice.transaction_ids._post_process()
        self.assertFalse(self.inst._release_change_funds(invoice))

    def test_funded_change_blocks_cancel_payment_again_and_trial_expiry(self):
        invoice = self._upgrade()
        self.inst._cancel_pending_upgrade()
        self.assertEqual(self.inst.pending_change_invoice_id, invoice, "never cancelled once funded")
        with self.assertRaises(UserError):
            self.inst._gate_collection([invoice.id])
        self.assertTrue(self.inst._change_in_flight())
        partner = self.fx['partner']
        partner.write({'saas_trial_end_date': fields.Date.today() - timedelta(days=1),
                       'saas_hosting_trial_used': True})
        self.inst.is_trial = True
        self.env['saas.instance']._cron_check_trial_expiry()
        self.assertEqual(self.inst.state, 'running', "a paid trial upgrade is not suspended")
        self.assertEqual(self.inst.pending_change_invoice_id, invoice)

    def test_storage_purchase_is_captured_on_delivery(self):
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('saas_master.storage_block_gb', '10')
        icp.set_param('saas_master.storage_block_price', '2.0')
        invoice = self.inst.action_purchase_storage_block(1)
        self.assertEqual(invoice.transaction_ids.state, 'authorized')
        self.assertEqual(self.inst.extra_storage_blocks, 0)
        self._run('_do_deliver_change')
        self.assertEqual(self.driver.set_package.call_args.args[1]['filestore_size'], '20Gi')
        self._run('_do_verify_package')
        self._run('_do_capture_change')
        self.assertIn(invoice.payment_state, ('paid', 'in_payment'))
        self._run('_do_activate_paid_storage')
        self.assertEqual(self.inst.extra_storage_blocks, 1)
        self.assertEqual(self.inst.effective_storage_limit_gb, 20)
