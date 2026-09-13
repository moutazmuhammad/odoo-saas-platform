import socket
import contextlib
import itertools

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
