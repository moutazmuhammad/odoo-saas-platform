"""Characterization tests for _do_redeploy's blue/green (zero-downtime)
and fallback (in-place recreate) paths.

Originally written BEFORE routing the green-sidecar stand-up/teardown
through ComputeDriver (MICROSERVICES-PLAN.md's Phase 2.3), as a safety
net for that refactor — this is the actual mechanism protecting a live
customer redeploy from an outage, and it had zero test coverage of its
own before this file existed. All 7 tests passed unmodified against the
pre-refactor implementation; after routing create_shadow()/
destroy_shadow() through the driver (same commit), the assertions on
green stand-up/teardown were updated from raw-SSH-string checks to
driver-call checks (matching how the canonical container's destroy()/
start() calls were already asserted), since that's now genuinely where
those actions happen — the underlying behavior verified is unchanged.

Scope deliberately excludes git pull/requirements-validation (repo_ids
is empty in every fixture here, making those loops no-ops) to isolate
the green-sidecar orchestration itself: stand up green, boot-check it,
flip nginx, promote (destroy+start the canonical container), flip back,
tear green down — and every failure branch in that sequence.
"""
from unittest.mock import ANY, MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


def _fake_ssh(responses=None, canon_compose=None):
    """A callable ssh.execute()/write_file() double. `responses` maps a
    command substring to a fixed (rc, out, err); anything unmatched
    returns (0, '', ''). `canon_compose` is returned for the `cat
    .../docker-compose.yml` read (defaults to a minimal but realistic
    compose fragment covering the container_name/port substitutions
    _do_redeploy performs)."""
    responses = dict(responses or {})
    calls = []
    writes = {}

    class FakeSSH:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, cmd, timeout=None):
            calls.append(cmd)
            if 'cat ' in cmd and 'docker-compose.yml' in cmd and 'green' not in cmd:
                return (0, canon_compose, '')
            for needle, resp in responses.items():
                if needle in cmd:
                    return resp
            return (0, '', '')

        def write_file(self, path, content):
            writes[path] = content

    return FakeSSH(), calls, writes


_CANON_COMPOSE = (
    "services:\n"
    "  odoo:\n"
    "    container_name: odoo_bgtest\n"
    "    ports:\n"
    "      - \"127.0.0.1:8169:8069\"\n"
    "      - \"127.0.0.1:8172:8072\"\n"
)


