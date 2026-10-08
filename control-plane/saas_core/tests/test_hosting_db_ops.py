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
            {'name': 'DBOps Region', 'code': 'dbops-region'})
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
        # Setting the pods' database filter talks to the cluster.
        p = patch.object(type(self.instance), '_ensure_hosting_access')
        self.m_filter = p.start()
        self.addCleanup(p.stop)

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
        self.m_filter.assert_called_once()
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

    def _restore_with_mock(self, instance):
        driver = self._driver()
        driver.exec.return_value = ExecResult(rc=0, stdout='dump.sql\nOK', stderr='')
        Backup = self.env['saas.instance.backup']
        backup = Backup.sudo().create({
            'instance_id': instance.id, 'db_name': instance._hosting_db_full_name('main'),
            'name': 'restore-upload', 'state': 'done', 'bucket_path': 'ondemand/x.zip',
        })
        with patch.object(type(instance), '_compute_driver', return_value=driver), \
             patch.object(type(backup), '_read_manifest_safe', return_value=None), \
             patch.object(type(backup), '_generate_presigned_url', return_value='https://example.com/x.zip'), \
             patch.object(type(instance), '_docker_exec_sql', return_value=(0, '', '')) as m_sql, \
             patch.object(type(instance), '_docker_exec_psql_file', return_value=(0, '', '')):
            instance._do_restore_backup(backup.id)
        return m_sql

    def test_restore_on_staging_neutralizes_mail_and_crons(self):
        """A copy on Staging/Development must never mail customers or run
        scheduled actions: the restore ends with the neutralize SQL on
        that database (Odoo.sh behaviour)."""
        for kind in ('staging', 'development'):
            child = self.instance.copy({'subdomain': 'dbops-n-' + kind, 'environment': kind,
                                        'parent_id': self.instance.id, 'state': 'running'})
            m_sql = self._restore_with_mock(child)
            neutralize = [c for c in m_sql.call_args_list if 'ir_mail_server' in c.args[0]]
            self.assertEqual(len(neutralize), 1, kind)
            sql = neutralize[0].args[0]
            self.assertEqual(neutralize[0].kwargs.get('db'), 'dbops-n-%s_main' % kind)
            for table in ('ir_cron', 'fetchmail_server', 'payment_provider', 'iap_account',
                          'DELETE FROM mail_mail', 'database.is_neutralized'):
                self.assertIn(table, sql)
            self.assertEqual(child.state, 'running')

    def test_restore_on_staging_masks_customer_pii(self):
        """After neutralizing, a Staging/Development copy anonymizes contacts
        (names, emails, phones, addresses, bank accounts, leads, employees)
        but keeps the company and internal users."""
        child = self.instance.copy({'subdomain': 'dbops-pii', 'environment': 'staging',
                                    'parent_id': self.instance.id, 'state': 'running'})
        m_sql = self._restore_with_mock(child)
        masks = [c for c in m_sql.call_args_list if '@example.invalid' in c.args[0]]
        self.assertEqual(len(masks), 1)
        sql = masks[0].args[0]
        self.assertEqual(masks[0].kwargs.get('db'), 'dbops-pii_main')
        for token in ('res_partner', 'email', 'phone', 'mobile', 'street', 'vat',
                      'res_partner_bank', 'crm_lead', 'hr_employee',
                      'share IS NOT TRUE', 'FROM res_company'):
            self.assertIn(token, sql)
        # Neutralization runs first, masking second.
        order = [i for i, c in enumerate(m_sql.call_args_list)
                 if 'ir_mail_server' in c.args[0] or '@example.invalid' in c.args[0]]
        self.assertEqual(len(order), 2)
        self.assertIn('ir_mail_server', m_sql.call_args_list[order[0]].args[0])

    def test_restore_on_production_is_not_neutralized(self):
        m_sql = self._restore_with_mock(self.instance)
        self.assertFalse([c for c in m_sql.call_args_list if 'ir_mail_server' in c.args[0]])
        self.assertFalse([c for c in m_sql.call_args_list if '@example.invalid' in c.args[0]])

    def test_neutralize_failure_aborts_the_copy(self):
        child = self.instance.copy({'subdomain': 'dbops-nfail', 'environment': 'staging',
                                    'parent_id': self.instance.id, 'state': 'running'})
        with patch.object(type(child), '_docker_exec_sql', return_value=(1, '', 'boom')):
            with self.assertRaisesRegex(UserError, 'neutralize'):
                child._hosting_neutralize_database('dbops-nfail_main')

    def test_copy_without_overwrite_preserves_a_newly_created_target(self):
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
            m_sql.return_value = (1, '', 'database already exists')
            with self.assertRaisesRegex(UserError, 'createdb failed'):
                self.instance._do_restore_backup(backup.id, overwrite=False)

        driver.stop.assert_not_called()
        driver.start.assert_not_called()
        m_sql.assert_called_once()
        self.assertEqual(m_sql.call_args.args[0], 'CREATE DATABASE "dbopsinst_prod"')

    # -- serving customer databases ------------------------------------------

    def test_database_filter_serves_customer_databases(self):
        self.assertEqual(self.instance._k8s_database_filter(), '^dbopsinst_.+$')
        self.instance.is_hosting = False
        self.assertEqual(self.instance._k8s_database_filter(), '')

    # -- duplicate (in-pod, no XML-RPC: list_db is off) ------------------------

    def _duplicate(self, clone_side_effect=None, fs_side_effect=None):
        Instance = type(self.instance)
        with patch.object(Instance, '_hosting_database_limit', return_value=0), \
             patch.object(Instance, 'hosting_db_list',
                          return_value=[{'name': 'dbopsinst_prod', 'admin_login': ''}]), \
             patch.object(Instance, '_docker_exec_sql', return_value=(0, '', '')) as m_sql, \
             patch.object(Instance, '_pg_clone_db', side_effect=clone_side_effect) as m_clone, \
             patch.object(Instance, '_hosting_clone_filestore',
                          side_effect=fs_side_effect) as m_fs, \
             patch.object(Instance, '_hosting_drop_filestore'), \
             patch.object(Instance, '_pg_drop_db') as m_drop:
            result = self.instance.hosting_db_duplicate('prod', 'copy')
        return result, m_sql, m_clone, m_fs, m_drop

    def test_hosting_db_duplicate_copies_db_and_filestore(self):
        name, m_sql, m_clone, m_fs, _drop = self._duplicate()
        self.assertEqual(name, 'dbopsinst_copy')
        self.assertIn('pg_terminate_backend', m_sql.call_args_list[0].args[0])
        m_clone.assert_called_once_with('dbopsinst_prod', 'dbopsinst_copy')
        m_fs.assert_called_once_with('dbopsinst_prod', 'dbopsinst_copy')
        uuid_call = m_sql.call_args_list[-1]
        self.assertIn('database.uuid', uuid_call.args[0])
        self.assertEqual(uuid_call.kwargs['db'], 'dbopsinst_copy')
        self.m_filter.assert_called_once()

    def test_hosting_db_duplicate_retries_when_source_busy(self):
        busy = UserError('source database "dbopsinst_prod" is being accessed by other users')
        name, _sql, m_clone, _fs, _drop = self._duplicate(clone_side_effect=[busy, None])
        self.assertEqual(name, 'dbopsinst_copy')
        self.assertEqual(m_clone.call_count, 2)

    def test_hosting_db_duplicate_rolls_back_on_filestore_failure(self):
        with self.assertRaises(UserError):
            self._duplicate(fs_side_effect=RuntimeError('disk full'))

    # -- database manager link ---------------------------------------------

    def test_database_manager_url_is_signed_for_this_host(self):
        import base64
        import hashlib
        import hmac
        import json
        import time
        driver = self._driver()
        driver.hosting_access_ready.return_value = True
        driver.database_manager_key.return_value = 'k3y'
        with patch.object(type(self.instance), '_compute_driver', return_value=driver):
            url = self.instance.hosting_database_manager_url()
        host = self.instance.name
        self.assertTrue(url.startswith('https://%s/saas/dbm/enter?t=' % host))
        payload, sig = url.split('t=', 1)[1].rsplit('.', 1)
        self.assertEqual(sig, hmac.new(b'k3y', payload.encode(), hashlib.sha256).hexdigest())
        data = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        self.assertEqual(data['h'], host)
        self.assertGreater(data['e'], time.time())
        self.assertLessEqual(data['e'], time.time() + 180)

    def test_database_manager_url_switches_access_on_first(self):
        driver = self._driver()
        driver.hosting_access_ready.return_value = False
        with patch.object(type(self.instance), '_compute_driver', return_value=driver):
            with self.assertRaises(UserError):
                self.instance.hosting_database_manager_url()
        self.m_filter.assert_called_once_with()
        driver.database_manager_key.assert_not_called()

    def test_database_manager_url_only_for_running_hosting(self):
        self.instance.state = 'stopped'
        with self.assertRaises(UserError):
            self.instance.hosting_database_manager_url()


    def test_production_rejects_second_database_before_work(self):
        Instance = type(self.instance)
        with patch.object(Instance, 'hosting_db_list', return_value=[{'name': 'dbopsinst_prod'}]), \
             patch.object(Instance, '_hosting_ensure_template_db') as template, \
             patch.object(Instance, '_pg_clone_db') as clone:
            for action in [lambda: self.instance.hosting_db_create('other', 'admin', 'secret'),
                           lambda: self.instance.hosting_db_create_async('other', 'admin', 'secret'),
                           lambda: self.instance.hosting_db_duplicate('prod', 'other'),
                           lambda: self.instance.hosting_db_duplicate_async('prod', 'other'),
                           lambda: self.instance.hosting_db_restore_prepare_upload('other')]:
                with self.assertRaisesRegex(UserError, 'one customer database'):
                    action()
            template.assert_not_called()
            clone.assert_not_called()

    def test_production_capacity_allows_first_database_and_replacement(self):
        self.instance._check_hosting_database_capacity('dbopsinst_new', existing=set())
        self.instance._check_hosting_database_capacity('dbopsinst_prod', existing={'dbopsinst_prod'}, replacing=True)
        with self.assertRaises(UserError):
            self.instance._check_hosting_database_capacity('dbopsinst_new', existing={'dbopsinst_prod'}, replacing=True)

    def test_uploaded_replacement_requires_confirmation_and_persists_it(self):
        with patch.object(type(self.instance), 'hosting_db_list', return_value=[{'name': 'dbopsinst_prod'}]), \
             patch.object(type(self.env['saas.instance.backup']), '_generate_presigned_put_url', return_value='https://upload.example.com'):
            with self.assertRaises(UserError):
                self.instance.hosting_db_restore_prepare_upload('prod')
            backup, url = self.instance.hosting_db_restore_prepare_upload('prod', overwrite=True)
            self.assertTrue(backup.restore_overwrite)
            self.assertEqual(backup.db_name, 'dbopsinst_prod')

    def test_stage_and_development_allow_one_database_too(self):
        """Odoo.sh model: every server serves exactly one database."""
        for kind in ('staging', 'development'):
            child = self.instance.copy({'subdomain': 'dbops-' + kind, 'environment': kind, 'parent_id': self.instance.id})
            self.assertEqual(child._hosting_database_limit(), 1)
            child._check_hosting_database_capacity('dbops-%s_main' % kind, existing=set())
            child._check_hosting_database_capacity('dbops-%s_main' % kind, existing={'dbops-%s_main' % kind}, replacing=True)
            with self.assertRaisesRegex(UserError, 'one customer database'):
                child._check_hosting_database_capacity('another', existing={'one'})

    def test_production_atomic_capacity_guard_and_replacement(self):
        driver = self._driver()
        with patch.object(type(self.instance), '_compute_driver', return_value=driver):
            self.instance._docker_exec_sql('CREATE DATABASE "dbopsinst_new"')
            script = driver.exec.call_args.args[1]
            self.assertIn('pg_advisory_lock(7482910562)', script)
            self.assertIn("'dbopsinst_'", script)
            compile(script.split('\n', 1)[1].rsplit('\n', 1)[0], '<capacity>', 'exec')
            self.instance._docker_exec_sql('CREATE DATABASE "dbopsinst_prod"', replace_database=True)
            script = driver.exec.call_args.args[1]
            self.assertIn('DROP DATABASE IF EXISTS', script)
            compile(script.split('\n', 1)[1].rsplit('\n', 1)[0], '<replacement>', 'exec')
