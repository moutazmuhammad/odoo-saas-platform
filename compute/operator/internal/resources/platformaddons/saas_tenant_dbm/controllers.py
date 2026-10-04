"""Database manager access for the instance's customer.

The SaaS portal signs a link with this instance's key (odoo.conf
``saas_dbm_key``): ``/saas/dbm/enter?t=<payload>.<hmac>``, where the
payload is base64url JSON ``{"h": host, "e": expiry, "n": nonce}``. A
valid link sets a signed cookie (path /web/database, one hour) during
which the database manager works without the master password, limited to
database names starting with ``saas_dbm_prefix``. Without it, the manager
only shows a pointer to the portal. Public visitors can select a database
limited by the hostname filter and tenant prefix, without administration
controls. The JSON database list still requires a signed manager cookie.

A cookie rather than the Odoo session: creating a database logs the
browser into it, which resets the session. Every action also checks the
Origin/Referer, because all tenants share one parent domain and so count
as "same-site" for the SameSite cookie rule.
"""
import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from html import escape
from urllib.parse import urlencode, urlparse

import odoo
from odoo import http
from odoo.http import request
try:
    from odoo.service import db as db_service
except ImportError:  # Odoo 20 moved database management out of RPC services.
    from odoo.modules import db as db_service
from odoo.tools import config

from odoo.addons.web.controllers.database import Database
from odoo.addons.web.controllers import database as web_database

_logger = logging.getLogger(__name__)

COOKIE = 'saas_dbm'
COOKIE_PATH = '/web/database'
ACCESS_SECONDS = 3600

_bypass = threading.local()
_password_check_name = 'check_super' if hasattr(db_service, 'check_super') else 'verify_admin_password'
_check_super = getattr(db_service, _password_check_name)


def check_super(passwd):
    if getattr(_bypass, 'active', False):
        return True
    return _check_super(passwd)


# dispatch() and the web controllers look check_super up on the module at
# call time, so replacing the module attribute covers every path.
setattr(db_service, _password_check_name, check_super)


def _db_list():
    if hasattr(http, 'db_list'):
        return http.db_list()
    from odoo.http.router import db_list
    return db_list()


def _prefix():
    return config.get('saas_dbm_prefix') or ''


def _key():
    return (config.get('saas_dbm_key') or '').encode()


def _request_host():
    return (request.httprequest.host or '').split(':')[0].lower()


def _sign(message):
    return hmac.new(_key(), message.encode(), hashlib.sha256).hexdigest()


def _verify(token):
    if not _key() or not token or '.' not in token:
        return False
    payload, sig = token.rsplit('.', 1)
    if not hmac.compare_digest(_sign(payload), sig):
        return False
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
    except ValueError:
        return False
    return data.get('h') == _request_host() and float(data.get('e') or 0) > time.time()


def _access_cookie(until):
    until = str(int(until))
    return until + '.' + _sign('access|%s|%s' % (_request_host(), until))


def _allowed():
    value = request.httprequest.cookies.get(COOKIE) or ''
    if not _key() or '.' not in value:
        return False
    until, sig = value.split('.', 1)
    if not until.isdigit() or int(until) < time.time():
        return False
    return hmac.compare_digest(_access_cookie(until), value)


def _same_origin():
    headers = request.httprequest.headers
    source = headers.get('Origin') or headers.get('Referer') or ''
    return (urlparse(source).hostname or '').lower() == _request_host()


def _own(name):
    prefix = _prefix()
    return bool(prefix and name and name.startswith(prefix) and len(name) > len(prefix))


def _with_prefix(name):
    name = (name or '').strip()
    return name if name.startswith(_prefix()) else _prefix() + name


class _Bypass:
    def __enter__(self):
        _bypass.active = True

    def __exit__(self, *exc):
        _bypass.active = False


_HIDE_MASTER_PASSWORD = """
<style>.o_database_list [data-bs-target=".o_database_master"]{display:none!important}</style>
<script>
document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('input[name="master_pwd"]').forEach(function (input) {
        input.value = 'saas-portal';
        input.removeAttribute('required');
        var row = input.closest('.row');
        (row || input).style.display = 'none';
    });
    document.querySelectorAll('[data-bs-target=".o_database_master"]').forEach(function (b) { b.remove(); });
    var prefix = %s;
    document.querySelectorAll('input[name="name"], input[name="new_name"]').forEach(function (input) {
        if (input.type === 'hidden') { return; }
        var hint = document.createElement('small');
        hint.className = 'form-text text-muted';
        hint.textContent = 'Saved as ' + prefix + '<name>';
        input.parentNode.appendChild(hint);
    });
});
</script>
"""

