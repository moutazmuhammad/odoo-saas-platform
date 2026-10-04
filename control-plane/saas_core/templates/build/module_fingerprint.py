"""Fingerprint Odoo upgrade inputs, excluding runtime-only Python and assets."""
import ast
import fnmatch
import hashlib
import json
import os

_MANIFEST_KEYS = ('version', 'depends', 'data', 'demo', 'assets')
_MODEL_KEYS = {'_name', '_inherit', '_inherits', '_table', '_auto',
               '_sql_constraints', '_parent_name', '_parent_store'}


def _schema(source):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source  # Invalid/dynamic definitions need conservative handling.
    aliases = {'fields'}
    constructors = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == 'odoo':
            aliases.update(a.asname or a.name for a in node.names if a.name == 'fields')
        if isinstance(node, ast.ImportFrom) and node.module == 'odoo.fields':
            constructors.update(a.asname or a.name for a in node.names)
    schemas = []
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        definitions, metadata = [], []
        for node in cls.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
                if names & _MODEL_KEYS:
                    metadata.append(node)
                calls = [n for n in ast.walk(node.value) if isinstance(n, ast.Call)] if node.value else []
                if any((isinstance(c.func, ast.Attribute) and isinstance(c.func.value, ast.Name)
                        and c.func.value.id in aliases) or
                       (isinstance(c.func, ast.Name) and c.func.id in constructors) for c in calls):
                    definitions.append(node)
                elif names & (_MODEL_KEYS - {'_inherit'}):
                    definitions.append(node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in ('init', '_auto_init'):
                definitions.append(node)
        if definitions:
            schemas.append(ast.dump(ast.ClassDef(name=cls.name, bases=cls.bases,
                           keywords=cls.keywords, body=metadata + definitions,
                           decorator_list=[]), include_attributes=False))
    return '\n'.join(schemas).encode()


def module_fingerprint(path):
    digest = hashlib.sha256()
    try:
        with open(os.path.join(path, '__manifest__.py'), 'rb') as source:
            manifest = ast.literal_eval(ast.parse(source.read()).body[0].value)
        settings = {k: manifest.get(k) for k in _MANIFEST_KEYS}
        digest.update(json.dumps(settings, sort_keys=True).encode())
        loaded = manifest.get('data', []) + manifest.get('demo', [])
    except (OSError, SyntaxError, ValueError, TypeError, AttributeError, IndexError):
        loaded = ['*.xml', '*.csv']
    for top, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if d not in ('.git', '__pycache__', 'static'))
        for name in sorted(files):
            full = os.path.join(top, name)
            relative = os.path.relpath(full, path).replace(os.sep, '/')
            if name == '__manifest__.py' or name.endswith(('.pyc', '.pyo')):
                continue
            migration = relative.startswith('migrations/') or relative.startswith('upgrades/')
            database_data = any(fnmatch.fnmatch(relative, pattern) for pattern in loaded)
            if not (migration or database_data or name.endswith('.py')):
                continue
            with open(full, 'rb') as source:
                content = source.read()
            if name.endswith('.py') and not (migration or database_data):
                content = _schema(content)
            if not content:
                continue
            digest.update(relative.encode() + b'\0' + content + b'\0')
    return digest.hexdigest()
