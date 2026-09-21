"""Phase 5 (ssh_docker removal), part B: full-instance backup/restore
redesigned around the Kubernetes operator's own CronJob-based mechanism
(KubernetesDriver.set_scheduled_backup/trigger_backup_now) instead of the
old restic-over-SSH pipeline. These tests mock `_compute_driver()` and the
bucket-listing helpers — no real cluster or object storage needed.
"""
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from ..drivers.base import ExecResult


@tagged('post_install', '-at_install')
class TestFullInstanceBackup(TransactionCase):

    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [], limit=1) or self.env['saas.product'].sudo().create(
            {'name': 'FIB Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'FIB Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.domain = self.env['saas.based.domain'].sudo().create(
            {'name': 'fib.example.com'})
        self.partner = self.env['res.partner'].sudo().create({'name': 'FIB Cust'})
        self.region = self.env['saas.region'].sudo().create(
            {'name': 'FIB Region', 'code': 'fib-region',
             'native_ingress_tls': True, 'tls_cluster_issuer': 'letsencrypt-prod'})
        self.server = self.env['saas.server'].sudo().create(
            {'name': 'fib-k8s-srv', 'compute_driver': 'kubernetes',
             'region_id': self.region.id})
        self.instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'fibinst', 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'docker_server_id': self.server.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running',
            'daily_backup_enabled': True,
        })
        self.Backup = self.env['saas.instance.backup']
        self._cfg = {
            'provider': 's3', 'bucket': 'test-bucket',
            'access_key': 'AK', 'secret_key': 'SK',
            'endpoint': 'https://s3.example.com', 'region': '',
        }

    # -- toggle wiring -------------------------------------------------------

    def test_sync_scheduled_backup_enables_with_bucket_config(self):
        driver = MagicMock()
        with patch.object(type(self.instance), '_compute_driver', return_value=driver), \
             patch.object(type(self.Backup), '_get_backup_config',
                          return_value=self._cfg):
            self.instance._sync_scheduled_backup()
        driver.set_scheduled_backup.assert_called_once()
        _args, kwargs = driver.set_scheduled_backup.call_args
        self.assertTrue(kwargs['enabled'])
        self.assertEqual(kwargs['bucket'], 'test-bucket')
        self.assertEqual(kwargs['prefix'], 'backups/fibinst')

    def test_sync_scheduled_backup_disables_when_suspended(self):
        self.instance.daily_backup_suspended = True
        driver = MagicMock()
        with patch.object(type(self.instance), '_compute_driver', return_value=driver):
            self.instance._sync_scheduled_backup()
        driver.set_scheduled_backup.assert_called_once()
        kwargs = driver.set_scheduled_backup.call_args.kwargs
        self.assertEqual(kwargs, {'enabled': False})

    def test_sync_scheduled_backup_noop_without_server(self):
        self.instance.docker_server_id = False
        driver = MagicMock()
        with patch.object(type(self.instance), '_compute_driver', return_value=driver):
            self.instance._sync_scheduled_backup()
        driver.set_scheduled_backup.assert_not_called()

    # -- bucket listing / record mirroring ------------------------------------

    def test_record_full_instance_backup_creates_row(self):
        with patch.object(type(self.Backup), '_bucket_object_size',
                          return_value=1024 * 1024):
            rec = self.Backup._record_full_instance_backup(
                self.instance, 'backups/fibinst', '20260101T000000Z')
        self.assertTrue(rec)
        self.assertEqual(rec.bucket_path, 'backups/fibinst/20260101T000000Z')
        self.assertEqual(rec.format, 'operator')
        self.assertTrue(rec.is_full_instance)
        self.assertEqual(rec.state, 'done')
        self.assertAlmostEqual(rec.size_mb, 2.0, places=1)

    def test_record_full_instance_backup_skips_incomplete_upload(self):
        with patch.object(type(self.Backup), '_bucket_object_size', return_value=0):
            rec = self.Backup._record_full_instance_backup(
                self.instance, 'backups/fibinst', '20260101T000000Z')
        self.assertFalse(rec)

    def test_record_full_instance_backup_updates_existing(self):
        with patch.object(type(self.Backup), '_bucket_object_size',
                          return_value=1024 * 1024):
            first = self.Backup._record_full_instance_backup(
                self.instance, 'backups/fibinst', '20260101T000000Z')
            second = self.Backup._record_full_instance_backup(
                self.instance, 'backups/fibinst', '20260101T000000Z')
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.Backup.search_count([
            ('instance_id', '=', self.instance.id),
            ('bucket_path', '=', 'backups/fibinst/20260101T000000Z'),
        ]), 1)

    # -- synchronous on-demand snapshot (pre-cancellation courtesy capture) ----

    def test_create_full_instance_backup_sync_happy_path(self):
        driver = MagicMock()
        stamps_seq = [set(), {'20260102T000000Z'}]

        def _fake_list(cfg, prefix):
            return stamps_seq.pop(0) if stamps_seq else {'20260102T000000Z'}

        with patch.object(type(self.instance), '_compute_driver', return_value=driver), \
             patch.object(type(self.Backup), '_get_backup_config',
                          return_value=self._cfg), \
             patch.object(type(self.Backup), '_list_backup_stamps',
                          side_effect=lambda cfg, prefix: _fake_list(cfg, prefix)), \
             patch.object(type(self.Backup), '_bucket_object_size',
                          return_value=2048), \
             patch('time.sleep', return_value=None):
            backup = self.Backup._create_full_instance_backup_sync(
                self.instance, wait_timeout=30)
        driver.set_scheduled_backup.assert_called_once()
        driver.trigger_backup_now.assert_called_once()
        self.assertTrue(backup)
        self.assertEqual(backup.bucket_path, 'backups/fibinst/20260102T000000Z')

    def test_create_full_instance_backup_sync_times_out(self):
        # wait_timeout=0 makes the poll loop's very first "have we run out
        # of time" check already true (real wall clock, no time.* mocking
        # needed — mocking the global `time` module risks colliding with
        # unrelated ORM internals that also call it) — zero iterations,
        # no new stamp found, so this must raise immediately.
        driver = MagicMock()
        with patch.object(type(self.instance), '_compute_driver', return_value=driver), \
             patch.object(type(self.Backup), '_get_backup_config',
                          return_value=self._cfg), \
             patch.object(type(self.Backup), '_list_backup_stamps',
                          return_value=set()):
            with self.assertRaises(UserError):
                self.Backup._create_full_instance_backup_sync(
                    self.instance, wait_timeout=0)

    # -- restore onto a live instance -----------------------------------------

    def test_do_restore_full_instance_uses_operator_pg_restore_flags(self):
        """Must mirror compute/tools/backup-tool/run-restore.sh's exact
        pg_restore invocation: --no-owner --single-transaction
        --exit-on-error."""
        driver = MagicMock()
        driver.exec.return_value = ExecResult(rc=0, stdout='', stderr='')
        backup = self.Backup.sudo().create({
            'instance_id': self.instance.id,
            'name': '20260101T000000Z',
            'is_full_instance': True,
            'format': 'operator',
            'bucket_path': 'backups/fibinst/20260101T000000Z',
            'state': 'done',
        })
        with patch.object(type(self.instance), '_compute_driver', return_value=driver), \
             patch.object(type(backup), '_presigned_get_url',
                          return_value='https://example.com/x'), \
             patch.object(type(self.instance), '_docker_exec_sql',
                          return_value=(0, '', '')) as m_sql:
            backup._do_restore_full_instance(self.instance.id)

        commands = [c.args[1] for c in driver.exec.call_args_list]
        pg_restore_cmd = next(c for c in commands if 'pg_restore' in c)
        self.assertIn('--no-owner', pg_restore_cmd)
        self.assertIn('--single-transaction', pg_restore_cmd)
        self.assertIn('--exit-on-error', pg_restore_cmd)
        tar_cmd = next(c for c in commands if c.startswith('tar '))
        self.assertIn('/var/lib/odoo', tar_cmd)
        self.assertIn('filestore.tar.gz', tar_cmd)
        # Connections released via SQL, never via stopping the pod.
        driver.stop.assert_not_called()
        self.assertTrue(any('pg_terminate_backend' in c.args[0]
                            for c in m_sql.call_args_list))
        self.assertEqual(self.instance.state, 'running')
        self.assertFalse(self.instance.pending_operation)
