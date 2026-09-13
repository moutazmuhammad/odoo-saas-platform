from unittest.mock import MagicMock

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestNginxVhost(TransactionCase):
    """SCALE-001/003: nginx vhost installs are serialised per-proxy (flock)
    and crash-safe (write to a temp OUTSIDE sites-enabled, then atomic mv with
    validate-and-rollback). These assert the emitted command shape; real nginx
    behaviour needs a proxy host (verified at deploy)."""

    def setUp(self):
        super().setUp()
        product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or \
            self.env['saas.product'].sudo().create(
                {'name': 'NGX Hosting', 'is_hosting': True, 'is_published': True})
        plan = self.env['saas.plan'].sudo().create({
            'name': 'NGX Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [product.id])]})
        domain = self.env['saas.based.domain'].sudo().search([], limit=1) or \
            self.env['saas.based.domain'].sudo().create({'name': 'ngx.example.com'})
        partner = self.env['res.partner'].sudo().create({'name': 'NGX Cust'})
        self.instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'ngxinst', 'domain_id': domain.id, 'partner_id': partner.id,
            'saas_product_id': product.id, 'plan_id': plan.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running', 'is_hosting': True})

    def test_apply_writes_temp_outside_glob_then_atomic_locked(self):
        ssh = MagicMock()
        ssh.execute.return_value = (0, '', '')
        self.instance._nginx_apply_vhost(ssh, 'SERVER_BLOCK')

        # The rendered config is SFTP'd to a temp that is NOT inside the
        # included sites-enabled glob (so a partial write can't wedge -t).
        (tmp_path, content), _ = ssh.write_file.call_args
        self.assertEqual(content, 'SERVER_BLOCK')
        self.assertNotIn('/sites-enabled/', tmp_path)
        self.assertIn('ngxinst', tmp_path)

        cmd = ssh.execute.call_args_list[0].args[0]
        # Serialised per-proxy + atomic placement + validate + reload.
        self.assertIn('flock', cmd)
        self.assertIn('mv -f', cmd)
        self.assertIn('nginx -t', cmd)
        self.assertIn('systemctl reload nginx', cmd)
        self.assertIn('/etc/nginx/sites-enabled/ngxinst', cmd)
        self.assertIn(tmp_path, cmd)

    def test_apply_raises_and_cleans_temp_on_failure(self):
        ssh = MagicMock()
        # First call (the locked apply) fails; cleanup rm then runs.
        ssh.execute.side_effect = [(10, 'bad config', ''), (0, '', '')]
        with self.assertRaises(UserError):
            self.instance._nginx_apply_vhost(ssh, 'SERVER_BLOCK')
        # A best-effort temp cleanup must have been attempted.
        last_cmd = ssh.execute.call_args_list[-1].args[0]
        self.assertIn('rm -f', last_cmd)

    def test_remove_is_locked(self):
        ssh = MagicMock()
        ssh.execute.return_value = (0, '', '')
        self.instance._nginx_remove_vhost(ssh)
        cmd = ssh.execute.call_args_list[0].args[0]
        self.assertIn('flock', cmd)
        self.assertIn('rm -f', cmd)
        self.assertIn('systemctl reload nginx', cmd)
        self.assertIn('/etc/nginx/sites-enabled/ngxinst', cmd)
