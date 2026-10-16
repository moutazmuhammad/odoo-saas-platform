"""C3.1/C3.2: a paid invoice queues a durable deploy or plan-change job
inside the payment transaction — never a thread that a restart can lose."""
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


class _PaidJobCase(TransactionCase):

    def setUp(self):
        super().setUp()
        env = self.env
        icp = env['ir.config_parameter'].sudo()
        icp.set_param('saas_master.hosting_worker_price', '10.0')
        icp.set_param('saas_master.hosting_storage_price_per_gb', '0.3')
        self.Job = env['saas.job'].sudo()
        self.product = env['saas.product'].sudo().search([('is_hosting', '=', True)], limit=1) \
            or env['saas.product'].sudo().create({
                'name': 'JQ Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self._mk_plan('JQ Plan', 2, 50.0)
        self.partner = env['res.partner'].sudo().create({'name': 'JQ Cust'})
        self.domain = env['saas.based.domain'].sudo().search([], limit=1) \
            or env['saas.based.domain'].sudo().create({'name': 'jq.example.com'})

    def _mk_plan(self, name, workers, price):
        return self.env['saas.plan'].sudo().create({
            'name': name, 'is_custom': True, 'workers': workers,
            'storage_limit': 20, 'cpu_limit': 1.0, 'ram_limit': '1g',
            'price': price, 'yearly_price': price * 10,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})

    def _mk(self, sub, state, **vals):
        return self.env['saas.instance'].sudo().create(dict({
            'subdomain': sub, 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': False, 'state': state,
        }, **vals))

    def _mk_invoice(self, amount=5.0):
        inv = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_line_ids': [(0, 0, {'name': 'jq', 'quantity': 1,
                                         'price_unit': amount})]})
        inv.action_post()
        return inv

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

    def _jobs(self, inst, method):
        return self.Job.search([
            ('model', '=', 'saas.instance'), ('res_id', '=', inst.id),
            ('method', '=', method)])

    def _pay(self, invoice):
        invoice.write({'payment_state': 'paid'})
        invoice._saas_check_instance_payment()


@tagged('post_install', '-at_install')
class TestPaidDeployJob(_PaidJobCase):

    def setUp(self):
        super().setUp()
        self.inst = self._mk('jqprod', state='draft')
        self.inst.action_confirm_and_bill()
        self.assertEqual(self.inst.state, 'pending_payment')
        self.invoice = self.inst.sale_order_id.invoice_ids
        self.assertEqual(len(self.invoice), 1)

    def _deploy_jobs(self, inst):
        return self._jobs(inst, '_do_deploy_after_payment')

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
        env_invoice = self._mk_invoice()
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


