#!/usr/bin/env python3
"""SEC-019 lint: flag any @http.route with csrf=False that isn't safe.

The security audit's own naive suggestion ("flag every type='http' +
csrf=False route") would false-positive on every route in this codebase,
because the reason each one is actually safe varies:

  - auth='none' (webhook.py): authority comes from the URL secret + HMAC
    signature, not the session cookie, so there is no session for a
    cross-site request to ride on — CSRF is moot regardless of method.
  - GET-only, no state mutation (ssh_terminal.py x2, portal.py): a
    cross-site page can trigger the request, but same-origin policy
    blocks it from ever reading the response, and GET-only means it
    can't be used to mutate state via a forged form/img/fetch either.

So the actual rule this lint enforces is: a csrf=False route must be
EITHER auth='none', OR explicitly restrict methods to a subset of {GET,
HEAD} (Odoo's own default, when `methods` is omitted, is "all methods
allowed" — confirmed by reading odoo/http.py's route() docstring; that
default is NOT safe to rely on for a csrf=False route, even one that
happens to only ever be called with GET today, which is exactly the gap
this lint would have caught in `portal_instance_log_stream` before this
session added its explicit `methods=['GET']`).

Usage: python3 scripts/lint_csrf_routes.py [file_or_dir ...]
Exits 1 (with each violation printed) if any csrf=False route fails the
rule above; exits 0 otherwise. Defaults to scanning saas_core/ and
saas_website/ under this script's own parent directory.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

_SAFE_METHODS = {'GET', 'HEAD'}


def _kwarg_value(call: ast.Call, name: str):
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _literal_str(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _literal_bool(node) -> bool | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    return None


def _literal_str_list(node) -> list[str] | None:
    if isinstance(node, (ast.List, ast.Tuple)):
        out = []
        for elt in node.elts:
            s = _literal_str(elt)
            if s is None:
                return None
            out.append(s)
        return out
    return None


def _is_route_call(node: ast.expr) -> ast.Call | None:
    """Return the Call node if this decorator is http.route(...) (bare
    `route` also matches, for `from odoo.http import route` style
    imports), else None."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == 'route':
        return node
    if isinstance(func, ast.Name) and func.id == 'route':
        return node
    return None


def check_source(source: str, filename: str = '<string>') -> list[str]:
    """Lint one file's already-read source text. Split out from
    check_file() so tests can exercise the actual rule against inline
    source fixtures without touching the filesystem."""
    violations = []
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as e:
        return ['%s: SyntaxError parsing file: %s' % (filename, e)]

    path = filename
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            call = _is_route_call(dec)
            if call is None:
                continue

            csrf_node = _kwarg_value(call, 'csrf')
            if csrf_node is None:
                continue  # default (True for type='http') — not our concern
            csrf = _literal_bool(csrf_node)
            if csrf is not False:
                # Either csrf=True explicitly, or a non-literal expression
                # (e.g. computed) we can't statically prove safe either
                # way — flag non-literal so a human looks at it once.
                if csrf is None:
                    violations.append(
                        '%s:%d: %s: csrf= is not a literal True/False — '
                        'cannot statically verify, please review by hand.'
                        % (path, node.lineno, node.name))
                continue

            auth_node = _kwarg_value(call, 'auth')
            auth = _literal_str(auth_node) if auth_node is not None else None
            if auth == 'none':
                continue  # safe: no session cookie to forge

            methods_node = _kwarg_value(call, 'methods')
            methods = _literal_str_list(methods_node) if methods_node is not None else None
            if methods is not None and set(methods) <= _SAFE_METHODS:
                continue  # safe: GET/HEAD only, no state mutation possible

            reason = (
                'no methods= restriction (Odoo default is ALL methods)'
                if methods is None else
                'methods=%r includes a non-GET/HEAD verb' % (methods,)
            )
            violations.append(
                '%s:%d: %s: csrf=False, auth=%r, %s — add methods=[\'GET\'] '
                'or auth=\'none\', or document why this is actually safe '
                'and add it to this lint\'s carve-out list.'
                % (path, node.lineno, node.name, auth, reason))
    return violations


def check_file(path: Path) -> list[str]:
    return check_source(path.read_text(encoding='utf-8'), filename=str(path))


def main(argv: list[str]) -> int:
    here = Path(__file__).resolve().parent.parent
    targets = [Path(a) for a in argv] or [here / 'saas_core', here / 'saas_website']

    files = []
    for t in targets:
        if t.is_dir():
            files.extend(sorted(t.rglob('*.py')))
        elif t.suffix == '.py':
            files.append(t)

    all_violations = []
    for f in files:
        all_violations.extend(check_file(f))

    if all_violations:
        print('SEC-019 lint: found %d unsafe/unverifiable csrf=False route(s):\n'
              % len(all_violations))
        for v in all_violations:
            print('  ' + v)
        return 1

    print('SEC-019 lint: all csrf=False routes are auth=\'none\' or GET/HEAD-only. OK.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
