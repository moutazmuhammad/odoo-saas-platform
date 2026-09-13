from unittest.mock import patch, MagicMock

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestKubernetesDriver(TransactionCase):
    """Phase 6: a SECOND ComputeDriver proves the Phase-1 seam — adding a backend
    is a new file + a one-line switch, with ZERO business-logic change."""

    def _handle(self):
        from odoo.addons.saas_core.drivers.base import ComputeHandle
        return ComputeHandle(server_id=1, container_name='odoo_acme',
                             instance_path='ns', host='1.2.3.4', http_port=8069)

    def _driver_over(self, fake_ssh):
        from odoo.addons.saas_core.drivers.kubernetes_driver import KubernetesDriver
        server = MagicMock()
        server._get_ssh_connection.return_value = fake_ssh
        server.id = 1
        return KubernetesDriver(server)

    # -------- the interface is fully satisfied (ABC enforces this) --------
    def test_implements_full_compute_driver_interface(self):
        from odoo.addons.saas_core.drivers.kubernetes_driver import KubernetesDriver
        from odoo.addons.saas_core.drivers.base import ComputeDriver
        d = KubernetesDriver(MagicMock())   # instantiates => all abstract methods implemented
        self.assertIsInstance(d, ComputeDriver)

    # -------- builds the right kubectl commands over a fake transport --------
    def test_builds_kubectl_commands(self):
        calls = []

        class FakeSSH:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, cmd, timeout=None):
                calls.append(cmd)
                return (0, 'Running|0', '')

        d = self._driver_over(FakeSSH())
        h = self._handle()
        d.start(h);   self.assertIn('scale deployment/odoo-acme --replicas=1', calls[-1])
        d.stop(h);    self.assertIn('scale deployment/odoo-acme --replicas=0', calls[-1])
        d.restart(h); self.assertIn('rollout restart deployment/odoo-acme', calls[-1])
        d.destroy(h); self.assertIn('delete deployment,service odoo-acme', calls[-1])
        d.destroy(h, purge=True); self.assertIn('delete namespace saas-odoo-acme', calls[-1])
        d.exec(h, 'echo hi'); self.assertIn('exec deployment/odoo-acme -- sh -c', calls[-1])
        d.logs(h, tail=50);   self.assertIn('logs deployment/odoo-acme --tail=50', calls[-1])
        # every command is namespaced
        self.assertTrue(all('-n saas-odoo-acme' in c for c in calls if c.startswith('kubectl -n')))

    def test_health_parses_pod_phase(self):
        class FakeSSH:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, cmd, timeout=None):
                return (0, 'Running|3', '')
        hs = self._driver_over(FakeSSH()).health(self._handle())
        self.assertEqual(hs.status, 'running')
        self.assertTrue(hs.running)
        self.assertEqual(hs.restart_count, 3)

    def test_health_not_found(self):
        class FakeSSH:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, cmd, timeout=None):
                return (1, '', 'No resources found')
        hs = self._driver_over(FakeSSH()).health(self._handle())
        self.assertEqual(hs.status, 'not_found')
        self.assertFalse(hs.running)

    def test_render_manifest(self):
        from odoo.addons.saas_core.drivers.kubernetes_driver import KubernetesDriver
        from odoo.addons.saas_core.drivers.base import ComputeSpec
        spec = ComputeSpec(container_name='odoo_acme', image='reg/odoo:18',
                           instance_path='/x', http_port=8069, longpolling_port=8072,
                           db_name='acme', db_host='db')
        y = KubernetesDriver(MagicMock()).render_manifest(spec)
        self.assertIn('kind: Deployment', y)
        self.assertIn('kind: Service', y)
        self.assertIn('image: reg/odoo:18', y)
        self.assertIn('tenant_id: odoo-acme', y)   # Phase-4 telemetry label

    # -------- the SEAM: business logic is identical for both backends --------
    def test_compute_driver_selected_by_server_type(self):
        from odoo.addons.saas_core.drivers.kubernetes_driver import KubernetesDriver
        from odoo.addons.saas_core.drivers.ssh_docker_driver import SshDockerDriver
        product = self.env['saas.product'].sudo().search([('is_hosting', '=', True)], limit=1) \
            or self.env['saas.product'].sudo().create({'name': 'K Host', 'is_hosting': True})
        plan = self.env['saas.plan'].sudo().create({
            'name': 'K Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [product.id])]})
        partner = self.env['res.partner'].sudo().create({'name': 'K Cust'})
        domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create({'name': 'k.example.com'})
        k8s_srv = self.env['saas.server'].sudo().create(
            {'name': 'k8s', 'compute_driver': 'kubernetes'})
        docker_srv = self.env['saas.server'].sudo().create(
            {'name': 'dkr', 'compute_driver': 'ssh_docker'})

        def mk(sub, srv):
            return self.env['saas.instance'].sudo().create({
                'subdomain': sub, 'domain_id': domain.id, 'partner_id': partner.id,
                'saas_product_id': product.id, 'plan_id': plan.id,
                'docker_server_id': srv.id, 'billing_period': 'monthly',
                'environment': 'production', 'region_id': False, 'state': 'running'})

        self.assertIsInstance(mk('konk8s', k8s_srv)._compute_driver(), KubernetesDriver)
        self.assertIsInstance(mk('kondkr', docker_srv)._compute_driver(), SshDockerDriver)

    def test_do_stop_routes_through_kubernetes_unchanged(self):
        """The god-model's _do_stop is NOT changed for K8s — it just calls
        self._compute_driver().stop(); on a k8s server that builds a kubectl
        scale-to-0. This is the no-rewrite proof."""
        product = self.env['saas.product'].sudo().search([('is_hosting', '=', True)], limit=1) \
            or self.env['saas.product'].sudo().create({'name': 'K Host2', 'is_hosting': True})
        plan = self.env['saas.plan'].sudo().create({
            'name': 'K Plan2', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [product.id])]})
        partner = self.env['res.partner'].sudo().create({'name': 'K Cust2'})
        domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create({'name': 'k2.example.com'})
        k8s_srv = self.env['saas.server'].sudo().create(
            {'name': 'k8s2', 'compute_driver': 'kubernetes'})
        inst = self.env['saas.instance'].sudo().create({
            'subdomain': 'konstop', 'domain_id': domain.id, 'partner_id': partner.id,
            'saas_product_id': product.id, 'plan_id': plan.id,
            'docker_server_id': k8s_srv.id, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': False, 'state': 'running'})

        calls = []

        class FakeSSH:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, cmd, timeout=None):
                calls.append(cmd); return (0, '', '')

        with patch.object(type(k8s_srv), '_get_ssh_connection', return_value=FakeSSH()):
            inst._do_stop()
        self.assertEqual(inst.state, 'stopped')
        self.assertTrue(any('scale deployment/odoo-konstop --replicas=0' in c for c in calls),
                        'expected _do_stop to issue a kubectl scale-to-0, got: %s' % calls)
