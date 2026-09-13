from unittest.mock import patch, MagicMock

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestComputeDriver(TransactionCase):
    """Phase 1: ComputeDriver seam — driver command-building (mocked SSH) and
    god-model call-site routing (mocked driver). No real SSH/Docker here; the
    real-infra checks run separately against the live test tenant."""

    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or self.env['saas.product'].sudo().create(
            {'name': 'CD Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'CD Plan', 'is_custom': True, 'workers': 2, 'storage_limit': 10,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 50.0, 'yearly_price': 480.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.partner = self.env['res.partner'].sudo().create({'name': 'CD Cust'})
        self.domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create({'name': 'cd.example.com'})

    def _running_instance(self, sub):
        return self.env['saas.instance'].sudo().create({
            'subdomain': sub, 'domain_id': self.domain.id, 'partner_id': self.partner.id,
            'saas_product_id': self.product.id, 'plan_id': self.plan.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running'})

    # -------- driver: builds the right commands over a fake SSH --------
    def test_driver_builds_expected_commands(self):
        from odoo.addons.saas_core.drivers.ssh_docker_driver import SshDockerDriver
        from odoo.addons.saas_core.drivers.base import ComputeHandle
        calls = []

        class FakeSSH:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, cmd, timeout=None):
                calls.append(cmd)
                return (0, 'running|healthy', '')

        server = MagicMock()
        server._get_ssh_connection.return_value = FakeSSH()
        d = SshDockerDriver(server)
        h = ComputeHandle(server_id=1, container_name='odoo_x', instance_path='/srv/x',
                          host='1.2.3.4', http_port=8069)

        d.stop(h)
        self.assertIn('docker stop odoo_x', calls[-1])
        d.restart(h)
        self.assertIn('docker restart odoo_x', calls[-1])
        d.start(h)
        self.assertIn('docker compose up -d', calls[-1])
        d.destroy(h)
        self.assertIn('docker compose down', calls[-1])
        d.destroy(h, purge=True)
        self.assertIn('docker compose down -v --remove-orphans', calls[-1])
        d.stats(h)
        self.assertIn('docker stats --no-stream --format', calls[-1])
        self.assertIn('odoo_x', calls[-1])
        hs = d.health(h)
        self.assertTrue(hs.running)
        self.assertEqual(hs.status, 'running')
        self.assertEqual(d.endpoint(h), ('1.2.3.4', 8069))
        r = d.exec(h, 'echo hi')
        self.assertTrue(r.ok)
        self.assertIn('docker exec', calls[-1])
        self.assertIn('sh -c', calls[-1])
        self.assertIn('odoo_x', calls[-1])
        # shell= switches the in-container interpreter (pip helpers use bash)
        d.exec(h, 'echo hi', shell='bash')
        self.assertIn('bash -c', calls[-1])
        # stats_many batches MANY containers into one docker stats call
        d.stats_many(['odoo_a', 'odoo_b'])
        self.assertIn('docker stats --no-stream --format', calls[-1])
        self.assertIn('odoo_a', calls[-1])
        self.assertIn('odoo_b', calls[-1])

    def test_start_retries_transient_compose_failure_then_succeeds(self):
        # A transient `docker compose up` blip (daemon hiccup / recreate port
        # release race) must self-heal instead of failing the deploy.
        from odoo.addons.saas_core.drivers.ssh_docker_driver import SshDockerDriver
        from odoo.addons.saas_core.drivers.base import ComputeHandle
        server = MagicMock()
        d = SshDockerDriver(server)
        h = ComputeHandle(server_id=1, container_name='odoo_x',
                          instance_path='/srv/x', host='1.2.3.4', http_port=8069)
        seq = [(1, '', 'Error response from daemon: fail'), (0, '', '')]
        calls = {'n': 0}

        def fake_compose(ssh, path, verb, timeout=None):
            calls['n'] += 1
            return seq[min(calls['n'] - 1, len(seq) - 1)]

        with patch.object(d, '_compose', side_effect=fake_compose), \
                patch('odoo.addons.saas_core.drivers.ssh_docker_driver.time.sleep'):
            d.start(h)  # must NOT raise
        self.assertEqual(calls['n'], 2, "should retry once then succeed")

    def test_start_gives_up_after_retry_budget(self):
        from odoo.addons.saas_core.drivers.ssh_docker_driver import SshDockerDriver
        from odoo.addons.saas_core.drivers.base import ComputeHandle
        server = MagicMock()
        d = SshDockerDriver(server)
        h = ComputeHandle(server_id=1, container_name='odoo_x',
                          instance_path='/srv/x', host='1.2.3.4', http_port=8069)
        with patch.object(d, '_compose', return_value=(1, '', 'boom')) as m, \
                patch('odoo.addons.saas_core.drivers.ssh_docker_driver.time.sleep'):
            with self.assertRaises(RuntimeError):
                d.start(h)
        self.assertEqual(m.call_count, d._COMPOSE_UP_ATTEMPTS,
                         "a persistent failure must exhaust the retry budget then raise")

    def test_driver_health_reports_not_running(self):
        from odoo.addons.saas_core.drivers.ssh_docker_driver import SshDockerDriver
        from odoo.addons.saas_core.drivers.base import ComputeHandle

        class FakeSSH:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, cmd, timeout=None):
                return (0, 'exited|unhealthy', '')

        server = MagicMock()
        server._get_ssh_connection.return_value = FakeSSH()
        d = SshDockerDriver(server)
        h = ComputeHandle(server_id=1, container_name='odoo_x', instance_path='/srv/x')
        self.assertFalse(d.health(h).running)

    def test_health_parses_status_and_restart_count(self):
        from odoo.addons.saas_core.drivers.ssh_docker_driver import SshDockerDriver
        from odoo.addons.saas_core.drivers.base import ComputeHandle

        class FakeSSH:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, cmd, timeout=None):
                return (0, 'restarting|7|', '')

        server = MagicMock()
        server._get_ssh_connection.return_value = FakeSSH()
        d = SshDockerDriver(server)
        h = ComputeHandle(server_id=1, container_name='odoo_x', instance_path='/srv/x')
        hs = d.health(h)
        self.assertEqual(hs.status, 'restarting')
        self.assertEqual(hs.restart_count, 7)
        self.assertFalse(hs.running)

    def test_health_missing_container_is_not_found(self):
        from odoo.addons.saas_core.drivers.ssh_docker_driver import SshDockerDriver
        from odoo.addons.saas_core.drivers.base import ComputeHandle

        class FakeSSH:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, cmd, timeout=None):
                return (1, '', 'Error: No such object: odoo_x')

        server = MagicMock()
        server._get_ssh_connection.return_value = FakeSSH()
        d = SshDockerDriver(server)
        h = ComputeHandle(server_id=1, container_name='odoo_x', instance_path='/srv/x')
        hs = d.health(h)
        self.assertEqual(hs.status, 'not_found')
        self.assertFalse(hs.running)

    def test_wait_until_running_stops_on_running(self):
        from odoo.addons.saas_core.drivers.ssh_docker_driver import SshDockerDriver
        from odoo.addons.saas_core.drivers.base import ComputeHandle, HealthStatus

        d = SshDockerDriver(MagicMock())
        h = ComputeHandle(server_id=1, container_name='odoo_x', instance_path='/srv/x')
        seq = [HealthStatus(running=False, status='created'),
               HealthStatus(running=True, status='running')]
        with patch.object(SshDockerDriver, 'health', side_effect=seq), \
             patch('odoo.addons.saas_core.drivers.ssh_docker_driver.time.sleep'):
            hs = d.wait_until_running(h, attempts=5, interval=0)
        self.assertTrue(hs.running)

    def test_wait_until_running_returns_on_terminal(self):
        from odoo.addons.saas_core.drivers.ssh_docker_driver import SshDockerDriver
        from odoo.addons.saas_core.drivers.base import ComputeHandle, HealthStatus

        d = SshDockerDriver(MagicMock())
        h = ComputeHandle(server_id=1, container_name='odoo_x', instance_path='/srv/x')
        with patch.object(SshDockerDriver, 'health',
                          return_value=HealthStatus(running=False, status='exited')), \
             patch('odoo.addons.saas_core.drivers.ssh_docker_driver.time.sleep'):
            hs = d.wait_until_running(h, attempts=5, interval=0)
        self.assertFalse(hs.running)
        self.assertEqual(hs.status, 'exited')

    # -------- god-model: _do_stop/_do_restart route to the driver --------
    def test_do_stop_routes_to_driver(self):
        inst = self._running_instance('cdstop')
        fake = MagicMock()
        with patch.object(type(inst), '_compute_driver', return_value=fake), \
             patch.object(type(inst), '_compute_handle', return_value='HANDLE'):
            inst._do_stop()
        fake.stop.assert_called_once_with('HANDLE')
        self.assertEqual(inst.state, 'stopped')
        self.assertFalse(inst.pending_operation)

    def test_do_restart_routes_to_driver(self):
        inst = self._running_instance('cdrestart')
        fake = MagicMock()
        with patch.object(type(inst), '_compute_driver', return_value=fake), \
             patch.object(type(inst), '_compute_handle', return_value='HANDLE'):
            inst._do_restart()
        fake.restart.assert_called_once_with('HANDLE')
        self.assertEqual(inst.state, 'running')

    def test_do_stop_wraps_driver_error_as_usererror(self):
        inst = self._running_instance('cderr')
        fake = MagicMock()
        fake.stop.side_effect = RuntimeError('boom')
        with patch.object(type(inst), '_compute_driver', return_value=fake), \
             patch.object(type(inst), '_compute_handle', return_value='H'):
            with self.assertRaises(UserError):
                inst._do_stop()

    # -------- service_exec: compose-exec form + connection reuse --------
    def test_service_exec_builds_compose_exec_command(self):
        from odoo.addons.saas_core.drivers.ssh_docker_driver import SshDockerDriver
        from odoo.addons.saas_core.drivers.base import ComputeHandle

        class FakeSSH:
            def __init__(self): self.cmds = []
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, cmd, timeout=None):
                self.cmds.append(cmd); return (0, 'ok', '')

        fake = FakeSSH()
        server = MagicMock()
        server._get_ssh_connection.return_value = fake
        d = SshDockerDriver(server)
        h = ComputeHandle(server_id=1, container_name='odoo_x', instance_path='/srv/x')
        r = d.service_exec(h, 'psql -tA -c "select 1"', env={'PGPASSWORD': 'secret'})
        server._get_ssh_connection.assert_called_once()
        self.assertIn(
            'cd /srv/x && docker compose exec -T -e PGPASSWORD=secret odoo psql',
            fake.cmds[-1])
        self.assertTrue(r.ok)

    def test_service_exec_reuses_provided_connection(self):
        from odoo.addons.saas_core.drivers.ssh_docker_driver import SshDockerDriver
        from odoo.addons.saas_core.drivers.base import ComputeHandle

        class FakeConn:
            def __init__(self): self.cmds = []
            def execute(self, cmd, timeout=None):
                self.cmds.append(cmd); return (0, 'ok', '')

        conn = FakeConn()
        server = MagicMock()
        d = SshDockerDriver(server, connection=conn)
        h = ComputeHandle(server_id=1, container_name='odoo_x', instance_path='/srv/x')
        d.service_exec(h, 'echo hi')
        # reused the open connection; never opened a new one
        server._get_ssh_connection.assert_not_called()
        self.assertIn('docker compose exec -T  odoo echo hi', conn.cmds[-1])

    # -------- saas.docker.container admin actions route to the driver --------
    def test_docker_container_actions_route_to_driver(self):
        server = self.env['saas.server'].sudo().create({'name': 'cd-srv'})
        cont = self.env['saas.docker.container'].sudo().create(
            {'server_id': server.id, 'name': 'odoo_x'})
        fake = MagicMock()
        with patch.object(type(cont), '_driver_handle', return_value=(fake, 'H')), \
             patch.object(type(server), 'action_refresh_containers', return_value=True):
            cont.action_stop_container()
            cont.action_restart_container()
        fake.stop.assert_called_once_with('H')
        fake.restart.assert_called_once_with('H')
