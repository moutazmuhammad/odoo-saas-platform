import json
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestRestoreRetained(TransactionCase):
    """Admin restore of a cancelled instance's retained snapshot onto a
    running instance of the same customer: free (queued now) or invoiced
    (queued when the invoice is paid)."""

    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1)
        self.partner = self.env['res.partner'].sudo().create({'name': 'Rr Cust'})
        self.domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create({'name': 'rr.example.com'})
        server = self.env['saas.server'].sudo().create(
            {'name': 'rr-srv', 'compute_driver': 'kubernetes'})
        vals = {'domain_id': self.domain.id, 'partner_id': self.partner.id,
                'saas_product_id': self.product.id, 'docker_server_id': server.id,
                'billing_period': 'monthly'}
        self.source = self.env['saas.instance'].sudo().create(
            dict(vals, subdomain='rrold', state='cancelled'))
        self.target = self.env['saas.instance'].sudo().create(
            dict(vals, subdomain='rrnew', state='running'))
        self.snapshot = self.env['saas.instance.backup'].sudo().create({
            'instance_id': self.source.id, 'name': '20260101T000000Z',
            'is_full_instance': True, 'format': 'operator',
            'bucket_path': 'backups/rrold/20260101T000000Z', 'state': 'done'})

    def _wizard(self, **kw):
        return self.env['saas.restore.retained.wizard'].with_context(
            active_model='saas.instance', active_id=self.source.id,
        ).create(dict({'target_instance_id': self.target.id}, **kw))

    def _restore_jobs(self):
        return self.env['saas.job'].sudo().search([
            ('method', '=', '_do_restore_full_instance'),
            ('model', '=', 'saas.instance.backup'),
            ('res_id', '=', self.snapshot.id)])

    def test_retained_snapshot_is_the_latest_full_backup(self):
        self.assertEqual(self.source.retained_snapshot_id, self.snapshot)
        self.assertEqual(self._wizard().retained_snapshot_id, self.snapshot)

    def test_restore_now_free_queues_the_snapshot_onto_target(self):
        with patch.object(type(self.env['saas.job']), '_spawn_worker',
                          lambda *a, **k: None):
            self._wizard().action_restore_now_free()
        self.assertEqual(self.target.state, 'provisioning')
        self.assertEqual(self.target.pending_operation, 'restore')
        job = self._restore_jobs()
        self.assertEqual(len(job), 1)
        self.assertEqual(json.loads(job.args_json), [self.target.id])

    def test_refuses_target_of_another_customer(self):
        self.target.partner_id = self.env['res.partner'].sudo().create({'name': 'Other'})
        with self.assertRaises(UserError):
            self._wizard().action_restore_now_free()

    def test_refuses_without_retained_snapshot(self):
        self.snapshot.state = 'failed'
        with self.assertRaises(UserError):
            self._wizard().action_restore_now_free()

    def test_invoice_payment_queues_the_restore(self):
        with patch.object(type(self.env['saas.restore.retained.wizard']),
                          '_create_restoration_invoice',
                          lambda wiz, s, t: self.env['account.move'].sudo().create(
                              {'move_type': 'out_invoice', 'partner_id': self.partner.id})):
            self._wizard(restoration_fee=10.0).action_send_invoice_and_schedule()
        self.assertEqual(self.target.restoration_backup_id, self.snapshot)
        invoice = self.target.restoration_invoice_id
        self.assertTrue(invoice)
        # The payment hook's restoration branch, as account.move runs it.
        with patch.object(type(self.env['saas.job']), '_spawn_worker',
                          lambda *a, **k: None):
            instances = self.env['saas.instance'].search(
                [('restoration_invoice_id', 'in', invoice.ids)])
            self.assertEqual(instances, self.target)
            self.env['account.move']._saas_start_paid_restorations(instances)
        self.assertEqual(len(self._restore_jobs()), 1)
        self.assertFalse(self.target.restoration_invoice_id)
        self.assertFalse(self.target.restoration_backup_id)
