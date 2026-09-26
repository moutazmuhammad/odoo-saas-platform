from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestKubernetesTeardown(TransactionCase):
    """Cancel/delete on Kubernetes: one OdooInstance CR delete (the
    operator's finalizer removes the whole tenant namespace), a retry flag
    when the cluster can't be reached, and a reactivation guard while the
    old CR still exists. Mocked at the ComputeDriver boundary."""

    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or self.env['saas.product'].sudo().create(
            {'name': 'Td Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'Td Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 10,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.partner = self.env['res.partner'].sudo().create({'name': 'Td Cust'})
        self.domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create({'name': 'td.example.com'})
        self.server = self.env['saas.server'].sudo().create(
            {'name': 'td-srv', 'compute_driver': 'kubernetes'})
        self.inst = self.env['saas.instance'].sudo().create({
            'subdomain': 'tdtest', 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'docker_server_id': self.server.id,
            'billing_period': 'monthly', 'state': 'running'})
        self.driver = MagicMock()
        self.driver.exists.return_value = False
        for target, attr, value in (
                (type(self.inst), '_compute_driver',
                 lambda rec, connection=None: self.driver),
                (self.env.cr, 'commit', lambda: None),
                (self.env.cr, 'rollback', lambda: None)):
            p = patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def _delete(self):
        # The final snapshot needs backup storage; not what's under test.
        Backup = type(self.env['saas.instance.backup'])
        with patch.object(Backup, '_create_full_instance_backup_sync',
                          side_effect=UserError('no backup storage')):
            self.inst._do_delete_instance()

    # ---------------- cancel/delete ----------------
    def test_delete_removes_the_cr_and_cancels(self):
        self._delete()
        self.driver.destroy.assert_called_once()
        self.assertIn(self.inst.state, ('cancelled', 'cancelled_by_client'))
        self.assertFalse(self.inst.infra_cleanup_pending)

    def test_delete_with_unreachable_cluster_still_cancels_and_flags(self):
        self.driver.destroy.side_effect = RuntimeError('cluster unreachable')
        self._delete()
        self.assertIn(self.inst.state, ('cancelled', 'cancelled_by_client'))
        self.assertTrue(self.inst.infra_cleanup_pending)

    # ---------------- retries ----------------
    def test_cron_retries_pending_cleanup(self):
        self.inst.write({'state': 'cancelled', 'infra_cleanup_pending': True})
        n = self.env['saas.instance']._cron_retry_infra_cleanup()
        self.assertEqual(n, 1)
        self.driver.destroy.assert_called_once()
        self.assertFalse(self.inst.infra_cleanup_pending)

    def test_cron_keeps_flag_when_still_unreachable(self):
        self.inst.write({'state': 'cancelled', 'infra_cleanup_pending': True})
        self.driver.destroy.side_effect = RuntimeError('cluster unreachable')
        self.env['saas.instance']._cron_retry_infra_cleanup()
        self.assertTrue(self.inst.infra_cleanup_pending)

    def test_reactivation_guard_resends_delete(self):
        self.inst.write({'state': 'cancelled', 'infra_cleanup_pending': True})
        self.inst._retry_pending_cleanup()
        self.driver.destroy.assert_called_once()
        self.assertFalse(self.inst.infra_cleanup_pending)

    def test_reactivation_refused_while_old_cr_still_exists(self):
        self.inst.state = 'cancelled'
        self.driver.exists.return_value = True
        with self.assertRaises(UserError):
            self.inst._retry_pending_cleanup()

    def test_reactivation_refused_when_cluster_unreachable(self):
        self.inst.write({'state': 'cancelled', 'infra_cleanup_pending': True})
        self.driver.destroy.side_effect = RuntimeError('cluster unreachable')
        with self.assertRaises(UserError):
            self.inst._retry_pending_cleanup()
        self.assertTrue(self.inst.infra_cleanup_pending)


@tagged('post_install', '-at_install')
class TestLogStream(TransactionCase):
    """Live logs relay a follow-mode pod log stream as server-sent events."""

    def _fake_resp(self, chunks):
        resp = MagicMock()
        resp.stream.return_value = iter(chunks)
        return resp

    def _events(self, response):
        return b''.join(response.response).decode()

    def test_relays_lines_as_sse_and_strips_ansi(self):
        from odoo.addons.saas_core.controllers.container_logs import log_stream_response
        resp = self._fake_resp([b'first li', b'ne\n\x1b[31mred\x1b[0m\npartial'])
        out = self._events(log_stream_response(resp, 'acme'))
        self.assertIn('data: "first line"', out)
        self.assertIn('data: "red"', out)
        self.assertIn('data: "partial"', out)
        self.assertIn('event: done', out)
        resp.release_conn.assert_called_once()

    def test_stream_error_is_generic(self):
        from odoo.addons.saas_core.controllers.container_logs import log_stream_response
        resp = MagicMock()
        resp.stream.side_effect = OSError('10.0.0.5:6443 connection reset')
        out = self._events(log_stream_response(resp, 'acme'))
        self.assertIn('event: error', out)
        self.assertNotIn('10.0.0.5', out)

    def test_driver_opens_follow_stream_on_web_pod(self):
        from .test_kubernetes_driver import _make_driver, _handle, _fake_pod
        driver, _server = _make_driver()
        driver._first_pod = MagicMock(return_value=_fake_pod(name='odoo-abc-123'))
        core = MagicMock()
        driver._core_api = MagicMock(return_value=core)
        driver.open_log_stream(_handle(), tail=50)
        args, kwargs = core.read_namespaced_pod_log.call_args
        self.assertEqual(args, ('odoo-abc-123', 'odoo-tenant-odoo-acme'))
        self.assertTrue(kwargs['follow'])
        self.assertEqual(kwargs['tail_lines'], 50)
        self.assertFalse(kwargs['_preload_content'])

    def test_driver_no_pod_returns_none(self):
        from .test_kubernetes_driver import _make_driver, _handle
        driver, _server = _make_driver()
        driver._first_pod = MagicMock(return_value=None)
        self.assertIsNone(driver.open_log_stream(_handle()))
