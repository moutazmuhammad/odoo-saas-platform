from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.saas_core.drivers.kubernetes_driver import (
    KubernetesDriver, PrometheusUnavailable,
)
from odoo.addons.saas_core.models import saas_instance as saas_instance_mod

GB = 1024 ** 3


@tagged('post_install', '-at_install')
class TestInstanceMetrics(TransactionCase):
    """Usage metrics on Kubernetes: CPU/RAM from the region's Prometheus,
    storage measured in the pod, live reading + history for the customer
    dashboard. Mocked at the ComputeDriver boundary."""

    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or self.env['saas.product'].sudo().create(
            {'name': 'Mx Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'Mx Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 10,
            'cpu_limit': 2.0, 'ram_limit': '2g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.partner = self.env['res.partner'].sudo().create({'name': 'Mx Cust'})
        self.domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create({'name': 'mx.example.com'})
        self.server = self.env['saas.server'].sudo().create(
            {'name': 'mx-srv', 'compute_driver': 'kubernetes'})
        self.inst = self._inst('mxtest')
        self.cr_name = KubernetesDriver._cr_name(self.inst._compute_handle())
        self.driver = MagicMock()
        self.driver._cr_name = KubernetesDriver._cr_name
        self.driver.usage_by_tenant.return_value = {
            self.cr_name: {'cpu_cores': 0.5, 'mem_bytes': 1 * GB}}
        self.driver.measure_storage.return_value = {
            'filestore_bytes': 2 * GB, 'db_bytes': 1 * GB}
        p = patch.object(type(self.inst), '_compute_driver',
                         lambda rec, connection=None: self.driver)
        p.start()
        self.addCleanup(p.stop)
        saas_instance_mod._LIVE_METRICS_CACHE.clear()

    def _inst(self, sub, **kw):
        vals = {
            'subdomain': sub, 'domain_id': self.domain.id, 'partner_id': self.partner.id,
            'saas_product_id': self.product.id, 'plan_id': self.plan.id,
            'docker_server_id': self.server.id, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': False, 'state': 'running'}
        vals.update(kw)
        return self.env['saas.instance'].sudo().create(vals)

    # ---------------- stored usage refresh ----------------
    def test_refresh_writes_plan_relative_cpu_ram_and_storage(self):
        self.inst.action_refresh_usage()
        self.assertEqual(self.inst.cpu_usage_pct, 25.0)     # 0.5 of 2 cores
        self.assertEqual(self.inst.ram_usage_pct, 50.0)     # 1 of 2 GB
        self.assertEqual(self.inst.total_storage_bytes, 3 * GB)
        self.assertEqual(self.inst.storage_usage_pct, 30.0)  # 3 of 10 GB
        self.assertEqual(self.inst.db_size, '1.00 GB')
        self.assertEqual(self.inst.disk_usage, '2.00 GB')
        self.assertTrue(self.inst.usage_last_updated)

    def test_refresh_batches_prometheus_per_server(self):
        other = self._inst('mxtest2')
        (self.inst | other).action_refresh_usage()
        self.driver.usage_by_tenant.assert_called_once_with()
        self.assertEqual(self.driver.measure_storage.call_count, 2)

    def test_refresh_without_prometheus_still_measures_storage(self):
        self.driver.usage_by_tenant.side_effect = PrometheusUnavailable('none')
        self.inst.action_refresh_usage()
        self.assertEqual(self.inst.total_storage_bytes, 3 * GB)
        self.assertEqual(self.inst.cpu_usage_pct, 0.0)

    def test_refresh_skips_non_running(self):
        stopped = self._inst('mxstopped', state='stopped')
        stopped.action_refresh_usage()
        self.driver.measure_storage.assert_not_called()

    def test_safe_refresh_swallows_storage_failure(self):
        self.driver.measure_storage.side_effect = RuntimeError('exec failed')
        self.inst._safe_refresh_usage()
        self.assertEqual(self.inst.cpu_usage_pct, 25.0)
        self.assertFalse(self.inst.total_storage_bytes)

    def test_refresh_stops_storage_after_repeated_failures(self):
        insts = self.inst | self._inst('mxf1') | self._inst('mxf2') | self._inst('mxf3')
        self.driver.measure_storage.side_effect = RuntimeError('cluster down')
        insts.action_refresh_usage()
        self.assertEqual(self.driver.measure_storage.call_count, 3)
        self.assertTrue(all(insts.mapped('usage_last_updated')))

    def test_strict_refresh_raises_on_storage_failure(self):
        self.driver.measure_storage.side_effect = RuntimeError('exec failed')
        with self.assertRaises(RuntimeError):
            self.inst._strict_refresh_usage()

    def test_strict_refresh_refuses_when_not_running(self):
        stopped = self._inst('mxstopped2', state='stopped')
        with self.assertRaises(UserError):
            stopped._strict_refresh_usage()

    def test_cron_refreshes_least_recently_measured_first(self):
        other = self._inst('mxtest3')
        self.inst.usage_last_updated = '2026-01-01 00:00:00'
        other.usage_last_updated = '2025-01-01 00:00:00'
        seen = []
        with patch.object(type(self.inst), '_refresh_usage',
                          lambda recs, strict=False: seen.extend(recs.ids)):
            self.env['saas.instance']._cron_refresh_usage()
        self.assertLess(seen.index(other.id), seen.index(self.inst.id))

    # ---------------- live reading ----------------
    def test_live_metrics_cached_per_instance(self):
        first = self.inst._get_live_metrics()
        second = self.inst._get_live_metrics()
        self.assertEqual(first, second)
        self.assertEqual((first['cpu'], first['ram'], first['available']),
                         (25.0, 50.0, True))
        self.driver.usage_by_tenant.assert_called_once()
        self.driver.measure_storage.assert_not_called()

    def test_live_metrics_unavailable_without_prometheus(self):
        self.driver.usage_by_tenant.side_effect = PrometheusUnavailable('none')
        live = self.inst._get_live_metrics()
        self.assertFalse(live['available'])
        self.assertEqual(live['cpu'], 0.0)

    # ---------------- history ----------------
    def test_series_from_prometheus_with_flat_storage_fallback(self):
        self.inst.total_storage_bytes = 1 * GB
        self.driver.usage_history.return_value = {
            'cpu_cores': [(1790000000.0, 1.0), (1790000060.0, 0.2)],
            'mem_bytes': [(1790000000.0, 0.5 * GB), (1790000060.0, 1.0 * GB)],
            'volume_bytes': [],
        }
        data = self.inst._get_metric_series(hours=24)
        self.assertTrue(data['available'])
        self.assertEqual(data['retention_days'], 14)
        s = data['samples']
        self.assertEqual(len(s), 2)
        self.assertEqual((s[0]['cpu'], s[0]['ram']), (50.0, 25.0))
        self.assertEqual((s[1]['cpu'], s[1]['ram']), (10.0, 50.0))
        self.assertTrue(s[0]['t'].endswith('Z'))
        self.assertEqual(s[0]['storage_mb'], 1024.0)
        self.assertEqual(s[0]['storage_pct'], 10.0)
        _handle, start, end, step = self.driver.usage_history.call_args[0]
        self.assertAlmostEqual(end - start, 24 * 3600, delta=1)
        self.assertEqual(step, 360)   # 24h over 240 points

    def test_series_uses_volume_stats_when_reported(self):
        self.driver.usage_history.return_value = {
            'cpu_cores': [(1790000000.0, 1.0)],
            'mem_bytes': [(1790000000.0, 1.0 * GB)],
            'volume_bytes': [(1790000000.0, 5.0 * GB)],
        }
        s = self.inst._get_metric_series(hours=1)['samples']
        self.assertEqual(s[0]['storage_pct'], 50.0)

    def test_series_empty_when_prometheus_unavailable(self):
        self.driver.usage_history.side_effect = PrometheusUnavailable('none')
        data = self.inst._get_metric_series(hours=6)
        self.assertFalse(data['available'])
        self.assertEqual(data['samples'], [])

    def test_range_capped_to_retention(self):
        self.driver.usage_history.return_value = {}
        data = self.inst._get_metric_series(hours=10000)
        self.assertEqual(data['hours'], 14 * 24)
