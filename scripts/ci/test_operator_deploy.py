"""Exercise deployment argument handling without a live Kubernetes cluster."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class OperatorDeploymentTests(unittest.TestCase):
    def execute(self, **overrides):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            log = path / 'commands'
            for tool in ('helm', 'kubectl'):
                executable = path / tool
                executable.write_text('#!/bin/bash\nprintf "%s\\n" "$0 $*" >> "$COMMAND_LOG"\n'
                                      'if [[ ${FAIL_CRD:-} == true && $1 == apply ]]; then exit 1; fi\n')
                executable.chmod(0o755)
            env = dict(os.environ, PATH=f'{path}:{os.environ["PATH"]}',
                       COMMAND_LOG=str(log), KUBECONFIG=str(path / 'kubeconfig'),
                       OPERATOR_VALUES_FILE=str(path / 'values.yaml'))
            env.update(overrides)
            process = subprocess.run(['bash', 'scripts/deploy/operator.sh'],
                                     cwd=ROOT, env=env, capture_output=True)
            return process.returncode, log.read_text()

    def test_backup_only_keeps_previous_operator_and_sets_restore(self):
        code, log = self.execute(BACKUP_IMAGE='registry:5000/team/backup:sha-123')
        self.assertEqual(code, 0)
        self.assertNotIn('image.repository=', log)
        self.assertIn('restore.toolImage=registry:5000/team/backup:sha-123', log)
        self.assertIn('--reset-then-reuse-values', log)
        self.assertIn('--atomic --wait', log)

    def test_operator_repository_with_registry_port(self):
        code, log = self.execute(OPERATOR_IMAGE='registry:5000/team/operator:sha-456')
        self.assertEqual(code, 0)
        self.assertIn('image.repository=registry:5000/team/operator', log)
        self.assertIn('image.tag=sha-456', log)
        self.assertNotIn('restore.toolImage=', log)

    def test_crd_failure_blocks_upgrade(self):
        code, log = self.execute(FAIL_CRD='true')
        self.assertNotEqual(code, 0)
        self.assertNotIn('upgrade --install', log)
