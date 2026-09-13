from contextlib import contextmanager
from unittest.mock import patch, MagicMock

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestCriticalFixes(TransactionCase):
    """Regression tests for the production blockers B2-B5."""

    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or self.env['saas.product'].sudo().create(
            {'name': 'CF Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'CF Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.trial_plan = self.env['saas.plan'].sudo().create({
            'name': 'CF Trial', 'is_custom': True, 'workers': 1, 'storage_limit': 2,
            'cpu_limit': 0.5, 'ram_limit': '512m', 'price': 0.0, 'yearly_price': 0.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create({'name': 'cf.example.com'})
        # Commercial entity (company) with two child contacts.
        self.company = self.env['res.partner'].sudo().create(
            {'name': 'Acme Co', 'is_company': True})
        self.c1 = self.env['res.partner'].sudo().create(
            {'name': 'Contact One', 'parent_id': self.company.id})
        self.c2 = self.env['res.partner'].sudo().create(
            {'name': 'Contact Two', 'parent_id': self.company.id})

    def _inst(self, partner, sub, trial=False, state='draft', server=None):
        return self.env['saas.instance'].sudo().create({
            'subdomain': sub, 'domain_id': self.domain.id, 'partner_id': partner.id,
            'saas_product_id': self.product.id,
            'plan_id': (self.trial_plan if trial else self.plan).id,
            'is_trial': trial, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': False, 'state': state,
            'docker_server_id': server.id if server else False})

    # ---- B2: trial gating is per commercial entity, not per contact ----

    def test_b2_commercial_resolution(self):
        self.assertEqual(self.c1.commercial_partner_id, self.company)
        self.assertEqual(self.c2._saas_commercial(), self.company)

    def test_b2_one_trial_per_company_across_contacts(self):
        self._inst(self.c1, 'cftrial1', trial=True)
        # A second contact of the SAME company must not get another trial.
        with self.assertRaises(ValidationError):
            self._inst(self.c2, 'cftrial2', trial=True)

    def test_b2_paid_instance_blocks_trial_across_contacts(self):
        # c1 owns a PAID hosting instance → c2 (same company) is disqualified.
        self._inst(self.c1, 'cfpaid', trial=False, state='running')
        self.assertTrue(self.c2._saas_has_paid_instance(hosting=True))

    def test_b2_trial_flag_is_company_wide(self):
        self.c1._saas_mark_trial_used(hosting=True)
        # Stored on the company, visible from any contact.
        self.assertTrue(self.company.saas_hosting_trial_used)
        self.assertTrue(self.c2._saas_trial_used(hosting=True))
        self.assertFalse(self.c2._saas_trial_used(hosting=False))

    def test_b2_create_gate_reads_company_flag(self):
        self.company.saas_hosting_trial_used = True
        with self.assertRaises(ValidationError):
            self._inst(self.c2, 'cfgate', trial=True)

    # ---- B3: deploy holds an operation lock the recovery cron probes ----

    def test_b3_operation_lock_detects_live_op(self):
        inst = self._inst(self.c1, 'cfb3')
        self.assertFalse(inst._operation_is_alive())
        # Hold the lock on a SEPARATE connection → must read as alive.
        cr2 = self.registry.cursor()
        try:
            cr2.execute("SELECT pg_advisory_lock(%s, %s)",
                        (inst._OP_LOCK_NAMESPACE, inst.id))
            self.assertTrue(inst._operation_is_alive())
        finally:
            cr2.execute("SELECT pg_advisory_unlock(%s, %s)",
                        (inst._OP_LOCK_NAMESPACE, inst.id))
            cr2.close()
        self.assertFalse(inst._operation_is_alive())

    # ---- B4: server allocation respects capacity (race-safe via lock) ----

    def test_b4_allocation_respects_capacity(self):
        server = self.env['saas.server'].sudo().create({
            'name': 'cf-host', 'is_docker_host': True, 'is_db_server': True,
            'max_instances': 1})
        # One instance already occupies the host's only slot.
        self._inst(self.c1, 'cfcap1', state='provisioning', server=server)
        # A second deploy in STRICT mode must NOT overcommit it.
        inst2 = self._inst(self.c2, 'cfcap2')
        inst2.provisioning_mode = 'strict'
        with self.assertRaises(ValidationError):
            inst2._allocate_servers()

    # ---- B5: reconciler recreates a missing container, escalates if needed --

    def _reconcile(self, inst, status, start_raises=False):
        from odoo.addons.saas_core.drivers.base import HealthStatus
        fake = MagicMock()
        fake.health.return_value = HealthStatus(
            running=False, status=status, restart_count=0, detail=status)
        if start_raises:
            fake.start.side_effect = RuntimeError("compose file missing")
        with patch.object(type(inst), '_compute_driver', return_value=fake), \
             patch.object(type(inst), '_compute_handle', return_value='H'), \
             patch.object(type(inst), 'action_redeploy') as redeploy:
            action = inst.reconcile()
        return fake, redeploy, action

    def test_b5_not_found_recreates_from_compose(self):
        inst = self._inst(self.c1, 'cfb5a', state='running',
                          server=self.env['saas.server'].sudo().create(
                              {'name': 'cf5a', 'is_docker_host': True}))
        fake, redeploy, action = self._reconcile(inst, 'not_found')
        fake.start.assert_called_once()          # compose up -d = recreate
        redeploy.assert_not_called()
        self.assertEqual(action, 'recreated')

    def test_b5_not_found_escalates_to_redeploy(self):
        inst = self._inst(self.c1, 'cfb5b', state='running',
                          server=self.env['saas.server'].sudo().create(
                              {'name': 'cf5b', 'is_docker_host': True}))
        fake, redeploy, action = self._reconcile(inst, 'not_found', start_raises=True)
        redeploy.assert_called_once()            # compose gone → full redeploy
        self.assertEqual(action, 'recreating')
