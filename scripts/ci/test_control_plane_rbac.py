"""Permission-audit failure handling and actual driver pod generation, without Odoo."""
import ast
import contextlib
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('rbac_audit', ROOT / 'scripts/security/check-control-plane-rbac.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class ControlPlaneRBACChecks(unittest.TestCase):
    def test_audit_checks_service_account_groups_and_fails_closed(self):
        def answer(args, **kwargs):
            for group in audit.impersonation('kube-system', 'saas-control-plane')[1:]:
                if '--as=system:serviceaccount:kube-system:saas-control-plane' in args:
                    self.assertIn(group, args)
            request = args[args.index('can-i') + 1:]
            namespace, account = ('saas-toolbox', 'saas-toolbox') if '--as=system:serviceaccount:saas-toolbox:saas-toolbox' in args else ('kube-system', 'saas-control-plane')
            if namespace == 'saas-toolbox':
                expected = request == ['list', 'nodes']
            else:
                expected = dict((tuple(r), yes) for yes, r in audit.checks(['odoo-tenant-test'], 'monitoring', 'prometheus-server:80'))[tuple(request)]
            return SimpleNamespace(returncode=0 if expected else 1, stdout='yes' if expected else 'no', stderr='')
        with contextlib.redirect_stdout(io.StringIO()), patch.object(audit.subprocess, 'run', side_effect=answer):
            self.assertEqual(audit.main(['--context', 'test', '--tenant-namespace', 'odoo-tenant-test']), 0)
        with contextlib.redirect_stdout(io.StringIO()), patch.object(audit.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='', stderr='connection refused')):
            self.assertEqual(audit.main(['--context', 'test']), 1)

    def test_admission_probe_uses_server_dry_run_and_requires_policy_rejection(self):
        def answer(args, **kwargs):
            self.assertIn('--dry-run=server', args)
            hostile = '"privileged": true' in kwargs['input']
            return SimpleNamespace(returncode=1 if hostile else 0, stderr='denied by veltnex-control-plane-workloads' if hostile else '')
        with contextlib.redirect_stdout(io.StringIO()), patch.object(audit.subprocess, 'run', side_effect=answer):
            self.assertTrue(audit.admission_checks(['kubectl', '--context', 'test']))
        with contextlib.redirect_stdout(io.StringIO()), patch.object(audit.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stderr='connection refused')):
            self.assertFalse(audit.admission_checks(['kubectl', '--context', 'test']))

    def test_actual_toolbox_pod_is_non_root_and_drops_capabilities(self):
        path = ROOT / 'control-plane/saas_core/drivers/kubernetes_driver.py'
        tree = ast.parse(path.read_text())
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_toolbox_pod_body')
        method.decorator_list = []
        module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
        namespace = {'_TOOLBOX_NAME': 'saas-toolbox', '_TOOLBOX_LIFETIME': 28800, '_TOOLBOX_PULL_SECRET': 'saas-toolbox-registry'}
        exec(compile(module, str(path), 'exec'), namespace)
        driver = SimpleNamespace(server=SimpleNamespace(name='test', _toolbox_image=lambda: 'toolbox:test'), _registry_pull_auth=lambda: None)
        pod = namespace['_toolbox_pod_body'](driver)
        self.assertTrue(pod['spec']['securityContext']['runAsNonRoot'])
        self.assertEqual(pod['spec']['securityContext']['seccompProfile']['type'], 'RuntimeDefault')
        container = pod['spec']['containers'][0]
        self.assertFalse(container['securityContext']['allowPrivilegeEscalation'])
        self.assertEqual(container['securityContext']['capabilities']['drop'], ['ALL'])

    def test_missing_build_namespace_requires_chart_instead_of_creating_namespace(self):
        path = ROOT / 'control-plane/saas_core/drivers/k8s_builds.py'
        tree = ast.parse(path.read_text())
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_ensure_build_namespace')
        module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
        class APIError(Exception):
            status = 404
        namespace = {'ApiException': APIError, 'BUILD_NAMESPACE': 'odoo-builds'}
        exec(compile(module, str(path), 'exec'), namespace)
        core = Mock()
        core.read_namespace.side_effect = APIError()
        driver = SimpleNamespace(_core_api=lambda: core)
        with self.assertRaisesRegex(RuntimeError, 'Install the operator chart'):
            namespace['_ensure_build_namespace'](driver)
        core.create_namespace.assert_not_called()
