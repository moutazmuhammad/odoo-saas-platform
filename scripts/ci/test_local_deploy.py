"""Local deploy routing, immutable worktrees and failure receipts; no live targets."""
import importlib.util
import contextlib
import io
import stat
import sys
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('local_deploy', ROOT / 'scripts/deploy/local.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class LocalDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.values = self.root / 'values.yaml'
        self.values.write_text('{}\n')
        self.config = {'saas': {'host': 'example.com', 'key': '/tmp/key'},
                       'images': {'prefix': 'registry:5000/team', 'platforms': 'linux/amd64'},
                       'clusters': [{'name': 'client', 'context': 'client',
                                     'kubeconfig': '/tmp/kube', 'values': str(self.values)}]}
        self.files = {'control-plane/saas_core/a.py': 'a', 'frontend/veltnex/a.ts': 'b',
                      'compute/operator/main.go': 'c', 'compute/tools/backup-tool/a.sh': 'd'}
        self.state = self.root / 'state'

    def plan(self, **kwargs):
        return deploy.make_plan(self.config, self.files, self.state, **kwargs)

    def delivered(self, plan):
        if plan['saas']:
            deploy.mark(plan['saas'], 'release')
        for cluster in plan['clusters']:
            deploy.mark(cluster['operator'], 'release')
            deploy.mark(cluster['backup'], 'release')
            for _, entry in cluster['releases']:
                deploy.mark(entry, 'release')

    def test_first_deployment_includes_all_configured_targets(self):
        plan = self.plan()
        self.assertTrue(plan['saas']['needed'])
        self.assertTrue(plan['clusters'][0]['operator']['needed'])
        self.assertTrue(plan['clusters'][0]['backup']['needed'])

    def test_identical_source_is_skipped(self):
        self.delivered(self.plan())
        plan = self.plan()
        self.assertFalse(plan['saas']['needed'])
        self.assertFalse(plan['clusters'][0]['operator']['needed'])
        self.assertFalse(plan['clusters'][0]['backup']['needed'])

    def test_backend_and_dirty_frontend_route_to_saas_only(self):
        self.delivered(self.plan())
        self.files['frontend/veltnex/uncommitted.tsx'] = 'new'
        plan = self.plan()
        self.assertTrue(plan['saas']['needed'])
        self.assertFalse(plan['clusters'][0]['operator']['needed'])
        self.assertFalse(plan['clusters'][0]['backup']['needed'])

    def test_backup_only_does_not_rebuild_operator(self):
        self.delivered(self.plan())
        self.files['compute/tools/backup-tool/a.sh'] = 'new'
        plan = self.plan()
        self.assertFalse(plan['saas']['needed'])
        self.assertFalse(plan['clusters'][0]['operator']['needed'])
        self.assertTrue(plan['clusters'][0]['backup']['needed'])

    def test_deletions_are_deployed(self):
        self.delivered(self.plan())
        del self.files['control-plane/saas_core/a.py']
        plan = self.plan()
        self.assertTrue(plan['saas']['needed'])
        self.assertIn('control-plane/saas_core/a.py', plan['saas']['changed'])

    def test_full_can_force_unchanged_targets(self):
        self.delivered(self.plan())
        self.assertTrue(self.plan(full=True)['saas']['needed'])

    def test_explicit_saas_selection_does_not_read_cluster_credentials_or_values(self):
        self.values.unlink()
        plan = self.plan(target='saas')
        self.assertTrue(plan['saas']['needed'])
        self.assertEqual(plan['clusters'], [])

    def test_unknown_cluster_fails(self):
        with self.assertRaises(deploy.DeployError):
            self.plan(clusters=['wrong'])

    def test_operator_values_change_routes_to_operator_only(self):
        self.delivered(self.plan())
        self.values.write_text('replicaCount: 2\n')
        plan = self.plan()
        self.assertTrue(plan['clusters'][0]['operator']['needed'])
        self.assertFalse(plan['clusters'][0]['backup']['needed'])
        self.assertFalse(plan['saas']['needed'])

    def test_image_registry_change_republishes_images(self):
        self.delivered(self.plan())
        self.config['images']['prefix'] = 'other.example/team'
        cluster = self.plan()['clusters'][0]
        self.assertTrue(cluster['operator']['needed'])
        self.assertTrue(cluster['backup']['needed'])

    def test_receipts_are_per_destination(self):
        self.delivered(self.plan())
        self.config['clusters'][0]['context'] = 'different-server'
        plan = self.plan()
        self.assertTrue(plan['clusters'][0]['operator']['needed'])
        self.assertFalse(plan['saas']['needed'])

    def test_partial_success_does_not_advance_saas(self):
        plan = self.plan()
        deploy.mark(plan['clusters'][0]['operator'], 'release')
        deploy.mark(plan['clusters'][0]['backup'], 'release')
        retried = self.plan()
        self.assertTrue(retried['saas']['needed'])
        self.assertFalse(retried['clusters'][0]['operator']['needed'])

    def test_extra_vendor_release_routes_its_chart_changes(self):
        self.config['clusters'][0]['releases'] = [{
            'name': 'traefik', 'namespace': 'ingress', 'chart': 'compute/charts/vendor/traefik'}]
        self.files['compute/charts/vendor/traefik/values.yaml'] = 'old'
        self.delivered(self.plan())
        self.files['compute/charts/vendor/traefik/values.yaml'] = 'new'
        cluster = self.plan()['clusters'][0]
        self.assertTrue(cluster['releases'][0][1]['needed'])
        self.assertFalse(cluster['operator']['needed'])
        self.assertFalse(cluster['backup']['needed'])

    def test_unsafe_chart_and_duplicate_release_are_rejected(self):
        self.config['clusters'][0]['releases'] = [{
            'name': '../outside', 'namespace': 'ingress', 'chart': '/tmp/chart'}]
        with self.assertRaises(deploy.DeployError):
            self.plan()

    def test_backup_only_helm_has_no_inherited_operator_override(self):
        self.delivered(self.plan())
        self.files['compute/tools/backup-tool/a.sh'] = 'new'
        plan = self.plan(target='cluster')
        calls = []
        def run(args, **kwargs):
            calls.append((args, kwargs))
        env = dict(os.environ, OPERATOR_IMAGE='old:do-not-use')
        prepared = {'client': (env, self.values, {})}
        with patch.object(deploy, 'run', side_effect=run):
            deploy.deploy(plan, self.config, self.root, self.root, prepared, '123-1-' + 'a' * 40)
        docker = next(args for args, _ in calls if args[0] == 'docker')
        self.assertIn('--push', docker)
        self.assertIn(self.root / 'compute/tools/backup-tool', docker)
        invocation = next(kw for args, kw in calls if args[0] == 'bash')
        self.assertNotIn('OPERATOR_IMAGE', invocation['env'])
        self.assertIn('BACKUP_IMAGE', invocation['env'])

    def test_failed_cluster_does_not_write_receipts(self):
        plan = self.plan(target='cluster')
        def run(args, **kwargs):
            if args[0] == 'bash':
                raise subprocess.CalledProcessError(1, args)
        with patch.object(deploy, 'run', side_effect=run), self.assertRaises(subprocess.CalledProcessError):
            deploy.deploy(plan, self.config, self.root, self.root,
                          {'client': (dict(os.environ), self.values, {})}, '123-1-' + 'a' * 40)
        self.assertFalse(plan['clusters'][0]['operator']['receipt'].exists())
        self.assertFalse(plan['clusters'][0]['backup']['receipt'].exists())

    def test_worktree_includes_untracked_ignores_secrets_and_freezes_dirty_content(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / '.gitignore').write_text('control-plane/.env\n')
        source = self.root / 'control-plane'
        source.mkdir()
        (source / '.env').write_text('DO_NOT_SHIP=secret')
        file = source / 'new.py'
        file.write_text('dirty version')
        destination = self.root / 'frozen'
        files = deploy.source_files(self.root)
        self.assertIn('control-plane/new.py', files)
        self.assertNotIn('control-plane/.env', files)
        deploy.snapshot(destination, files)
        file.write_text('changed while deploying')
        self.assertEqual((destination / 'control-plane/new.py').read_text(), 'dirty version')

    def test_symlinks_cannot_escape_snapshot(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        directory = self.root / 'control-plane'
        directory.mkdir()
        (directory / 'outside.py').symlink_to('/etc/passwd')
        with self.assertRaises(deploy.DeployError):
            deploy.source_files(self.root)

    def test_preflight_captures_snap_kubectl_through_pipe_without_logging_credentials(self):
        binary_dir = self.root / 'bin'
        binary_dir.mkdir()
        kubectl = binary_dir / 'kubectl'
        kubectl.write_text(
            '#!' + sys.executable + '\n'
            'import os, stat, sys\n'
            'if stat.S_ISREG(os.fstat(1).st_mode):\n'
            '    sys.stderr.write("error: write /dev/stdout: bad file descriptor\\n")\n'
            '    sys.exit(1)\n'
            'os.write(1, b"apiVersion: v1\\nusers: [private-test-credential]\\n")\n')
        kubectl.chmod(0o755)
        original_run = deploy.run
        commands = []
        def execute(args, **kwargs):
            commands.append((args, kwargs))
            if '--raw' in args:
                return original_run(args, **kwargs)
            return b''
        log = io.StringIO()
        with patch.dict(os.environ, PATH=str(binary_dir) + os.pathsep + os.environ['PATH']), \
                patch.object(deploy, 'require'), patch.object(deploy, 'run', side_effect=execute), \
                contextlib.redirect_stdout(log):
            prepared = deploy.preflight(self.plan(target='cluster'), self.config, self.root, self.root)
        env, _, _ = prepared['client']
        kubeconfig = Path(env['KUBECONFIG'])
        self.assertIn(b'private-test-credential', kubeconfig.read_bytes())
        self.assertEqual(stat.S_IMODE(kubeconfig.stat().st_mode), 0o600)
        self.assertNotIn('private-test-credential', log.getvalue())
        raw_args, raw_kwargs = next((args, kw) for args, kw in commands if '--raw' in args)
        self.assertTrue(raw_kwargs['capture'])
        self.assertEqual(raw_args[raw_args.index('--context') + 1], 'client')

    def test_failed_kubeconfig_capture_does_not_create_partial_credentials_file(self):
        with patch.object(deploy, 'require'), \
                patch.object(deploy, 'run', side_effect=subprocess.CalledProcessError(1, ['kubectl'])), \
                self.assertRaises(subprocess.CalledProcessError):
            deploy.preflight(self.plan(target='cluster'), self.config, self.root, self.root)
        self.assertEqual(list(self.root.glob('*.kubeconfig')), [])

    def test_auto_platforms_build_only_architectures_of_target_nodes(self):
        self.config['images']['platforms'] = 'auto'
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if args[:3] == ['kubectl', 'get', 'nodes']:
                return b'{"items":[{"status":{"nodeInfo":{"architecture":"amd64"}}}]}'
            return b'private-kubeconfig'
        plan = self.plan(target='cluster')
        with patch.object(deploy, 'require'), patch.object(deploy, 'run', side_effect=run):
            prepared = deploy.preflight(plan, self.config, self.root, self.root)
            deploy.deploy(plan, self.config, self.root, self.root, prepared, '123-1-' + 'a' * 40)
        builds = [args for args in calls if args[:3] == ['docker', 'buildx', 'build']]
        self.assertEqual(len(builds), 2)
        for args in builds:
            self.assertEqual(args[args.index('--platform') + 1], 'linux/amd64')

    def test_auto_platforms_refuses_empty_cluster_before_build(self):
        self.config['images']['platforms'] = 'auto'
        def run(args, **kwargs):
            return b'{"items":[]}' if args[:3] == ['kubectl', 'get', 'nodes'] else b'kubeconfig'
        with patch.object(deploy, 'require'), patch.object(deploy, 'run', side_effect=run), \
                self.assertRaises(deploy.DeployError):
            deploy.preflight(self.plan(target='cluster'), self.config, self.root, self.root)

    def test_dry_run_never_builds_uploads_or_contacts_targets(self):
        path = self.root / 'config.json'
        deploy.save_json(path, self.config)
        source = self.root / 'a.py'
        source.write_text('local source')
        with patch.object(deploy, 'source_files', return_value={'control-plane/a.py': source}), \
                patch.object(deploy, 'preflight') as preflight, \
                patch.object(deploy, 'deploy') as execute, \
                patch.object(deploy, 'show_plan', return_value=True):
            deploy.main(['deploy', '--config', str(path), '--dry-run'])
        preflight.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(list((self.root / 'state').rglob('*.json')), [])

    def test_init_defaults_to_this_machines_doks_context(self):
        path = self.root / 'doks/config.json'
        deploy.initialize(path)
        cluster = deploy.read_json(path)['clusters'][0]
        self.assertEqual(cluster['name'], 'doks')
        self.assertEqual(cluster['context'], 'do-fra1-node')
        self.assertEqual(cluster['kubeconfig'], '~/Downloads/node-kubeconfig.yaml')
        self.assertEqual(Path(cluster['values']).read_text(), '{}\n')

    def test_init_accepts_an_explicit_cluster_context(self):
        path = self.root / 'other/config.json'
        deploy.initialize(path, cluster_name='doks-prod', kubeconfig='/tmp/doks.yaml',
                          context='do-ams3-production')
        cluster = deploy.read_json(path)['clusters'][0]
        self.assertEqual(cluster['name'], 'doks-prod')
        self.assertEqual(cluster['context'], 'do-ams3-production')
        self.assertEqual(cluster['kubeconfig'], '/tmp/doks.yaml')

    def test_init_does_not_overwrite_machine_configuration(self):
        path = self.root / 'machine/config.json'
        deploy.initialize(path)
        with self.assertRaises(deploy.DeployError):
            deploy.initialize(path)


if __name__ == '__main__':
    unittest.main()
