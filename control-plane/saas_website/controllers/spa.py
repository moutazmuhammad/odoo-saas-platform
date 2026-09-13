# -*- coding: utf-8 -*-
"""Serve the VELTNEX single-page app (SPA) and let it own the frontend.

Strategy — "replace the whole site at /":

* The built React app lives in ``static/spa/`` and is served by Odoo's
  ordinary module-static handler at ``/saas_website/static/spa/``
  (Vite is configured with that ``base``).
* We override the *page-rendering GET routes* of the existing
  controllers so they return the SPA shell instead of QWeb. We do this
  by subclassing each controller and re-declaring the same method — the
  most-derived class wins, so the original files stay untouched.
* We deliberately DO NOT touch the purchase funnel: the configure forms,
  ``/services/order`` / ``/hosting/order``, ``/checkout``, plan changes,
  and ``/my/invoices`` stay on Odoo QWeb so payment/billing keep working.
  The SPA hands off to those with a normal navigation when money is
  involved (see the "reuse Odoo checkout" decision).

So the SPA owns: ``/``, ``/services``, ``/services/<id>``,
``/services/register``, ``/hosting``, ``/docs``, ``/login``, ``/my`` and
the instance-management + billing-view pages under ``/my``.
"""
import os
import logging
from urllib.parse import urlencode

from odoo import http
from odoo.http import request
from odoo.addons.web.controllers.home import Home

from .main import SaasWebsite
from .portal import SaasPortal
from .registration import SaasRegistration

_logger = logging.getLogger(__name__)

_INDEX_PATH = os.path.join(
    os.path.dirname(__file__), '..', 'static', 'spa', 'index.html',
)
# Cache the shell HTML in-process, keyed by the file's mtime — so a fresh SPA
# build (new hashed bundle in index.html) is picked up automatically, without a
# server restart. Belt-and-braces vs. the old "cleared only on worker restart"
# behaviour, which made redeploys silently serve a stale shell.
_INDEX_CACHE = {'html': None, 'mtime': None}


def spa_shell():
    """Return the SPA's index.html as an HTTP response.

    The static `<html lang="en">` tag is rewritten on the fly to
    include `data-theme="…"` (and the matching `.dark` class) based
    on the `veltnex-theme` cookie. This means the first byte of HTML
    the browser receives already has the correct theme — the inline
    FOUC script then just confirms it, no flash.

    If the app hasn't been built yet, return a clear, friendly message
    instead of a 500 so a fresh checkout doesn't look broken.
    """
    try:
        mtime = os.path.getmtime(_INDEX_PATH)
    except OSError:
        mtime = None
    html = _INDEX_CACHE['html']
    if html is None or mtime != _INDEX_CACHE['mtime']:
        try:
            with open(_INDEX_PATH, 'r', encoding='utf-8') as fh:
                html = fh.read()
            _INDEX_CACHE['html'] = html
            _INDEX_CACHE['mtime'] = mtime
        except FileNotFoundError:
            _logger.warning("VELTNEX SPA not built — %s missing", _INDEX_PATH)
            return request.make_response(
                "<!doctype html><html><body style='font-family:sans-serif;"
                "background:#09090B;color:#FAFAFA;padding:3rem'>"
                "<h1>VELTNEX frontend not built yet</h1>"
                "<p>Run <code>npm install &amp;&amp; npm run build</code> in "
                "the <code>veltnex/</code> project to generate "
                "<code>saas_website/static/spa/</code>.</p></body></html>",
                headers=[('Content-Type', 'text/html; charset=utf-8')],
            )

    # Server-side theme injection — see cloudodoo_html_theme XML twin
    # for the QWeb side of the same mechanism.
    theme = request.httprequest.cookies.get('veltnex-theme', 'dark')
    if theme not in ('light', 'dark'):
        theme = 'dark'
    cls = ' class="dark"' if theme == 'dark' else ''
    # The built `<html lang="en">` has no other attributes, so a
    # straight string replace is safe — and keeps the cached shell
    # bytes intact (the cache holds the unmodified file).
    rendered = html.replace(
        '<html lang="en">',
        f'<html lang="en" data-theme="{theme}"{cls}>',
        1,
    )

    response = request.make_response(
        rendered,
        headers=[
            ('Content-Type', 'text/html; charset=utf-8'),
            # The shell is tiny and route-agnostic; let the browser cache
            # it briefly but always revalidate so a redeploy is picked up.
            ('Cache-Control', 'no-cache'),
        ],
    )
    # Pin the theme cookie on the response so the very next request
    # (e.g. clicking through to /hosting/configure) already carries
    # the value our QWeb override will read. This closes the gap
    # where a brand-new visitor lands on the SPA, has nothing in
    # localStorage / cookies, gets `dark` as the SPA default — but
    # would have ended up with `dark` on the QWeb page too because
    # the client-side JS hadn't run yet to backfill the cookie.
    response.set_cookie(
        'veltnex-theme', theme,
        max_age=60 * 60 * 24 * 365,  # 1 year
        path='/',
        samesite='Lax',
    )
    return response


# ----------------------------------------------------------------------
#  Overrides: repoint existing page routes at the SPA shell
# ----------------------------------------------------------------------

class SaasHomeSpa(http.Controller):
    @http.route('/', type='http', auth='public', website=True, sitemap=True)
    def home_page(self, **kw):
        return spa_shell()


