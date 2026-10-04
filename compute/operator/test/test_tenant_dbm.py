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
        self.assertIn('No database has been created yet', html)
        self.assertIn('create or restore a database from the control panel', html)
        self.assertNotIn('This website isn’t ready yet', html)
        self.assertNotIn('<a ', html)
        self.assertNotIn('/web/database/create', html)
        self.assertNotIn('master_pwd', html)

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


class Odoo20DatabaseCompatibility(unittest.TestCase):
    def setUp(self):
        config.clear()
        config['saas_dbm_prefix'] = 'tenant_'
        self.password_check = Mock(side_effect=PermissionError('master password required'))
        self.router_list = Mock(return_value=['tenant_shop', 'tenant_blog', 'other_secret'])
        self.logout = Mock()
        self.render = Mock(return_value='<html><head></head><body>manager</body></html>')
        self.rename = Mock(return_value='renamed')
        base = type('Database20', (), {'rename': lambda _self, *args: self.rename(*args)})
        request = Mock()
        request.db = None
        request.session = SimpleNamespace(db=None)
        request.redirect.side_effect = lambda url: url
        request.make_response.side_effect = lambda html, **kw: html
        modules = {key: value for key, value in stubs.items() if key != 'odoo.service.db'}
        modules.update({
            'odoo.http': module('odoo.http', route=lambda *a, **kw: lambda fn: fn, request=request),
            'odoo.modules': module('odoo.modules'),
            'odoo.modules.db': module('odoo.modules.db', verify_admin_password=self.password_check),
            'odoo.http.router': module('odoo.http.router', db_list=self.router_list),
            'odoo.http.session': module('odoo.http.session', logout=self.logout),
            'odoo.addons.web.controllers.database': module('odoo.addons.web.controllers.database',
                                                          Database=base, _render_template=self.render),
        })
        modules['odoo'] = module('odoo', http=modules['odoo.http'])
        self.patch = patch.dict(sys.modules, modules)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        spec = importlib.util.spec_from_file_location('tenant_dbm_20_test', path)
        self.addon = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.addon)
        self.controller = self.addon.SaasTenantDatabase()

    def test_20_database_password_checks_only_bypass_inside_guard(self):
        with self.assertRaises(PermissionError):
            self.addon.db_service.verify_admin_password('incorrect')
        with self.addon._Bypass():
            self.addon.db_service.verify_admin_password('saas-portal')
        with self.assertRaises(PermissionError):
            self.addon.db_service.verify_admin_password('incorrect')

    def test_20_router_and_session_selection_preserve_tenant_scope(self):
        self.assertEqual(self.controller._public_databases(), ['tenant_blog', 'tenant_shop'])
        self.assertEqual(self.controller.select_database('tenant_shop'), '/')
        self.logout.assert_called_once_with(self.addon.request.session, keep_db=False)
        self.assertEqual(self.addon.request.session.db, 'tenant_shop')

    def test_20_module_template_retains_portal_controls(self):
        with patch.object(self.addon, '_allowed', return_value=True):
            html = self.controller.manager()
        self.render.assert_called_once()
        self.assertIn("input[name=\"master_pwd\"]", html)
        self.assertIn('Saved as ', html)

    def test_20_rename_cannot_escape_access_and_prefix_controls(self):
        with patch.object(self.controller, '_refuse', return_value='refused'):
            self.assertEqual(self.controller.rename('unused', 'other_secret', 'new'), 'refused')
            self.rename.assert_not_called()
        with patch.object(self.addon, '_allowed', return_value=False), \
                patch.object(self.controller, '_render_template', return_value='locked'):
            self.assertEqual(self.controller.rename('unused', 'tenant_shop', 'new'), 'locked')
            self.rename.assert_not_called()
        with patch.object(self.addon, '_allowed', return_value=True), \
                patch.object(self.addon, '_same_origin', return_value=True):
            self.assertEqual(self.controller.rename('unused', 'tenant_shop', 'new'), 'renamed')
            self.rename.assert_called_once_with('unused', 'tenant_shop', 'tenant_new')


if __name__ == '__main__':
    unittest.main()
