"""Tests for single-pod Kubernetes deployment and vertical resource resizing."""
import json
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestDeployOnKubernetes(TransactionCase):
    """_do_deploy_locked_kubernetes — provisioning a BRAND-NEW instance
    directly on Kubernetes (no migration involved)."""

    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or self.env['saas.product'].sudo().create(
            {'name': 'Deploy Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'Deploy Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.domain = self.env['saas.based.domain'].sudo().create(
            {'name': 'deploy.example.com'})
        self.partner = self.env['res.partner'].sudo().create({'name': 'Deploy Cust'})
        self.region = self.env['saas.region'].sudo().create(
            {'name': 'Deploy Region', 'code': 'deploy-region'})
        self.k8s_server = self.env['saas.server'].sudo().create(
            {'name': 'deploy-k8s-srv', 'compute_driver': 'kubernetes',
             'region_id': self.region.id})
        self.version = self.env['saas.odoo.version'].sudo().search(
            [('is_hosting_version', '=', True)], limit=1) or \
            self.env['saas.odoo.version'].sudo().create({
                'name': '18.0', 'docker_image': 'odoo',
                'docker_image_tag': '18.0', 'nginx_template': 'new',
                'is_hosting_version': True})
        self.instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'freshk8s', 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'docker_server_id': self.k8s_server.id,
            'odoo_version_id': self.version.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'draft',
        })

    def test_deploy_calls_driver_create_natively_no_ssh_or_nginx(self):
        """A Kubernetes deploy never touches SSH/Nginx — TLS/ingress is
        exclusively cert-manager + the cluster's own Ingress."""
        driver = MagicMock()
        driver.create.return_value = MagicMock()

        with patch.object(type(self.instance), '_compute_driver', return_value=driver), \
                patch.object(type(self.instance), '_data_service') as mock_ds:
            mock_ds.return_value._wait_until_healthy.return_value = None
            self.instance._do_deploy_locked_kubernetes()

        driver.create.assert_called_once()
        spec = driver.create.call_args.args[0]
        self.assertEqual(spec.env['domain'], self.instance.name)
        # Every instance runs one Odoo replica.
        self.assertEqual(spec.env['replicas'], 1)
        self.assertTrue(spec.env['tls_enabled'])
        self.assertEqual(spec.env['tls_issuer_name'], 'letsencrypt-prod')
        # No external Nginx step for a Kubernetes-native-TLS region.
        driver.endpoint.assert_not_called()
        self.assertEqual(self.instance.state, 'running')
        # No per-tenant host-port was ever assigned — there's no such
        # concept for a Kubernetes-backed instance.
        self.assertFalse(self.instance.xmlrpc_port)


    def test_deploy_rejects_stalled_cluster_before_creating_resources(self):
        driver = MagicMock()
        driver.require_cluster_ready.side_effect = RuntimeError('controllers are stalled')
        with patch.object(type(self.instance), '_compute_driver', return_value=driver):
            with self.assertRaisesRegex(RuntimeError, 'controllers are stalled'):
                self.instance._do_deploy_locked_kubernetes()
        driver.create.assert_not_called()

    def test_deploy_requests_plan_resources(self):
        """The pod is sized from the plan, not the driver's 1 CPU / 2Gi
        defaults."""
        self.plan.write({'cpu_limit': 2.0, 'ram_limit': '4g', 'workers': 3})
        driver = MagicMock()
        driver.create.return_value = MagicMock()
        with patch.object(type(self.instance), '_compute_driver', return_value=driver), \
                patch.object(type(self.instance), '_data_service') as mock_ds:
            mock_ds.return_value._wait_until_healthy.return_value = None
            self.instance._do_deploy_locked_kubernetes()
        env = driver.create.call_args.args[0].env
        self.assertEqual(env['cpu_limit'], '2000m')
        self.assertEqual(env['cpu_request'], '500m')
        self.assertEqual(env['mem_limit'], '4096Mi')
        self.assertEqual(env['mem_request'], '1024Mi')
        self.assertEqual(env['workers'], 3)

    def test_plan_resources_floor_small_requests(self):
        self.plan.write({'cpu_limit': 0.2, 'ram_limit': '256m', 'workers': 0})
        res = self.instance._k8s_plan_resources()
        self.assertEqual(res['cpu_request'], '100m')
        self.assertEqual(res['mem_request'], '128Mi')
        self.assertEqual(res['workers'], 0)

    def test_update_container_resources_applies_the_whole_package(self):
        self.instance.state = 'running'
        self.plan.write({'cpu_limit': 1.5, 'ram_limit': '1536m', 'workers': 2})
        driver = MagicMock()
        with patch.object(type(self.instance), '_compute_driver', return_value=driver), \
                patch.object(type(self.instance), '_compute_handle', return_value='H'):
            self.instance._update_container_resources()
        handle, res = driver.set_package.call_args.args
        self.assertEqual(handle, 'H')
        self.assertEqual(
            (res['cpu_request'], res['cpu_limit'], res['mem_request'], res['mem_limit'], res['workers']),
            ('375m', '1500m', '384Mi', '1536Mi', 2))
        # PostgreSQL: 0.25 CPU and 256 MB per worker, 512 MB minimum.
        self.assertEqual((res['db_cpu_limit'], res['db_mem_limit']), ('500m', '512Mi'))
        self.assertIn('limits.cpu', res['quota'])

    def test_update_container_resources_skips_instance_without_plan(self):
        self.instance.plan_id = False
        driver = MagicMock()
        with patch.object(type(self.instance), '_compute_driver', return_value=driver):
            self.instance._update_container_resources()
        driver.set_package.assert_not_called()

    def test_deploy_refuses_without_tls_issuer(self):
        """TLS is always cluster-native; a cluster with no ClusterIssuer
        can't deploy."""
        self.k8s_server.tls_cluster_issuer = False
        driver = MagicMock()
        with patch.object(type(self.instance), '_compute_driver', return_value=driver):
            with self.assertRaises(UserError):
                self.instance._do_deploy_locked_kubernetes()
        driver.create.assert_not_called()