class SaasWebsiteSpa(SaasWebsite):
    @http.route('/services', type='http', auth='public', website=True, sitemap=True)
    def services_page(self, **kw):
        if not self._section_enabled('services'):
            return request.redirect('/')
        return spa_shell()

    @http.route('/services/<int:product_id>', type='http', auth='public',
                website=True, sitemap=True)
    def service_plans(self, product_id, **kw):
        if not self._section_enabled('services'):
            return request.redirect('/')
        return spa_shell()

    @http.route('/hosting', type='http', auth='public', website=True, sitemap=True)
    def hosting_page(self, **kw):
        if not self._section_enabled('hosting'):
            return request.redirect('/')
        return spa_shell()

    @http.route('/docs', type='http', auth='public', website=True, sitemap=True)
    def docs_page(self, **kw):
        return spa_shell()

    @http.route('/docs/<string:slug>', type='http', auth='public',
                website=True, sitemap=False)
    def docs_article(self, slug, **kw):
        return spa_shell()

    @http.route('/help', type='http', auth='public', website=True, sitemap=True)
    def help_page(self, **kw):
        return spa_shell()


class SaasRegistrationSpa(SaasRegistration):
    @http.route('/services/register', type='http', auth='public',
                website=True, methods=['GET', 'POST'], sitemap=False)
    def register_form(self, **post):
        # The SPA drives registration through the JSON API
        # (/saas/api/v1/auth/register/*). A bare GET just loads the app;
        # any legacy POST still falls through to the original handler so
        # nothing regresses for non-SPA callers.
        if request.httprequest.method == 'GET':
            if not request.env.user._is_public():
                return request.redirect(self._build_redirect_url(post))
            return spa_shell()
        return super().register_form(**post)

    @http.route('/register', type='http', auth='public',
                website=True, methods=['GET'], sitemap=False)
    def spa_register(self, **kw):
        # Generic sign-up entry point — NOT gated by the services
        # section, so "Create an account" / "Get started" works even when
        # only hosting is enabled.
        if not request.env.user._is_public():
            return request.redirect('/my')
        return spa_shell()


class SaasPortalSpa(SaasPortal):

    # --- Portal landing (was the QWeb "My Account" home) ---------------
    @http.route(['/my', '/my/home'], type='http', auth='public', website=True)
    def home(self, **kw):
        return spa_shell()

    # --- Instance management views -------------------------------------
    @http.route(['/my/instances', '/my/instances/page/<int:page>'],
                type='http', auth='public', website=True)
    def portal_my_instances(self, page=1, **kw):
        return spa_shell()

    @http.route('/my/instances/<int:instance_id>',
                type='http', auth='public', website=True)
    def portal_my_instance_detail(self, instance_id, access_token=None, **kw):
        return spa_shell()

    @http.route('/my/instances/<int:instance_id>/databases',
                type='http', auth='public', website=True)
    def portal_instance_databases(self, instance_id, access_token=None,
                                  error=None, notice=None, **kw):
        return spa_shell()

    @http.route('/my/instances/<int:instance_id>/code',
                type='http', auth='public', website=True)
    def portal_instance_code(self, instance_id, access_token=None, **kw):
        return spa_shell()

    @http.route('/my/instances/<int:instance_id>/backups',
                type='http', auth='public', website=True)
    def portal_instance_backups(self, instance_id, access_token=None,
                                error=None, notice=None, **kw):
        return spa_shell()

    # --- New SPA-only routes (no QWeb equivalent existed) --------------
    @http.route('/my/instances/<int:instance_id>/logs',
                type='http', auth='public', website=True)
    def portal_instance_logs(self, instance_id, access_token=None, **kw):
        return spa_shell()

    @http.route('/my/instances/<int:instance_id>/metrics',
                type='http', auth='public', website=True)
    def portal_instance_metrics(self, instance_id, access_token=None, **kw):
        return spa_shell()

    @http.route('/my/instances/<int:instance_id>/shell',
                type='http', auth='public', website=True)
    def portal_instance_shell(self, instance_id, access_token=None, **kw):
        return spa_shell()

    @http.route('/my/instances/<int:instance_id>/sql',
                type='http', auth='public', website=True)
    def portal_instance_sql(self, instance_id, access_token=None, **kw):
        return spa_shell()

    @http.route('/my/instances/<int:instance_id>/environments',
                type='http', auth='public', website=True)
    def portal_instance_environments(self, instance_id, access_token=None,
                                     **kw):
        return spa_shell()

    # Billing list/detail live at /my/billing so Odoo keeps owning
    # /my/invoices/<id> (the canonical PDF + payment portal URL the
    # "pay invoice" button links to).
    @http.route('/my/billing', type='http', auth='public', website=True)
    def portal_billing(self, **kw):
        return spa_shell()

    @http.route('/my/settings', type='http', auth='public', website=True)
    def portal_settings(self, **kw):
        return spa_shell()

    @http.route('/my/billing/<int:invoice_id>',
                type='http', auth='public', website=True)
    def portal_billing_detail(self, invoice_id, **kw):
        return spa_shell()


class SaasSpaAux(http.Controller):
    """Routes the SPA needs that have no controller to subclass."""

    @http.route('/login', type='http', auth='public', website=True, sitemap=False)
    def spa_login(self, **kw):
        return spa_shell()


class SaasWebLogin(Home):
    """Funnel every visitor through the single branded SPA login.

    We don't want two login pages. Odoo's stock ``/web/login`` form is
    kept (password-reset completion, 2FA, and any programmatic POST still
    rely on it) but a plain anonymous GET is bounced to ``/login`` — the
    React page — carrying the ``redirect`` target so post-login deep-links
    (e.g. ``/odoo`` for staff, a portal page for customers) still work.

    Authenticated GETs and non-GET requests fall through to the stock
    handler unchanged.
    """

    @http.route()
    def web_login(self, redirect=None, **kw):
        if request.httprequest.method == 'GET' and not request.session.uid:
            url = '/login'
            if redirect:
                url += '?' + urlencode({'redirect': redirect})
            return request.redirect(url)
        return super().web_login(redirect=redirect, **kw)
