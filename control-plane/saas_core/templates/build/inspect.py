"""Build Job, step 2 (the Odoo base image, for its Python): find each repo's
modules, pick its addons directory (repo root, or the single sub-folder
holding the modules), read module versions, and merge requirements (the
instance's pip packages + each repo's root requirements.txt). Writes
/workspace/requirements.txt and /workspace/meta/result.json."""
import ast
import json
import os

ROOT = '/workspace/addons'


def manifests(repo_path):
    for top, dirs, files in os.walk(repo_path):
        depth = os.path.relpath(top, repo_path).count(os.sep)
        dirs[:] = [d for d in dirs if not d.startswith('.')] if depth < 2 else []
        if '__manifest__.py' in files:
            yield os.path.relpath(top, repo_path)


def detect_subdir(module_dirs):
    """'' when modules sit at the repo root, else the single common parent."""
    parents = set()
    for rel in module_dirs:
        parts = rel.split(os.sep)
        if len(parts) == 1:
            return ''
        if len(parts) == 2:
            parents.add(parts[0])
    return parents.pop() if len(parents) == 1 else ''


def main():
    shas = {}
    with open('/workspace/meta/shas') as fh:
        for line in fh:
            idx, sha = line.split()
            shas[int(idx)] = sha
    dirs = json.loads(os.environ.get('REPO_DIRS', '[]'))
    repos, modules = [], {}
    req_lines = []
    with open('/files/requirements.txt') as fh:
        req_lines += fh.read().splitlines()
    for idx, name in enumerate(dirs):
        path = os.path.join(ROOT, name)
        mods = list(manifests(path))
        subdir = detect_subdir(mods)
        base = os.path.join(path, subdir) if subdir else path
        for mod in sorted(os.listdir(base)):
            mf = os.path.join(base, mod, '__manifest__.py')
            if not os.path.isfile(mf):
                continue
            try:
                with open(mf) as fh:
                    manifest = ast.literal_eval(fh.read())
                modules[mod] = str(manifest.get('version') or '')
            except (SyntaxError, ValueError):
                modules[mod] = ''
        req = os.path.join(path, 'requirements.txt')
        if os.path.isfile(req):
            with open(req) as fh:
                req_lines += fh.read().splitlines()
        repos.append({'dir': name, 'subdir': subdir, 'sha': shas.get(idx, '')})
    seen, merged = set(), []
    for line in req_lines:
        line = line.strip()
        key = line.lower()
        if line and not line.startswith('#') and key not in seen:
            seen.add(key)
            merged.append(line)
    with open('/workspace/requirements.txt', 'w') as fh:
        fh.write('\n'.join(merged) + ('\n' if merged else ''))
    with open('/workspace/meta/result.json', 'w') as fh:
        json.dump({'repos': repos, 'modules': modules}, fh, separators=(',', ':'))
    print('modules: %d, requirements: %d' % (len(modules), len(merged)))


if __name__ == '__main__':
    main()
