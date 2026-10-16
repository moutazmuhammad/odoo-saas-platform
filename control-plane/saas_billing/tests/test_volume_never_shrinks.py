"""C17.1: a downgrade never sends a smaller volume. Billing follows the new
plan; the volume the cluster holds stays (PVCs cannot shrink)."""
from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

from .test_upgrade_gate import _GatePatches, _mk_instance, _setup_fixture


@tagged('post_install', '-at_install')
class TestVolumeNeverShrinks(_GatePatches, TransactionCase):

    def setUp(self):
        super().setUp()
        self.fx = _setup_fixture(self.env, 'v')
        self.small, self.big = self.fx['small'], self.fx['big']  # 10 GB / 20 GB
        self.inst = _mk_instance(self.env, self.fx, 'voltest', plan=self.big)
        self.start_gate_patches(self.inst)
        self.inst._update_container_resources()
        self.inst._verify_package()                  # C6.3: verified, in sync

    def _sent(self):
        return self.driver.set_package.call_args.args[1]

    def test_scheduled_downgrade_keeps_the_volume(self):
        today = fields.Date.today()
        self.inst.write({'scheduled_plan_id': self.small.id, 'scheduled_billing_period': 'monthly',
                         'next_invoice_date': today, 'last_invoice_date': today - timedelta(days=30)})
        self.inst._generate_renewal_invoice()
        self.assertEqual(self.inst.plan_id, self.small, "billing follows the new plan")
        self.assertEqual(self.inst.effective_storage_limit_gb, 10)
        sent = self._sent()
        self.assertEqual(sent['filestore_size'], '20Gi', "the volume is never asked to shrink")
        self.assertEqual(sent['db_storage_size'], '20Gi')
        self.assertEqual(sent['quota']['requests.storage'], '21Gi')
        small = self.small._package()
        self.assertEqual(sent['workers'], small['workers'], "CPU/RAM/workers do go down")
        self.assertEqual(sent['cpu_limit'], '%dm' % small['odoo_cpu_m'])
        self.assertEqual(self.inst.package_status, 'applying')
        self.inst._verify_package()
        self.assertEqual(self.inst.package_status, 'in_sync')

    def test_released_storage_block_keeps_the_volume(self):
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('saas_master.storage_block_gb', '10')
        icp.set_param('saas_master.storage_block_price', '2.0')
        self.inst.extra_storage_blocks = 1
        self.inst._update_container_resources()
        self.assertEqual(self._sent()['filestore_size'], '30Gi')
        self.inst.total_storage_bytes = 0
        self.inst.action_release_storage_block(1)
        self.assertEqual(self.inst.effective_storage_limit_gb, 20)
        self.assertEqual(self._sent()['filestore_size'], '30Gi')

    def test_growth_still_grows(self):
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('saas_master.storage_block_gb', '10')
        icp.set_param('saas_master.storage_block_price', '2.0')
        self.inst.extra_storage_blocks = 2           # 20 GB plan + 2 × 10 GB, volume 20 GiB
        self.assertEqual(self.inst._k8s_plan_resources()['filestore_size'], '40Gi')

    def test_fresh_instance_uses_the_plan_size(self):
        fresh = _mk_instance(self.env, self.fx, 'volfresh', plan=self.small)
        res = fresh._k8s_plan_resources()
        self.assertEqual(res['filestore_size'], '10Gi')
        self.assertEqual(res['quota']['requests.storage'], '11Gi')

    def test_floor_parsing(self):
        for applied, floor in (
                (False, 0), ('not json', 0), ('[]', 0), ('{"filestore_size": "5G"}', 0),
                ('{"filestore_size": "7Gi", "db_storage_size": "9Gi"}', 9)):
            self.inst.write({'package_applied': applied, 'package_desired': False})
            self.assertEqual(self.inst._volume_floor_gb(), floor, applied)
        self.inst.write({'package_applied': '{"filestore_size": "10Gi"}',
                         'package_desired': '{"filestore_size": "30Gi"}'})
        self.assertEqual(self.inst._volume_floor_gb(), 30, "a package being verified counts")

    def test_upgrade_back_needs_no_volume_growth(self):
        self.inst.plan_id = self.small               # downgraded; volume still 20 GiB
        self.inst._update_container_resources()
        self.assertEqual(self._sent()['filestore_size'], '20Gi')
        self.inst._verify_package()
        self.fx['server'].volume_expansion = 'no'
        inv = self.inst.action_request_plan_change(self.big.id)
        self.assertTrue(inv.exists(), "20 GB fits the existing volume: nothing to grow")
