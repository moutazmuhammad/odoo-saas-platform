import json
import re
from functools import lru_cache
from urllib.parse import quote

from markupsafe import Markup
from odoo import models, tools
from odoo.http import request


class IrHttp(models.AbstractModel):
    _inherit = 'ir.http'

    @classmethod
    def _pre_dispatch(cls, rule, arguments):
        super()._pre_dispatch(rule, arguments)
        # JSON API routes do not run website's frontend language dispatch.
        # Apply only a validated display language; authorization remains unchanged.
        if request.httprequest.path.startswith('/saas/api/'):
            language = request.httprequest.cookies.get('veltnex-language')
            if language in ('en', 'ar'):
                request.update_context(lang='ar_001' if language == 'ar' else 'en_US')

    def _saas_language_destination(self):
        return quote(request.httprequest.full_path, safe='')

    def _saas_display_text(self, message):
        if not message or not request.env.lang.startswith('ar'):
            return message
        return _translate_message(str(message))

    def _saas_language_catalog(self):
        if not request.env.lang.startswith('ar'):
            return Markup('{}')
        return _arabic_catalog()


@lru_cache(maxsize=1)
def _arabic_catalog():
    catalog = _message_catalog()
    # A JSON script is a raw-text element: neutralize HTML closing tags.
    return Markup(json.dumps(catalog, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026'))


@lru_cache(maxsize=1)
def _message_catalog():
    with tools.file_open('saas_website/static/src/i18n/ar.json', 'r') as source:
        return json.load(source)


_PLACEHOLDER = re.compile(r'\{\d+\}|%\([^)]+\)[sdf]|%[sdfdr]')


@lru_cache(maxsize=1)
def _message_patterns():
    patterns = []
    for source, target in _message_catalog().items():
        matches = list(_PLACEHOLDER.finditer(source))
        if not matches:
            continue
        parts, cursor = [], 0
        for match in matches:
            parts.extend([re.escape(source[cursor:match.start()]), '([\\s\\S]+?)'])
            cursor = match.end()
        parts.append(re.escape(source[cursor:]))
        patterns.append((re.compile('^' + ''.join(parts) + '$'), [m.group() for m in matches], target))
    return patterns


def _translate_message(message):
    direct = _message_catalog().get(message)
    if direct:
        return direct
    for pattern, tokens, target in _message_patterns():
        match = pattern.match(message)
        if not match:
            continue
        used = set()
        def substitute(token):
            index = next((i for i, value in enumerate(tokens) if value == token.group() and i not in used), None)
            if index is None:
                return token.group()
            used.add(index)
            return match[index + 1]
        return _PLACEHOLDER.sub(substitute, target)
    return message
