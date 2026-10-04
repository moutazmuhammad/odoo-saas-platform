"""Public website selection must never grant database administration."""
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


def module(name, **attrs):
    result = ModuleType(name)
    result.__dict__.update(attrs)
    return result


http = module('odoo.http', route=lambda *a, **k: lambda f: f, request=Mock())
config = {}
stubs = {
    'odoo': module('odoo', http=http), 'odoo.http': http,
    'odoo.service': module('odoo.service'),
    'odoo.service.db': module('odoo.service.db', check_super=lambda p: False),
    'odoo.tools': module('odoo.tools', config=config),
    'odoo.addons': module('odoo.addons'), 'odoo.addons.web': module('odoo.addons.web'),
    'odoo.addons.web.controllers': module('odoo.addons.web.controllers'),
    'odoo.addons.web.controllers.database': module('odoo.addons.web.controllers.database', Database=object),
}
path = Path(__file__).resolve().parents[1] / 'internal/resources/platformaddons/saas_tenant_dbm/controllers.py'
spec = importlib.util.spec_from_file_location('tenant_dbm_test', path)
dbm = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, stubs):
    spec.loader.exec_module(dbm)


class PublicDatabaseSelection(unittest.TestCase):
    def setUp(self):
        config.clear()
        config['saas_dbm_prefix'] = 'tenant_'
        self.request = Mock()
        self.request.make_response.side_effect = lambda html, **kw: html
        self.request.redirect.side_effect = lambda url: url
        self.request.session.db = None
        self.controller = dbm.SaasTenantDatabase()
        self.request_patch = patch.object(dbm, 'request', self.request)
        self.request_patch.start()
        self.addCleanup(self.request_patch.stop)
        http.db_list = Mock(return_value=['tenant_shop', 'tenant_blog', 'other_secret'])

    def test_multiple_databases_only_show_scoped_safe_website_choices(self):
        html = self.controller.selector()
        self.assertIn('Choose your workspace', html)
        self.assertIn('/saas/db/select?db=tenant_shop', html)
        self.assertNotIn('other_secret', html)
        for forbidden in ['control panel', 'Database Manager', 'master_pwd', '/web/database/create', 'Powered by']:
            self.assertNotIn(forbidden, html)

    def test_names_are_html_escaped_and_urls_encoded(self):
        http.db_list.return_value = ['tenant_<script>', 'tenant_a&b']
        html = self.controller.selector()
        self.assertNotIn('<script>', html)
        self.assertIn('tenant_&lt;script&gt;', html)
        self.assertIn('tenant_a%26b', html)

    def test_single_database_skips_choice(self):
        http.db_list.return_value = ['tenant_shop', 'other_secret']
        self.assertEqual(self.controller.selector(), '/saas/db/select?db=tenant_shop')

    def test_no_database_has_no_create_or_manager_controls(self):
        http.db_list.return_value = []
        html = self.controller.selector()
        self.assertIn('contact the site owner', html)
        self.assertNotIn('<a ', html)
        self.assertNotIn('control panel', html)

    def test_selection_opens_public_home_and_clears_previous_login(self):
        self.request.session.db = 'tenant_blog'
        self.assertEqual(self.controller.select_database('tenant_shop'), '/')
        self.request.session.logout.assert_called_once_with(keep_db=False)
        self.assertEqual(self.request.session.db, 'tenant_shop')

    def test_same_database_preserves_login(self):
        self.request.session.db = 'tenant_shop'
        self.controller.select_database('tenant_shop')
        self.request.session.logout.assert_not_called()

    def test_foreign_and_missing_databases_cannot_change_session(self):
        for name in ['other_secret', 'tenant_missing', None]:
            self.assertEqual(self.controller.select_database(name), '/web/database/selector')
        self.request.session.logout.assert_not_called()
        self.assertIsNone(self.request.session.db)

    def test_manager_and_json_list_remain_protected(self):
        with patch.object(dbm, '_allowed', return_value=False):
            html = self.controller.manager()
            self.assertIn('from your control panel', html)
            self.assertNotIn('Each secure link is valid for one hour', html)
            self.assertEqual(self.controller.list(), [])

    def test_manager_capacity_checks_run_under_shared_postgresql_lock(self):
        config['saas_dbm_max_databases'] = 1
        cursor = Mock()
        context = Mock()
        context.__enter__ = Mock(return_value=cursor)
        context.__exit__ = Mock(return_value=False)
        dbm.odoo.sql_db = SimpleNamespace(db_connect=Mock(return_value=SimpleNamespace(cursor=lambda: context)))
        dbm.db_service.list_dbs = Mock(return_value=['tenant_shop', 'other_secret'])
        operation = Mock(return_value='created')
        with patch.object(dbm, '_allowed', return_value=True), patch.object(dbm, '_same_origin', return_value=True), \
             patch.object(self.controller, '_refuse', return_value='full'):
            self.assertEqual(self.controller._guarded(operation, new_database='tenant_new'), 'full')
            operation.assert_not_called()
            cursor.execute.assert_any_call('SELECT pg_advisory_lock(7482910562)')
            cursor.execute.assert_any_call('SELECT pg_advisory_unlock(7482910562)')
            dbm.db_service.list_dbs.return_value = ['other_secret']
            self.assertEqual(self.controller._guarded(operation, new_database='tenant_new'), 'created')
            operation.assert_called_once()

    def test_unlimited_environments_and_delete_have_no_capacity_restriction(self):
        operation = Mock(return_value='done')
        with patch.object(dbm, '_allowed', return_value=True), patch.object(dbm, '_same_origin', return_value=True):
            self.assertEqual(self.controller._guarded(operation, new_database='tenant_new'), 'done')
            config['saas_dbm_max_databases'] = 1
            self.assertEqual(self.controller._guarded(operation), 'done')



if __name__ == '__main__':
    unittest.main()
