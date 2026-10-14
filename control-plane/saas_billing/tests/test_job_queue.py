"""C3.1: a paid invoice queues a durable deploy job inside the payment
transaction — never a thread that a restart can lose."""
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPaidDeployJob(TransactionCase):

    def setUp(self):
        super().setUp()
        env = self.env
        icp = env['ir.config_parameter'].sudo()
        icp.set_param('saas_master.hosting_worker_price', '10.0')
        icp.set_param('saas_master.hosting_storage_price_per_gb', '0.3')
        self.Job = env['saas.job'].sudo()
        product = env['saas.product'].sudo().search([('is_hosting', '=', True)], limit=1) \
            or env['saas.product'].sudo().create({
                'name': 'JQ Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = env['saas.plan'].sudo().create({
            'name': 'JQ Plan', 'is_custom': True, 'workers': 2, 'storage_limit': 20,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 50.0, 'yearly_price': 480.0,
            'currency_id': env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [product.id])]})
        self.product = product
        self.partner = env['res.partner'].sudo().create({'name': 'JQ Cust'})
        self.domain = env['saas.based.domain'].sudo().search([], limit=1) \
            or env['saas.based.domain'].sudo().create({'name': 'jq.example.com'})
        self.inst = self._mk('jqprod', state='draft')
        self.inst.action_confirm_and_bill()
        self.assertEqual(self.inst.state, 'pending_payment')
        self.invoice = self.inst.sale_order_id.invoice_ids
        self.assertEqual(len(self.invoice), 1)

    def _mk(self, sub, state, **vals):
        return self.env['saas.instance'].sudo().create(dict({
            'subdomain': sub, 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': False, 'state': state,
        }, **vals))

    def _forbid_commit(self):
        """The payment transaction must never be committed by the handler."""
        p = patch.object(self.env.cr, 'commit',
                         side_effect=AssertionError('payment txn committed'))
        p.start()
        self.addCleanup(p.stop)

    def _no_commit(self):
        # The job executor commits per step; the test cursor can't.
        for op in ('commit', 'rollback'):
            p = patch.object(self.env.cr, op)
            p.start()
            self.addCleanup(p.stop)

    def _deploy_jobs(self, inst):
        return self.Job.search([
            ('model', '=', 'saas.instance'), ('res_id', '=', inst.id),
            ('method', '=', '_do_deploy_after_payment')])

    def _pay(self, invoice):
        invoice.write({'payment_state': 'paid'})
        invoice._saas_check_instance_payment()

    # ------------------------------------------------------------- enqueue
    def test_payment_queues_one_job_in_the_payment_transaction(self):
        self._forbid_commit()
        self._pay(self.invoice)
        self.assertEqual(self.inst.state, 'paid')
        job = self._deploy_jobs(self.inst)
        self.assertEqual(len(job), 1)
        self.assertEqual(job.state, 'pending')
        self.assertEqual(job.idempotency_key,
                         'deploy:inv%s:inst%s' % (self.invoice.id, self.inst.id))
        self.assertEqual(job.channel, 'deploy')
        self.assertEqual(job.lock_key, 'instance:%s' % self.inst.id)
        self.assertTrue(job.idempotent)
        self.assertEqual(job.on_error, '_on_background_error')

    def test_duplicate_paid_webhook_queues_one_job(self):
        self._forbid_commit()
        self._pay(self.invoice)
        self._pay(self.invoice)
        self.assertEqual(len(self._deploy_jobs(self.inst)), 1)

    def test_enqueue_is_idempotent_on_the_invoice_key(self):
        first = self.inst._enqueue_deploy_after_payment(self.invoice)
        again = self.inst._enqueue_deploy_after_payment(self.invoice)
        self.assertEqual(first, again)

    def test_rolled_back_payment_queues_nothing(self):
        self._forbid_commit()
        with self.assertRaises(RuntimeError):
            with self.env.cr.savepoint():
                self._pay(self.invoice)
                self.assertEqual(len(self._deploy_jobs(self.inst)), 1)
                raise RuntimeError('payment transaction aborted')
        self.env.invalidate_all()
        self.assertFalse(self._deploy_jobs(self.inst))
        self.assertEqual(self.inst.state, 'pending_payment')

    def test_paid_environment_queues_one_job(self):
        self._forbid_commit()
        env_invoice = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_line_ids': [(0, 0, {'name': 'stg', 'quantity': 1,
                                         'price_unit': 5.0})]})
        env_invoice.action_post()
        child = self._mk('jqprod-stg', state='pending_payment',
                         environment='staging', parent_id=self.inst.id,
                         env_pending_invoice_id=env_invoice.id)
        self._pay(env_invoice)
        self._pay(env_invoice)
        self.assertEqual(child.state, 'paid')
        self.assertFalse(child.env_pending_invoice_id)
        job = self._deploy_jobs(child)
        self.assertEqual(len(job), 1)
        self.assertEqual(job.idempotency_key,
                         'deploy:inv%s:inst%s' % (env_invoice.id, child.id))

    # ----------------------------------------------------------------- run
    def test_job_run_deploys_the_instance(self):
        self._pay(self.invoice)
        job = self._deploy_jobs(self.inst)
        deployed = []
        self._no_commit()
        with patch.object(type(self.inst), 'action_deploy',
                          lambda rec: deployed.append(rec.id)):
            self.Job._run_one(job.id)
        self.assertEqual(deployed, [self.inst.id])
        self.assertEqual(job.state, 'done')

    def test_rerun_after_deploy_started_is_a_noop(self):
        # A job requeued by the reaper after its work committed must not
        # call action_deploy again (it would raise and fail the instance).
        self._pay(self.invoice)
        job = self._deploy_jobs(self.inst)
        self.inst.state = 'provisioning'
        deployed = []
        self._no_commit()
        with patch.object(type(self.inst), 'action_deploy',
                          lambda rec: deployed.append(rec.id)):
            self.Job._run_one(job.id)
        self.assertFalse(deployed)
        self.assertEqual(job.state, 'done')
        self.assertEqual(self.inst.state, 'provisioning')

    def test_terminal_failure_marks_the_instance_failed(self):
        self._pay(self.invoice)
        job = self._deploy_jobs(self.inst)
        job.max_attempts = 1

        def boom(rec):
            raise ValueError('cluster unreachable')

        self._no_commit()
        with patch.object(type(self.inst), 'action_deploy', boom):
            self.Job._run_one(job.id)
        self.assertEqual(job.state, 'failed')
        self.assertEqual(self.inst.state, 'failed')
        self.assertIn('cluster unreachable', self.inst.last_error)
