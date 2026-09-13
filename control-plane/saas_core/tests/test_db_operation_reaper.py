from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestDbOperationReaper(TransactionCase):
    """PROV-001: a stuck DB operation whose background worker died is failed
    fast via heartbeat staleness, instead of waiting the 30-min create-time
    net. A live op (fresh heartbeat) is never touched."""

    def setUp(self):
        super().setUp()
        product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or \
            self.env['saas.product'].sudo().create(
                {'name': 'OPS Hosting', 'is_hosting': True, 'is_published': True})
        plan = self.env['saas.plan'].sudo().create({
            'name': 'OPS Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [product.id])]})
        domain = self.env['saas.based.domain'].sudo().search([], limit=1) or \
            self.env['saas.based.domain'].sudo().create({'name': 'ops.example.com'})
        partner = self.env['res.partner'].sudo().create({'name': 'OPS Cust'})
        self.instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'opsinst', 'domain_id': domain.id, 'partner_id': partner.id,
            'saas_product_id': product.id, 'plan_id': plan.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running', 'is_hosting': True})
        self.Op = self.env['saas.instance.db.operation'].sudo()

    def _op(self):
        return self.Op.create({
            'instance_id': self.instance.id, 'db_name': 'opsdb',
            'operation': 'create'})

    def test_stale_heartbeat_is_reaped_fast(self):
        op = self._op()
        # Worker died: heartbeat went stale well past _HEARTBEAT_STALE_MIN
        # but the op is far younger than the 30-min create-time net.
        op.last_heartbeat = fields.Datetime.now() - timedelta(minutes=10)
        # The reaper commits per reaped op (correct in production); the test
        # cursor forbids commit/rollback, so neutralise them for this call.
        with patch.object(self.env.cr, 'commit'), \
                patch.object(self.env.cr, 'rollback'):
            self.Op._cron_reap_stuck_db_operations()
        self.assertEqual(op.state, 'failed',
                         "a stale-heartbeat op must be failed fast")
        self.assertTrue(op.error_message)

    def test_fresh_heartbeat_is_left_running(self):
        op = self._op()
        # A live worker beat just now — however long the op runs, it must
        # never be reaped while it keeps beating.
        op.last_heartbeat = fields.Datetime.now()
        self.Op._cron_reap_stuck_db_operations()
        self.assertEqual(op.state, 'running',
                         "a freshly-beating op must NOT be reaped")

    def test_default_heartbeat_is_set_on_create(self):
        op = self._op()
        self.assertTrue(op.last_heartbeat,
                        "new ops must carry a heartbeat so the fast reaper "
                        "has a baseline")
