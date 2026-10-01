from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBackupEndpoint(TransactionCase):
    """In-cluster backup Jobs (rclone, PROVIDER=Other) can't derive a
    provider's endpoint themselves: an empty endpoint made every
    DigitalOcean backup fail. The control plane hands them the same URL its
    own client uses."""

    def _cfg(self, provider, region='fra1', endpoint=''):
        return {'provider': provider, 'region': region, 'endpoint': endpoint,
                'bucket': 'b', 'access_key': 'ak', 'secret_key': 'sk'}

    def test_endpoint_derived_per_provider(self):
        B = self.env['saas.instance.backup']
        self.assertEqual(B._storage_endpoint_url(self._cfg('digitalocean')),
                         'https://fra1.digitaloceanspaces.com')
        self.assertEqual(B._storage_endpoint_url(self._cfg('hetzner', region='nbg1')),
                         'https://nbg1.your-objectstorage.com')
        self.assertEqual(B._storage_endpoint_url(self._cfg('s3', endpoint='https://minio.example.com')),
                         'https://minio.example.com')
        self.assertEqual(B._storage_endpoint_url(self._cfg('s3')), '')

    def test_final_backup_job_gets_the_spaces_endpoint(self):
        B = self.env['saas.instance.backup']
        instance = MagicMock()
        driver = instance._compute_driver.return_value
        with patch.object(type(B), '_get_backup_config', return_value=self._cfg('digitalocean')), \
                patch.object(type(B), '_list_backup_stamps', side_effect=[set(), {'20260101T000000Z'}]), \
                patch('odoo.addons.saas_core.models.saas_instance_backup.time.sleep'):
            try:
                B._create_full_instance_backup_sync(instance, wait_timeout=30)
            except Exception:
                pass  # only the handoff to the cluster matters here
        self.assertEqual(driver.set_scheduled_backup.call_args.kwargs['endpoint'],
                         'https://fra1.digitaloceanspaces.com')
