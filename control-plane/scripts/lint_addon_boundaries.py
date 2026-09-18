#!/usr/bin/env python3
"""Boundary lint (billing/pricing architecture plan, §4): flag any addon
calling another addon's underscore-prefixed "private" method on a model
it doesn't own, via ``self.env['some.model']._private_method(...)``.

Scope, deliberately narrow (to keep the false-positive rate near zero —
Odoo private-helper names like ``_get_default``/``_compute_x`` are reused
across dozens of unrelated models, so a name-only check would be mostly
noise): this lint ONLY flags calls that go through an explicit
``self.env['model.name']`` (optionally chained with ``.sudo()`` /
``.with_context(...)`` / ``.with_user(...)``) string-literal model
lookup — exactly the pattern that crosses an addon boundary at the
call site (e.g. ``self.env['saas.wallet']._refund_move(...)`` called
from ``saas_core/models/account_move.py``, where ``saas.wallet`` is
owned by ``saas_billing``). It deliberately does NOT try to catch
``some_record._private_method()`` on an already-typed recordset
variable (e.g. ``instance._capture_payment_token_from_invoice(...)``)
— that would require real type inference to know which model
``instance`` is, which this lint doesn't attempt. Ownership is "which
addon's ``models/*.py`` declares ``_name = 'model.name'``" (NOT
``_inherit`` — an addon that only extends a model via ``_inherit``
doesn't own it).

Usage: python3 scripts/lint_addon_boundaries.py [addon_dir ...]
Exits 1 (with each violation printed) if any addon calls another
addon's private method through ``self.env[...]``; exits 0 otherwise.
Defaults to scanning saas_core/, saas_billing/, and saas_website/ under
this script's own parent directory.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

_DUNDER_OK = {'__init__', '__str__', '__repr__'}


def _is_dunder(name: str) -> bool:
    return name.startswith('__') and name.endswith('__')


def find_model_owners(addon_dir: Path) -> dict[str, str]:
    """Map model name -> addon name, for every ``_name = 'x.y.z'``
    class-level assignment found under ``addon_dir/models/`` (and
    ``addon_dir/wizards/``, ``addon_dir/controllers/`` if present, for
    completeness). ``_inherit``-only extensions are NOT ownership."""
    addon = addon_dir.name
    owners: dict[str, str] = {}
    for sub in ('models', 'wizards'):
        d = addon_dir / sub
        if not d.is_dir():
            continue
        for f in sorted(d.rglob('*.py')):
            try:
                tree = ast.parse(f.read_text(encoding='utf-8'), filename=str(f))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                for stmt in node.body:
                    if not isinstance(stmt, ast.Assign):
                        continue
                    if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
                        continue
                    if stmt.targets[0].id != '_name':
                        continue
                    if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                        owners[stmt.value.value] = addon
    return owners


def _unwrap_chain(node: ast.expr) -> ast.expr:
    """Peel off trailing ``.sudo()``/``.with_context(...)``/``.with_user(...)``
    calls to get back to the base expression (e.g. the ``self.env[...]``
    subscript)."""
    _PASSTHROUGH = {'sudo', 'with_context', 'with_user', 'with_company'}
    while (isinstance(node, ast.Call)
           and isinstance(node.func, ast.Attribute)
           and node.func.attr in _PASSTHROUGH):
        node = node.func.value
    return node


def _env_model_literal(node: ast.expr) -> str | None:
    """If ``node`` is ``self.env['model.name']`` (after unwrapping
    .sudo()/.with_context() etc.), return the literal model name."""
    base = _unwrap_chain(node)
    if not isinstance(base, ast.Subscript):
        return None
    value = base.value
    if not (isinstance(value, ast.Attribute) and value.attr == 'env'
            and isinstance(value.value, ast.Name) and value.value.id == 'self'):
        return None
    idx = base.slice
    if isinstance(idx, ast.Constant) and isinstance(idx.value, str):
        return idx.value
    return None


def check_source(source: str, addon: str, owners: dict[str, str],
                  filename: str = '<string>') -> list[str]:
    violations = []
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as e:
        return ['%s: SyntaxError parsing file: %s' % (filename, e)]

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        method_name = func.attr
        if not method_name.startswith('_') or _is_dunder(method_name):
            continue
        model_name = _env_model_literal(func.value)
        if model_name is None:
            continue
        owner = owners.get(model_name)
        if owner is None or owner == addon:
            continue  # unowned (base Odoo model) or same-addon — fine
        violations.append(
            "%s:%d: calls self.env[%r].%s(...) — %r is owned by "
            "addon %r, not %r. Either make %s a real public method "
            "(no leading underscore + a docstring saying which "
            "addon(s) call it), or move this call site's logic into "
            "%r." % (filename, node.lineno, model_name, method_name,
                     model_name, owner, addon, method_name, owner))
    return violations


def check_addon(addon_dir: Path, owners: dict[str, str]) -> list[str]:
    addon = addon_dir.name
    violations = []
    for f in sorted(addon_dir.rglob('*.py')):
        if '/tests/' in str(f) or '/migrations/' in str(f):
            continue
        violations.extend(
            check_source(f.read_text(encoding='utf-8'), addon, owners, filename=str(f)))
    return violations


def main(argv: list[str]) -> int:
    here = Path(__file__).resolve().parent.parent
    targets = [Path(a) for a in argv] or [
        here / 'saas_core', here / 'saas_billing', here / 'saas_website']

    owners: dict[str, str] = {}
    for t in targets:
        owners.update(find_model_owners(t))

    all_violations = []
    for t in targets:
        all_violations.extend(check_addon(t, owners))

    if all_violations:
        print('Boundary lint: found %d cross-addon private-method call(s):\n'
              % len(all_violations))
        for v in all_violations:
            print('  ' + v)
        return 1

    print('Boundary lint: no cross-addon private self.env[...] calls found. OK.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
