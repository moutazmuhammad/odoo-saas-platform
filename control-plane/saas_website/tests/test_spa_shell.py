import os
import tempfile
from contextlib import contextmanager

from odoo.tests.common import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestSpaShellRoutes(HttpCase):
    """B.1.5: saas_website/controllers/spa.py had zero test coverage.

    Almost every route here is a one-line `return spa_shell()` — the
    SPA owns client-side routing/auth for these paths, so there's no
    portal.py-style per-route business logic to test. What IS worth
    testing: the handful of routes with real server-side branching
    (the `_section_enabled` gates, the logged-in-vs-public redirects on
    `/services/register` and `/register`, and `SaasWebLogin`'s
    redirect-to-/login override), plus `spa_shell()` itself — the
    theme-cookie injection and the "frontend not built" fallback,
    which is genuinely reachable right now since this checkout's
    saas_website/static/spa/ has no built index.html.
    """

    @contextmanager
    def _patched_shell_file(self, html=None):
        """Point spa.py's cached index.html at a file we control, so the
        theme-injection test doesn't depend on whether the frontend has
        actually been built in this checkout — and isn't left dangling
        for tests that run after it, since _INDEX_CACHE is a plain
        module-global dict shared across every request in the process."""
        from odoo.addons.saas_website.controllers import spa as spa_module
        orig_path = spa_module._INDEX_PATH
        orig_cache = dict(spa_module._INDEX_CACHE)
        if html is None:
            spa_module._INDEX_PATH = os.path.join(
                tempfile.gettempdir(), 'does-not-exist-%s.html' % os.getpid())
        else:
            fd, path = tempfile.mkstemp(suffix='.html')
            with os.fdopen(fd, 'w') as fh:
                fh.write(html)
            spa_module._INDEX_PATH = path
        spa_module._INDEX_CACHE['html'] = None
        spa_module._INDEX_CACHE['mtime'] = None
        try:
            yield
        finally:
            if html is not None:
                os.unlink(spa_module._INDEX_PATH)
            spa_module._INDEX_PATH = orig_path
            spa_module._INDEX_CACHE.clear()
            spa_module._INDEX_CACHE.update(orig_cache)

    # ---- spa_shell() itself -------------------------------------------

    def test_shell_serves_frontend_not_built_message_when_missing(self):
        with self._patched_shell_file(html=None):
            resp = self.url_open('/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('not built yet', resp.text)

    def test_shell_injects_dark_theme_by_default(self):
        with self._patched_shell_file(html='<html lang="en"><body>App</body></html>'):
            resp = self.url_open('/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('data-theme="dark"', resp.text)
        self.assertIn('class="dark"', resp.text)
        self.assertEqual(resp.cookies.get('veltnex-theme'), 'dark')

    def test_shell_honours_light_theme_cookie(self):
        with self._patched_shell_file(html='<html lang="en"><body>App</body></html>'):
            self.opener.cookies.set('veltnex-theme', 'light')
            resp = self.url_open('/')
        self.assertIn('data-theme="light"', resp.text)
        self.assertNotIn('class="dark"', resp.text)

    def test_shell_falls_back_to_dark_for_invalid_theme_cookie(self):
        with self._patched_shell_file(html='<html lang="en"><body>App</body></html>'):
            self.opener.cookies.set('veltnex-theme', 'not-a-real-theme')
            resp = self.url_open('/')
        self.assertIn('data-theme="dark"', resp.text)

    # ---- Section gating (/services, /hosting) --------------------------

    def test_services_page_enabled_by_default(self):
        resp = self.url_open('/services')
        self.assertEqual(resp.status_code, 200)

    def test_services_page_redirects_home_when_disabled(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'saas_master.show_services_section', 'False')
        resp = self.url_open('/services', allow_redirects=False)
        self.assertIn(resp.status_code, (301, 302, 303))
        self.assertEqual(resp.headers['Location'], '/')

    def test_hosting_page_redirects_home_when_disabled(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'saas_master.show_hosting_section', 'False')
        resp = self.url_open('/hosting', allow_redirects=False)
        self.assertEqual(resp.headers['Location'], '/')

    def test_service_plans_page_redirects_home_when_services_disabled(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'saas_master.show_services_section', 'False')
        resp = self.url_open('/services/1', allow_redirects=False)
        self.assertEqual(resp.headers['Location'], '/')

    # ---- Static shell-only routes — breadth, not depth -----------------

    def test_static_shell_routes_all_serve_200(self):
        for path in ('/', '/docs', '/docs/some-article', '/help', '/login',
                     '/my', '/my/home', '/my/instances', '/my/billing',
                     '/my/settings'):
            resp = self.url_open(path)
            self.assertEqual(
                resp.status_code, 200, "%s did not return 200" % path)

    # ---- /services/register and /register: logged-in vs. public -------

    def test_register_form_get_serves_shell_for_public_user(self):
        resp = self.url_open('/services/register')
        self.assertEqual(resp.status_code, 200)

    def test_register_form_get_redirects_logged_in_user(self):
        user = self.env['res.users'].sudo().create({
            'name': 'Spa User', 'login': 'spauser@example.com',
            'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])]})
        user.password = 'spauserpass1'
        self.authenticate('spauser@example.com', 'spauserpass1')
        resp = self.url_open(
            '/services/register?hosting=1&workers=4&storage=10',
            allow_redirects=False)
        self.assertIn(resp.status_code, (301, 302, 303))
        self.assertTrue(resp.headers['Location'].startswith('/hosting/configure'))

    def test_spa_register_serves_shell_for_public_user(self):
        resp = self.url_open('/register')
        self.assertEqual(resp.status_code, 200)

    def test_spa_register_redirects_logged_in_user_to_my(self):
        user = self.env['res.users'].sudo().create({
            'name': 'Spa User 2', 'login': 'spauser2@example.com',
            'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])]})
        user.password = 'spauserpass2'
        self.authenticate('spauser2@example.com', 'spauserpass2')
        resp = self.url_open('/register', allow_redirects=False)
        self.assertEqual(resp.headers['Location'], '/my')

    # ---- SaasWebLogin: funnel anonymous GETs to the SPA login page ----

    def test_web_login_redirects_anonymous_get_to_spa_login(self):
        self.authenticate(None, None)
        resp = self.url_open(
            '/web/login?redirect=/my/instances', allow_redirects=False)
        self.assertIn(resp.status_code, (301, 302, 303))
        self.assertTrue(resp.headers['Location'].startswith('/login'))
        self.assertIn('redirect=', resp.headers['Location'])

    def test_web_login_does_not_redirect_authenticated_get(self):
        user = self.env['res.users'].sudo().create({
            'name': 'Spa User 3', 'login': 'spauser3@example.com',
            'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])]})
        user.password = 'spauserpass3'
        self.authenticate('spauser3@example.com', 'spauserpass3')
        resp = self.url_open('/web/login', allow_redirects=False)
        location = resp.headers.get('Location', '')
        self.assertFalse(
            location.startswith('/login'),
            "an already-authenticated GET must not be bounced to /login")

    def test_arabic_shell_keeps_spa_urls_unprefixed(self):
        self.opener.cookies.set('veltnex-language', 'ar')
        self.opener.cookies.set('frontend_lang', 'ar_001')
        with self._patched_shell_file(html='<html lang="en"><body>App</body></html>'):
            response = self.url_open('/my/instances?category=shared', allow_redirects=False)
        self.assertEqual(response.status_code, 200)
        self.assertIn('lang="ar" dir="rtl"', response.text)

    def test_language_switch_preserves_destination_and_sets_both_cookies(self):
        response = self.url_open('/saas/language?lang=ar&redirect=%2Fmy%2Finstances%3Fcategory%3Dshared', allow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['Location'], '/my/instances?category=shared')
        self.assertEqual(response.cookies.get('veltnex-language'), 'ar')
        self.assertEqual(response.cookies.get('frontend_lang'), 'ar_001')

    def test_switch_to_english_removes_native_language_prefix(self):
        response = self.url_open('/saas/language?lang=en&redirect=%2Far%2Fmy%2Finvoices%3Fpage%3D2', allow_redirects=False)
        self.assertEqual(response.headers['Location'], '/my/invoices?page=2')
        self.assertEqual(response.cookies.get('frontend_lang'), 'en_US')

    def test_language_switch_rejects_external_redirects(self):
        from odoo.addons.saas_website.controllers.language import SaasLanguage
        for value in ('https://evil.example', '//evil.example/path', '/\\evil.example', '/\nevil.example', '/saas/language?lang=ar'):
            self.assertEqual(SaasLanguage._destination(value), '/')

    def test_arabic_cleanup_route_no_longer_exists(self):
        response = self.url_open('/saas/cleanup-arabic', allow_redirects=False)
        self.assertEqual(response.status_code, 404)

    def test_native_hosting_review_is_translated_and_has_language_control(self):
        user = self.env['res.users'].sudo().create({
            'name': 'Arabic Portal User', 'login': 'arabic-portal@example.com',
            'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])]})
        user.password = 'arabic-portal-test-password'
        self.authenticate(user.login, 'arabic-portal-test-password')
        self.opener.cookies.set('veltnex-language', 'ar')
        self.opener.cookies.set('frontend_lang', 'ar_001')
        response = self.url_open('/hosting/configure?workers=2&storage=5&error=Database%20%27demo%27%20already%20exists.')
        self.assertEqual(response.status_code, 200)
        self.assertIn('dir="rtl"', response.text)
        self.assertIn('ملخص الطلب', response.text)
        self.assertIn('/saas/language?lang=en', response.text)
        self.assertIn('veltnex-language-catalog', response.text)
        from lxml import html
        document = html.fromstring(response.content)
        banner = ''.join(document.xpath("//div[contains(@class, 'alert')]//text()"))
        self.assertIn("قاعدة البيانات 'demo' موجودة بالفعل.", banner)
        self.assertNotIn('href="/ar/my/instances', response.text)
