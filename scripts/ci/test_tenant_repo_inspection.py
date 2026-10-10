"""Run the actual build inspection stage against customer repository layouts."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / 'control-plane/saas_core/templates/build'


class TenantRepoInspectionTests(unittest.TestCase):
    def inspect(self, module_dirs):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            workspace = work / 'workspace'
            repo = workspace / 'addons/customer_app'
            repo.mkdir(parents=True)
            (workspace / 'meta').mkdir()
            (workspace / 'meta/shas').write_text('0 ' + 'a' * 40 + '\n')
            files = work / 'files'
            files.mkdir()
            (files / 'requirements.txt').write_text('')
            (repo / 'requirements.txt').write_text('requests==2.31.0\n')
            for relative in module_dirs:
                module = repo / relative
                module.mkdir(parents=True, exist_ok=True)
                (module / '__manifest__.py').write_text("{'name':'Custom addon','version':'18.0.1.0.0'}")
                (module / '__init__.py').write_text('# customer module\n')
            script = work / 'inspect.py'
            script.write_text((TEMPLATES / 'inspect.py').read_text()
                              .replace('/workspace', str(workspace)).replace('/files', str(files)))
            env = dict(os.environ, REPO_DIRS='["customer_app"]', PYTHONPATH=str(TEMPLATES))
            result = subprocess.run([sys.executable, script], env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            metadata = json.loads((workspace / 'meta/result.json').read_text())
            self.assertEqual((workspace / 'requirements.txt').read_text(), 'requests==2.31.0\n')
            self.assertEqual(metadata['repos'][0]['sha'], 'a' * 40)
            for fingerprint in metadata['modules'].values():
                self.assertEqual(len(fingerprint['sha256']), 64)
            return metadata

    def test_single_addon_at_repository_root_is_discoverable_by_odoo(self):
        result = self.inspect(['.'])
        self.assertEqual(set(result['modules']), {'customer_app'})
        self.assertEqual(result['repos'][0]['addons_subdirs'], ['..'])

    def test_addons_directly_under_repository_keep_their_search_path(self):
        result = self.inspect(['sale_extra', 'stock_extra'])
        self.assertEqual(set(result['modules']), {'sale_extra', 'stock_extra'})
        self.assertEqual(result['repos'][0]['addons_subdirs'], [''])

    def test_addons_subfolder_keeps_legacy_metadata(self):
        result = self.inspect(['addons/sale_extra'])
        self.assertEqual(result['repos'][0]['subdir'], 'addons')
        self.assertEqual(result['repos'][0]['addons_subdirs'], ['addons'])

    def test_mixed_addon_directories_all_reach_odoo_search_path(self):
        result = self.inspect(['sale_extra', 'addons/stock_extra', 'custom/hr_extra'])
        self.assertEqual(set(result['modules']), {'sale_extra', 'stock_extra', 'hr_extra'})
        self.assertEqual(result['repos'][0]['addons_subdirs'], ['', 'addons', 'custom'])


if __name__ == '__main__':
    unittest.main()
