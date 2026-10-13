"""Exercise actual Job generation and read-only build-pool evidence checks."""
import ast
import contextlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('build_pool_audit', ROOT / 'scripts/security/check-build-pool.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def build_module():
    # Import the real dependency-free driver logic, replacing only SDK model
    # constructors. Job dicts, Secret references and scripts execute unchanged.
    path = ROOT / 'control-plane/saas_core/drivers/k8s_builds.py'
    tree = ast.parse(path.read_text())
    tree.body = [node for node in tree.body if not (
        isinstance(node, ast.ImportFrom) and (node.module or '').startswith('kubernetes'))]
    class APIError(Exception):
        def __init__(self, status):
            self.status = status
    namespace = {'__file__': str(path), 'k8s_client': NS(V1Secret=NS, V1ConfigMap=NS, V1ObjectMeta=NS),
                 'ApiException': APIError}
    exec(compile(tree, str(path), 'exec'), namespace)
    return namespace


class BuildPoolTests(unittest.TestCase):
    def job(self, authenticated=True):
        module = build_module()
        driver = module['ImageBuildMixin']()
        core, batch = MagicMock(), MagicMock()
        driver._core_api = lambda: core
        driver._batch_api = lambda: batch
        driver._ensure_build_namespace = MagicMock()
        batch.create_namespaced_job.return_value.metadata.uid = 'job-uid'
        driver.start_image_build(name='build-test', repos=[], dockerfile='FROM odoo:18.0',
            requirements='', base_image='odoo:18.0', image_ref='registry.example.com/test:build',
            builder_image='moby/buildkit:rootless', git_image='alpine/git',
            registry_host='registry.example.com', registry_username='test' if authenticated else None,
            registry_password='fixture-password')
        return batch.create_namespaced_job.call_args.args[1], core

    def test_real_job_requires_build_pool_without_fallback(self):
        job, _ = self.job()
        spec = job['spec']['template']['spec']
        self.assertTrue(audit.build_placement(spec))
        self.assertNotIn('nodeName', spec)
        self.assertFalse(spec['automountServiceAccountToken'])
        required = spec['affinity']['nodeAffinity']['requiredDuringSchedulingIgnoredDuringExecution']
        required['nodeSelectorTerms'].append({'matchExpressions': [
            {'key': 'kubernetes.io/os', 'operator': 'In', 'values': ['linux']}]})
        self.assertFalse(audit.build_placement(spec), 'An OR fallback to general nodes must fail')

    def test_credentials_and_client_filesystem_are_separate_from_executor(self):
        for authenticated in (True, False):
            job, core = self.job(authenticated)
            spec = job['spec']['template']['spec']
            daemon = next(c for c in spec['initContainers'] if c['name'] == 'buildkitd')
            client = spec['containers'][0]
            self.assertFalse(spec['shareProcessNamespace'])
            self.assertEqual(daemon['restartPolicy'], 'Always')
            self.assertIn('startupProbe', daemon)
            self.assertEqual(daemon['securityContext']['runAsUser'], 1000)
            self.assertEqual(client['securityContext']['runAsUser'], 1001)
            self.assertEqual(daemon['args'][-2:], ['--group', '0'])
            self.assertEqual({m['name'] for m in daemon['volumeMounts']}, {'buildkit', 'buildkit-socket', 'files'})
            self.assertNotIn('env', daemon)
            self.assertEqual(daemon['volumeMounts'][-1]['subPath'], 'buildkitd.toml')
            self.assertNotIn('buildkit', {m['name'] for m in client['volumeMounts']})
            self.assertEqual('docker-config' in {m['name'] for m in client['volumeMounts']}, authenticated)
            self.assertEqual({m['name'] for c in spec['initContainers'][:2] for m in c['volumeMounts']}, {'workspace', 'files'})
            config = core.create_namespaced_config_map.call_args.args[1]
            self.assertIn('buildctl --addr', config.data['build.sh'])
            self.assertNotIn('buildctl-daemonless', config.data['build.sh'])
            self.assertNotIn('--secret', config.data['build.sh'])

    def evidence(self):
        job, _ = self.job()
        node = {'metadata': {'name': 'builder', 'labels': {audit.KEY: 'build'}},
                'spec': {'taints': [{'key': audit.KEY, 'value': 'build', 'effect': 'NoSchedule'}]}}
        pod = {'metadata': {'name': 'build-test-1', 'namespace': 'odoo-builds'},
               'spec': dict(job['spec']['template']['spec'], nodeName='builder'), 'status': {'phase': 'Running'}}
        pools = [{'name': 'build', 'auto_scale': True, 'min_nodes': 0, 'max_nodes': 2,
                  'labels': {audit.KEY: 'build'}, 'taints': node['spec']['taints'], 'nodes': [{'name': 'builder'}]},
                 {'name': 'general', 'auto_scale': False, 'nodes': [{'name': 'general-1'}]}]
        return node, pod, job, pools

    def test_running_and_zero_evidence_are_distinct(self):
        node, pod, job, pools = self.evidence()
        self.assertEqual(audit.audit([node], [pod], [job], pools, 'running')[0], [])
        self.assertTrue(audit.audit([node], [pod], [job], pools, 'zero')[0])
        pools[0]['nodes'] = []
        self.assertEqual(audit.audit([], [], [], pools, 'zero')[0], [])
        self.assertTrue(audit.audit([], [], [], pools, 'running')[0])

    def test_missing_taint_or_tenant_placement_fails(self):
        node, pod, job, pools = self.evidence()
        node['spec']['taints'] = []
        self.assertTrue(audit.audit([node], [pod], [job], pools)[0])
        node, pod, job, pools = self.evidence()
        pod['metadata']['namespace'] = 'odoo-tenant-test'
        self.assertTrue(audit.audit([node], [pod], [job], pools)[0])

    def test_zero_node_pool_still_needs_provider_template(self):
        _, _, _, pools = self.evidence()
        pools[0]['nodes'] = []
        pools[0]['labels'] = {}
        self.assertTrue(audit.audit([], [], [], pools, 'zero')[0])
        pools[0]['labels'] = {audit.KEY: 'build'}
        pools[0]['min_nodes'] = 1
        self.assertTrue(audit.audit([], [], [], pools, 'zero')[0])

    def test_required_infrastructure_daemonsets_are_reported(self):
        node, pod, _, pools = self.evidence()
        pod['metadata'].update(namespace='kube-system', ownerReferences=[{'kind': 'DaemonSet', 'controller': True}])
        errors, evidence = audit.audit([node], [pod], [], pools)
        self.assertEqual(errors, [])
        self.assertEqual(evidence['infrastructure_daemonsets'], ['kube-system/build-test-1'])

    def test_cli_only_reads_explicit_context_and_provider(self):
        node, pod, job, pools = self.evidence()
        def run(args, **kwargs):
            self.assertTrue(kwargs['check'])
            if args[0] == 'doctl':
                self.assertIn('test-cluster-id', args)
                body = pools
                self.assertIn('list', args)
            else:
                self.assertEqual(args[:3], ['kubectl', '--context', 'test'])
                self.assertEqual(args[3], 'get')
                body = {'items': {'nodes': [node], 'pods': [pod], 'jobs': [job]}[args[4]]}
            return NS(stdout=json.dumps(body))
        with contextlib.redirect_stdout(io.StringIO()), patch.object(audit.subprocess, 'run', side_effect=run):
            self.assertEqual(audit.main(['--context', 'test', '--doks-cluster', 'test-cluster-id', '--expect-running']), 0)
        with contextlib.redirect_stdout(io.StringIO()), patch.object(audit.subprocess, 'run', side_effect=OSError('offline')):
            self.assertEqual(audit.main(['--context', 'test']), 1)


if __name__ == '__main__':
    unittest.main()
