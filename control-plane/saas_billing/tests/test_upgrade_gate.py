"""C6.7: pre-payment feasibility gate for upgrades. An upgrade that a known
limit makes infeasible gets no invoice and no charge, and is checked again
whenever money is collected for it."""
from unittest.mock import MagicMock, patch

import uuid

import psycopg2

from odoo import SUPERUSER_ID, api, fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

REFUSED = "isn't available on your server right now"


def _setup_fixture(env, tag):
    """Hosting product, two plans, a cluster and a customer: the records
    every gate test needs. Returns a dict of records."""
    sudo = env['saas.plan'].sudo()
    product = env['saas.product'].sudo().search([('is_hosting', '=', True)], limit=1) \
        or env['saas.product'].sudo().create(
            {'name': 'Gate Hosting %s' % tag, 'is_hosting': True, 'is_published': True})
    common = {'is_custom': True, 'storage_limit': 10, 'price': 20.0, 'yearly_price': 192.0,
              'currency_id': env.company.currency_id.id,
              'saas_product_ids': [(6, 0, [product.id])]}
    small = sudo.create(dict(common, name='Gate Small %s' % tag, workers=1,
                             cpu_limit=1.0, ram_limit='1g'))
    big = sudo.create(dict(common, name='Gate Big %s' % tag, workers=3, cpu_limit=2.0,
                           ram_limit='3g', price=60.0, yearly_price=576.0, storage_limit=20))
    server = env['saas.server'].sudo().create({
        'name': 'gate-srv-%s' % tag, 'compute_driver': 'kubernetes',
        'node_max_cpu_m': 16000, 'node_max_mem_mi': 65536, 'volume_expansion': 'yes',
        'cluster_facts_at': fields.Datetime.now()})
    domain = env['saas.based.domain'].sudo().search([], limit=1) \
        or env['saas.based.domain'].sudo().create({'name': 'gate.example.com'})
    version = env['saas.odoo.version'].sudo().search(
        [('docker_image', '!=', False), ('docker_image_tag', '!=', False)], limit=1) \
        or env['saas.odoo.version'].sudo().create(
            {'name': 'Gate 18', 'docker_image': 'odoo', 'docker_image_tag': '18.0'})
    partner = env['res.partner'].sudo().create(
        {'name': 'Gate Cust %s' % tag, 'email': 'gate-%s@example.com' % tag})
    return {'product': product, 'small': small, 'big': big, 'server': server,
            'domain': domain, 'version': version, 'partner': partner}


def _mk_instance(env, fx, sub, plan=None, **vals):
    base = {
        'subdomain': sub, 'domain_id': fx['domain'].id,
        'partner_id': fx['partner'].id, 'saas_product_id': fx['product'].id,
        'plan_id': (plan or fx['small']).id, 'docker_server_id': fx['server'].id,
        'odoo_version_id': fx['version'].id, 'billing_period': 'monthly',
        'environment': 'production', 'region_id': False, 'state': 'running'}
    base.update(vals)
    return env['saas.instance'].sudo().create(base)


class _GatePatches:
    """Class-level patches (they hold across cursors): a fake cluster driver,
    captured alerts, no job workers, no saved card."""

    def start_gate_patches(self, inst_model):
        # Test cursors never really commit: drop the per-transaction probe.
        inst_model.env.cr.cache.pop(type(inst_model)._GATE_PROBE_CACHE, None)
        self.addCleanup(inst_model.env.cr.cache.pop, type(inst_model)._GATE_PROBE_CACHE, None)
        self.driver = MagicMock()
        self.driver.cluster_facts.return_value = {
            'node_max_cpu_m': 16000, 'node_max_mem_mi': 65536, 'volume_expansion': True}
        self.alerts = []
        Job = type(inst_model.env['saas.job'])
        for target, attr, value in (
                (type(inst_model), '_compute_driver', lambda rec, connection=None: self.driver),
                (type(inst_model), '_compute_handle', lambda rec: rec.subdomain),
                (type(inst_model), '_auto_renew_method', lambda rec: False),
                (type(inst_model.env['saas.alert']), '_notify',
                 lambda rec, event, message, **kw: self.alerts.append((event, message))),
                (Job, '_spawn_worker', lambda rec: None),
                (Job, '_spawn_worker_after_commit', lambda rec: None)):
            p = patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)


