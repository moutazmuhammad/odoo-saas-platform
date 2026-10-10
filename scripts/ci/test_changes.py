import unittest
from changes import components


class ChangeSelectionTests(unittest.TestCase):
    def test_first_release_builds_everything(self):
        self.assertTrue(all(components([], full=True).values()))

    def test_backup_does_not_rebuild_operator(self):
        self.assertEqual(components(['compute/tools/backup-tool/run-backup.sh']),
                         dict(operator=False, backup=True, chart=False, saas=False))

    def test_frontend_and_backend_are_one_release(self):
        for path in ['frontend/veltnex/src/App.tsx', 'control-plane/requirements.txt']:
            self.assertEqual(components([path]),
                             dict(operator=False, backup=False, chart=False, saas=True))

    def test_chart_and_operator_helper_do_not_rebuild_images(self):
        for path in ['compute/charts/odoo-operator/crds/cr.yaml', 'scripts/deploy/operator.sh']:
            self.assertEqual(components([path]),
                             dict(operator=False, backup=False, chart=True, saas=False))

    def test_operator_source_does_not_rebuild_backup_or_saas(self):
        self.assertEqual(components(['compute/operator/internal/controller/a.go']),
                         dict(operator=True, backup=False, chart=False, saas=False))

    def test_server_helper_selects_only_saas(self):
        self.assertEqual(components(['scripts/deploy/saas-deploy.sh']),
                         dict(operator=False, backup=False, chart=False, saas=True))

    def test_cumulative_changes_and_deletions(self):
        self.assertEqual(components(['control-plane/deleted.py',
                                     'compute/tools/backup-tool/Dockerfile']),
                         dict(operator=False, backup=True, chart=False, saas=True))

    def test_non_runtime_changes_do_not_deploy(self):
        for path in ['setup/06-CICD.md', 'README.md', 'scripts/ci/test_changes.py',
                     'scripts/ci/changes.py', '.github/workflows/ci.yml',
                     '.github/workflows/cd.yml', 'scripts/deploy/local.py',
                     'scripts/deploy/local.sh', 'compute/examples/doks/operator-values.yaml',
                     'compute/charts/vendor/traefik/values.yaml']:
            with self.subTest(path=path):
                self.assertFalse(any(components([path]).values()))
