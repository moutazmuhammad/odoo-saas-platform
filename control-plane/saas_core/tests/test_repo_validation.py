from contextlib import contextmanager
from unittest.mock import patch, MagicMock

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestRepoValidation(TransactionCase):
    """Pre-flight repo validation: an unreachable URL, missing branch or bad
    token is rejected via `git ls-remote` BEFORE any redeploy — so a bad repo
    can never crash-loop the running instance."""

    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or self.env['saas.product'].sudo().create(
            {'name': 'RV Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'RV Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.partner = self.env['res.partner'].sudo().create({'name': 'RV Cust'})
        self.domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create({'name': 'rv.example.com'})
        self.server = self.env['saas.server'].sudo().create({'name': 'rv-srv'})

    def _inst(self):
        return self.env['saas.instance'].sudo().create({
            'subdomain': 'rvtest', 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'docker_server_id': self.server.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running'})

    def _patch_ssh(self, exit_code, stdout='', stderr=''):
        ssh = MagicMock()
        ssh.execute.return_value = (exit_code, stdout, stderr)

        @contextmanager
        def conn():
            yield ssh
        return patch.object(type(self.server), '_get_ssh_connection',
                            return_value=conn())

    def test_unreachable_repo_raises(self):
        inst = self._inst()
        with self._patch_ssh(128, '', 'fatal: repository not found'):
            with self.assertRaises(UserError):
                inst._validate_repo_before_change(
                    'https://github.com/x/missing.git', 'main')

    def test_missing_branch_raises(self):
        inst = self._inst()
        # ls-remote succeeds but the requested branch isn't among the heads.
        with self._patch_ssh(0, 'abc123\trefs/heads/main\n'):
            with self.assertRaises(UserError):
                inst._validate_repo_before_change(
                    'https://github.com/x/repo.git', 'does-not-exist')

    def test_valid_repo_branch_passes(self):
        inst = self._inst()
        with self._patch_ssh(0, 'abc123\trefs/heads/18.0\n'):
            inst._validate_repo_before_change(
                'https://github.com/x/repo.git', '18.0')  # must not raise

    def test_no_docker_host_is_noop(self):
        inst = self._inst()
        inst.docker_server_id = False
        # No SSH should be attempted and nothing should raise.
        inst._validate_repo_before_change('https://github.com/x/repo.git', 'main')

    def test_token_is_redacted_in_error(self):
        inst = self._inst()
        with self._patch_ssh(128, '', 'auth failed for x-access-token:SEKRET'):
            with self.assertRaises(UserError) as cm:
                inst._validate_repo_before_change(
                    'https://github.com/x/repo.git', 'main', github_token='SEKRET')
            self.assertNotIn('SEKRET', str(cm.exception))
