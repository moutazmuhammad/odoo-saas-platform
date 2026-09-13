from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestDataService(TransactionCase):
    """Phase 1: DataService primitives (snapshot/materialize) delegate to the
    proven restic backup + restore. Unit-level (mocked); the real restic→DO-Spaces
    round-trip is verified separately against the live test tenant."""

    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or self.env['saas.product'].sudo().create(
            {'name': 'DS Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'DS Plan', 'is_custom': True, 'workers': 2, 'storage_limit': 10,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 50.0, 'yearly_price': 480.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.partner = self.env['res.partner'].sudo().create({'name': 'DS Cust'})
        self.domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create({'name': 'ds.example.com'})

    def _instance(self, sub):
        return self.env['saas.instance'].sudo().create({
            'subdomain': sub, 'domain_id': self.domain.id, 'partner_id': self.partner.id,
            'saas_product_id': self.product.id, 'plan_id': self.plan.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running'})

    def _done_backup(self, inst, name):
        return self.env['saas.instance.backup'].sudo().create({
            'instance_id': inst.id, 'name': name, 'state': 'done', 'is_full_instance': True})

    def test_snapshot_delegates_and_returns_done_backup(self):
        inst = self._instance('dssnap')
        bk = self._done_backup(inst, 'snap1')
        Backup = self.env['saas.instance.backup']
        with patch.object(type(Backup), '_perform_full_instance_backup', return_value=None) as m:
            snap = inst._data_service().snapshot(inst)
        m.assert_called_once()
        self.assertEqual(snap, bk)

    def test_snapshot_raises_when_no_backup_produced(self):
        inst = self._instance('dsnone')
        Backup = self.env['saas.instance.backup']
        with patch.object(type(Backup), '_perform_full_instance_backup', return_value=None):
            with self.assertRaises(RuntimeError):
                inst._data_service().snapshot(inst)

    def test_materialize_routes_to_restore(self):
        inst = self._instance('dsmat')
        bk = self._done_backup(inst, 'snap2')
        with patch.object(type(inst), '_do_restore_full_instance', return_value=None) as m:
            out = inst._data_service().materialize(bk)
        m.assert_called_once_with(bk.id)
        self.assertEqual(out, inst)

    def test_materialize_neutralize_not_implemented(self):
        inst = self._instance('dsneu')
        bk = self._done_backup(inst, 'snap3')
        with self.assertRaises(NotImplementedError):
            inst._data_service().materialize(bk, neutralize=True)