@tagged('post_install', '-at_install')
class TestPaidPlanChangeJob(_PaidJobCase):

    def setUp(self):
        super().setUp()
        self.big = self._mk_plan('JQ Big', 4, 90.0)
        self.inst = self._mk('jqchange', state='running')
        self.invoice = self._mk_invoice(40.0)
        self.inst.write({'pending_plan_id': self.big.id,
                         'pending_billing_period': 'monthly',
                         'pending_change_invoice_id': self.invoice.id})
        resize = patch.object(type(self.inst), '_update_container_resources',
                              lambda rec: None)
        resize.start()
        self.addCleanup(resize.stop)

    def _change_jobs(self):
        return self._jobs(self.inst, '_do_apply_paid_plan_change')

    def _deliver_jobs(self):
        return self._jobs(self.inst, '_do_deliver_change')

    def _pay_and_deliver(self):
        """Pay, then run the delivery step (C6.10): this instance has no
        cluster, so it is delivered at once and the billing switch is queued."""
        self._pay(self.invoice)
        self._no_commit()
        self._run(self._deliver_jobs())
        return self._change_jobs()

    def _run(self, job):
        # _run_one claims with raw SQL: flush ORM writes (requeues) first.
        self.env.flush_all()
        self.Job._run_one(job.id)
        job.invalidate_recordset()
        self.inst.invalidate_recordset()

    # ------------------------------------------------------------- enqueue
    def test_payment_queues_one_job_in_the_payment_transaction(self):
        self._forbid_commit()
        self._pay(self.invoice)
        job = self._deliver_jobs()
        self.assertEqual(len(job), 1, "delivery first; billing switches on delivery (C6.10)")
        self.assertEqual(job.idempotency_key,
                         'deliver:inv%s:inst%s' % (self.invoice.id, self.inst.id))
        self.assertEqual(job.args_json, '[%d]' % self.invoice.id)
        self.assertEqual(job.lock_key, 'instance:%s' % self.inst.id)
        self.assertFalse(self._change_jobs())
        self.assertEqual(self.invoice.saas_capture_mode, 'immediate')
        self.assertEqual(self.inst.plan_id, self.plan, "applied by the jobs, not inline")

    def test_delivery_queues_the_billing_switch(self):
        job = self._pay_and_deliver()
        self.assertEqual(len(job), 1)
        self.assertEqual(job.idempotency_key,
                         'planchange:inv%s:inst%s' % (self.invoice.id, self.inst.id))
        self.assertEqual(job.on_error, '_on_plan_change_job_error')
        self.assertTrue(self.invoice.saas_delivered_at)

    def test_duplicate_paid_webhook_queues_one_job(self):
        self._forbid_commit()
        self._pay(self.invoice)
        self._pay(self.invoice)
        self.assertEqual(len(self._deliver_jobs()), 1)

    # ----------------------------------------------------------------- run
    def test_job_applies_the_change_exactly_once(self):
        job = self._pay_and_deliver()
        self._run(job)
        self.assertEqual(job.state, 'done')
        self.assertEqual(self.inst.plan_id, self.big)
        self.assertFalse(self.inst.pending_plan_id)
        # A re-run (reaper requeue after its commit) changes nothing.
        job.write({'state': 'pending', 'attempts': 0})
        with patch.object(type(self.inst), '_apply_pending_plan_change',
                          side_effect=AssertionError('applied twice')):
            self._run(job)
        self.assertEqual(job.state, 'done')

    def test_trial_upgrade_runs_the_upgrade_method(self):
        self.inst.is_trial = True
        job = self._pay_and_deliver()
        calls = []
        with patch.object(type(self.inst), '_apply_pending_upgrade',
                          lambda rec: calls.append('upgrade')), \
                patch.object(type(self.inst), '_apply_pending_plan_change',
                             lambda rec: calls.append('change')):
            self._run(job)
        self.assertEqual(calls, ['upgrade'])

    def test_superseded_change_is_not_applied(self):
        job = self._pay_and_deliver()
        # The customer replaced the request before the job ran.
        self.inst.pending_change_invoice_id = self._mk_invoice(60.0)
        self._run(job)
        self.assertEqual(job.state, 'done')
        self.assertEqual(self.inst.plan_id, self.plan)
        self.assertEqual(self.inst.pending_plan_id, self.big)

    def test_worker_killed_mid_apply_completes_after_restart(self):
        job = self._pay_and_deliver()
        # The worker claimed the job and died: no heartbeat since.
        job.write({'state': 'running', 'attempts': 1,
                   'last_heartbeat': fields.Datetime.now() - timedelta(hours=1)})
        self._no_commit()
        self.Job._cron_reap_jobs()
        self.assertEqual(job.state, 'pending')
        self._run(job)
        self.assertEqual(job.state, 'done')
        self.assertEqual(self.inst.plan_id, self.big)

    def test_terminal_failure_keeps_state_and_is_visible(self):
        job = self._pay_and_deliver()
        job.max_attempts = 1

        def boom(rec):
            raise ValueError('restart failed')

        self._no_commit()
        with patch.object(type(self.inst), '_apply_pending_plan_change', boom):
            self._run(job)
        self.assertEqual(job.state, 'failed')
        self.assertEqual(self.inst.state, 'running')
        self.assertEqual(self.inst.pending_plan_id, self.big, "still pending for a retry")
        self.assertIn('restart failed', self.inst.last_error)


