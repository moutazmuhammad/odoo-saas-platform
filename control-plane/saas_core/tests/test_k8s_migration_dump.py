import json
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestK8sMigrationDump(TransactionCase):
    """Phase 2 (/ROADMAP.md §5): unit coverage for the dump-for-K8s-restore
    helpers on SaasInstanceBackup — the bridge that lets
    DataService.migrate_to_kubernetes hand a legacy (ssh_docker) tenant's
    real data to the Kubernetes operator's restore Job, which expects the
    exact object layout compute/tools/backup-tool/run-backup.sh produces
    (<bucket>/<prefix>/<stamp>/{db.dump,filestore.tar.gz,manifest.json}).
    Mocked at the SSH/upload boundary, matching test_kubernetes_driver.py's
    convention — the real end-to-end path (real microk8s + MinIO) is
    exercised separately."""

    def _mock_instance(self, subdomain='acme', container='odoo_acme'):
        instance = MagicMock()
        instance.subdomain = subdomain
        instance._get_container_name.return_value = container
        ssh = MagicMock()
        instance.docker_server_id._get_ssh_connection.return_value.__enter__.return_value = ssh
        return instance, ssh

    def test_filestore_tar_matches_backup_tool_convention(self):
        Backup = self.env['saas.instance.backup']
        instance, ssh = self._mock_instance()
        ssh.execute.return_value = (0, '/var/lib/odoo/filestore/acme\n', '')
        stdout = MagicMock()
        stdout.channel.recv_exit_status.return_value = 0
        stderr = MagicMock()
        stderr.read.return_value = b''
        ssh.exec_command_streaming.return_value = (stdout, stderr)

        with patch.object(type(Backup), '_upload_stream_to_bucket') as upload:
            Backup._stream_filestore_tar_for_k8s_restore(
                instance, 'acme-k8s', '20260101T000000Z')

        upload.assert_called_once()
        object_key = upload.call_args.args[0]
        self.assertEqual(object_key, 'acme-k8s/20260101T000000Z/filestore.tar.gz')

        tar_cmd = ssh.exec_command_streaming.call_args.args[0]
        self.assertIn('find . -mindepth 1 -print0', tar_cmd)
        self.assertIn('tar --null --no-recursion -czf - -T -', tar_cmd)
        self.assertIn('cd /var/lib/odoo/filestore/acme', tar_cmd)

    def test_filestore_tar_raises_when_path_not_found(self):
        Backup = self.env['saas.instance.backup']
        instance, ssh = self._mock_instance()
        ssh.execute.return_value = (0, '', '')
        with self.assertRaises(Exception):
            Backup._stream_filestore_tar_for_k8s_restore(
                instance, 'acme-k8s', '20260101T000000Z')

    def test_manifest_matches_backup_tool_shape(self):
        Backup = self.env['saas.instance.backup']
        with patch.object(type(Backup), '_upload_to_bucket') as upload:
            Backup._write_k8s_restore_manifest(
                'acme-k8s', '20260101T000000Z', 'acme', retention=3)

        upload.assert_called_once()
        object_key, body = upload.call_args.args
        self.assertEqual(object_key, 'acme-k8s/20260101T000000Z/manifest.json')
        manifest = json.loads(body)
        self.assertEqual(manifest, {
            'instance': 'acme',
            'timestamp': '20260101T000000Z',
            'db_dump': 'db.dump',
            'filestore_archive': 'filestore.tar.gz',
            'retention': 3,
        })

    def test_dump_for_k8s_migration_cleans_up_on_partial_failure(self):
        """If the filestore upload fails after the db dump already
        succeeded, the db.dump object must not be left behind — a partial
        restore source is worse than none (the operator would restore an
        incomplete/broken snapshot)."""
        Backup = self.env['saas.instance.backup']
        instance, _ssh = self._mock_instance()
        with patch.object(type(Backup), '_get_backup_config',
                          return_value={'bucket': 'b'}), \
             patch.object(type(Backup), '_stream_pg_dump_for_k8s_restore',
                          return_value=100), \
             patch.object(type(Backup), '_stream_filestore_tar_for_k8s_restore',
                          side_effect=RuntimeError('boom')), \
             patch.object(type(Backup), '_delete_bucket_path') as delete_mock:
            with self.assertRaises(RuntimeError):
                Backup.dump_for_k8s_migration(instance, 'acme-k8s')

        delete_mock.assert_called_once()
        cleaned_key = delete_mock.call_args.args[0]
        self.assertRegex(cleaned_key, r'^acme-k8s/\d{8}T\d{6}Z/db\.dump$')

    def test_dump_for_k8s_migration_returns_bucket_prefix_stamp(self):
        Backup = self.env['saas.instance.backup']
        instance, _ssh = self._mock_instance()
        with patch.object(type(Backup), '_get_backup_config',
                          return_value={'bucket': 'my-bucket'}), \
             patch.object(type(Backup), '_stream_pg_dump_for_k8s_restore',
                          return_value=100), \
             patch.object(type(Backup), '_stream_filestore_tar_for_k8s_restore',
                          return_value=200), \
             patch.object(type(Backup), '_write_k8s_restore_manifest',
                          return_value=None):
            bucket, prefix, stamp = Backup.dump_for_k8s_migration(instance, 'acme-k8s')

        self.assertEqual(bucket, 'my-bucket')
        self.assertEqual(prefix, 'acme-k8s')
        self.assertRegex(stamp, r'^\d{8}T\d{6}Z$')