@tagged('post_install', '-at_install')
class TestRedeployBlueGreen(TransactionCase):
    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or self.env['saas.product'].sudo().create(
            {'name': 'BG Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'BG Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.domain = self.env['saas.based.domain'].sudo().create(
            {'name': 'bg.example.com'})  # no proxy_server_id -> zero-downtime eligible
        self.partner = self.env['res.partner'].sudo().create({'name': 'BG Cust'})
        self.server = self.env['saas.server'].sudo().create(
            {'name': 'bg-srv', 'docker_base_path': '/home/odoo'})
        self.instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'bgtest', 'domain_id': self.domain.id, 'partner_id': self.partner.id,
            'saas_product_id': self.product.id, 'plan_id': self.plan.id,
            'docker_server_id': self.server.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running',
            'xmlrpc_port': 8169, 'longpolling_port': 8172,
        })

    def _run(self, fake_ssh, driver, wait_healthy_side_effect, flip_calls,
             ephemeral_ports=(34001, 34002)):
        with patch.object(type(self.server), '_get_ssh_connection',
                          lambda self: fake_ssh), \
                patch.object(type(self.instance), '_compute_driver',
                             return_value=driver), \
                patch.object(type(self.instance), '_wait_until_healthy',
                             side_effect=wait_healthy_side_effect), \
                patch.object(type(self.instance), '_flip_nginx',
                             side_effect=lambda ssh, h, l: flip_calls.append((h, l))), \
                patch.object(type(self.instance), '_allocate_ephemeral_ports',
                             return_value=ephemeral_ports), \
                patch.object(type(self.instance), '_render_and_write_configs',
                             lambda self, ssh: None):
            self.instance._do_redeploy()

    # ---------------------------------------------------------------
    # Zero-downtime (blue/green) path
    # ---------------------------------------------------------------
    def test_zero_downtime_happy_path(self):
        driver = MagicMock()
        fake_ssh, calls, writes = _fake_ssh(canon_compose=_CANON_COMPOSE)
        flip_calls = []

        def wait_healthy(ssh, timeout=180, container=None):
            return (True, '')

        self._run(fake_ssh, driver, wait_healthy, flip_calls)

        # Green stood up on the alternate compose file/project, sharing
        # the ephemeral ports, then torn down again after promotion —
        # both now via the driver seam (create_shadow/destroy_shadow),
        # not raw SSH commands.
        green_file = '%s/docker-compose.green.yml' % self.instance._get_instance_path()
        driver.create_shadow.assert_called_once()
        create_kwargs = driver.create_shadow.call_args.kwargs
        self.assertEqual(create_kwargs['compose_path'], green_file)
        self.assertEqual(create_kwargs['project'], 'bgtest_green')
        self.assertIn('container_name: odoo_bgtest_green', create_kwargs['compose_content'])
        self.assertIn(':34001:8069', create_kwargs['compose_content'])
        self.assertIn(':34002:8072', create_kwargs['compose_content'])
        driver.destroy_shadow.assert_called_once_with(
            ANY, compose_path=green_file, project='bgtest_green')
        # Canonical container recreated via the ALREADY-routed driver calls.
        driver.destroy.assert_called_once()
        driver.start.assert_called_once()
        # Traffic flipped to green first, then back to the canonical ports.
        self.assertEqual(flip_calls, [(34001, 34002), (8169, 8172)])
        self.assertEqual(self.instance.state, 'running')

    def test_zero_downtime_green_boot_failure_keeps_traffic_on_blue(self):
        driver = MagicMock()
        fake_ssh, calls, writes = _fake_ssh(canon_compose=_CANON_COMPOSE)
        flip_calls = []

        def wait_healthy(ssh, timeout=180, container=None):
            if container == 'odoo_bgtest_green':
                return (False, 'green never answered')
            self.fail("canonical health check must not run if green never booted")

        with self.assertRaises(UserError) as cm:
            self._run(fake_ssh, driver, wait_healthy, flip_calls)
        self.assertIn('zero downtime', str(cm.exception))
        # Never touched nginx or the canonical container — blue kept serving.
        self.assertEqual(flip_calls, [])
        driver.destroy.assert_not_called()
        driver.start.assert_not_called()
        # Green was torn down after the failed boot check.
        driver.destroy_shadow.assert_called_once()

    def test_zero_downtime_promotion_failure_flips_traffic_back(self):
        driver = MagicMock()
        driver.destroy.side_effect = RuntimeError('docker daemon hiccup')
        fake_ssh, calls, writes = _fake_ssh(canon_compose=_CANON_COMPOSE)
        flip_calls = []

        def wait_healthy(ssh, timeout=180, container=None):
            return (True, '')  # green boots fine; promotion itself fails

        with self.assertRaises(UserError) as cm:
            self._run(fake_ssh, driver, wait_healthy, flip_calls)
        self.assertIn('Failed to recreate container', str(cm.exception))
        # Flipped TO green, then immediately back to blue on the failure —
        # customers never lose service, they just don't get the new code.
        self.assertEqual(flip_calls, [(34001, 34002), (8169, 8172)])
        driver.destroy_shadow.assert_called_once()

    def test_zero_downtime_canonical_reboot_failure_keeps_traffic_on_green(self):
        """The one branch that deliberately does NOT flip nginx back:
        green already proved this exact code boots, so if the canonical
        container fails to come back up afterward, traffic stays on the
        (healthy, running new code) green standby rather than flipping to
        a container that just failed its own boot check."""
        driver = MagicMock()
        fake_ssh, calls, writes = _fake_ssh(canon_compose=_CANON_COMPOSE)
        flip_calls = []

        def wait_healthy(ssh, timeout=180, container=None):
            if container == 'odoo_bgtest_green':
                return (True, '')
            return (False, 'canonical did not come back up')

        with self.assertRaises(UserError) as cm:
            self._run(fake_ssh, driver, wait_healthy, flip_calls)
        self.assertIn('served from a standby container', str(cm.exception))
        driver.destroy.assert_called_once()
        driver.start.assert_called_once()
        # Only the initial flip to green — deliberately never flipped back.
        self.assertEqual(flip_calls, [(34001, 34002)])
        # Green is NOT torn down in this branch (it's still serving traffic).
        driver.destroy_shadow.assert_not_called()

    # ---------------------------------------------------------------
    # Fallback (non-zero-downtime) path: remote proxy or immutable image
    # ---------------------------------------------------------------
    def test_fallback_path_used_when_deploy_image_set(self):
        self.instance.deploy_image = 'registry.example.com/tenant-bgtest:abc123'
        driver = MagicMock()
        fake_ssh, calls, writes = _fake_ssh(canon_compose=_CANON_COMPOSE)
        flip_calls = []

        def wait_healthy(ssh, timeout=180, container=None):
            return (True, '')

        self._run(fake_ssh, driver, wait_healthy, flip_calls)

        # No green sidecar at all — straight in-place recreate.
        self.assertEqual(writes, {})
        driver.create_shadow.assert_not_called()
        driver.destroy_shadow.assert_not_called()
        driver.destroy.assert_called_once()
        driver.start.assert_called_once()
        self.assertEqual(flip_calls, [], "fallback path never touches nginx routing")

    def test_fallback_path_recreate_failure_raises(self):
        self.instance.deploy_image = 'registry.example.com/tenant-bgtest:abc123'
        driver = MagicMock()
        driver.destroy.side_effect = RuntimeError('boom')
        fake_ssh, calls, writes = _fake_ssh(canon_compose=_CANON_COMPOSE)

        with self.assertRaises(UserError) as cm:
            self._run(fake_ssh, driver, lambda *a, **kw: (True, ''), [])
        self.assertIn('Failed to restart container', str(cm.exception))

    def test_fallback_path_boot_failure_rolls_back_and_raises(self):
        self.instance.deploy_image = 'registry.example.com/tenant-bgtest:abc123'
        driver = MagicMock()
        fake_ssh, calls, writes = _fake_ssh(canon_compose=_CANON_COMPOSE)
        healthy_calls = {'n': 0}

        def wait_healthy(ssh, timeout=180, container=None):
            healthy_calls['n'] += 1
            return (False, 'never came up')  # both the initial and post-rollback checks fail

        with self.assertRaises(UserError) as cm:
            self._run(fake_ssh, driver, wait_healthy, [])
        self.assertIn('rolled back', str(cm.exception))
        # Recreate attempted twice: once for the new code, once for rollback.
        self.assertEqual(driver.destroy.call_count, 2)
        self.assertEqual(driver.start.call_count, 2)
        self.assertEqual(healthy_calls['n'], 2)
