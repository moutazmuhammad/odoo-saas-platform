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

    def _check(self, inst):
        """C6.10 order: funds secured ("applying") -> verified ("Upgrade
        complete") -> billing switched ("Payment complete")."""
        invoice = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice', 'partner_id': inst.partner_id.id,
            'invoice_line_ids': [(0, 0, {'name': 'up', 'quantity': 1, 'price_unit': 0.0})]})
        invoice.action_post()                       # zero total: paid at once
        inst.write({'pending_plan_id': self.fx['big'].id, 'pending_change_invoice_id': invoice.id})
        inst._advance_change(invoice)
        self.assertIn("We're applying it now", self._bodies(inst))
        inst._do_deliver_change(invoice.id)
        self.assertEqual(inst.package_status, 'applying')
        self.assertEqual(inst.plan_id, self.fx['small'], "billing unchanged until delivery")
        self.assertNotIn('Upgrade complete', self._bodies(inst))
        self.assertNotIn('Payment complete', self._bodies(inst))
        inst._verify_package()
        self.assertEqual(inst.package_status, 'in_sync')
        self.assertIn('Upgrade complete', self._bodies(inst))
        inst._do_apply_paid_plan_change(invoice.id)
        self.assertEqual(inst.plan_id, self.fx['big'])
        self.assertIn('Payment complete', self._bodies(inst))
        self.assertNotIn('Upgraded from', self._bodies(inst))

    def test_production_upgrade(self):
        self._check(self.inst)

    def test_staging_resize(self):
        child = _mk_instance(self.env, self.fx, 'msgstaging', parent_id=self.inst.id,
                             environment='staging')
        child._update_container_resources()
        child._verify_package()
        self._check(child)
