import unittest
from changes import components


class ChangeSelectionTests(unittest.TestCase):
    def test_first_release_builds_everything(self):
        self.assertTrue(all(components([], full=True).values()))

    def test_backup_does_not_rebuild_operator(self):
        self.assertEqual(components(['compute/tools/backup-tool/run-backup.sh']),
                         dict(operator=False, backup=True, saas=False))

    def test_frontend_and_backend_are_one_release(self):
        for path in ['frontend/veltnex/src/App.tsx', 'control-plane/requirements.txt']:
            self.assertTrue(components([path])['saas'])

    def test_chart_and_release_infrastructure(self):
        self.assertTrue(components(['compute/charts/odoo-operator/crds/cr.yaml'])['operator'])
        self.assertTrue(all(components(['scripts/deploy/operator.sh']).values()))

    def test_cumulative_changes_and_deletions(self):
        self.assertEqual(components(['control-plane/deleted.py',
                                     'compute/tools/backup-tool/Dockerfile']),
                         dict(operator=False, backup=True, saas=True))

    def test_docs_only(self):
        self.assertFalse(any(components(['setup/06-CICD.md', 'README.md']).values()))