@tagged('post_install', '-at_install')
class TestUpgradeGate(_GatePatches, TransactionCase):

    def setUp(self):
        super().setUp()
        self.fx = _setup_fixture(self.env, 'u')
        self.server = self.fx['server']
        self.inst = _mk_instance(self.env, self.fx, 'gatetest')
        self.Inst = self.env['saas.instance'].sudo()
        self.start_gate_patches(self.inst)

    # ------------------------------------------------------------ helpers
    def _need(self, current, target):
        return self.Inst._package_change_need(current._package(), target._package())

    def _cap_cpu_for(self, *needs, slack_m=0):
        """A CPU cap that fits the running packages plus ``needs`` (m),
        give or take ``slack_m``."""
        self.server.invalidate_recordset(['allocated_cpu'])
        return self.server.allocated_cpu + (sum(needs) + slack_m) / 1000.0

    def _upgrade(self, inst=None, plan=None):
        return (inst or self.inst).action_request_plan_change((plan or self.fx['big']).id)

    def _moves(self):
        return self.env['account.move'].sudo().search_count(
            [('partner_id', '=', self.fx['partner'].id)])

    def _refused(self, fn, text=REFUSED):
        with self.assertRaises(UserError) as cm:
            fn()
        self.assertIn(text, str(cm.exception))
        self.assertIn('not been charged', str(cm.exception))
        return str(cm.exception)

    # ------------------------------------------------------------ tests
    def test_feasible_upgrade_creates_pending_invoice_only(self):
        result = self._upgrade()
        self.assertTrue(result.exists(), "an invoice awaiting payment")
        self.assertEqual(self.inst.pending_plan_id, self.fx['big'])
        self.assertEqual(self.inst.plan_id, self.fx['small'], "nothing applied before payment")
        self.assertEqual(self.inst.pending_change_invoice_id, result)
        self.assertNotIn('upgrade_refused', [e for e, _m in self.alerts])

    def test_full_cluster_refuses_with_no_invoice(self):
        cpu, _mem = self._need(self.fx['small'], self.fx['big'])
        self.server.max_cpu_cores = self._cap_cpu_for(cpu, slack_m=-1)
        self._refused(self._upgrade)
        self.assertEqual(self._moves(), 0, "no invoice for a refused upgrade")
        self.assertFalse(self.inst.pending_plan_id)
        self.assertEqual([e for e, _m in self.alerts], ['upgrade_refused'])
        self.assertIn('capacity_limit', self.alerts[0][1])

    def test_exact_fit_including_surge_passes(self):
        cpu, _mem = self._need(self.fx['small'], self.fx['big'])
        self.server.max_cpu_cores = self._cap_cpu_for(cpu)
        self.assertTrue(self._upgrade().exists())

    def test_surge_pod_is_counted(self):
        small, big = self.fx['small']._package(), self.fx['big']._package()
        delta = big['total_cpu_m'] - small['total_cpu_m']
        cpu, _mem = self.Inst._package_change_need(small, big)
        self.assertEqual(cpu, delta + big['odoo_cpu_m'] + big['cron_cpu_m'])
        same, _ = self.Inst._package_change_need(small, small)
        self.assertEqual(same, 0, "no resize, no rolling update: nothing added")

    def test_ram_cap_is_checked(self):
        _cpu, mem = self._need(self.fx['small'], self.fx['big'])
        self.server.invalidate_recordset(['allocated_ram_gb'])
        self.server.max_ram_gb = self.server.allocated_ram_gb + (mem - 1) / 1024.0
        self._refused(self._upgrade)

    def test_oversized_pod_refused(self):
        big = self.fx['big']._package()
        self.server.node_max_cpu_m = big['odoo_cpu_m'] + big['cron_cpu_m'] - 1
        self._refused(self._upgrade)
        self.assertIn('node_too_small', self.alerts[0][1])
        self.assertEqual(self._moves(), 0)

    def test_unknown_node_sizes_pass_and_log(self):
        self.server.write({'node_max_cpu_m': 0, 'node_max_mem_mi': 0,
                           'cluster_facts_at': fields.Datetime.now()})
        with self.assertLogs('odoo.addons.saas_billing.models.saas_instance', 'INFO') as logs:
            self.assertTrue(self._upgrade().exists())
        self.assertIn('node fit not checked', '\n'.join(logs.output))

    def test_cluster_unavailable_refused(self):
        self.driver.require_cluster_ready.side_effect = RuntimeError('operator is unavailable')
        self._refused(self._upgrade)
        self.assertIn('cluster_unavailable', self.alerts[0][1])
        self.assertEqual(self._moves(), 0)

    def test_volume_that_cannot_grow_refused(self):
        self.server.volume_expansion = 'no'
        self._refused(self._upgrade)
        self.assertIn('volume_expansion', self.alerts[0][1])

    def test_storage_block_refused_when_volume_cannot_grow(self):
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('saas_master.storage_block_gb', '10')
        icp.set_param('saas_master.storage_block_price', '2.0')
        self.server.volume_expansion = 'no'
        self._refused(lambda: self.inst.action_purchase_storage_block(1))
        self.assertFalse(self.inst.storage_block_pending_invoice_id)
        self.server.volume_expansion = 'yes'
        inv = self.inst.action_purchase_storage_block(1)
        self.assertEqual(self.inst.storage_block_pending_invoice_id, inv)

    def test_no_caps_logs_capacity_not_checked(self):
        self.server.write({'max_cpu_cores': 0, 'max_ram_gb': 0})
        with self.assertLogs('odoo.addons.saas_billing.models.saas_instance', 'INFO') as logs:
            self._upgrade()
        self.assertIn('capacity not checked (no caps configured)', '\n'.join(logs.output))
        for reason in ('capacity_limit', 'change_in_progress'):
            text = self.inst._gate_refusal_text(reason).lower()
            self.assertNotIn('reserved', text)
            self.assertNotIn('guarantee', text)

    def test_unpaid_inflight_upgrade_of_another_tenant_is_counted(self):
        other = _mk_instance(self.env, self.fx, 'gateother')
        cpu, _mem = self._need(self.fx['small'], self.fx['big'])
        self.server.max_cpu_cores = self._cap_cpu_for(cpu, cpu, slack_m=-1)
        self.assertTrue(self._upgrade(other).exists(), "the first one fits")
        before = self._moves()
        self._refused(self._upgrade)
        self.assertEqual(self._moves(), before, "the second gets no invoice")
        self.assertFalse(self.inst.pending_plan_id)
        # Withdrawn (invoice cancelled) → no longer counted.
        other.pending_change_invoice_id.button_cancel()
        self.assertTrue(self._upgrade().exists())

    def test_pending_package_blocks_a_second_upgrade(self):
        self.inst.package_status = 'pending'
        self._refused(self._upgrade, text='still being applied')
        self.assertEqual(self._moves(), 0)

    def test_paid_unapplied_change_is_never_replaced(self):
        inv = self._upgrade()
        mid = self._mid()
        with patch.object(type(self.inst), '_paid_pending_change_invoice', lambda rec: inv):
            self._refused(lambda: self._upgrade(plan=mid), text='still being applied')
        self.assertEqual(self.inst.pending_change_invoice_id, inv, "the paid change is kept")
        self.assertEqual(inv.state, 'posted')

    def _mid(self):
        return self.env['saas.plan'].sudo().create({
            'name': 'Gate Mid', 'is_custom': True, 'workers': 2, 'storage_limit': 15,
            'cpu_limit': 1.5, 'ram_limit': '2g', 'price': 40.0, 'yearly_price': 384.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.fx['product'].id])]})

    def test_log_only_mode_lets_it_through_and_alerts(self):
        self.env['ir.config_parameter'].sudo().set_param('saas_master.upgrade_gate', 'log')
        self.server.node_max_cpu_m = 100
        self.assertTrue(self._upgrade().exists())
        self.assertEqual([e for e, _m in self.alerts], ['upgrade_refused'])

    def test_collection_point_rechecks_before_any_transaction(self):
        inv = self._upgrade()
        # The cluster fills up before the customer pays.
        cpu, _mem = self._need(self.fx['small'], self.fx['big'])
        self.server.max_cpu_cores = self._cap_cpu_for(cpu, slack_m=-1)
        with self.assertRaises(UserError) as cm:
            self.env['payment.transaction'].sudo().create({
                'amount': inv.amount_residual, 'invoice_ids': [(6, 0, [inv.id])]})
        self.assertIn(REFUSED, str(cm.exception))
        self.assertFalse(self.env['payment.transaction'].sudo().search(
            [('invoice_ids', 'in', inv.ids)]), "no transaction, so no charge")

    def test_collection_point_passes_while_feasible(self):
        inv = self._upgrade()
        self.Inst._gate_collection([inv.id])  # no raise
        self.Inst._gate_collection([])        # nothing to check

    def test_saved_card_charge_is_refused_at_the_transaction(self):
        inv = self._upgrade()
        self.server.node_max_cpu_m = 100
        method = MagicMock()
        method.token_id.active = True
        method.provider_id.state = 'test'
        method.provider_id.journal_id = False
        state, message = self.env['saas.payment.gateway'].charge(method, inv)
        self.assertEqual(state, 'failed')
        self.assertIn(REFUSED, message)
        self.assertNotIn(inv.payment_state, ('paid', 'in_payment'))

    def test_checkout_withdraws_an_infeasible_change(self):
        inv = self._upgrade()
        self.server.node_max_cpu_m = 100
        text = self.inst._withdraw_infeasible_change(inv)
        self.assertIn(REFUSED, text)
        self.assertEqual(inv.state, 'cancel')
        self.assertFalse(self.inst.pending_plan_id)
        self.assertFalse(self.inst.pending_change_invoice_id)
        self.assertEqual(self.inst.plan_id, self.fx['small'])

    def test_checkout_keeps_a_feasible_change(self):
        inv = self._upgrade()
        self.assertIsNone(self.inst._withdraw_infeasible_change(inv))
        self.assertEqual(inv.state, 'posted')
        self.assertIsNone(self.inst._withdraw_infeasible_change(
            self.env['account.move'].sudo().browse()), "not a change invoice")

    def test_trial_subscription_is_gated(self):
        self.inst.is_trial = True
        self.server.node_max_cpu_m = 100
        self._refused(lambda: self.inst.action_subscribe_from_trial(self.fx['big'].id))
        self.assertFalse(self.inst.pending_plan_id)

    def test_environment_resize_is_gated(self):
        child = _mk_instance(self.env, self.fx, 'gatestaging', parent_id=self.inst.id,
                             environment='staging')
        self.server.node_max_cpu_m = 100
        self._refused(lambda: child.action_change_environment_plan(self.fx['big'].id))
        self.assertFalse(child.pending_plan_id)

    def test_stale_cluster_facts_are_refreshed(self):
        self.server.cluster_facts_at = False
        self.env.cr.cache.pop(self.Inst._GATE_PROBE_CACHE, None)
        self.driver.cluster_facts.return_value = {
            'node_max_cpu_m': 8000, 'node_max_mem_mi': 32768, 'volume_expansion': False}
        self.inst._gate_probe_cluster()
        self.assertEqual(self.server.node_max_cpu_m, 8000)
        self.assertEqual(self.server.volume_expansion, 'no')
        self.assertTrue(self.server.cluster_facts_at)

    def test_failed_fact_refresh_keeps_previous_values(self):
        self.driver.cluster_facts.side_effect = RuntimeError('timeout')
        self.assertFalse(self.server._refresh_cluster_facts(self.driver))
        self.assertEqual(self.server.node_max_cpu_m, 16000)

    def test_transaction_command_parsing(self):
        parse = type(self.env['payment.transaction'])._saas_invoice_ids
        self.assertEqual(parse([(6, 0, [1, 2]), (4, 3, 0)]), {1, 2, 3})
        self.assertEqual(parse(None), set())


