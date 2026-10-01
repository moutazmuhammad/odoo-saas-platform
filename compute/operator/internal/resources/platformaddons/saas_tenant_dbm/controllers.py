"""Database manager access for the instance's customer.

The SaaS portal signs a link with this instance's key (odoo.conf
``saas_dbm_key``): ``/saas/dbm/enter?t=<payload>.<hmac>``, where the
payload is base64url JSON ``{"h": host, "e": expiry, "n": nonce}``. A
valid link sets a signed cookie (path /web/database, one hour) during
which the database manager works without the master password, limited to
database names starting with ``saas_dbm_prefix``. Without it, the manager
only shows a pointer to the portal.

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
from urllib.parse import urlparse

import odoo
from odoo import http
from odoo.http import request
from odoo.service import db as db_service
from odoo.tools import config

from odoo.addons.web.controllers.database import Database

_logger = logging.getLogger(__name__)

COOKIE = 'saas_dbm'
COOKIE_PATH = '/web/database'
ACCESS_SECONDS = 3600

_bypass = threading.local()
_check_super = db_service.check_super


def check_super(passwd):
    if getattr(_bypass, 'active', False):
        return True
    return _check_super(passwd)


# dispatch() and the web controllers look check_super up on the module at
# call time, so replacing the module attribute covers every path.
db_service.check_super = check_super


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
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Database Manager</title>
<style>body{font-family:system-ui,sans-serif;max-width:520px;margin:15vh auto;padding:0 16px;color:#222}
h1{font-size:1.4rem}p{color:#555;line-height:1.5}</style></head>
<body><h1>Open the database manager from your control panel</h1>
<p>For your security, the database manager opens only through the
<b>Database Manager</b> button on your instance's Databases page. The link is valid for
one hour.</p>%s</body></html>"""


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
        html = super()._render_template(**d)
        if d.get('manage', True) and _allowed():
            script = _HIDE_MASTER_PASSWORD % json.dumps(escape(_prefix()))
            # The template renders to Markup, whose replace() would escape
            # the script; work on the plain string.
            html = str(html).replace('</head>', script + '</head>', 1)
        return html

    @http.route('/web/database/manager', type='http', auth='none')
    def manager(self, **kw):
        if not _allowed():
            return request.make_response(_PORTAL_ONLY % '')
        return super().manager(**kw)

    def _guarded(self, call):
        if not _allowed():
            return self._render_template(error="Open the database manager from your control panel.")
        if not _same_origin():
            return self._render_template(error="Request refused: it did not come from this page.")
        with _Bypass():
            return call()

    def _refuse(self, message):
        return self._render_template(error=message)

    @http.route('/web/database/create', type='http', auth='none', methods=['POST'], csrf=False)
    def create(self, master_pwd, name, lang, password, **post):
        name = _with_prefix(name)
        return self._guarded(lambda: super(SaasTenantDatabase, self).create(
            master_pwd, name, lang, password, **post))

    @http.route('/web/database/duplicate', type='http', auth='none', methods=['POST'], csrf=False)
    def duplicate(self, master_pwd, name, new_name, neutralize_database=False):
        if not _own(name):
            return self._refuse("Database %r is not yours." % name)
        new_name = _with_prefix(new_name)
        return self._guarded(lambda: super(SaasTenantDatabase, self).duplicate(
            master_pwd, name, new_name, neutralize_database))

    @http.route('/web/database/drop', type='http', auth='none', methods=['POST'], csrf=False)
    def drop(self, master_pwd, name):
        if not _own(name):
            return self._refuse("Database %r is not yours." % name)
        return self._guarded(lambda: super(SaasTenantDatabase, self).drop(master_pwd, name))

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
            master_pwd, backup_file, name, copy, neutralize_database))

    @http.route('/web/database/change_password', type='http', auth='none', methods=['POST'], csrf=False)
    def change_password(self, master_pwd, master_pwd_new):
        return self._refuse("The master password is managed by the platform.")
