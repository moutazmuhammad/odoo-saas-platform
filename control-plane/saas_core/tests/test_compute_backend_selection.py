"""Tests for the platform-level compute-backend choice (Docker Compose vs.
Kubernetes, saas_master.default_compute_driver) and the customer-facing
compute tiers (saas.compute.tier — Standard/HA/Scale, replica count within
Kubernetes, entirely independent of the backend choice — see
saas_instance.py's own comments on _allocate_servers /
action_change_compute_tier).
"""
import json
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestComputeBackendSelection(TransactionCase):
    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or self.env['saas.product'].sudo().create(
            {'name': 'Backend Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'Backend Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.domain = self.env['saas.based.domain'].sudo().create(
            {'name': 'backend.example.com'})
        self.partner = self.env['res.partner'].sudo().create({'name': 'Backend Cust'})
        self.region = self.env['saas.region'].sudo().create(
            {'name': 'Backend Region', 'code': 'backend-region',
             'ingress_host': '192.168.1.20', 'ingress_port': 80})
        self.icp = self.env['ir.config_parameter'].sudo()

    def _isolate_docker_hosts(self):
        self.env['saas.server'].sudo().search(
            [('is_docker_host', '=', True)]).write({'is_docker_host': False})

    # ---------------------------------------------------------------
    # _allocate_servers honors saas_master.default_compute_driver
    # ---------------------------------------------------------------
    def test_new_instance_prefers_kubernetes_when_available(self):
        self._isolate_docker_hosts()
        self.icp.set_param('saas_master.default_compute_driver', 'kubernetes')
        docker_srv = self.env['saas.server'].sudo().create(
            {'name': 'bk-docker', 'is_docker_host': True, 'ip_v4': '127.0.0.1'})
        k8s_srv = self.env['saas.server'].sudo().create(
            {'name': 'bk-k8s', 'is_docker_host': True,
             'compute_driver': 'kubernetes', 'region_id': self.region.id})
        instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'backendtest1', 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': False, 'state': 'draft',
        })
        with patch.object(type(k8s_srv), '_probe_reachable', return_value=(True, '')), \
                patch.object(type(docker_srv), '_probe_reachable', return_value=(True, '')):
            instance._allocate_servers()
        self.assertEqual(instance.docker_server_id, k8s_srv)

    def test_falls_back_to_docker_compose_when_no_kubernetes_server_exists(self):
        """The platform default is Kubernetes, but nothing stands up a
        cluster automatically — an operator running only Docker Compose
        hosts must not have every deploy fail over an unmet preference."""
        self._isolate_docker_hosts()
        self.icp.set_param('saas_master.default_compute_driver', 'kubernetes')
        docker_srv = self.env['saas.server'].sudo().create(
            {'name': 'bk-docker-only', 'is_docker_host': True, 'ip_v4': '127.0.0.1'})
        instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'backendtest2', 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': False, 'state': 'draft',
        })
        with patch.object(type(docker_srv), '_probe_reachable', return_value=(True, '')):
            instance._allocate_servers()
        self.assertEqual(instance.docker_server_id, docker_srv)

    def test_default_compute_driver_can_be_set_to_docker_compose(self):
        self._isolate_docker_hosts()
        self.icp.set_param('saas_master.default_compute_driver', 'ssh_docker')
        docker_srv = self.env['saas.server'].sudo().create(
            {'name': 'bk-docker2', 'is_docker_host': True, 'ip_v4': '127.0.0.1'})
        k8s_srv = self.env['saas.server'].sudo().create(
            {'name': 'bk-k8s2', 'is_docker_host': True,
             'compute_driver': 'kubernetes', 'region_id': self.region.id})
        instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'backendtest3', 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': False, 'state': 'draft',
        })
        with patch.object(type(k8s_srv), '_probe_reachable', return_value=(True, '')), \
                patch.object(type(docker_srv), '_probe_reachable', return_value=(True, '')):
            instance._allocate_servers()
        self.assertEqual(instance.docker_server_id, docker_srv)


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
            {'name': 'Deploy Region', 'code': 'deploy-region',
             'ingress_host': '192.168.1.30', 'ingress_port': 80})
        self.k8s_server = self.env['saas.server'].sudo().create(
            {'name': 'deploy-k8s-srv', 'compute_driver': 'kubernetes',
             'region_id': self.region.id})
        self.version = self.env['saas.odoo.version'].sudo().search(
            [('is_hosting_version', '=', True)], limit=1) or \
            self.env['saas.odoo.version'].sudo().create({
                'name': '18.0', 'docker_image': 'odoo',
                'docker_image_tag': '18.0', 'nginx_template': 'new',
                'is_hosting_version': True})
        self.ha_tier = self.env['saas.compute.tier'].sudo().search(
            [('code', '=', 'ha')], limit=1) or self.env['saas.compute.tier'].sudo().create(
            {'name': 'HA', 'code': 'ha-deploytest', 'replicas': 2, 'monthly_price': 15.0})
        self.instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'freshk8s', 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'docker_server_id': self.k8s_server.id,
            'odoo_version_id': self.version.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'draft',
        })

    def _fake_ssh(self):
        class FakeSSH:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, cmd, timeout=None):
                return (0, '', '')

            def write_file(self, path, content):
                pass

        return FakeSSH()

    def test_deploy_calls_driver_create_and_provisions_nginx(self):
        driver = MagicMock()
        driver.create.return_value = MagicMock()
        driver.endpoint.return_value = ('192.168.1.30', 80)
        fake_ssh = self._fake_ssh()

        with patch.object(type(self.instance), '_compute_driver', return_value=driver), \
                patch.object(type(self.k8s_server), '_get_ssh_connection',
                             lambda self: fake_ssh), \
                patch.object(type(self.instance), '_data_service') as mock_ds:
            mock_ds.return_value._wait_until_healthy.return_value = None
            self.instance._do_deploy_locked_kubernetes()

        driver.create.assert_called_once()
        spec = driver.create.call_args.args[0]
        self.assertEqual(spec.env['domain'], self.instance.name)
        # Default compute tier is Standard = 1 replica.
        self.assertEqual(spec.env['replicas'], 1)
        driver.endpoint.assert_called_once()
        self.assertEqual(self.instance.state, 'running')
        # No per-tenant host-port was ever assigned — there's no such
        # concept for a Kubernetes-backed instance.
        self.assertFalse(self.instance.xmlrpc_port)

    def test_deploy_requests_tier_replicas(self):
        self.instance.compute_tier_id = self.ha_tier
        driver = MagicMock()
        driver.create.return_value = MagicMock()
        driver.endpoint.return_value = ('192.168.1.30', 80)
        fake_ssh = self._fake_ssh()

        with patch.object(type(self.instance), '_compute_driver', return_value=driver), \
                patch.object(type(self.k8s_server), '_get_ssh_connection',
                             lambda self: fake_ssh), \
                patch.object(type(self.instance), '_data_service') as mock_ds:
            mock_ds.return_value._wait_until_healthy.return_value = None
            self.instance._do_deploy_locked_kubernetes()

        spec = driver.create.call_args.args[0]
        self.assertEqual(spec.env['replicas'], 2)

    def test_deploy_refuses_without_region_ingress_host(self):
        self.region.ingress_host = False
        driver = MagicMock()
        with patch.object(type(self.instance), '_compute_driver', return_value=driver):
            with self.assertRaises(UserError):
                self.instance._do_deploy_locked_kubernetes()
        driver.create.assert_not_called()


