"""C3.7: crash-safety suite. A paid deploy, a paid plan change, a suspend and
a cancel are each interrupted mid-effect — the worker "dies": its transaction
rolls back and the job stays ``running`` without a heartbeat — then the reaper
requeues them and a fresh run completes each exactly once.

The same flows run against a real SIGKILLed worker process in
``scripts/tests/crash_safety/run.sh`` (test server).
"""
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


class WorkerKilled(BaseException):
    """Not an Exception: the job's own error handling must not see it, the
    way it never sees a SIGKILL."""


@tagged('post_install', '-at_install')
class TestCrashSafety(TransactionCase):

    def setUp(self):
        super().setUp()
        env = self.env
        icp = env['ir.config_parameter'].sudo()
        icp.set_param('saas_master.hosting_worker_price', '10.0')
        icp.set_param('saas_master.hosting_storage_price_per_gb', '0.3')
        self.Job = env['saas.job'].sudo()
        self.Backup = env['saas.instance.backup'].sudo()
        self.product = env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or env['saas.product'].sudo().create(
            {'name': 'Cs Hosting', 'is_hosting': True, 'is_published': True})
        self.small = self._plan('Cs Small', 2, 50.0)
        self.big = self._plan('Cs Big', 4, 90.0)
        self.partner = env['res.partner'].sudo().create({'name': 'Cs Cust'})
        self.domain = env['saas.based.domain'].sudo().search([], limit=1) \
            or env['saas.based.domain'].sudo().create({'name': 'cs.example.com'})
        self.server = env['saas.server'].sudo().create(
            {'name': 'cs-srv', 'compute_driver': 'kubernetes'})
        self.version = env['saas.odoo.version'].sudo().search(
            [('docker_image', '!=', False), ('docker_image_tag', '!=', False)], limit=1) \
            or env['saas.odoo.version'].sudo().create(
                {'name': 'Cs 18', 'docker_image': 'odoo', 'docker_image_tag': '18.0'})
        self.starts = []        # (effect, subdomain) for every effect begun
        self.kill_at = set()    # effects at which the worker dies (once each)
        self._fake_effects()
        Job = type(self.Job)
        for target, name, fn in (
                (env.cr, 'commit', lambda: None), (env.cr, 'rollback', lambda: None),
                # Jobs only run where the test runs them, never in a thread.
                (Job, '_spawn_worker', lambda rec: None),
                (Job, '_spawn_worker_after_commit', lambda rec: None)):
            p = patch.object(target, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def _plan(self, name, workers, price):
        return self.env['saas.plan'].sudo().create({
            'name': name, 'is_custom': True, 'workers': workers, 'storage_limit': 20,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': price,
            'yearly_price': price * 10, 'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})

    def _mk(self, sub, state):
        return self.env['saas.instance'].sudo().create({
            'subdomain': sub, 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.small.id, 'docker_server_id': self.server.id,
            'odoo_version_id': self.version.id, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': False, 'state': state})

    def _fake_effects(self):
        """Fake the cluster/storage effects (as the harness worker does);
        each one is where the worker can die."""
        test = self

        def effect(name, subdomain):
            test.starts.append((name, subdomain))
            if name in test.kill_at:
                test.kill_at.discard(name)
                raise WorkerKilled(name)

        class Driver:
            def __init__(self, sub):
                self.sub = sub

            def stop(self, handle):
                effect('stop', self.sub)

            def destroy(self, handle, **kw):
                effect('destroy', self.sub)

            def exists(self, handle):
                return False

        def deploy_locked(rec):
            effect('deploy', rec.subdomain)
            rec.write({'state': 'running', 'pending_operation': False,
                       'pre_provisioning_state': False})
            return True

        def snapshot(backup_model, instance, *a, **kw):
            effect('snapshot', instance.subdomain)
            return backup_model.create({
                'instance_id': instance.id, 'name': 'fresh-final', 'state': 'done',
                'is_full_instance': True, 'format': 'operator', 'source': 'final',
                'bucket_path': 'backups/%s/fresh' % instance.subdomain})

        Instance = type(self.env['saas.instance'])
        Backup = type(self.Backup)
        for target, attr, fn in (
                (Instance, '_compute_driver', lambda rec, connection=None: Driver(rec.subdomain)),
                (Instance, '_compute_handle', lambda rec: rec.subdomain),
                (Instance, '_do_deploy_locked', deploy_locked),
                (Instance, '_update_container_resources',
                 lambda rec: effect('resize', rec.subdomain)),
                (Backup, '_create_full_instance_backup_sync', snapshot),
                (Backup, '_delete_bucket_prefix', lambda rec, prefix: None),
                (Backup, '_delete_from_bucket', lambda rec: None)):
            p = patch.object(target, attr, fn)
            p.start()
            self.addCleanup(p.stop)

    def _pay(self, invoice):
        invoice.write({'payment_state': 'paid'})
        invoice._saas_check_instance_payment()

    def _job(self, inst, method):
        return self.Job.search([('model', '=', 'saas.instance'),
                                ('res_id', '=', inst.id), ('method', '=', method)])

    def _run(self, job):
        self.env.flush_all()
        self.Job._run_one(job.id)
        self.env.invalidate_all()

    def _run_and_kill(self, job, effect):
        """Claim *job* and let the worker die at *effect*: everything the
        run did rolls back, the claim stays (it was committed first)."""
        job.write({'state': 'running', 'attempts': job.attempts + 1,
                   'started_at': fields.Datetime.now(),
                   'last_heartbeat': fields.Datetime.now()})
        self.env.flush_all()
        self.kill_at.add(effect)
        with self.assertRaises(WorkerKilled):
            with self.env.cr.savepoint():
                job._execute()
        self.env.invalidate_all()
        self.assertEqual(job.state, 'running')

    def _restart(self, job):
        """The dead worker stops heart-beating; the reaper requeues the job
        and a new worker runs it."""
        job.last_heartbeat = fields.Datetime.now() - timedelta(hours=1)
        self.env.flush_all()
        self.Job._cron_reap_jobs()
        self.env.invalidate_all()
        self.assertEqual(job.state, 'pending')
        self._run(job)
        self.assertEqual(job.state, 'done')

    def _count(self, effect, inst):
        return self.starts.count((effect, inst.subdomain))

    # ------------------------------------------------------------- tests
    def test_paid_deploy_completes_once_after_a_crash(self):
        inst = self._mk('csdeploy', 'draft')
        inst.action_confirm_and_bill()
        invoice = inst.sale_order_id.invoice_ids
        self._pay(invoice)
        self._run(self._job(inst, '_do_deploy_after_payment'))
        deploy = self._job(inst, '_do_deploy')
        self.assertEqual(len(deploy), 1)
        self._run_and_kill(deploy, 'deploy')
        self.assertEqual((inst.state, inst.pending_operation), ('provisioning', 'deploy'))
        self._restart(deploy)
        self.assertEqual(inst.state, 'running')
        self.assertEqual(self._count('deploy', inst), 2)
        self.assertEqual(len(self._job(inst, '_do_deploy_after_payment')), 1)
        self.assertEqual(invoice.payment_state, 'paid', "the paid invoice has its effect")

    def test_paid_plan_change_applies_once_after_a_crash(self):
        inst = self._mk('cschange', 'running')
        invoice = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_line_ids': [(0, 0, {'name': 'up', 'quantity': 1, 'price_unit': 40.0})]})
        invoice.action_post()
        inst.write({'pending_plan_id': self.big.id, 'pending_billing_period': 'monthly',
                    'pending_change_invoice_id': invoice.id})
        self._pay(invoice)
        # C6.10: delivery first (killed mid-resize, then restarted) ...
        deliver = self._job(inst, '_do_deliver_change')
        self._run_and_kill(deliver, 'resize')
        self.assertFalse(invoice.saas_delivered_at, "the killed run rolled back")
        self._restart(deliver)
        self.assertEqual(inst.plan_id, self.small, "billing waits for verified delivery")
        inst._on_package_verified()               # C6.3 verified the rollout
        self.assertTrue(invoice.saas_delivered_at)
        # ... then the billing switch (killed once too), applied exactly once.
        job = self._job(inst, '_do_apply_paid_plan_change')
        self._run_and_kill(job, 'resize')
        self.assertEqual(inst.plan_id, self.small, "the killed run rolled back")
        self._restart(job)
        self.assertEqual(inst.plan_id, self.big)
        self.assertFalse(inst.pending_plan_id)
        self.assertEqual(self.env['saas.audit.log'].sudo().search_count([
            ('model', '=', 'saas.instance'), ('res_id', '=', inst.id),
            ('action', '=', 'instance_scale')]), 1)
        self.assertEqual(self._count('resize', inst), 4, "2 delivery + 2 billing runs")

    def test_suspend_completes_once_after_a_crash(self):
        inst = self._mk('cssuspend', 'running')
        inst.action_suspend()
        job = self._job(inst, '_do_suspend')
        self._run_and_kill(job, 'stop')
        self.assertEqual(inst.state, 'provisioning')
        self._restart(job)
        self.assertEqual((inst.state, inst.pending_operation), ('suspended', False))
        self.assertEqual(self._count('stop', inst), 2)

    def test_cancel_completes_once_after_a_crash(self):
        inst = self._mk('cscancel', 'running')
        old = self.Backup.create({
            'instance_id': inst.id, 'name': 'old-final', 'state': 'done',
            'is_full_instance': True, 'format': 'operator', 'source': 'final',
            'bucket_path': 'backups/cscancel/old'})
        inst.action_cancel()
        job = self._job(inst, '_do_delete_instance')
        self._run_and_kill(job, 'destroy')
        self.assertEqual(inst.state, 'provisioning')
        self._restart(job)
        self.assertIn(inst.state, ('cancelled', 'cancelled_by_client'))
        self.assertEqual(self._count('destroy', inst), 2)
        kept = self.Backup.search([('instance_id', '=', inst.id)])
        self.assertEqual(kept.mapped('name'), ['fresh-final'])
        self.assertFalse(old.exists())
