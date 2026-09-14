from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestProvisioningHelpers(TransactionCase):
    """B.3 group 3: the 3 provisioning helpers on saas.instance that Phase D
    (compute-layer migration) will supersede — deliberately thin per the
    plan (one test each proving current behavior, not exhaustive coverage).
    """

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].sudo().create(
            {'name': 'Prov Co', 'customer_rank': 1})
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or \
            self.env['saas.product'].sudo().create(
                {'name': 'TEST Prov Hosting', 'is_hosting': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'Prov Plan', 'is_custom': True, 'workers': 2,
            'storage_limit': 10, 'cpu_limit': 1.0, 'ram_limit': '1g',
            'price': 30.0, 'yearly_price': 288.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])],
        })
        self.domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create(
                {'name': 'prov.example.com'})
        self.server = self.env['saas.server'].sudo().create({'name': 'prov-srv'})

    def _inst(self, sub, **kw):
        vals = {
            'subdomain': sub, 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'billing_period': 'monthly',
            'state': 'running',
        }
        vals.update(kw)
        return self.env['saas.instance'].sudo().create(vals)

    def _patch_ssh(self, server, exit_code, stdout='', stderr=''):
        """Returns (patcher, ssh_mock) — the mock's .execute call args are
        inspectable after the `with` block via the returned ssh_mock."""
        ssh = MagicMock()
        ssh.execute.return_value = (exit_code, stdout, stderr)

        @contextmanager
        def conn():
            yield ssh
        return patch.object(type(server), '_get_ssh_connection',
                             lambda self: conn()), ssh

    # ---------------- _pg_clone_db ----------------

    def test_pg_clone_db_issues_create_database_with_template(self):
        inst = self._inst('pgc1', db_server_id=self.server.id, db_user='pgc1_user')
        patcher, ssh = self._patch_ssh(self.server, 0)
        with patcher:
            inst._pg_clone_db('src_tmpl', 'pgc1')
        cmd = ssh.execute.call_args.args[0]
        self.assertIn('CREATE DATABASE', cmd)
        self.assertIn('pgc1', cmd)
        self.assertIn('src_tmpl', cmd)
        self.assertIn('pgc1_user', cmd)

    def test_pg_clone_db_raises_on_failure(self):
        inst = self._inst('pgc2', db_server_id=self.server.id, db_user='pgc2_user')
        patcher, _ssh = self._patch_ssh(self.server, 1, '', 'template db in use')
        with patcher:
            with self.assertRaises(UserError):
                inst._pg_clone_db('src_tmpl', 'pgc2')

    def test_pg_clone_db_raises_without_db_server(self):
        inst = self._inst('pgc3', db_server_id=False, db_user='pgc3_user')
        with self.assertRaises(UserError):
            inst._pg_clone_db('src_tmpl', 'pgc3')

    def test_pg_clone_db_rejects_unsafe_identifier(self):
        inst = self._inst('pgc4', db_server_id=self.server.id, db_user='pgc4_user')
        with self.assertRaises(UserError):
            inst._pg_clone_db('src; DROP TABLE x;--', 'pgc4')

    # ---------------- _provision_nginx ----------------

    def _ssh_mock(self, responses):
        """responses: dict cmd-substring -> (exit_code, stdout, stderr),
        matched in order; default (0, '', '') if nothing matches."""
        ssh = MagicMock()

        def execute(cmd, timeout=None):
            for needle, resp in responses.items():
                if needle in cmd:
                    return resp
            return (0, '', '')
        ssh.execute.side_effect = execute
        return ssh

    def test_provision_nginx_success_delegates_to_apply_vhost(self):
        inst = self._inst('nx1')
        ssh = self._ssh_mock({'certbot certonly --nginx': (0, 'ok', '')})
        calls = []
        with patch.object(type(inst), '_nginx_apply_vhost',
                           lambda self, ssh_, content: calls.append(content)):
            inst._provision_nginx(ssh)
        self.assertEqual(len(calls), 1)
        self.assertIn('nx1', calls[0])

    def test_provision_nginx_falls_back_to_standalone(self):
        inst = self._inst('nx2')
        ssh = self._ssh_mock({
            'certbot certonly --nginx': (1, '', 'port 80 in use'),
            'certbot certonly --standalone': (0, 'ok', ''),
        })
        calls = []
        with patch.object(type(inst), '_nginx_apply_vhost',
                           lambda self, ssh_, content: calls.append(content)):
            inst._provision_nginx(ssh)
        self.assertEqual(len(calls), 1)

    def test_provision_nginx_raises_when_both_certbot_modes_fail(self):
        inst = self._inst('nx3')
        ssh = self._ssh_mock({
            'certbot certonly --nginx': (1, '', 'nginx mode failed'),
            'certbot certonly --standalone': (1, '', 'standalone failed too'),
        })
        with patch.object(type(inst), '_nginx_apply_vhost',
                           lambda self, ssh_, content: None):
            with self.assertRaises(UserError):
                inst._provision_nginx(ssh)

    # ---------------- _clone_product_repos ----------------

    def _odoo_version(self):
        return self.env['saas.odoo.version'].sudo().search([], limit=1) or \
            self.env['saas.odoo.version'].sudo().create({
                'name': '18.0', 'docker_image': 'odoo',
                'docker_image_tag': '18.0',
            })

    def test_clone_product_repos_noop_without_repos(self):
        inst = self._inst('cr1', odoo_version_id=self._odoo_version().id)
        ssh = MagicMock()
        inst._clone_product_repos(ssh)
        ssh.execute.assert_not_called()

    def test_clone_product_repos_clones_each_repo(self):
        product = self.env['saas.product'].sudo().create(
            {'name': 'CR Product', 'is_hosting': True})
        self.env['saas.product.repo'].sudo().create({
            'product_id': product.id,
            'repo_url': 'https://github.com/x/addons.git',
            'branch': '18.0',
        })
        inst = self._inst('cr2', odoo_version_id=self._odoo_version().id,
                           saas_product_id=product.id,
                           docker_server_id=self.server.id)
        ssh = self._ssh_mock({})  # every call succeeds (0, '', '')
        inst._clone_product_repos(ssh)
        clone_calls = [c.args[0] for c in ssh.execute.call_args_list
                       if 'git clone' in c.args[0]]
        self.assertEqual(len(clone_calls), 1)
        self.assertIn('--branch 18.0', clone_calls[0])
        self.assertIn('https://github.com/x/addons.git', clone_calls[0])

    def test_clone_product_repos_raises_on_clone_failure(self):
        product = self.env['saas.product'].sudo().create(
            {'name': 'CR Product 2', 'is_hosting': True})
        self.env['saas.product.repo'].sudo().create({
            'product_id': product.id,
            'repo_url': 'https://github.com/x/broken.git',
            'branch': 'main',
        })
        inst = self._inst('cr3', odoo_version_id=self._odoo_version().id,
                           saas_product_id=product.id,
                           docker_server_id=self.server.id)
        ssh = self._ssh_mock({'git clone': (128, '', 'fatal: repository not found')})
        with self.assertRaises(UserError):
            inst._clone_product_repos(ssh)

    def test_clone_product_repos_permissions_are_owner_only(self):
        """SEC-006: chmod must be 700 (owner-only), never 777 — a chown to
        the container's own UID already covers the container's access;
        addons/ is loaded and executed by Odoo, so world-writable there
        was a cross-tenant code-injection path, not just a leak."""
        product = self.env['saas.product'].sudo().create(
            {'name': 'CR Product 3', 'is_hosting': True})
        self.env['saas.product.repo'].sudo().create({
            'product_id': product.id,
            'repo_url': 'https://github.com/x/perms.git',
            'branch': '18.0',
        })
        inst = self._inst('cr4', odoo_version_id=self._odoo_version().id,
                           saas_product_id=product.id,
                           docker_server_id=self.server.id)
        ssh = self._ssh_mock({})
        inst._clone_product_repos(ssh)
        chmod_calls = [c.args[0] for c in ssh.execute.call_args_list
                       if 'chmod' in c.args[0]]
        self.assertEqual(len(chmod_calls), 1)
        self.assertIn('chmod -R 700', chmod_calls[0])
        self.assertNotIn('777', chmod_calls[0])

    def test_pull_product_repos_permissions_are_owner_only(self):
        """SEC-006, same as _clone_product_repos above but for the
        already-cloned/git-pull path."""
        product = self.env['saas.product'].sudo().create(
            {'name': 'CR Product 4', 'is_hosting': True})
        self.env['saas.product.repo'].sudo().create({
            'product_id': product.id,
            'repo_url': 'https://github.com/x/pullperms.git',
            'branch': '18.0',
        })
        inst = self._inst('cr5', odoo_version_id=self._odoo_version().id,
                           saas_product_id=product.id,
                           docker_server_id=self.server.id)
        # 'test -d' succeeding (default 0,'','') means "already cloned",
        # routing into the git-pull branch rather than a fresh clone.
        ssh = self._ssh_mock({})
        inst._pull_product_repos(ssh)
        chmod_calls = [c.args[0] for c in ssh.execute.call_args_list
                       if 'chmod' in c.args[0]]
        self.assertEqual(len(chmod_calls), 1)
        self.assertIn('chmod -R 700', chmod_calls[0])
        self.assertNotIn('777', chmod_calls[0])
