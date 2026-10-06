"""Run with the control-plane Python environment; no cluster is contacted."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('cleanup', ROOT / 'scripts/cleanup-legacy-build-caches.py')
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


class CacheCleanupTests(unittest.TestCase):
    def test_preview_and_delete_protect_tenant_data_and_job_references(self):
        core, batch = MagicMock(), MagicMock()
        def claim(name):
            return NS(metadata=NS(name=name, uid=name, resource_version='7',
                labels={'app.kubernetes.io/managed-by': cleanup.MANAGED_BY}, deletion_timestamp=None))
        core.list_namespaced_persistent_volume_claim.return_value.items = [
            claim('buildkit-cache-unused'), claim('buildkit-cache-running'), claim('tenant-filestore')]
        core.list_namespaced_pod.return_value.items = []
        batch.list_namespaced_job.return_value.items = [NS(spec=NS(template=NS(spec=NS(volumes=[
            NS(persistent_volume_claim=NS(claim_name='buildkit-cache-running'))]))))]
        with contextlib.redirect_stdout(io.StringIO()):
            cleanup.cleanup(core, batch)
            core.delete_namespaced_persistent_volume_claim.assert_not_called()
            cleanup.cleanup(core, batch, delete=True)
        call = core.delete_namespaced_persistent_volume_claim.call_args
        self.assertEqual(core.delete_namespaced_persistent_volume_claim.call_count, 1)
        self.assertEqual(call.args, ('buildkit-cache-unused', 'odoo-builds'))
        self.assertEqual(call.kwargs['body'].preconditions.uid, 'buildkit-cache-unused')
        self.assertEqual(call.kwargs['body'].preconditions.resource_version, '7')

    def test_new_reference_blocks_deletion(self):
        core, batch = MagicMock(), MagicMock()
        meta = NS(name='buildkit-cache-unused', uid='uid', resource_version='7',
                  labels={'app.kubernetes.io/managed-by': cleanup.MANAGED_BY}, deletion_timestamp=None)
        core.list_namespaced_persistent_volume_claim.return_value.items = [NS(metadata=meta)]
        batch.list_namespaced_job.return_value.items = []
        core.list_namespaced_pod.side_effect = [NS(items=[]), NS(items=[NS(spec=NS(volumes=[
            NS(persistent_volume_claim=NS(claim_name=meta.name))]))])]
        with contextlib.redirect_stdout(io.StringIO()):
            cleanup.cleanup(core, batch, delete=True)
        core.delete_namespaced_persistent_volume_claim.assert_not_called()


class FetchTests(unittest.TestCase):
    def run_fetch(self, refs=('', '', ''), fail=False):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            workspace = folder / 'workspace'
            script = (ROOT / 'control-plane/saas_core/templates/build/fetch.sh').read_text().replace('/workspace', str(workspace))
            (folder / 'fetch.sh').write_text(script)
            git = folder / 'git'
            git.write_text('''#!/bin/sh
case "$*" in
  'init -q '*) mkdir -p "$3/.git";;
  *fetch*)
    touch "$FAKE_ROOT/active-$(basename "$PWD")"
    if [ "$(find "$FAKE_ROOT" -name 'active-*' | wc -l)" -ge 2 ]; then touch "$FAKE_ROOT/concurrent"; fi
    sleep 0.1
    rm "$FAKE_ROOT/active-$(basename "$PWD")"
    [ "$FAKE_FAIL" != 1 ];;
  'rev-parse HEAD') echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa;;
esac
''')
            git.chmod(0o755)
            env = dict(os.environ, PATH=str(folder)+':'+os.environ['PATH'],
                       FAKE_ROOT=tmp, FAKE_FAIL='1' if fail else '', REPO_COUNT=str(len(refs)))
            for i, ref in enumerate(refs):
                env.update({f'REPO_URL_{i}':'https://secret-token@example.com/repo.git',
                            f'REPO_REF_{i}':ref, f'REPO_BRANCH_{i}':'main', f'REPO_DIR_{i}':f'repo-{i}'})
            result = subprocess.run(['sh', str(folder/'fetch.sh')], env=env, capture_output=True, text=True, timeout=10)
            self.assertNotIn('secret-token', result.stdout+result.stderr)
            content = (workspace/'meta/shas').read_text() if (workspace/'meta/shas').exists() else ''
            return result, content, (folder/'concurrent').exists()

    def test_parallel_fetch_preserves_every_commit_in_order(self):
        result, content, concurrent = self.run_fetch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(concurrent)
        self.assertEqual(content.splitlines(), [f'{i} '+ 'a'*40 for i in range(3)])

    def test_failed_fetch_fails_the_build(self):
        result, _, _ = self.run_fetch(fail=True)
        self.assertNotEqual(result.returncode, 0)

    def test_pinned_commit_never_falls_back_to_different_code(self):
        result, _, _ = self.run_fetch(refs=('b'*40,))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Requested commit could not be fetched', result.stderr)


class RetiredTierMigrationTests(unittest.TestCase):
    def test_removes_old_fields_and_inherited_xpath_without_losing_billing_fields(self):
        migration_spec = importlib.util.spec_from_file_location('migration',
            ROOT / 'control-plane/saas_core/migrations/18.0.58.0.0/pre-migrate.py')
        migration = importlib.util.module_from_spec(migration_spec)
        migration_spec.loader.exec_module(migration)
        from lxml import etree
        arch = """<data><xpath expr="//field[@name='compute_tier_id']" position="after">
            <field name="pending_compute_tier_id"/></xpath>
            <group><field name="compute_tier_id"/><field name="invoice_count"/></group></data>"""
        updated = etree.fromstring(migration.without_tier_fields(arch).encode())
        self.assertEqual(updated.xpath('//field/@name'), ['invoice_count'])
        self.assertFalse(updated.xpath('//xpath'))
        self.assertIn('invoice_count', migration.without_tier_fields(migration.without_tier_fields(arch)))


if __name__ == '__main__':
    unittest.main()
