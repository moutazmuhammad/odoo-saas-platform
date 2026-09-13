from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestInstanceMetrics(TransactionCase):
    """Phase 4 (customer metrics): time-series recording, 14-day retention,
    downsampled series for the per-tenant dashboard."""

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
        self.server = self.env['saas.server'].sudo().create({'name': 'mx-srv'})
        self.inst = self.env['saas.instance'].sudo().create({
            'subdomain': 'mxtest', 'domain_id': self.domain.id, 'partner_id': self.partner.id,
            'saas_product_id': self.product.id, 'plan_id': self.plan.id,
            'docker_server_id': self.server.id, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': False, 'state': 'running'})

    def _add(self, minutes_ago, cpu, ram, storage_mb=100.0):
        self.env['saas.instance.metric'].sudo().create({
            'instance_id': self.inst.id,
            'ts': fields.Datetime.now() - timedelta(minutes=minutes_ago),
            'cpu_pct': cpu, 'ram_pct': ram, 'storage_mb': storage_mb, 'storage_pct': 1.0})

    def test_series_shape_and_downsampling(self):
        for i in range(60):                 # 60 samples over the last ~5h
            self._add(i * 5, cpu=10 + i, ram=20 + i)
        series = self.inst._get_metric_series(hours=24, max_points=24)
        self.assertEqual(series['instance'], 'mxtest')
        self.assertEqual(series['retention_days'], 14)
        self.assertEqual(series['plan']['cpu_limit'], 2.0)
        self.assertTrue(series['samples'])
        # downsampled to <= max_points buckets, each carrying the metrics
        self.assertLessEqual(len(series['samples']), 24)
        s0 = series['samples'][0]
        self.assertIn('t', s0); self.assertIn('cpu', s0); self.assertIn('ram', s0)
        self.assertIn('storage_mb', s0)
        # ascending time order
        ts = [s['t'] for s in series['samples']]
        self.assertEqual(ts, sorted(ts))

    def test_series_empty_when_no_data(self):
        series = self.inst._get_metric_series(hours=24)
        self.assertEqual(series['samples'], [])
        self.assertEqual(series['plan']['storage_limit_gb'], 10)

    def test_retention_prunes_older_than_14_days(self):
        self._add(60, cpu=5, ram=5)                          # recent
        self._add(15 * 24 * 60, cpu=9, ram=9)                # 15 days old
        Metric = self.env['saas.instance.metric'].sudo()
        self.assertEqual(Metric.search_count([('instance_id', '=', self.inst.id)]), 2)
        self.env['saas.instance'].sudo()._cron_prune_metrics()
        remaining = Metric.search([('instance_id', '=', self.inst.id)])
        self.assertEqual(len(remaining), 1)
        self.assertEqual(round(remaining.cpu_pct), 5)        # only the recent one survives

    def test_range_capped_to_retention(self):
        # requesting > 14d worth of hours is clamped to the retention window
        series = self.inst._get_metric_series(hours=999999)
        self.assertEqual(series['hours'], 14 * 24)
