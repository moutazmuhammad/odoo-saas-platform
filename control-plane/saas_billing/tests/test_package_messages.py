"""C6.3: a paid change tells the customer it is being applied; "Upgrade
complete" appears only once the rollout is verified."""
from odoo.tests.common import TransactionCase, tagged

from .test_upgrade_gate import _GatePatches, _mk_instance, _setup_fixture


@tagged('post_install', '-at_install')
class TestPackageMessages(_GatePatches, TransactionCase):

    def setUp(self):
        super().setUp()
        self.fx = _setup_fixture(self.env, 'm')
        self.inst = _mk_instance(self.env, self.fx, 'msgtest')
        self.start_gate_patches(self.inst)
        self.inst._update_container_resources()
        self.inst._verify_package()

    def _bodies(self, inst):
        return ' '.join(inst.message_ids.mapped('body'))

    def _check(self, inst, applying_text):
        inst._apply_pending_plan_change()
        self.assertEqual(inst.plan_id, self.fx['big'])
        self.assertEqual(inst.package_status, 'applying')
        self.assertIn(applying_text, self._bodies(inst))
        self.assertNotIn('Upgrade complete', self._bodies(inst))
        self.assertNotIn('Upgraded from', self._bodies(inst))
        inst._verify_package()
        self.assertEqual(inst.package_status, 'in_sync')
        self.assertIn('Upgrade complete', self._bodies(inst))

    def test_production_upgrade(self):
        self.inst.pending_plan_id = self.fx['big']
        self._check(self.inst, 'Applying your upgrade')

    def test_staging_resize(self):
        child = _mk_instance(self.env, self.fx, 'msgstaging', parent_id=self.inst.id,
                             environment='staging')
        child._update_container_resources()
        child._verify_package()
        child.pending_plan_id = self.fx['big']
        self._check(child, 'Resizing the server')
