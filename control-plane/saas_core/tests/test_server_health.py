import socket
import contextlib
import itertools
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

# Distinct loopback IPs so each saas.server gets a unique public IP (the model
# forbids two records sharing one). 127.0.0.0/8 is all loopback on Linux.
_ips = itertools.count(2)


def _next_ip():
    return '127.0.0.%d' % next(_ips)


@contextlib.contextmanager
def _listening(host):
    """Yield a port on *host* that is actually accepting TCP connections."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((host, 0))
    s.listen(1)
    try:
        yield s.getsockname()[1]
    finally:
        s.close()


def _closed_port(host):
    """A port on *host* that is NOT listening (bind to grab one, then free it)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((host, 0))
    port = s.getsockname()[1]
    s.close()
    return port


@tagged('post_install', '-at_install')
class TestServerHealth(TransactionCase):
    """Reachability gating: a customer must never be allocated to a Docker
    host we can't connect to (the failure that strands a deploy in
    'pending provision')."""

    def _server(self, name, host, port, **kw):
        vals = {
            'name': name,
            'ip_v4': host,
            'ssh_connect_using': 'public_ip',
            'ssh_port': port,
            'is_docker_host': True,
        }
        vals.update(kw)
        return self.env['saas.server'].sudo().create(vals)

    def _isolate_docker_hosts(self):
        """Drop any pre-existing docker hosts so allocation only sees ours
        (rolled back with the test transaction)."""
        self.env['saas.server'].sudo().search(
            [('is_docker_host', '=', True)]).write({'is_docker_host': False})

    def test_probe_reachable_open_vs_closed(self):
        host = _next_ip()
        with _listening(host) as port:
            up = self._server('up', host, port)
            ok, err = up._probe_reachable()
            self.assertTrue(ok, 'open port should probe reachable (%s)' % err)

        host2 = _next_ip()
        down = self._server('down', host2, _closed_port(host2))
        ok, err = down._probe_reachable()
        self.assertFalse(ok, 'closed port should probe unreachable')
        self.assertTrue(err, 'an unreachable probe should report an error')

    def test_update_health_persists_and_clears_error(self):
        host = _next_ip()
        s = self._server('s', host, _closed_port(host))
        s._update_health(False, 'boom')
        self.assertEqual(s.health_state, 'unreachable')
        self.assertEqual(s.last_health_error, 'boom')
        self.assertTrue(s.last_health_check)
        s._update_health(True, '')
        self.assertEqual(s.health_state, 'ok')
        self.assertFalse(s.last_health_error)

    def test_allocator_skips_unreachable_and_picks_reachable(self):
        self._isolate_docker_hosts()
        Server = self.env['saas.server'].sudo()
        gh = _next_ip()
        bh = _next_ip()
        with _listening(gh) as port:
            good = self._server('good', gh, port)
            bad = self._server('bad', bh, _closed_port(bh))
            chosen = Server._allocate_docker_server(plan=None)
            self.assertEqual(
                chosen, good,
                'allocation must pick the reachable host, not the dead one')
            self.assertEqual(bad.health_state, 'unreachable')

    def test_allocator_returns_none_when_all_unreachable(self):
        self._isolate_docker_hosts()
        Server = self.env['saas.server'].sudo()
        h1, h2 = _next_ip(), _next_ip()
        self._server('d1', h1, _closed_port(h1))
        self._server('d2', h2, _closed_port(h2))
        self.assertFalse(
            Server._allocate_docker_server(plan=None),
            'no reachable host => no allocation (order fails fast instead of '
            'stranding the customer in pending_provision)')

    def test_excludes_known_unreachable_without_probing(self):
        """A host already flagged unreachable is filtered out by the domain
        before any probe — even if it were momentarily back up."""
        self._isolate_docker_hosts()
        Server = self.env['saas.server'].sudo()
        host = _next_ip()
        with _listening(host) as port:
            flagged = self._server('flagged', host, port)
            flagged.health_state = 'unreachable'
            self.assertFalse(
                Server._allocate_docker_server(plan=None),
                'a host flagged unreachable is excluded from candidates')

    # -------- Kubernetes servers: a TCP/SSH probe would always fail -------
    def test_probe_reachable_dispatches_to_kubernetes_check(self):
        """A compute_driver='kubernetes' server has no ip_v4/SSH — the old
        TCP-only probe would report it permanently unreachable. It must go
        through the Kubernetes-specific check instead."""
        host = _next_ip()
        server = self._server(
            'k8s', host, _closed_port(host), compute_driver='kubernetes')
        with patch.object(
                type(server), '_probe_kubernetes_reachable',
                return_value=(True, '')) as probe:
            ok, err = server._probe_reachable()
        self.assertTrue(ok, err)
        probe.assert_called_once()

    def test_probe_kubernetes_reachable_uses_core_api(self):
        host = _next_ip()
        server = self._server(
            'k8s2', host, _closed_port(host), compute_driver='kubernetes')
        fake_driver = MagicMock()
        with patch(
                'odoo.addons.saas_core.drivers.kubernetes_driver.KubernetesDriver',
                return_value=fake_driver):
            ok, err = server._probe_kubernetes_reachable()
        self.assertTrue(ok, err)
        fake_driver._core_api.return_value.list_namespace.assert_called_once()

    def test_probe_kubernetes_reachable_false_on_api_error(self):
        host = _next_ip()
        server = self._server(
            'k8s3', host, _closed_port(host), compute_driver='kubernetes')
        fake_driver = MagicMock()
        fake_driver._core_api.return_value.list_namespace.side_effect = (
            RuntimeError('no kubeconfig'))
        with patch(
                'odoo.addons.saas_core.drivers.kubernetes_driver.KubernetesDriver',
                return_value=fake_driver):
            ok, err = server._probe_kubernetes_reachable()
        self.assertFalse(ok)
        self.assertIn('no kubeconfig', err)

    # -------- Allocation honors compute_driver -----------------------------
    def test_allocator_filters_by_compute_driver(self):
        self._isolate_docker_hosts()
        Server = self.env['saas.server'].sudo()
        dh, kh = _next_ip(), _next_ip()
        with _listening(dh) as dport, _listening(kh) as kport:
            docker_srv = self._server('docker', dh, dport)
            k8s_srv = self._server(
                'k8s', kh, kport, compute_driver='kubernetes')
            with patch.object(
                    type(k8s_srv), '_probe_kubernetes_reachable',
                    return_value=(True, '')):
                chosen = Server._allocate_docker_server(
                    plan=None, compute_driver='kubernetes')
            self.assertEqual(chosen, k8s_srv)
            self.assertNotEqual(chosen, docker_srv)

    def test_allocator_no_filter_when_compute_driver_not_passed(self):
        """Backward compatibility: existing callers that don't pass
        compute_driver keep today's driver-blind behavior."""
        self._isolate_docker_hosts()
        Server = self.env['saas.server'].sudo()
        h = _next_ip()
        with _listening(h) as port:
            srv = self._server('plain', h, port)
            self.assertEqual(Server._allocate_docker_server(plan=None), srv)
