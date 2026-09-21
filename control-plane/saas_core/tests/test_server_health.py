import itertools
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

# Distinct loopback IPs so each saas.server gets a unique public IP (the model
# forbids two records sharing one). 127.0.0.0/8 is all loopback on Linux.
_ips = itertools.count(2)


def _next_ip():
    return '127.0.0.%d' % next(_ips)


@tagged('post_install', '-at_install')
class TestServerHealth(TransactionCase):
    """Reachability gating: a customer must never be allocated to a
    Kubernetes cluster we can't reach (the failure that strands a deploy
    in 'pending provision').

    Kubernetes is the only compute backend now, so ``_probe_reachable()``
    always dispatches to ``_probe_kubernetes_reachable()`` (a real API
    call against the cluster) — there is no TCP/SSH probe branch left to
    exercise with plain sockets, so reachability here is mocked at
    ``_probe_kubernetes_reachable`` rather than opening/closing real
    ports.
    """

    def _server(self, name, host=None, **kw):
        vals = {
            'name': name,
            'ip_v4': host or _next_ip(),
        }
        vals.update(kw)
        return self.env['saas.server'].sudo().create(vals)

    def _isolate_docker_hosts(self):
        """Drop any pre-existing servers' health so allocation only sees
        ours (rolled back with the test transaction)."""
        self.env['saas.server'].sudo().search([]).write(
            {'health_state': 'unreachable'})

    def _reachable_map(self, outcomes):
        """Patch _probe_kubernetes_reachable for MULTIPLE servers at once.

        ``type(server)`` is the shared model class (one per registry, not
        per record), so patching it per-server one at a time with
        plain ``return_value``s clobbers earlier patches instead of
        layering — this dispatches on ``self.id`` instead so each
        server in ``outcomes`` gets its own answer from a single patch.
        """
        by_id = {server.id: (ok, err) for server, ok, err in outcomes}

        def _fake(rec_self, timeout=None):
            return by_id[rec_self.id]

        any_server = outcomes[0][0]
        return patch.object(
            type(any_server), '_probe_kubernetes_reachable', _fake)

    def test_probe_reachable_dispatches_to_kubernetes_check(self):
        server = self._server('k8s')
        with patch.object(
                type(server), '_probe_kubernetes_reachable',
                return_value=(True, '')) as probe:
            ok, err = server._probe_reachable()
        self.assertTrue(ok, err)
        probe.assert_called_once()

    def test_probe_kubernetes_reachable_uses_core_api(self):
        server = self._server('k8s2')
        fake_driver = MagicMock()
        with patch(
                'odoo.addons.saas_core.drivers.kubernetes_driver.KubernetesDriver',
                return_value=fake_driver):
            ok, err = server._probe_kubernetes_reachable()
        self.assertTrue(ok, err)
        fake_driver._core_api.return_value.list_namespace.assert_called_once()

    def test_probe_kubernetes_reachable_false_on_api_error(self):
        server = self._server('k8s3')
        fake_driver = MagicMock()
        fake_driver._core_api.return_value.list_namespace.side_effect = (
            RuntimeError('no kubeconfig'))
        with patch(
                'odoo.addons.saas_core.drivers.kubernetes_driver.KubernetesDriver',
                return_value=fake_driver):
            ok, err = server._probe_kubernetes_reachable()
        self.assertFalse(ok)
        self.assertIn('no kubeconfig', err)

    def test_update_health_persists_and_clears_error(self):
        s = self._server('s')
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
        good = self._server('good')
        bad = self._server('bad')
        with self._reachable_map([(good, True, ''), (bad, False, 'down')]):
            chosen = Server._allocate_docker_server(plan=None)
        self.assertEqual(
            chosen, good,
            'allocation must pick the reachable cluster, not the dead one')
        self.assertEqual(bad.health_state, 'unreachable')

    def test_allocator_returns_none_when_all_unreachable(self):
        self._isolate_docker_hosts()
        Server = self.env['saas.server'].sudo()
        d1 = self._server('d1')
        d2 = self._server('d2')
        with self._reachable_map([(d1, False, 'down'), (d2, False, 'down')]):
            self.assertFalse(
                Server._allocate_docker_server(plan=None),
                'no reachable cluster => no allocation (fails fast instead '
                'of stranding the customer in pending_provision)')

    def test_excludes_known_unreachable_without_probing(self):
        """A host already flagged unreachable is filtered out by the domain
        before any probe — even if it were momentarily back up."""
        self._isolate_docker_hosts()
        Server = self.env['saas.server'].sudo()
        flagged = self._server('flagged')
        flagged.health_state = 'unreachable'
        with self._reachable_map([(flagged, True, '')]):
            self.assertFalse(
                Server._allocate_docker_server(plan=None),
                'a host flagged unreachable is excluded from candidates')