@tagged('post_install', '-at_install')
class TestComputeTiers(TransactionCase):
    """action_change_compute_tier / _do_scale_compute_tier — the
    customer-facing, priced, multi-tier replica-count feature. Entirely
    independent of which backend the instance runs on being chosen
    elsewhere; this only tests what happens once an instance IS on
    Kubernetes."""

    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or self.env['saas.product'].sudo().create(
            {'name': 'Tier Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'Tier Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.domain = self.env['saas.based.domain'].sudo().create(
            {'name': 'tier.example.com'})
        self.partner = self.env['res.partner'].sudo().create({'name': 'Tier Cust'})
        self.region = self.env['saas.region'].sudo().create(
            {'name': 'Tier Region', 'code': 'tier-region'})
        self.k8s_server = self.env['saas.server'].sudo().create(
            {'name': 'tier-k8s-srv', 'compute_driver': 'kubernetes',
             'region_id': self.region.id})
        self.docker_server = self.env['saas.server'].sudo().create(
            {'name': 'tier-docker-srv'})
        self.standard_tier = self.env['saas.compute.tier'].sudo().search(
            [('code', '=', 'standard')], limit=1)
        self.ha_tier = self.env['saas.compute.tier'].sudo().create(
            {'name': 'HA', 'code': 'ha-tiertest', 'replicas': 2, 'monthly_price': 15.0})
        self.scale_tier = self.env['saas.compute.tier'].sudo().create(
            {'name': 'Scale', 'code': 'scale-tiertest', 'replicas': 4, 'monthly_price': 40.0})
        self.free_tier = self.env['saas.compute.tier'].sudo().create(
            {'name': 'Free 2-replica', 'code': 'free2-tiertest', 'replicas': 2,
             'monthly_price': 0.0})
        self.instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'tiertest', 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'docker_server_id': self.k8s_server.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running',
        })

    def test_new_instance_defaults_to_standard_tier(self):
        self.assertEqual(self.instance.compute_tier_id.code, 'standard')
        self.assertEqual(self.instance.compute_tier_id.replicas, 1)

    def test_change_requires_kubernetes_backend(self):
        self.instance.docker_server_id = self.docker_server
        with self.assertRaises(UserError):
            self.instance.action_change_compute_tier(self.ha_tier.id)

    def test_change_rejects_same_tier(self):
        with self.assertRaises(UserError):
            self.instance.action_change_compute_tier(self.standard_tier.id)

    def test_upgrade_to_priced_tier_creates_invoice_no_immediate_scale(self):
        invoice = self.instance.action_change_compute_tier(self.ha_tier.id)
        self.assertEqual(invoice.state, 'posted')
        self.assertEqual(self.instance.compute_tier_pending_invoice_id, invoice)
        self.assertEqual(self.instance.pending_compute_tier_id, self.ha_tier)
        # Not scaled yet — only once the invoice is paid.
        self.assertEqual(self.instance.compute_tier_id.code, 'standard')

    def test_upgrade_rejects_trial(self):
        self.instance.is_trial = True
        with self.assertRaises(UserError):
            self.instance.action_change_compute_tier(self.ha_tier.id)

    def test_downgrade_from_priced_tier_is_immediate_no_invoice(self):
        """No payment gate for a downgrade — verified at the enqueue
        boundary (a job targeting _do_scale_compute_tier, no invoice
        created); the scale itself is covered by test_scale_success_sets_tier."""
        self.instance.compute_tier_id = self.scale_tier
        with patch.object(type(self.env['saas.job']), '_spawn_worker',
                          lambda self: None):
            result = self.instance.action_change_compute_tier(self.ha_tier.id)
        self.assertTrue(result)
        self.assertFalse(self.instance.compute_tier_pending_invoice_id)
        job = self.env['saas.job'].search([
            ('model', '=', 'saas.instance'), ('res_id', '=', self.instance.id),
            ('method', '=', '_do_scale_compute_tier'),
        ], order='id desc', limit=1)
        self.assertTrue(job)
        self.assertEqual(json.loads(job.args_json), [self.ha_tier.id])
        # Not applied synchronously — that's the job's job.
        self.assertEqual(self.instance.compute_tier_id, self.scale_tier)

    def test_free_tier_change_is_immediate_no_invoice(self):
        """Even an upgrade in replica count (1 -> 2) is free/immediate if
        the target tier's price is 0 — nothing to gate payment behind."""
        with patch.object(type(self.env['saas.job']), '_spawn_worker',
                          lambda self: None):
            result = self.instance.action_change_compute_tier(self.free_tier.id)
        self.assertTrue(result)
        self.assertFalse(self.instance.compute_tier_pending_invoice_id)
        job = self.env['saas.job'].search([
            ('model', '=', 'saas.instance'), ('res_id', '=', self.instance.id),
            ('method', '=', '_do_scale_compute_tier'),
        ], order='id desc', limit=1)
        self.assertTrue(job)
        self.assertEqual(json.loads(job.args_json), [self.free_tier.id])

    def test_scale_success_sets_tier(self):
        driver = MagicMock()
        with patch.object(type(self.instance), '_compute_driver', return_value=driver), \
                patch.object(type(self.instance), '_data_service') as mock_ds, \
                patch('odoo.addons.saas_core.models.saas_instance.time.sleep'):
            mock_ds.return_value._wait_until_healthy.return_value = None
            self.instance._do_scale_compute_tier(self.ha_tier.id)
        driver.scale.assert_called_once()
        self.assertEqual(driver.scale.call_args.args[1], 2)
        self.assertEqual(self.instance.compute_tier_id, self.ha_tier)
        self.assertFalse(self.instance.pending_compute_tier_id)

    def test_scale_failure_rolls_back_and_keeps_previous_tier(self):
        driver = MagicMock()
        with patch.object(type(self.instance), '_compute_driver', return_value=driver), \
                patch.object(type(self.instance), '_data_service') as mock_ds, \
                patch('odoo.addons.saas_core.models.saas_instance.time.sleep'):
            mock_ds.return_value._wait_until_healthy.side_effect = RuntimeError(
                'never became healthy')
            with self.assertRaises(UserError):
                self.instance._do_scale_compute_tier(self.ha_tier.id)
        # Two scale() calls: the attempt (to 2), then the rollback (to 1,
        # the Standard tier's replica count).
        self.assertEqual(driver.scale.call_count, 2)
        self.assertEqual(driver.scale.call_args_list[0].args[1], 2)
        self.assertEqual(driver.scale.call_args_list[1].args[1], 1)
        self.assertEqual(self.instance.compute_tier_id.code, 'standard')

    def test_scale_requires_kubernetes_backend(self):
        self.instance.docker_server_id = self.docker_server
        with self.assertRaises(UserError):
            self.instance._do_scale_compute_tier(self.ha_tier.id)

    def test_paying_upgrade_invoice_enqueues_scale_and_clears_pending(self):
        """account_move's payment hook: paying the compute-tier upgrade
        invoice enqueues _do_scale_compute_tier(target tier) and clears
        the pending invoice — it does NOT set compute_tier_id itself,
        that's the job's job once the scale is confirmed healthy."""
        invoice = self.instance.action_change_compute_tier(self.ha_tier.id)
        with patch.object(type(self.env['saas.job']), '_spawn_worker',
                          lambda self: None):
            invoice.payment_state = 'paid'
        self.assertFalse(self.instance.compute_tier_pending_invoice_id)
        self.assertEqual(self.instance.compute_tier_id.code, 'standard')
        job = self.env['saas.job'].search([
            ('model', '=', 'saas.instance'), ('res_id', '=', self.instance.id),
            ('method', '=', '_do_scale_compute_tier'),
        ], order='id desc', limit=1)
        self.assertTrue(job)
        self.assertEqual(json.loads(job.args_json), [self.ha_tier.id])