@tagged('post_install', '-at_install')
class TestUpgradeGateConcurrency(_GatePatches, TransactionCase):
    """Two real cursors: the gate holds the cluster row until commit, so a
    second attempt waits, then sees the first one's in-flight change."""

    def setUp(self):
        super().setUp()
        self.start_gate_patches(self.env['saas.instance'])
        self.tag = uuid.uuid4().hex[:8]
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            fx = _setup_fixture(env, self.tag)
            a = _mk_instance(env, fx, 'gatea%s' % self.tag)
            b = _mk_instance(env, fx, 'gateb%s' % self.tag)
            need, _ = env['saas.instance']._package_change_need(
                fx['small']._package(), fx['big']._package())
            fx['server'].invalidate_recordset(['allocated_cpu'])
            # Room for exactly one upgrade.
            fx['server'].max_cpu_cores = fx['server'].allocated_cpu + (need + need // 2) / 1000.0
            self.ids = {'a': a.id, 'b': b.id, 'big': fx['big'].id, 'server': fx['server'].id}
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            insts = env['saas.instance'].browse([self.ids['a'], self.ids['b']])
            moves = insts.mapped('pending_change_invoice_id')
            moves.filtered(lambda m: m.state == 'posted').button_cancel()
            insts.write({'state': 'cancelled', 'pending_plan_id': False,
                         'pending_change_invoice_id': False})

    def _upgrade_a_uncommitted(self, cr1):
        env1 = api.Environment(cr1, SUPERUSER_ID, {})
        inv = env1['saas.instance'].browse(self.ids['a']).action_request_plan_change(
            self.ids['big'])
        self.assertTrue(inv.exists())

    def _upgrade_b(self, cr2):
        env2 = api.Environment(cr2, SUPERUSER_ID, {})
        return env2['saas.instance'].browse(self.ids['b']).action_request_plan_change(
            self.ids['big'])

    def test_second_decision_waits_then_sees_the_first(self):
        with self.registry.cursor() as cr1:
            self._upgrade_a_uncommitted(cr1)
            # cr1 holds the cluster row until it commits.
            with self.registry.cursor() as cr2:
                with self.assertRaises(psycopg2.errors.LockNotAvailable):
                    cr2.execute("SELECT id FROM saas_server WHERE id = %s FOR UPDATE NOWAIT",
                                (self.ids['server'],))
                cr2.rollback()
            cr1.commit()
        with self.registry.cursor() as cr2:
            with self.assertRaises(UserError):
                self._upgrade_b(cr2)
            cr2.rollback()
            env2 = api.Environment(cr2, SUPERUSER_ID, {})
            self.assertFalse(env2['saas.instance'].browse(self.ids['b']).pending_change_invoice_id,
                             "one invoice, one refusal")

    def test_stale_snapshot_is_retried_not_decided(self):
        """REPEATABLE READ: B's snapshot predates A's commit. B must not
        decide on it; it gets a serialization failure, which Odoo retries."""
        with self.registry.cursor() as cr1, self.registry.cursor() as cr2:
            cr2.execute("SELECT count(*) FROM saas_instance")  # B's snapshot starts here
            self._upgrade_a_uncommitted(cr1)
            cr1.commit()
            with self.assertRaises(psycopg2.errors.SerializationFailure):
                self._upgrade_b(cr2)
            cr2.rollback()
