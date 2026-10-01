from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBucketCors(TransactionCase):

    def _apply(self, error_code):
        client = MagicMock()
        client.put_bucket_cors.side_effect = ClientError(
            {'Error': {'Code': error_code, 'Message': 'x'}}, 'PutBucketCors')
        self.env['ir.config_parameter'].sudo().set_param('web.base.url', 'https://portal.example.com')
        Backup = self.env['saas.instance.backup']
        with patch.object(type(Backup), '_get_backup_config', return_value={}), \
                patch.object(type(Backup), '_get_s3_client', return_value=(client, 'my-bucket')):
            Backup.apply_bucket_cors()

    def test_not_implemented_explains_full_access_key_or_console(self):
        with self.assertRaises(UserError) as cm:
            self._apply('NotImplemented')
        msg = str(cm.exception)
        self.assertIn('Full Access', msg)
        self.assertIn('console', msg)
        self.assertIn('my-bucket', msg)
        self.assertIn('https://portal.example.com', msg)

    def test_access_denied_still_explains_the_key(self):
        with self.assertRaises(UserError) as cm:
            self._apply('AccessDenied')
        self.assertIn('PutBucketCORS', str(cm.exception))
