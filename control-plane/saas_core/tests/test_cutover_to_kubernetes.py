"""Tests for the Phase 2 (/ROADMAP.md §5) per-tenant cutover primitive:
action_cutover_to_kubernetes / _do_cutover_to_kubernetes.

These flip a legacy (ssh_docker) instance's live nginx vhost to an
already-migrated, verified Kubernetes instance's ingress. Mocked at the
same boundaries test_redeploy_blue_green.py uses (SSH connection,
ComputeDriver) since this reuses the same atomic-nginx-flip machinery,
plus the driver's health()/endpoint() calls this step adds.
"""
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


def _fake_ssh(healthy=True):
    calls = []

    class FakeSSH:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, cmd, timeout=None):
            calls.append(cmd)
            if 'curl' in cmd and '/web/login' in cmd:
                return (0, '', '') if healthy else (1, '', 'curl: timeout')
            return (0, '', '')

        def write_file(self, path, content):
            calls.append('WRITE %s' % path)

    return FakeSSH(), calls


@tagged('post_install', '-at_install')
class TestCutoverToKubernetes(TransactionCase):
    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or self.env['saas.product'].sudo().create(
            {'name': 'Cutover Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'Cutover Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.domain = self.env['saas.based.domain'].sudo().create(
            {'name': 'cutover.example.com'})
        self.partner = self.env['res.partner'].sudo().create({'name': 'Cutover Cust'})
        self.region = self.env['saas.region'].sudo().create(
            {'name': 'Cutover Region', 'code': 'cutover-region',
             'ingress_host': '192.168.1.15', 'ingress_port': 80})
        self.legacy_server = self.env['saas.server'].sudo().create(
            {'name': 'cutover-legacy-srv', 'docker_base_path': '/home/odoo'})
        self.k8s_server = self.env['saas.server'].sudo().create(
            {'name': 'cutover-k8s-srv', 'compute_driver': 'kubernetes',
             'region_id': self.region.id})
        self.source = self.env['saas.instance'].sudo().create({
            'subdomain': 'acme', 'domain_id': self.domain.id, 'partner_id': self.partner.id,
            'saas_product_id': self.product.id, 'plan_id': self.plan.id,
            'docker_server_id': self.legacy_server.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running',
            'xmlrpc_port': 8169, 'longpolling_port': 8172,
        })
        self.target = self.env['saas.instance'].sudo().create({
            'subdomain': 'acme-k8s', 'domain_id': self.domain.id, 'partner_id': self.partner.id,
            'saas_product_id': self.product.id, 'plan_id': self.plan.id,
            'docker_server_id': self.k8s_server.id,
            'migration_source_instance_id': self.source.id,
            'migration_state': 'verified',
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running',
        })

    def _run(self, fake_ssh, driver):
        with patch.object(type(self.legacy_server), '_get_ssh_connection',
                          lambda self: fake_ssh), \
                patch.object(type(self.target), '_compute_driver',
                             return_value=driver):
            self.source._do_cutover_to_kubernetes(self.target.id)

    def test_happy_path_flips_and_sets_active_instance(self):
        driver = MagicMock()
        driver.health.return_value = MagicMock(running=True, status='running')
        driver.endpoint.return_value = ('192.168.1.15', 80)
        fake_ssh, calls = _fake_ssh(healthy=True)

        self._run(fake_ssh, driver)

        self.assertEqual(self.source.active_instance_id, self.target)
        # nginx was written exactly once (the flip) — no rollback write.
        writes = [c for c in calls if c.startswith('WRITE')]
        self.assertEqual(len(writes), 1)

    def test_target_not_healthy_refuses_to_flip(self):
        driver = MagicMock()
        driver.health.return_value = MagicMock(running=False, status='restarting')
        fake_ssh, calls = _fake_ssh(healthy=True)

        with self.assertRaises(UserError):
            self._run(fake_ssh, driver)

        self.assertFalse(self.source.active_instance_id)
        self.assertFalse([c for c in calls if c.startswith('WRITE')])

    def test_no_ingress_host_configured_refuses_to_flip(self):
        self.region.ingress_host = False
        driver = MagicMock()
        driver.health.return_value = MagicMock(running=True, status='running')
        driver.endpoint.return_value = ('', 0)
        fake_ssh, calls = _fake_ssh(healthy=True)

        with self.assertRaises(UserError):
            self._run(fake_ssh, driver)

        self.assertFalse(self.source.active_instance_id)

    def test_post_flip_health_check_fails_rolls_back(self):
        driver = MagicMock()
        driver.health.return_value = MagicMock(running=True, status='running')
        driver.endpoint.return_value = ('192.168.1.15', 80)
        fake_ssh, calls = _fake_ssh(healthy=False)

        with patch.object(type(self.source), '_wait_until_domain_healthy',
                          return_value=False):
            with self.assertRaises(UserError):
                self._run(fake_ssh, driver)

        # active_instance_id was never set, and nginx was written TWICE:
        # once for the flip, once for the rollback.
        self.assertFalse(self.source.active_instance_id)
        writes = [c for c in calls if c.startswith('WRITE')]
        self.assertEqual(len(writes), 2)

    def test_rejects_unverified_target(self):
        self.target.migration_state = 'restoring'
        driver = MagicMock()
        fake_ssh, _calls = _fake_ssh()

        with self.assertRaises(UserError):
            self._run(fake_ssh, driver)

    def test_action_requires_verified_migration(self):
        other = self.env['saas.instance'].sudo().create({
            'subdomain': 'noverify', 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'docker_server_id': self.legacy_server.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running',
        })
        with self.assertRaises(UserError):
            other.action_cutover_to_kubernetes()

    def test_action_refuses_double_cutover(self):
        self.source.active_instance_id = self.target
        with self.assertRaises(UserError):
            self.source.action_cutover_to_kubernetes()
