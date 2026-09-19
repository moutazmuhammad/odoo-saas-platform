"""Regression guard for the billing/pricing architecture redesign, Part
18's "internal financial data is never exposed to customers" acceptance
criterion — locked in for real over HTTP, not just by code inspection.

The redesign added several new cost/profit/margin fields (cost_price,
profit, margin_pct, is_profitable, cost_tracked, minimum_profitable_price
on saas.compute.tier/saas.addon/saas.support.plan/saas.plan; two new
worker_cost_floor/storage_cost_floor breakdown keys on the pricing
engine's compute()). None of them should ever reach a customer-facing
JSON response — every serializer in api.py already builds an explicit
allowlist dict rather than a raw .read(), so this is a "stays true"
regression test, not new enforcement.
"""
import json

from odoo.tests.common import HttpCase, tagged

from .test_portal_security import _PortalTestBase

_FORBIDDEN_SUBSTRINGS = (
    'cost_price', 'cost_tracked', 'minimum_profitable_price',
    'is_profitable', 'worker_floor', 'storage_floor', 'cost_floor',
    'worker_cost_floor', 'storage_cost_floor', 'monthly_cost',
    'monthly_margin', '"profit"', '"margin_pct"',
)


def _assert_no_leak(test, payload):
    blob = json.dumps(payload)
    for needle in _FORBIDDEN_SUBSTRINGS:
        test.assertNotIn(
            needle, blob,
            "internal cost/profit/margin key %r leaked into a "
            "customer-facing response: %s" % (needle, blob[:2000]))


@tagged('post_install', '-at_install')
class TestPricingQuoteHasNoFinancialLeak(HttpCase):
    def _json_call(self, route, params=None):
        resp = self.url_open(
            route,
            data=json.dumps({'jsonrpc': '2.0', 'method': 'call',
                             'params': params or {}}),
            headers={'Content-Type': 'application/json'})
        return resp.json().get('result')

    def test_hosting_calculate_has_no_cost_or_floor_data(self):
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('saas_master.hosting_worker_floor', '4.0')
        icp.set_param('saas_master.hosting_storage_floor', '0.1')
        result = self._json_call(
            '/saas/api/v1/hosting/calculate',
            {'workers': 4, 'storage': 20, 'billing': 'monthly'})
        self.assertTrue(result['ok'])
        _assert_no_leak(self, result)

    def test_services_calculate_has_no_cost_or_floor_data(self):
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('saas_master.worker_floor', '5.0')
        icp.set_param('saas_master.storage_floor', '0.2')
        result = self._json_call(
            '/saas/api/v1/services/calculate',
            {'workers': 4, 'storage': 20, 'billing': 'monthly'})
        self.assertTrue(result['ok'])
        _assert_no_leak(self, result)


@tagged('post_install', '-at_install')
class TestInstanceDetailHasNoFinancialLeak(_PortalTestBase):
    def test_instance_detail_and_compute_tiers_have_no_financial_leak(self):
        region = self.env['saas.region'].sudo().create(
            {'name': 'NoLeak Region', 'code': 'noleak-region'})
        server = self.env['saas.server'].sudo().create({
            'name': 'noleak-k8s', 'compute_driver': 'kubernetes',
            'region_id': region.id, 'cost_per_cpu_month': 5.0,
            'cost_per_gb_ram_month': 2.0, 'cost_per_gb_storage_month': 0.1})
        self.instance.sudo().docker_server_id = server.id
        self.env['saas.compute.tier'].sudo().create({
            'name': 'NoLeak HA', 'code': 'noleak-ha', 'replicas': 2,
            'monthly_price': 15.0, 'cost_price': 6.0,
        })
        self.authenticate('portalowner@example.com', 'ownerpass123')
        result = self._json_call(
            '/saas/api/v1/instances/%d' % self.instance.id)
        self.assertTrue(result['ok'])
        _assert_no_leak(self, result)
