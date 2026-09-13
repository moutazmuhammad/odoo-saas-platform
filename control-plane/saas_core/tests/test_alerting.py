from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestAlerting(TransactionCase):
    """SEC-009: provider-agnostic, opt-in operational alerting."""

    def setUp(self):
        super().setUp()
        self.Alert = self.env['saas.alert']
        self.env['ir.config_parameter'].sudo().set_param(
            'saas_master.alert_webhook', '')

    def test_noop_without_webhook(self):
        import os
        os.environ.pop('SAAS_ALERT_WEBHOOK', None)
        with patch('odoo.addons.saas_core.models.saas_alert.http_requests.post') as post:
            sent = self.Alert._notify('test_event', 'something happened')
        self.assertFalse(sent, "no webhook configured -> nothing POSTed")
        post.assert_not_called()

    def test_posts_when_webhook_configured(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'saas_master.alert_webhook', 'https://hooks.example.com/abc')
        with patch('odoo.addons.saas_core.models.saas_alert.http_requests.post') as post:
            sent = self.Alert._notify('server_health', 'host down', level='error',
                                      detail='timeout')
        self.assertTrue(sent)
        post.assert_called_once()
        _args, kwargs = post.call_args
        self.assertEqual(kwargs['json']['event'], 'server_health')
        self.assertEqual(kwargs['json']['level'], 'error')
        self.assertIn('host down', kwargs['json']['message'])
        self.assertIn('timeout', kwargs['json']['detail'])

    def test_webhook_failure_never_raises(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'saas_master.alert_webhook', 'https://hooks.example.com/abc')
        with patch('odoo.addons.saas_core.models.saas_alert.http_requests.post',
                   side_effect=Exception('boom')):
            sent = self.Alert._notify('x', 'y')  # must not raise
        self.assertFalse(sent)

    def test_bad_level_falls_back_to_error(self):
        with patch('odoo.addons.saas_core.models.saas_alert.http_requests.post'):
            # An unknown level must not blow up the logger lookup.
            self.Alert._notify('e', 'm', level='bogus')
