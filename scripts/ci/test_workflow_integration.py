"""Exercise the actual delivery planner and workflow failure/skip conditions."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace as NS
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
CD = yaml.safe_load((ROOT / '.github/workflows/cd.yml').read_text())
CI = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())


def evaluate(expression, **context):
    expression = expression.strip().removeprefix('${{').removesuffix('}}').strip()
    expression = re.sub(r'!(?!=)', ' not ', expression)
    expression = expression.replace('&&', ' and ').replace('||', ' or ')
    return eval(' '.join(expression.split()), {'__builtins__': {}},
                dict(always=lambda: True, cancelled=lambda: False, **context))


class WorkflowConditions(unittest.TestCase):
    def needs(self, images='[]', clusters='[]', saas='true', image_result='skipped',
              operator_result='skipped', saas_result='success', package_result='success'):
        return NS(plan=NS(result='success', outputs=NS(images=images, clusters=clusters,
                                                      saas=saas, tag='release')),
                  images=NS(result=image_result), operator=NS(result=operator_result),
                  saas=NS(result=saas_result), package=NS(result=package_result))

    def test_saas_only_release_runs_despite_skipped_compute_jobs(self):
        self.assertTrue(evaluate(CD['jobs']['saas']['if'], needs=self.needs()))
        self.assertTrue(evaluate(CD['jobs']['delivered']['if'], needs=self.needs()))

    def test_required_cluster_failure_or_skip_blocks_saas_and_receipt(self):
        for result in ['failure', 'skipped', 'cancelled']:
            needs = self.needs(images='[{}]', clusters='["cluster"]',
                               image_result='success', operator_result=result)
            self.assertFalse(evaluate(CD['jobs']['saas']['if'], needs=needs))
            self.assertFalse(evaluate(CD['jobs']['delivered']['if'], needs=needs))

    def test_docs_only_can_advance_baseline_without_deploying(self):
        needs = self.needs(saas='false', saas_result='skipped')
        self.assertFalse(evaluate(CD['jobs']['saas']['if'], needs=needs))
        self.assertTrue(evaluate(CD['jobs']['delivered']['if'], needs=needs))

    def test_failed_saas_cannot_advance_baseline(self):
        self.assertFalse(evaluate(CD['jobs']['delivered']['if'],
                                  needs=self.needs(saas_result='failure')))

    def test_only_main_push_or_manual_run_can_start_release(self):
        for event, ref, expected in [
            ('push', 'refs/heads/main', True),
            ('workflow_dispatch', 'refs/heads/main', True),
            ('push', 'refs/heads/dev', False),
            ('workflow_dispatch', 'refs/heads/dev', False),
            ('workflow_run', 'refs/heads/main', False),
            ('pull_request', 'refs/heads/main', False),
        ]:
            github = NS(event_name=event, ref=ref, run_id=456)
            self.assertEqual(bool(evaluate(CD['jobs']['plan']['if'], github=github)), expected)
            group = CD['concurrency']['group'].removeprefix('delivery-')
            self.assertEqual(evaluate(group, github=github), 'production' if expected else 456)

    def test_chart_only_runs_cluster_deployment_without_image_builds(self):
        needs = self.needs(images='[]', clusters='["cluster"]', image_result='skipped',
                           operator_result='success', saas='false')
        self.assertTrue(evaluate(CD['jobs']['operator']['if'], needs=needs))
        self.assertTrue(evaluate(CD['jobs']['delivered']['if'], needs=needs))

    def test_saas_only_skips_cluster_deployment(self):
        self.assertFalse(evaluate(CD['jobs']['operator']['if'], needs=self.needs()))

    def test_cluster_deployment_requires_selected_image_builds_to_succeed(self):
        for result in ['failure', 'skipped', 'cancelled']:
            self.assertFalse(evaluate(CD['jobs']['operator']['if'], needs=self.needs(
                images='[{}]', clusters='["cluster"]', image_result=result)))

    def test_manual_deployment_is_incremental_by_default(self):
        self.assertIs(CD[True]['workflow_dispatch']['inputs']['full']['default'], False)

    def test_failed_or_skipped_package_blocks_saas(self):
        for result in ['failure', 'skipped', 'cancelled']:
            self.assertFalse(evaluate(CD['jobs']['saas']['if'],
                                      needs=self.needs(package_result=result)))


class PlannerIntegration(unittest.TestCase):
    def execute(self, changed, *, first=False, stale=False, clusters='["cluster"]',
                full=False, legacy=False):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=path,
                                               stderr=subprocess.DEVNULL, text=True).strip()
            git('init', '-q')
            script = path / 'scripts/ci/changes.py'
            script.parent.mkdir(parents=True)
            shutil.copyfile(ROOT / 'scripts/ci/changes.py', script)
            git('add', '.')
            git('-c', 'user.name=CI', '-c', 'user.email=ci@example.invalid', 'commit', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            target = path / changed
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('changed')
            git('add', '.')
            git('-c', 'user.name=CI', '-c', 'user.email=ci@example.invalid', 'commit', '-qm', 'change')
            head = git('rev-parse', 'HEAD')
            fake = path / 'bin'
            fake.mkdir()
            gh = fake / 'gh'
            gh.write_text('''#!/usr/bin/env python3
import json,os,sys
endpoint=sys.argv[2]
if 'git/ref/' in endpoint:
 print(os.environ['LATEST'])
elif '/workflows/cd.yml/runs?' in endpoint:
 if os.environ['FIRST'] != 'true':
  print('123\\t'+os.environ['BASE']+'\\t'+('push' if os.environ['LEGACY']=='true' else 'workflow_run'))
elif '/artifacts?' in endpoint:
 name='delivered' if os.environ['LEGACY']=='true' else 'delivered-'+os.environ['BASE']
 print(json.dumps({'artifacts':[{'name':name,'expired':False}]}))
else:
 sys.exit('Unexpected API endpoint: '+endpoint)
''')
            gh.chmod(0o755)
            output = path / 'outputs'
            step = next(x for x in CD['jobs']['plan']['steps'] if x.get('id') == 'plan')
            env = dict(os.environ, PATH=f'{fake}:{os.environ["PATH"]}',
                       GITHUB_REPOSITORY='owner/repo', GITHUB_OUTPUT=str(output),
                       GITHUB_STEP_SUMMARY=str(path / 'summary'), GITHUB_SHA='0' * 40,
                       GITHUB_RUN_ID='456', GITHUB_RUN_ATTEMPT='1',
                       TARGET_SHA=head, LATEST='f' * 40 if stale else head,
                       BASE=base, FIRST=str(first).lower(), LEGACY=str(legacy).lower(),
                       FORCE_FULL=str(full).lower(), CLUSTERS=clusters)
            result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', step['run']],
                                    cwd=path, env=env, capture_output=True, text=True)
            outputs = dict(line.split('=', 1) for line in output.read_text().splitlines()) if output.exists() else {}
            return result, outputs, head

    def test_saas_only_uses_selected_release_sha(self):
        result, outputs, head = self.execute('frontend/veltnex/src/App.tsx', clusters='')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs['saas'], 'true')
        self.assertEqual(outputs['images'], '[]')
        self.assertEqual(outputs['sha'], head)

    def test_backup_only_matrix_and_legacy_receipt(self):
        result, outputs, _ = self.execute('compute/tools/backup-tool/run-backup.sh', legacy=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([x['name'] for x in json.loads(outputs['images'])], ['backup'])
        self.assertEqual(outputs['operator'], 'false')
        self.assertEqual(outputs['clusters'], '["cluster"]')

    def test_chart_only_has_cluster_but_no_image_builds(self):
        result, outputs, _ = self.execute('compute/charts/odoo-operator/templates/deployment.yaml')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs['chart'], 'true')
        self.assertEqual(outputs['images'], '[]')
        self.assertEqual(outputs['clusters'], '["cluster"]')
        self.assertEqual(outputs['saas'], 'false')

    def test_operator_only_builds_operator_image(self):
        result, outputs, _ = self.execute('compute/operator/cmd/change.go')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([x['name'] for x in json.loads(outputs['images'])], ['operator'])
        self.assertEqual(outputs['saas'], 'false')
        self.assertEqual(outputs['backup'], 'false')

    def test_local_deploy_and_ci_changes_do_not_trigger_remote_deployment(self):
        for path in ['scripts/deploy/local.py', 'scripts/ci/test_changes.py',
                     '.github/workflows/ci.yml', '.github/workflows/cd.yml']:
            with self.subTest(path=path):
                result, outputs, _ = self.execute(path, clusters='')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(outputs['images'], '[]')
                self.assertEqual(outputs['clusters'], '[]')
                self.assertEqual(outputs['saas'], 'false')

    def test_first_delivery_and_full_delivery_select_everything(self):
        for option in [{'first': True}, {'full': True}]:
            result, outputs, _ = self.execute('README.md', **option)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([outputs[x] for x in ['saas', 'operator', 'backup', 'chart']], ['true'] * 4)

    def test_superseded_commit_has_no_delivery_receipt_target(self):
        result, outputs, _ = self.execute('control-plane/change.py', stale=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('tag', outputs)
        self.assertNotIn('sha', outputs)
        self.assertEqual(outputs['images'], '[]')

    def test_invalid_cluster_configuration_stops_before_publication(self):
        result, _, _ = self.execute('compute/operator/cmd/change.go', clusters='[]')
        self.assertNotEqual(result.returncode, 0)
