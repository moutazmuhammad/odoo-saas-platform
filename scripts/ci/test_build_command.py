"""Execute the tenant build wrapper with a CLI-contract stub; no cluster needed."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class BuildCommandTests(unittest.TestCase):
    def execute(self, *, insecure=False, fail=False):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            workspace = work / 'workspace'
            (workspace / 'meta').mkdir(parents=True)
            (workspace / 'meta/result.json').write_text('{"modules":["sale"]}')
            script = work / 'build.sh'
            script.write_text((ROOT / 'control-plane/saas_core/templates/build/build.sh')
                              .read_text().replace('/workspace', str(workspace)))
            binary = work / 'buildctl'
            binary.write_text('#!' + sys.executable + '\n' + '''
import json, os, pathlib, sys
args = sys.argv[1:]
options = {}
for i, arg in enumerate(args):
    if arg == '--opt':
        key, separator, value = args[i + 1].partition('=')
        if not separator:
            sys.stderr.write('error: invalid opt: invalid value ' + key + '\\n')
            sys.exit(1)
        options[key] = value
pathlib.Path(os.environ['BUILD_CALL']).write_text(json.dumps({'args': args, 'options': options}))
if os.environ['BUILD_FAIL'] == '1':
    sys.exit(42)
pathlib.Path(args[args.index('--metadata-file') + 1]).write_text(
    json.dumps({'containerimage.digest': 'sha256:' + 'a' * 64}))
''')
            binary.chmod(0o755)
            env = dict(os.environ, PATH=str(work) + os.pathsep + os.environ['PATH'],
                       IMAGE_REF='registry.example.com/team/tenant:build-123',
                       BUILDKIT_HOST='unix:///run/buildkit/buildkitd.sock',
                       REGISTRY_INSECURE='1' if insecure else '',
                       BUILD_FAIL='1' if fail else '', BUILD_CALL=str(work / 'call.json'))
            result = subprocess.run(['sh', script], env=env, capture_output=True, text=True, timeout=10)
            call = json.loads((work / 'call.json').read_text()) if (work / 'call.json').exists() else None
            return result, call

    def test_build_passes_valid_options_and_reports_pushed_image(self):
        result, call = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        # Empty means all stages; "true" would name a stage instead.
        self.assertEqual(call['options']['no-cache'], '')
        self.assertEqual(call['options']['filename'], 'Dockerfile')
        self.assertEqual(call['args'][:3], ['--addr', 'unix:///run/buildkit/buildkitd.sock', 'build'])
        output = call['args'][call['args'].index('--output') + 1]
        self.assertEqual(output, 'type=image,name=registry.example.com/team/tenant:build-123,push=true')
        self.assertIn('SAAS_BUILD_DIGEST sha256:' + 'a' * 64, result.stdout)
        self.assertIn('SAAS_BUILD_RESULT {"modules":["sale"]}', result.stdout)

    def test_insecure_registry_preserves_build_option_contract(self):
        result, call = self.execute(insecure=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = call['args'][call['args'].index('--output') + 1]
        self.assertTrue(output.endswith(',registry.insecure=true'))
        self.assertEqual(call['options']['no-cache'], '')

    def test_build_failure_does_not_emit_success_markers(self):
        result, _ = self.execute(fail=True)
        self.assertEqual(result.returncode, 42)
        self.assertNotIn('SAAS_BUILD_DIGEST', result.stdout)
        self.assertNotIn('SAAS_BUILD_RESULT', result.stdout)


if __name__ == '__main__':
    unittest.main()
