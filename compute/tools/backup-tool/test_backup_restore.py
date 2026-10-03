"""Exercise backup/restore entrypoints with local artifacts and mocked transports."""
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parent
MOCK = '''#!/usr/bin/env python3
import json, os, pathlib, shutil, sys
args = sys.argv[1:]
name = pathlib.Path(sys.argv[0]).name
with open(os.environ['COMMAND_LOG'], 'a') as f:
    f.write(json.dumps([name] + args) + '\\n')
if name == 'psql' and '-tA' in args:
    print(os.environ.get('DATABASES', ''))
    sys.exit(int(os.environ.get('INVENTORY_RC', '0')))
if name == 'pg_dump':
    pathlib.Path(next(a.split('=', 1)[1] for a in args if a.startswith('--file='))).write_text('dump')
if name == 'rclone' and args[0] == 'copy':
    source = pathlib.Path(os.environ['SNAPSHOT']) / args[1].rsplit('/', 1)[-1]
    target = pathlib.Path(args[2])
    if not source.exists(): sys.exit(1)
    target.mkdir(parents=True, exist_ok=True)
    if source.is_dir(): shutil.copytree(source, target, dirs_exist_ok=True)
    else: shutil.copy2(source, target / source.name)
'''


class BackupRestoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        for name in ('psql', 'pg_dump', 'pg_restore', 'rclone'):
            p = bin_dir / name
            p.write_text(MOCK)
            p.chmod(0o755)
        self.log = self.root / 'commands.jsonl'
        self.snapshot = self.root / 'snapshot'
        self.snapshot.mkdir()
        self.data = self.root / 'data'
        self.data.mkdir()
        self.env = dict(os.environ, PATH=str(bin_dir) + ':' + os.environ['PATH'],
                        COMMAND_LOG=str(self.log), SNAPSHOT=str(self.snapshot),
                        INSTANCE_NAME='target', SOURCE_TYPE='ObjectStorage',
                        SOURCE_PREFIX='backups/source', SOURCE_BUCKET='bucket',
                        BACKUP_ID='stamp', ODOO_DATA_DIR=str(self.data),
                        DB_NAME='odoo', DB_HOST='localhost', DB_PORT='5432', DB_USER='odoo')

    def fixture(self, names, missing=()):
        (self.snapshot / 'manifest.json').write_text(json.dumps(
            {'instance': 'source', 'extra_databases': names}))
        (self.snapshot / 'db.dump').write_text('primary dump')
        if names:
            (self.snapshot / 'dbs').mkdir()
        fs = self.root / 'archive'
        for name in names:
            if name not in missing:
                (self.snapshot / 'dbs' / (name + '.dump')).write_text('extra dump')
            path = fs / 'filestore' / name
            path.mkdir(parents=True)
            (path / 'attachment').write_text('file contents')
        fs.mkdir(exist_ok=True)
        with tarfile.open(self.snapshot / 'filestore.tar.gz', 'w:gz') as archive:
            for path in fs.iterdir():
                archive.add(path, arcname=path.name)

    def run_tool(self, name):
        script = (TOOLS / name).read_text().replace(
            'source /usr/local/lib/lib-objectstorage.sh', '_rclone_configure_remote() { :; }')
        script = script.replace('/filestore &&', str(self.data) + ' &&')
        script = script.replace('/backups/', str(self.root / 'backups') + '/')
        path = self.root / name
        path.write_text(script)
        return subprocess.run(['bash', str(path)], env=self.env, capture_output=True, text=True)

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_legacy_primary_only_object_storage_restore(self):
        self.fixture([])
        # Legacy manifests lack extra_databases entirely.
        (self.snapshot / 'manifest.json').write_text('{"instance":"source"}')
        result = self.run_tool('run-restore.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len([c for c in self.commands() if c[0] == 'pg_restore']), 1)

    def test_cross_instance_restore_maps_database_and_filestore(self):
        self.fixture(['source_live'])
        result = self.run_tool('run-restore.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.data / 'filestore/target_live/attachment').is_file())
        restores = [c for c in self.commands() if c[0] == 'pg_restore']
        self.assertEqual(restores[-1][restores[-1].index('-d') + 1], 'target_live')

    def test_hosting_prefix_is_independent_of_operator_resource_name(self):
        self.fixture(['source_live'])
        (self.snapshot / 'manifest.json').write_text(json.dumps({
            'instance': 'odoo-source', 'database_prefix': 'source_',
            'extra_databases': ['source_live']}))
        self.env.update(INSTANCE_NAME='odoo-target', DATABASE_PREFIX='target_')
        result = self.run_tool('run-restore.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.data / 'filestore/target_live/attachment').is_file())

    def test_missing_dump_aborts_before_database_changes(self):
        self.fixture(['source_live'], missing=['source_live'])
        result = self.run_tool('run-restore.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(c[0] in ('psql', 'pg_restore') for c in self.commands()))

    def test_corrupt_manifest_aborts_before_database_changes(self):
        self.fixture([])
        (self.snapshot / 'manifest.json').write_text('{')
        result = self.run_tool('run-restore.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(c[0] in ('psql', 'pg_restore') for c in self.commands()))

    def test_inventory_failure_aborts_backup(self):
        self.env.update(INVENTORY_RC='1', DESTINATION_TYPE='PVC')
        result = self.run_tool('run-backup.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'backups').exists())

    def test_backup_publishes_all_database_dumps(self):
        self.env.update(DATABASES='target_live\ntarget_test', DESTINATION_TYPE='PVC')
        result = self.run_tool('run-backup.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = next((self.root / 'backups').rglob('manifest.json'))
        self.assertEqual(json.loads(manifest.read_text())['extra_databases'], ['target_live', 'target_test'])
        self.assertTrue((manifest.parent / 'dbs/target_live.dump').is_file())
        self.assertTrue((manifest.parent / 'dbs/target_test.dump').is_file())


if __name__ == '__main__':
    unittest.main()
