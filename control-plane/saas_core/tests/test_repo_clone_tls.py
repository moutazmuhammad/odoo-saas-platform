from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestRepoCloneTls(TransactionCase):
    """ARCH-017: customer code is fetched with TLS verification forced on, so a
    downgraded/compromised host git config can't enable a MITM of the clone."""

    def setUp(self):
        super().setUp()
        product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or \
            self.env['saas.product'].sudo().create(
                {'name': 'TLS Hosting', 'is_hosting': True, 'is_published': True})
        plan = self.env['saas.plan'].sudo().create({
            'name': 'TLS Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [product.id])]})
        domain = self.env['saas.based.domain'].sudo().search([], limit=1) or \
            self.env['saas.based.domain'].sudo().create({'name': 'tls.example.com'})
        partner = self.env['res.partner'].sudo().create({'name': 'TLS Cust'})
        self.server = self.env['saas.server'].sudo().create({
            'name': 'tls-host', 'ip_v4': '10.0.0.5', 'is_docker_host': True})
        self.instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'tlsinst', 'domain_id': domain.id, 'partner_id': partner.id,
            'saas_product_id': product.id, 'plan_id': plan.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running', 'is_hosting': True,
            'docker_server_id': self.server.id})
        self.repo = self.env['saas.instance.repo'].sudo().create({
            'instance_id': self.instance.id,
            'repo_url': 'https://github.com/acme/app.git', 'branch': 'main'})

    def test_clone_forces_tls_verify(self):
        cmds = []

        class FakeSSH:
            def __enter__(self_): return self_
            def __exit__(self_, *a): return False
            def execute(self_, cmd, timeout=None):
                cmds.append(cmd)
                return (0, '', '')

        with patch.object(type(self.server), '_get_ssh_connection', return_value=FakeSSH()), \
                patch.object(type(self.instance), '_ensure_can_ssh', return_value=None), \
                patch.object(type(self.instance), '_get_container_uid', return_value='100'):
            try:
                self.repo._clone_repo()
            except Exception:
                pass  # later steps may no-op under a fake SSH; we only assert the clone cmd
        clone = next((c for c in cmds if 'git ' in c and ' clone ' in c), '')
        self.assertTrue(clone, "a git clone command should have been issued")
        self.assertIn('http.sslVerify=true', clone,
                      "clone must force TLS verification (ARCH-017)")
