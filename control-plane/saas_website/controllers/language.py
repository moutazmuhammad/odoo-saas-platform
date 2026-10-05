"""One language preference for the React portal and native website pages."""
from urllib.parse import urlsplit, urlunsplit

from odoo import http
from odoo.http import request


class SaasLanguage(http.Controller):
    @staticmethod
    def _destination(value):
        # Never send a visitor to a third-party URL supplied by a query parameter.
        value = value or '/'
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc or not value.startswith('/') or '\\' in value or any(ord(c) < 32 for c in value):
            return '/'
        path = parsed.path
        for prefix in ('/ar_001', '/en_US', '/ar', '/en'):
            if path == prefix or path.startswith(prefix + '/'):
                path = path[len(prefix):] or '/'
                break
        if path.startswith('/saas/language'):
            return '/'
        return urlunsplit(('', '', path, parsed.query, parsed.fragment))

    @http.route('/saas/language', type='http', auth='public', website=False,
                multilang=False, sitemap=False, methods=['GET'])
    def switch_language(self, lang='en', redirect='/', **kwargs):
        language = 'ar' if lang == 'ar' else 'en'
        code = 'ar_001' if language == 'ar' else 'en_US'
        request.session.context = dict(request.session.context or {}, lang=code)
        response = request.redirect(self._destination(redirect), code=303)
        for key, value in [('veltnex-language', language), ('frontend_lang', code)]:
            response.set_cookie(key, value, max_age=31536000, path='/', samesite='Lax')
        return response
