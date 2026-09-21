"""Phase 5 (ssh_docker removal): hosting_db_* self-service database
operations, ported from raw SSH + `docker exec` onto
`KubernetesDriver.exec()`/`exec_stream_out()`. These tests mock
`_compute_driver()` the same way test_compute_backend_selection.py's
TestDeployOnKubernetes does — no real cluster needed.
"""
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from ..drivers.base import ExecResult


@tagged('post_install', '-at_install')
class TestHostingDbOps(TransactionCase):

    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or self.env['saas.product'].sudo().create(
            {'name': 'DBOps Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'DBOps Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.domain = self.env['saas.based.domain'].sudo().create(
            {'name': 'dbops.example.com'})
        self.partner = self.env['res.partner'].sudo().create({'name': 'DBOps Cust'})
        self.region = self.env['saas.region'].sudo().create(
            {'name': 'DBOps Region', 'code': 'dbops-region',
             'native_ingress_tls': True, 'tls_cluster_issuer': 'letsencrypt-prod'})
        self.server = self.env['saas.server'].sudo().create(
            {'name': 'dbops-k8s-srv', 'compute_driver': 'kubernetes',
             'region_id': self.region.id})
        self.version = self.env['saas.odoo.version'].sudo().search(
            [('is_hosting_version', '=', True)], limit=1) or \
            self.env['saas.odoo.version'].sudo().create({
                'name': '18.0', 'docker_image': 'odoo',
                'docker_image_tag': '18.0', 'nginx_template': 'new',
                'is_hosting_version': True})
        self.instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'dbopsinst', 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'docker_server_id': self.server.id,
            'odoo_version_id': self.version.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running', 'is_hosting': True,
        })

    def _driver(self, **overrides):
        driver = MagicMock()
        driver.exec.return_value = ExecResult(rc=0, stdout='', stderr='')
        for k, v in overrides.items():
            setattr(driver, k, v)
        return driver

    # -- pod-exec transport ------------------------------------------------

    def test_docker_exec_python_runs_heredoc_via_driver_exec(self):
        driver = self._driver()
        driver.exec.return_value = ExecResult(rc=0, stdout='hi\n', stderr='')
        with patch.object(type(self.instance), '_compute_driver', return_value=driver):
            rc, out, err = self.instance._docker_exec_python(
                "print('hi')", env={'X': '1'}, timeout=42)
        self.assertEqual((rc, out, err), (0, 'hi\n', ''))
        driver.exec.assert_called_once()
        args, kwargs = driver.exec.call_args
        command = args[1]
        self.assertIn("python3 - <<'SAAS_DBOPS_EOF'", command)
        self.assertIn("print('hi')", command)
        self.assertIn("odoo.tools.config.parse_config", command)
        self.assertEqual(kwargs['env'], {'X': '1'})
        self.assertEqual(kwargs['timeout'], 42)

    def test_docker_exec_sql_reads_conn_from_odoo_conf_not_server_fields(self):
        """No more saas.server.psql_port (deleted with ssh_docker) —
        connection info must come from /etc/odoo/odoo.conf inside the pod."""
        driver = self._driver()
        driver.exec.return_value = ExecResult(rc=0, stdout='1\n', stderr='')
        with patch.object(type(self.instance), '_compute_driver', return_value=driver):
            rc, out, err = self.instance._docker_exec_sql(
                "SELECT 1", db='somedb', timeout=10)
        self.assertEqual(rc, 0)
        command = driver.exec.call_args.args[1]
        self.assertIn('/etc/odoo/odoo.conf', command)
        self.assertIn('psql', command)
        self.assertIn(repr('somedb'), command)
        self.assertIn(repr('SELECT 1'), command)
        # No leftover reference to the deleted ssh_docker-era field.
        self.assertNotIn('psql_port', command)

    # -- hosting_db_list -----------------------------------------------------

    def test_hosting_db_list_parses_markers(self):
        driver = self._driver()
        stdout = (
            "---SAAS_DB_LIST_BEGIN---\n"
            "dbopsinst_prod|admin\n"
            "dbopsinst_staging|\n"
            "---SAAS_DB_LIST_END---\n"
        )
        driver.exec.return_value = ExecResult(rc=0, stdout=stdout, stderr='')
        with patch.object(type(self.instance), '_compute_driver', return_value=driver):
            rows = self.instance.hosting_db_list()
        self.assertEqual(rows, [
            {'name': 'dbopsinst_prod', 'admin_login': 'admin'},
            {'name': 'dbopsinst_staging', 'admin_login': ''},
        ])

    def test_hosting_db_list_requires_running_instance(self):
        self.instance.state = 'stopped'
        with self.assertRaises(UserError):
            self.instance.hosting_db_list()

    def test_hosting_db_list_requires_hosting_product(self):
        self.instance.write({'is_hosting': False})
        with self.assertRaises(UserError):
            self.instance.hosting_db_list()

    # -- hosting_db_create ----------------------------------------------------

    def test_hosting_db_create_happy_path(self):
        with patch.object(type(self.instance), 'hosting_db_list', return_value=[]), \
             patch.object(type(self.instance), '_hosting_ensure_template_db',
                          return_value='__odoo_template_dbopsinst') as m_tpl, \
             patch.object(type(self.instance), '_pg_clone_db') as m_clone, \
             patch.object(type(self.instance), '_hosting_clone_filestore') as m_fs, \
             patch.object(type(self.instance), '_hosting_patch_admin_creds') as m_creds:
            name = self.instance.hosting_db_create(
                'prod', 'admin', 'S3cret!', lang='en_US', country_code='US')
        self.assertEqual(name, 'dbopsinst_prod')
        m_tpl.assert_called_once()
        m_clone.assert_called_once_with('__odoo_template_dbopsinst', 'dbopsinst_prod')
        m_fs.assert_called_once_with('__odoo_template_dbopsinst', 'dbopsinst_prod')
        m_creds.assert_called_once_with(
            db_name='dbopsinst_prod', login='admin', password='S3cret!',
            lang='en_US', country_code='US')

    def test_hosting_db_create_rejects_existing_name(self):
        with patch.object(type(self.instance), 'hosting_db_list',
                          return_value=[{'name': 'dbopsinst_prod', 'admin_login': ''}]):
            with self.assertRaises(UserError):
                self.instance.hosting_db_create('prod', 'admin', 'S3cret!')

    def test_hosting_db_create_rolls_back_on_filestore_failure(self):
        with patch.object(type(self.instance), 'hosting_db_list', return_value=[]), \
             patch.object(type(self.instance), '_hosting_ensure_template_db',
                          return_value='__odoo_template_dbopsinst'), \
             patch.object(type(self.instance), '_pg_clone_db'), \
             patch.object(type(self.instance), '_hosting_clone_filestore',
                          side_effect=RuntimeError('disk full')), \
             patch.object(type(self.instance), '_pg_drop_db') as m_drop:
            with self.assertRaises(UserError):
                self.instance.hosting_db_create('prod', 'admin', 'S3cret!')
        m_drop.assert_called_once_with('dbopsinst_prod')

    def test_hosting_db_create_rolls_back_on_admin_patch_failure(self):
        with patch.object(type(self.instance), 'hosting_db_list', return_value=[]), \
             patch.object(type(self.instance), '_hosting_ensure_template_db',
                          return_value='__odoo_template_dbopsinst'), \
             patch.object(type(self.instance), '_pg_clone_db'), \
             patch.object(type(self.instance), '_hosting_clone_filestore'), \
             patch.object(type(self.instance), '_hosting_patch_admin_creds',
                          side_effect=RuntimeError('boom')), \
             patch.object(type(self.instance), '_hosting_drop_filestore') as m_dropfs, \
             patch.object(type(self.instance), '_pg_drop_db') as m_drop:
            with self.assertRaises(UserError):
                self.instance.hosting_db_create('prod', 'admin', 'S3cret!')
        m_dropfs.assert_called_once_with('dbopsinst_prod')
        m_drop.assert_called_once_with('dbopsinst_prod')

    # -- hosting_db_drop -------------------------------------------------------

    def test_hosting_db_drop_drops_pg_and_filestore(self):
        with patch.object(
                type(self.instance), 'hosting_db_list',
                return_value=[{'name': 'dbopsinst_prod', 'admin_login': ''}]), \
             patch.object(type(self.instance), '_pg_drop_db') as m_drop, \
             patch.object(type(self.instance), '_hosting_drop_filestore') as m_dropfs:
            name = self.instance.hosting_db_drop('prod')
        self.assertEqual(name, 'dbopsinst_prod')
        m_drop.assert_called_once_with('dbopsinst_prod')
        m_dropfs.assert_called_once_with('dbopsinst_prod')

    def test_hosting_db_drop_rejects_foreign_db(self):
        with patch.object(type(self.instance), 'hosting_db_list', return_value=[]):
            with self.assertRaises(UserError):
                self.instance.hosting_db_drop('not-mine')

    # -- module upgrade: no stop/start on Kubernetes ---------------------------

    def test_hosting_db_upgrade_module_does_not_stop_start_pod(self):
        """Unlike ssh_docker's docker-compose-run pattern, the recovery
        upgrade must run directly against the live pod — stopping it would
        cut off the only channel available to run the recovery command."""
        driver = self._driver()
        driver.exec.return_value = ExecResult(rc=0, stdout='ok', stderr='')
        with patch.object(type(self.instance), 'hosting_db_list',
                          return_value=[{'name': 'dbopsinst_prod', 'admin_login': ''}]), \
             patch.object(type(self.instance), '_compute_driver', return_value=driver):
            output = self.instance.hosting_db_upgrade_module('prod', 'sale')
        self.assertEqual(output, 'ok')
        driver.stop.assert_not_called()
        driver.start.assert_not_called()
        command = driver.exec.call_args.args[1]
        self.assertIn('--stop-after-init', command)
        self.assertIn('-u sale', command)

    def test_hosting_db_upgrade_modules_live_runs_button_immediate_upgrade(self):
        driver = self._driver()
        combined = '---SAAS_UPGRADE_BEGIN---\nupgraded=sale\n---SAAS_UPGRADE_END---\n'
        driver.exec.return_value = ExecResult(rc=0, stdout=combined, stderr='')
        with patch.object(type(self.instance), 'hosting_db_list',
                          return_value=[{'name': 'dbopsinst_prod', 'admin_login': ''}]), \
             patch.object(type(self.instance), '_compute_driver', return_value=driver):
            output = self.instance.hosting_db_upgrade_modules('prod', 'sale')
        self.assertIn('upgraded=sale', output)
        driver.stop.assert_not_called()
        driver.start.assert_not_called()
        command = driver.exec.call_args.args[1]
        self.assertIn('button_immediate_upgrade', command)

    # -- admin password reset ---------------------------------------------------

    def test_hosting_db_reset_admin_password(self):
        driver = self._driver()
        driver.exec.return_value = ExecResult(
            rc=0,
            stdout='---SAAS_PW_RESET_BEGIN---\nlogin=admin\n---SAAS_PW_RESET_END---\n',
            stderr='')
        with patch.object(type(self.instance), 'hosting_db_list',
                          return_value=[{'name': 'dbopsinst_prod', 'admin_login': ''}]), \
             patch.object(type(self.instance), '_compute_driver', return_value=driver):
            login = self.instance.hosting_db_reset_admin_password('prod', 'newpassword123')
        self.assertEqual(login, 'admin')

    def test_hosting_db_reset_admin_password_rejects_short_password(self):
        with patch.object(type(self.instance), 'hosting_db_list',
                          return_value=[{'name': 'dbopsinst_prod', 'admin_login': ''}]):
            with self.assertRaises(UserError):
                self.instance.hosting_db_reset_admin_password('prod', '123')

    # -- per-DB restore: no stop/start, uses pg_terminate_backend --------------

    def test_do_restore_backup_releases_connections_without_stopping_pod(self):
        driver = self._driver()
        driver.exec.return_value = ExecResult(
            # Satisfies both the zipfile-listing check ("dump.sql" present)
            # and the post-extraction existence check ("OK" present) — the
            # mock returns the same ExecResult for every exec() call.
            rc=0, stdout='dump.sql\nOK', stderr='')

        Backup = self.env['saas.instance.backup']
        backup = Backup.sudo().create({
            'instance_id': self.instance.id,
            'db_name': 'dbopsinst_prod',
            'name': 'restore-upload',
            'state': 'done',
            'bucket_path': 'ondemand/x.zip',
        })
        with patch.object(type(self.instance), '_compute_driver', return_value=driver), \
             patch.object(type(backup), '_read_manifest_safe', return_value=None), \
             patch.object(type(backup), '_generate_presigned_url',
                          return_value='https://example.com/x.zip'), \
             patch.object(type(self.instance), '_docker_exec_sql') as m_sql, \
             patch.object(type(self.instance), '_docker_exec_psql_file',
                          return_value=(0, '', '')):
            m_sql.return_value = (0, '', '')
            self.instance._do_restore_backup(backup.id)

        driver.stop.assert_not_called()
        driver.start.assert_not_called()
        # First _docker_exec_sql call must be the connection-release query.
        first_call_sql = m_sql.call_args_list[0].args[0]
        self.assertIn('pg_terminate_backend', first_call_sql)
        self.assertEqual(self.instance.state, 'running')
