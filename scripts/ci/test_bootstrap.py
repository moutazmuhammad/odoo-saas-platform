"""Provisioning boundaries and readiness failures; no live infrastructure."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('bootstrap', ROOT / 'scripts/bootstrap/bootstrap.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.cfg = {'name': 'test', 'domain': 'apps.example.com', 'email': 'admin@example.com',
                    'kubeconfig': str(self.work / 'admin'), 'context': 'expected',
                    'storage_class': 'do-block-storage', 'output': str(self.work / 'output')}
        (self.work / 'admin').write_text('private')

    def test_plan_does_not_contact_or_mutate_infrastructure(self):
        path = self.work / 'config.json'
        path.write_text(json.dumps(self.cfg))
        with patch.object(bootstrap, 'command') as execute, patch.object(bootstrap, 'prepare_cluster') as prepare:
            bootstrap.main(['doks', 'plan', '--config', str(path)])
        execute.assert_not_called()
        prepare.assert_not_called()
        self.assertFalse((self.work / 'output').exists())

    def test_doks_requires_explicit_context(self):
        self.cfg.pop('context')
        with self.assertRaises(bootstrap.Error):
            bootstrap.validate('doks', self.cfg)

    def test_output_is_private_even_when_existing_file_was_public(self):
        path = self.work / 'output'
        path.write_text('old')
        path.chmod(0o644)
        bootstrap.private(path, b'new-token')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_output_refuses_symlink(self):
        output = self.work / 'output'
        target = self.work / 'original'
        target.write_text('preserve')
        output.symlink_to(target)
        # Path.resolve must not follow the destination before O_NOFOLLOW.
        with self.assertRaises(OSError):
            bootstrap.private(output, b'token')
        self.assertEqual(target.read_text(), 'preserve')

    def test_wrong_provider_is_rejected_before_any_helm_install(self):
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if args[:4] == ['kubectl', 'get', 'nodes', '-o']:
                return b'{"items":[{"spec":{"providerID":"aws:///node"}}]}'
            return b'config'
        with patch.object(bootstrap.local, 'require'), patch.object(bootstrap, 'command', side_effect=run), self.assertRaises(bootstrap.Error):
            bootstrap.prepare_cluster('doks', self.cfg, self.work / 'admin', self.work)
        self.assertFalse(any(args[0] == 'helm' for args in calls))

    def test_export_replaces_admin_credentials_with_service_account_token(self):
        cluster = bootstrap.Cluster(self.cfg, {}, self.work)
        calls = []
        def run(*args, **kwargs):
            calls.append((args, kwargs))
            if args[:2] == ('get', 'secret'):
                return json.dumps({'data': {'token': base64.b64encode(b'account-token').decode(), 'ca.crt': 'CA'}}).encode()
            if args[:2] == ('config', 'view'):
                return json.dumps({'clusters': [{'cluster': {'server': 'https://api.example.com'}}],
                                   'users': [{'user': {'exec': {'command': 'doctl'}, 'client-key-data': 'ADMIN'}}]}).encode()
        with patch.object(cluster, 'kubectl', side_effect=run), patch.object(cluster, 'apply'):
            output = bootstrap.export_kubeconfig(cluster)
        self.assertEqual(output['users'][0]['user'], {'token': 'account-token'})
        self.assertNotIn('ADMIN', json.dumps(output))
        self.assertNotIn('doctl', json.dumps(output))
        for args, kwargs in calls:
            if args[0] in ('get', 'config'):
                self.assertTrue(kwargs['capture'])

    def test_storage_failure_cleans_resources_and_does_not_request_certificate(self):
        cluster = bootstrap.Cluster(self.cfg, {}, self.work)
        applied = []
        calls = []
        def run(*args, **kwargs):
            calls.append(args)
            if args[0] == 'wait':
                raise subprocess.CalledProcessError(1, ['kubectl', 'wait'])
        with patch.object(cluster, 'kubectl', side_effect=run), \
                patch.object(cluster, 'apply', side_effect=lambda *objs: applied.extend(objs)), \
                self.assertRaises(subprocess.CalledProcessError):
            bootstrap.smoke(cluster)
        self.assertTrue(any(args[0] == 'delete' for args in calls))
        self.assertFalse(any(obj['kind'] == 'Certificate' for obj in applied))

    def test_failed_readiness_keeps_existing_output_and_skips_saas_registration(self):
        output = Path(self.cfg['output'])
        output.write_text('previous verified credential')
        cfg = dict(self.cfg, register_saas={'ssh': {}})
        def run(args, **kwargs):
            if args[:3] == ['kubectl', 'get', 'nodes']:
                return b'{"items":[{"spec":{"providerID":"digitalocean://123"}}]}'
            if args[:3] == ['kubectl', 'get', 'storageclass']:
                return b'{"items":[{"metadata":{"name":"do-block-storage","annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}]}'
            if args[:3] == ['kubectl', 'get', 'service']:
                return b'{"status":{"loadBalancer":{"ingress":[{"ip":"203.0.113.5"}]}}}'
            return b'private admin'
        with patch.object(bootstrap.local, 'require'), patch.object(bootstrap, 'command', side_effect=run), \
                patch.object(bootstrap.Cluster, 'helm'), patch.object(bootstrap.Cluster, 'apply'), \
                patch.object(bootstrap, 'smoke', side_effect=subprocess.CalledProcessError(1, ['kubectl', 'wait'])), \
                patch.object(bootstrap, 'remote_python') as register, \
                patch.object(bootstrap, 'export_kubeconfig') as export, \
                self.assertRaises(subprocess.CalledProcessError):
            bootstrap.prepare_cluster('doks', cfg, self.work / 'admin', self.work)
        export.assert_not_called()
        register.assert_not_called()
        self.assertEqual(output.read_text(), 'previous verified credential')

    def test_failed_upload_never_executes_privileged_script_and_cleans_staging(self):
        ssh_calls = []
        def ssh(server, remote, **kwargs):
            ssh_calls.append(remote)
            return b'/tmp/saas-bootstrap.ABCD1234\n'
        with patch.object(bootstrap, 'ssh', side_effect=ssh), \
                patch.object(bootstrap.local, 'ssh_options', return_value=([], '22', 'root@host')), \
                patch.object(bootstrap, 'command', side_effect=subprocess.CalledProcessError(1, ['scp'])), \
                self.assertRaises(subprocess.CalledProcessError):
            bootstrap.remote_python({}, 'ubuntu_microk8s.py', self.cfg)
        self.assertFalse(any('sudo' in text for text in ssh_calls))
        self.assertTrue(ssh_calls[-1].startswith('rm -rf -- /tmp/saas-bootstrap.'))

    def test_every_node_is_preflighted_before_first_install(self):
        cfg = dict(self.cfg, nodes=[{'name': 'a'}, {'name': 'b'}])
        with patch.object(bootstrap.local, 'require'), \
                patch.object(bootstrap, 'ssh', side_effect=[b'', subprocess.CalledProcessError(1, ['ssh'])]), \
                patch.object(bootstrap, 'remote_python') as install, \
                self.assertRaises(subprocess.CalledProcessError):
            bootstrap.microk8s(cfg, self.work)
        install.assert_not_called()


if __name__ == '__main__':
    unittest.main()