@tagged('post_install', '-at_install')
class TestPaidPlanChangeConverges(_PaidJobCase):
    """C6.1: a paid upgrade whose package patch fails stays paid and
    desired; the reconciler applies it once the fault clears."""

    def setUp(self):
        super().setUp()
        self.big = self._mk_plan('JQ Big', 4, 90.0)
        server = self.env['saas.server'].sudo().create(
            {'name': 'jq-srv', 'compute_driver': 'kubernetes'})
        self.inst = self._mk('jqconv', state='running', docker_server_id=server.id)
        self.invoice = self._mk_invoice(40.0)
        self.inst.write({'pending_plan_id': self.big.id,
                         'pending_billing_period': 'monthly',
                         'pending_change_invoice_id': self.invoice.id})
        self.applied = []
        self.fault = True

        def set_package(handle, res):
            if self.fault:
                raise RuntimeError('cluster unavailable')
            self.applied.append(res)

        driver = type('Driver', (), {
            'set_package': staticmethod(set_package),
            # C6.3: the rollout verifies and the delivery verdict passes.
            'package_rollout': staticmethod(lambda handle, res, since=None: ('applied', None, '')),
            'delivery_status': staticmethod(lambda handle: {'ready': True, 'failure': None, 'detail': ''}),
            'check_public_url': staticmethod(lambda hostname: (True, '')),
        })()
        Instance = type(self.inst)
        for target, attr, fn in (
                (Instance, '_compute_driver', lambda rec, connection=None: driver),
                (Instance, '_compute_handle', lambda rec: rec.subdomain),
                (type(self.Job), '_spawn_worker', lambda rec: None),
                (type(self.Job), '_spawn_worker_after_commit', lambda rec: None)):
            p = patch.object(target, attr, fn)
            p.start()
            self.addCleanup(p.stop)
        self._no_commit()

    def _run(self, job):
        self.env.flush_all()
        self.Job._run_one(job.id)
        self.env.invalidate_all()

    def test_failed_patch_converges_after_the_fault_clears(self):
        self._pay(self.invoice)
        self._run(self._jobs(self.inst, '_do_deliver_change'))
        self.assertEqual(self.inst.pending_plan_id, self.big, "the paid change is kept")
        self.assertEqual(self.inst._package_plan(), self.big, "and is the cluster's target")
        self.assertEqual(self.inst.package_status, 'pending')
        want = self.inst._package_key(self.inst._k8s_plan_resources())
        self.assertEqual(self.inst.package_desired, want)
        self.assertNotEqual(self.inst.package_applied, want)
        self.assertFalse(self.applied)

        # C6.4: a paid change's failure schedules its own fast retry, at an
        # exact time (the reconciler would only re-find the same job).
        retry = self._jobs(self.inst, '_do_converge_package')
        self.assertEqual(len(retry), 1)
        self.assertEqual(retry.run_after, self.inst.package_next_retry)
        self.assertLessEqual(self.inst.package_next_retry,
                             fields.Datetime.now() + timedelta(minutes=1, seconds=5))
        self.fault = False
        retry.run_after = self.inst.package_next_retry = fields.Datetime.now() - timedelta(seconds=1)
        self.env['saas.instance']._converge_packages()
        self.assertEqual(len(self._jobs(self.inst, '_do_converge_package')), 1)
        self._run(retry)
        self.assertEqual(self.inst.package_status, 'applying', "accepted, then verified (C6.3)")
        self.inst._verify_package()
        self.assertEqual(self.inst.package_status, 'in_sync')
        self.assertEqual(self.inst.package_applied, want)
        self.assertEqual([r['workers'] for r in self.applied],
                         [self.inst._k8s_plan_resources()['workers']])
        # Delivered (C6.10): the billing switch follows; nothing to re-apply.
        self.assertTrue(self.invoice.saas_delivered_at)
        self._run(self._jobs(self.inst, '_do_apply_paid_plan_change'))
        self.assertEqual(self.inst.plan_id, self.big)
        self.assertEqual(self.inst.package_status, 'in_sync')