_PORTAL_ONLY = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Secure access · Database Manager</title>
<style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:32px 20px;background:#f6f5fb;color:#242033;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{width:min(100vw - 40px,540px);padding:40px;border:1px solid #e8e5f0;border-radius:24px;background:#fff;box-shadow:0 16px 48px #3422540d}
.icon{display:grid;place-items:center;width:60px;height:60px;margin-bottom:24px;border-radius:18px;background:#f0eafa;color:#7953ba}
.eyebrow{margin:0 0 10px;color:#7953ba;font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase}
h1{margin:0;font-size:28px;line-height:1.25;letter-spacing:-.7px}p{font-size:15px;line-height:1.7;color:#70697d}
.steps{margin:24px 0;padding:18px 20px;border:1px solid #e9e3f3;border-radius:14px;background:#faf8fe;font-size:14px;line-height:1.8;color:#554b65}
.steps strong{color:#30263f}.expiry{display:flex;align-items:center;gap:8px;font-size:13px;color:#81758f}.notice:empty{display:none}.notice p{padding:12px 16px;border-radius:10px;background:#fff0f0;color:#a13737;font-size:13px}
footer{display:flex;align-items:center;gap:8px;margin-top:32px;padding-top:20px;border-top:1px solid #eeeaf3;font-size:12px;color:#9b93a6}footer strong{color:#635575;letter-spacing:1px;font-size:11px}
@media(max-width:480px){main{padding:28px 24px}h1{font-size:24px}}
</style></head><body><main>
<div class="icon" aria-hidden="true"><svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/></svg></div>
<p class="eyebrow">Secure database access</p>
<h1>Open Database Manager<br>from your control panel</h1>
<p>For your security, Database Manager opens through a secure link from your instance's Databases page.</p>
<div class="steps">Go to <strong>your control panel → your instance → Databases</strong>, then select <strong>Database Manager</strong>.</div>
<div class="notice" role="alert">%s</div>
<footer>Powered by <strong>VELTNEX</strong></footer>
</main></body></html>"""


class SaasTenantDatabase(Database):

    @http.route('/saas/dbm/enter', type='http', auth='none')
    def saas_dbm_enter(self, t=None, **kw):
        if not _verify(t):
            return request.make_response(_PORTAL_ONLY % '<p><b>This link is invalid or expired.</b></p>', status=403)
        response = request.redirect('/web/database/manager')
        response.set_cookie(
            COOKIE, _access_cookie(time.time() + ACCESS_SECONDS), max_age=ACCESS_SECONDS,
            path=COOKIE_PATH, httponly=True, samesite='Lax',
            secure=request.httprequest.scheme == 'https')
        return response

    def _render_template(self, **d):
        render = getattr(super(), '_render_template', None) or web_database._render_template
        html = render(**d)
        if d.get('manage', True) and _allowed():
            script = _HIDE_MASTER_PASSWORD % json.dumps(escape(_prefix()))
            limit = int(config.get('saas_dbm_max_databases') or 0)
            if limit and len([name for name in _db_list() if _own(name)]) >= limit:
                script += """<style>
[data-bs-target=".o_database_create"],[data-bs-target=".o_database_duplicate"],[data-bs-target=".o_database_restore"]{display:none!important}
</style><script>document.addEventListener('DOMContentLoaded', function () {
var notice = document.createElement('p'); notice.className = 'alert alert-info';
notice.textContent = 'Production allows one database. Use your control panel to restore a backup into the existing database, or delete it before creating a replacement.';
var list = document.querySelector('.o_database_list'); if (list) { list.prepend(notice); }
});</script>"""

            # The template renders to Markup, whose replace() would escape
            # the script; work on the plain string.
            html = str(html).replace('</head>', script + '</head>', 1)
        return html

    @http.route('/web/database/manager', type='http', auth='none')
    def manager(self, **kw):
        if not _allowed():
            return request.make_response(_PORTAL_ONLY % '')
        if not hasattr(Database, '_render_template'):
            # Odoo 20 renders through a module function rather than an
            # overridable method; keep our access controls and template edits.
            if request.db:
                request.env.cr.close()
            return self._render_template()
        return super().manager(**kw)

    @http.route('/web/database/selector', type='http', auth='none')
    def selector(self, **kw):
        databases = self._public_databases()
        if len(databases) == 1:
            return request.redirect('/saas/db/select?' + urlencode({'db': databases[0]}))
        choices = ''.join(
            '<a href="%s">%s<span aria-hidden="true"> →</span></a>' % (
                escape('/saas/db/select?' + urlencode({'db': name}), quote=True), escape(name))
            for name in databases)
        heading = 'Choose your workspace' if databases else 'No database has been created yet'
        description = ('Select a workspace to continue to its website.' if databases
                       else 'The site owner can create or restore a database from the control panel to get started.')
        html = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>%s</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f7f8fa;color:#202735;font-family:system-ui,sans-serif}
main{width:min(440px,calc(100vw - 80px));padding:32px;background:white;border:1px solid #e6e9ee;border-radius:18px}
h1{font-size:26px;margin:0}p{color:#667085;line-height:1.6}a{display:flex;justify-content:space-between;gap:16px;overflow-wrap:anywhere;margin-top:12px;padding:16px;border:1px solid #e6e9ee;border-radius:10px;color:#364152;text-decoration:none}a:hover,a:focus{background:#f1f4f9;border-color:#8492a6}</style>
</head><body><main><h1>%s</h1><p>%s</p>%s</main></body></html>""" % (heading, heading, description, choices)
        return request.make_response(html, headers=[('Cache-Control', 'no-store')])

    def _public_databases(self):
        # db_list applies Odoo's hostname filter; the prefix additionally
        # prevents choosing another tenant's database on a shared server.
        return sorted(name for name in _db_list()
                      if not _prefix() or _own(name))

    @http.route('/saas/db/select', type='http', auth='none', methods=['GET'])
    def select_database(self, db=None, **kw):
        if not db or db not in self._public_databases():
            return request.redirect('/web/database/selector')
        if request.session.db != db:
            if hasattr(request.session, 'logout'):
                request.session.logout(keep_db=False)
            else:
                from odoo.http.session import logout
                logout(request.session, keep_db=False)
            request.session.db = db
        # Selecting a workspace opens the public website, which can also
        # serve anonymous visitors, rather than forcing a backend login.
        return request.redirect('/')

    # Inherit the upstream route type: Odoo 17/18 use json and 19 uses jsonrpc.
    @http.route()
    def list(self):
        # JSON twin of the selector — same database-name leak.
        if not _allowed():
            return []
        return super().list()

    def _guarded(self, call, new_database=None):
        if not _allowed():
            return self._render_template(error="Open the database manager from your control panel.")
        if not _same_origin():
            return self._render_template(error="Request refused: it did not come from this page.")
        limit = int(config.get('saas_dbm_max_databases') or 0)
        if not new_database or not limit:
            with _Bypass():
                return call()
        # Same PostgreSQL lock as portal CREATE DATABASE: multiple HTTP
        # workers and control-panel jobs cannot claim the final slot twice.
        with odoo.sql_db.db_connect('postgres').cursor() as cr:
            cr.execute('SELECT pg_advisory_lock(7482910562)')
            try:
                existing = [name for name in db_service.list_dbs(force=True) if _own(name)]
                if len(existing) >= limit:
                    return self._refuse('Production allows one customer database. Restore the existing database from your control panel, or delete it before creating another.')
                with _Bypass():
                    return call()
            finally:
                cr.execute('SELECT pg_advisory_unlock(7482910562)')

    def _refuse(self, message):
        return self._render_template(error=message)

    @http.route('/web/database/create', type='http', auth='none', methods=['POST'], csrf=False)
    def create(self, master_pwd, name, lang, password, **post):
        name = _with_prefix(name)
        return self._guarded(lambda: super(SaasTenantDatabase, self).create(
            master_pwd, name, lang, password, **post), new_database=name)

    @http.route('/web/database/duplicate', type='http', auth='none', methods=['POST'], csrf=False)
    def duplicate(self, master_pwd, name, new_name, neutralize_database=False):
        if not _own(name):
            return self._refuse("Database %r is not yours." % name)
        new_name = _with_prefix(new_name)
        return self._guarded(lambda: super(SaasTenantDatabase, self).duplicate(
            master_pwd, name, new_name, neutralize_database), new_database=new_name)

    @http.route('/web/database/drop', type='http', auth='none', methods=['POST'], csrf=False)
    def drop(self, master_pwd, name):
        if not _own(name):
            return self._refuse("Database %r is not yours." % name)
        return self._guarded(lambda: super(SaasTenantDatabase, self).drop(master_pwd, name))

    @http.route('/web/database/rename', type='http', auth='none', methods=['POST'], csrf=False)
    def rename(self, master_pwd, name, new_name):
        # Odoo 20 added this route. Never inherit an unguarded operation
        # that could rename another database or remove our tenant prefix.
        if not _own(name):
            return self._refuse("Database %r is not yours." % name)
        rename = getattr(super(), 'rename', None)
        if not rename:
            return self._refuse('Database renaming is unavailable in this Odoo version.')
        return self._guarded(lambda: rename(master_pwd, name, _with_prefix(new_name)))

    @http.route('/web/database/backup', type='http', auth='none', methods=['POST'], csrf=False)
    def backup(self, master_pwd, name, backup_format='zip', **kw):
        if not _own(name):
            return self._refuse("Database %r is not yours." % name)
        return self._guarded(lambda: super(SaasTenantDatabase, self).backup(
            master_pwd, name, backup_format, **kw))

    @http.route('/web/database/restore', type='http', auth='none', methods=['POST'], csrf=False,
                max_content_length=None)
    def restore(self, master_pwd, backup_file, name, copy=False, neutralize_database=False):
        name = _with_prefix(name)
        return self._guarded(lambda: super(SaasTenantDatabase, self).restore(
            master_pwd, backup_file, name, copy, neutralize_database), new_database=name)

    @http.route('/web/database/change_password', type='http', auth='none', methods=['POST'], csrf=False)
    def change_password(self, master_pwd, master_pwd_new):
        return self._refuse("The master password is managed by the platform.")
