from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestResConfigSettings(TransactionCase):
    """res.config.settings (saas_core extension): three deliberate
    "falsy-value trap" workarounds documented inline in the model, plus the
    set_values() side effect of re-deriving named-tier prices. The plain
    config_parameter= fields with no custom get/set logic are Odoo-framework
    behaviour and are not covered here — only what this module hand-writes."""

    def setUp(self):
        super().setUp()
        self.Settings = self.env['res.config.settings'].sudo()
        self.icp = self.env['ir.config_parameter'].sudo()

    def _settings(self, **vals):
        return self.Settings.create(vals)

    def _get(self):
        return self.Settings.get_values()

    # ---- falsy-value trap #1: section toggles stored as literal strings ----
    def test_hide_services_section_survives_reload(self):
        self._settings(saas_show_services_section=False).set_values()
        self.assertEqual(
            self.icp.get_param('saas_master.show_services_section'), 'False')
        self.assertFalse(self._get()['saas_show_services_section'])

    def test_show_services_section_defaults_true_when_unset(self):
        # Fresh install: no ICP row yet -> get_values must read shown-by-default.
        self.assertIsNone(
            self.icp.get_param('saas_master.show_services_section', None))
        self.assertTrue(self._get()['saas_show_services_section'])

    def test_hide_hosting_section_survives_reload(self):
        self._settings(saas_show_hosting_section=False).set_values()
        self.assertEqual(
            self.icp.get_param('saas_master.show_hosting_section'), 'False')
        self.assertFalse(self._get()['saas_show_hosting_section'])

    def test_re_enabling_section_survives_reload(self):
        self._settings(saas_show_services_section=False).set_values()
        self._settings(saas_show_services_section=True).set_values()
        self.assertEqual(
            self.icp.get_param('saas_master.show_services_section'), 'True')
        self.assertTrue(self._get()['saas_show_services_section'])

    # ---- falsy-value trap #2: 0-is-meaningful integers ----
    def test_max_instances_zero_means_unlimited_and_sticks(self):
        self._settings(saas_max_instances_per_user=0).set_values()
        self.assertEqual(
            self.icp.get_param('saas_master.max_instances_per_user'), '0')
        self.assertEqual(self._get()['saas_max_instances_per_user'], 0)

    def test_grace_period_zero_sticks(self):
        self._settings(saas_grace_period_days=0).set_values()
        self.assertEqual(
            self.icp.get_param('saas_master.grace_period_days'), '0')
        self.assertEqual(self._get()['saas_grace_period_days'], 0)

    def test_storage_grace_days_zero_sticks(self):
        self._settings(saas_storage_grace_days=0).set_values()
        self.assertEqual(
            self.icp.get_param('saas_master.storage_grace_days'), '0')
        self.assertEqual(self._get()['saas_storage_grace_days'], 0)

    def test_grace_period_nonzero_roundtrips(self):
        self._settings(saas_grace_period_days=14).set_values()
        self.assertEqual(self._get()['saas_grace_period_days'], 14)

    # ---- falsy-value trap #3: 0.0-is-meaningful float ----
    def test_env_price_factor_zero_means_free_and_sticks(self):
        self._settings(saas_env_price_factor=0.0).set_values()
        self.assertEqual(
            self.icp.get_param('saas_master.env_price_factor'), '0.0')
        self.assertEqual(self._get()['saas_env_price_factor'], 0.0)

    def test_env_price_factor_nonzero_roundtrips(self):
        self._settings(saas_env_price_factor=0.7).set_values()
        self.assertAlmostEqual(self._get()['saas_env_price_factor'], 0.7, 2)

    # ---- set_values() re-derives public-tier plan prices ----
    def test_set_values_resyncs_public_tier_price(self):
        product = self.env['saas.product'].sudo().create(
            {'name': 'CFG Services Product'})
        plan = self.env['saas.plan'].sudo().create({
            'name': 'CFG Public Tier', 'is_public_tier': True,
            'is_trial_plan': False, 'manual_price': False,
            'workers': 4, 'storage_limit': 50, 'cpu_limit': 2.0,
            'ram_limit': '2g',
            'saas_product_ids': [(6, 0, [product.id])],
        })
        before = plan.price
        self._settings(
            saas_worker_price=before + 100.0,
            saas_storage_price_per_gb=0.5,
        ).set_values()
        plan.invalidate_recordset(['price', 'yearly_price'])
        self.assertGreater(plan.price, before)

    def test_set_values_leaves_manually_priced_plan_alone(self):
        product = self.env['saas.product'].sudo().create(
            {'name': 'CFG Manual Product'})
        plan = self.env['saas.plan'].sudo().create({
            'name': 'CFG Manual Tier', 'is_public_tier': True,
            'is_trial_plan': False, 'manual_price': True,
            'workers': 4, 'storage_limit': 50, 'cpu_limit': 2.0,
            'ram_limit': '2g', 'price': 999.0, 'yearly_price': 9999.0,
            'saas_product_ids': [(6, 0, [product.id])],
        })
        self._settings(saas_worker_price=1234.0).set_values()
        plan.invalidate_recordset(['price', 'yearly_price'])
        self.assertEqual(plan.price, 999.0)
