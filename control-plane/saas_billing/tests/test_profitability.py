from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestProfitabilityEngine(TransactionCase):
    """saas.pricing.engine.profitability() / minimum_profitable_price() —
    THE one profit/margin/minimum-price formula every priced model in the
    platform must share (billing/pricing architecture redesign, Part 12).
    """

    def setUp(self):
        super().setUp()
        self.engine = self.env['saas.pricing.engine']

    def test_profit_and_margin_basic(self):
        result = self.engine.profitability(100.0, 42.0)
        self.assertAlmostEqual(result['profit'], 58.0, places=2)
        self.assertAlmostEqual(result['margin_pct'], 58.0, places=2)
        self.assertTrue(result['is_profitable'])

    def test_loss_making_configuration(self):
        result = self.engine.profitability(35.0, 42.0)
        self.assertAlmostEqual(result['profit'], -7.0, places=2)
        self.assertAlmostEqual(result['margin_pct'], -20.0, places=2)
        self.assertFalse(result['is_profitable'])

    def test_break_even_is_profitable(self):
        result = self.engine.profitability(50.0, 50.0)
        self.assertAlmostEqual(result['profit'], 0.0, places=2)
        self.assertTrue(result['is_profitable'])

    def test_zero_price_does_not_divide_by_zero(self):
        result = self.engine.profitability(0.0, 10.0)
        self.assertAlmostEqual(result['profit'], -10.0, places=2)
        self.assertEqual(result['margin_pct'], 0.0)
        self.assertFalse(result['is_profitable'])

    def test_zero_cost_is_full_margin(self):
        result = self.engine.profitability(100.0, 0.0)
        self.assertAlmostEqual(result['profit'], 100.0, places=2)
        self.assertAlmostEqual(result['margin_pct'], 100.0, places=2)
        self.assertTrue(result['is_profitable'])

    def test_none_price_and_cost_treated_as_zero(self):
        result = self.engine.profitability(None, None)
        self.assertEqual(result['profit'], 0.0)
        self.assertEqual(result['margin_pct'], 0.0)
        self.assertTrue(result['is_profitable'])

    def test_minimum_profitable_price_matches_worked_example(self):
        # cost $40, target margin 40% -> 40 / (1 - 0.40) = 66.666...
        price = self.engine.minimum_profitable_price(40.0, 40.0)
        self.assertAlmostEqual(price, 66.67, places=2)

    def test_minimum_profitable_price_zero_cost(self):
        self.assertEqual(self.engine.minimum_profitable_price(0.0, 30.0), 0.0)

    def test_minimum_profitable_price_undefined_at_100pct_target(self):
        self.assertIsNone(self.engine.minimum_profitable_price(40.0, 100.0))
        self.assertIsNone(self.engine.minimum_profitable_price(40.0, 150.0))

    def test_price_at_minimum_profitable_price_hits_target_margin(self):
        cost = 40.0
        target = 40.0
        price = self.engine.minimum_profitable_price(cost, target)
        result = self.engine.profitability(price, cost)
        self.assertAlmostEqual(result['margin_pct'], target, places=2)


@tagged('post_install', '-at_install')
class TestProfitabilitySharedAcrossModels(TransactionCase):
    """Every cost_price/margin_pct field in the platform must produce the
    SAME number for the same (price, cost) pair — regression guard against
    the three-different-hand-rolled-formulas problem this redesign fixed."""

    def test_compute_tier_addon_support_plan_agree(self):
        engine = self.env['saas.pricing.engine']
        expected = engine.profitability(100.0, 40.0)

        tier = self.env['saas.compute.tier'].sudo().create({
            'name': 'ProfitTestTier', 'code': 'profit-test-tier',
            'replicas': 2, 'monthly_price': 100.0, 'cost_price': 40.0,
        })
        addon = self.env['saas.addon'].sudo().create({
            'name': 'ProfitTestAddon', 'code': 'profit-test-addon',
            'monthly_price': 100.0, 'cost_price': 40.0,
        })
        support = self.env['saas.support.plan'].sudo().create({
            'name': 'ProfitTestSupport', 'code': 'profit-test-support',
            'monthly_price': 100.0, 'cost_price': 40.0,
        })

        for rec in (tier, addon, support):
            self.assertAlmostEqual(rec.profit, expected['profit'], places=2)
            self.assertAlmostEqual(rec.margin_pct, expected['margin_pct'], places=2)
            self.assertEqual(rec.is_profitable, expected['is_profitable'])

    def test_plan_profitability_uses_the_real_cost_floor(self):
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('saas_master.worker_floor', '5.0')
        icp.set_param('saas_master.storage_floor', '0.1')
        product = self.env['saas.product'].sudo().create(
            {'name': 'ProfitTestService', 'is_hosting': False, 'is_published': True})
        plan = self.env['saas.plan'].sudo().create({
            'name': 'ProfitTestPlan', 'is_custom': True,
            'workers': 4, 'storage_limit': 20, 'cpu_limit': 2.0, 'ram_limit': '4g',
            'price': 100.0, 'manual_price': True,
            'saas_product_ids': [(6, 0, [product.id])],
        })
        # cost floor = 4*5.0 + 20*0.1 = 22.0
        self.assertAlmostEqual(plan.cost_price, 22.0, places=2)
        self.assertTrue(plan.cost_tracked)
        engine = self.env['saas.pricing.engine']
        expected = engine.profitability(100.0, 22.0)
        self.assertAlmostEqual(plan.profit, expected['profit'], places=2)
        self.assertAlmostEqual(plan.margin_pct, expected['margin_pct'], places=2)
        self.assertEqual(plan.is_profitable, expected['is_profitable'])

    def test_plan_cost_not_tracked_when_floor_unconfigured(self):
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('saas_master.worker_floor', '0')
        icp.set_param('saas_master.storage_floor', '0')
        product = self.env['saas.product'].sudo().create(
            {'name': 'ProfitTestService2', 'is_hosting': False, 'is_published': True})
        plan = self.env['saas.plan'].sudo().create({
            'name': 'ProfitTestPlanNoFloor', 'is_custom': True,
            'workers': 4, 'storage_limit': 20, 'cpu_limit': 2.0, 'ram_limit': '4g',
            'price': 100.0, 'manual_price': True,
            'saas_product_ids': [(6, 0, [product.id])],
        })
        self.assertFalse(plan.cost_tracked)
        self.assertEqual(plan.cost_price, 0.0)

    def test_plan_minimum_profitable_price_reflects_target_margin_setting(self):
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('saas_master.worker_floor', '10.0')
        icp.set_param('saas_master.storage_floor', '0')
        icp.set_param('saas_master.target_margin_pct', '40')
        product = self.env['saas.product'].sudo().create(
            {'name': 'ProfitTestService3', 'is_hosting': False, 'is_published': True})
        plan = self.env['saas.plan'].sudo().create({
            'name': 'ProfitTestPlanMinPrice', 'is_custom': True,
            'workers': 4, 'storage_limit': 0, 'cpu_limit': 2.0, 'ram_limit': '4g',
            'price': 100.0, 'manual_price': True,
            'saas_product_ids': [(6, 0, [product.id])],
        })
        # cost floor = 4*10 = 40; target 40% -> minimum price 66.67
        self.assertAlmostEqual(plan.cost_price, 40.0, places=2)
        self.assertAlmostEqual(plan.minimum_profitable_price, 66.67, places=2)

    def test_trial_plan_profitability_is_not_evaluated(self):
        product = self.env['saas.product'].sudo().create(
            {'name': 'ProfitTestTrialService', 'is_hosting': False, 'is_published': True})
        plan = self.env['saas.plan'].sudo().create({
            'name': 'ProfitTestTrialPlan', 'is_custom': True, 'is_trial_plan': True,
            'workers': 2, 'storage_limit': 5, 'cpu_limit': 1.0, 'ram_limit': '2g',
            'saas_product_ids': [(6, 0, [product.id])],
        })
        self.assertFalse(plan.cost_tracked)
        self.assertEqual(plan.cost_price, 0.0)
        self.assertTrue(plan.is_profitable)

    def test_instance_margin_matches_engine_profitability(self):
        """Regression guard: saas.instance's margin must be computed via
        the same shared formula, not a hand-rolled copy."""
        product = self.env['saas.product'].sudo().create(
            {'name': 'ProfitTestInstanceSvc', 'is_hosting': True, 'is_published': True})
        plan = self.env['saas.plan'].sudo().create({
            'name': 'ProfitTestInstancePlan', 'is_custom': True,
            'workers': 2, 'storage_limit': 10, 'cpu_limit': 2.0, 'ram_limit': '4g',
            'price': 100.0, 'manual_price': True,
            'saas_product_ids': [(6, 0, [product.id])],
        })
        partner = self.env['res.partner'].sudo().create({'name': 'ProfitTest Cust'})
        domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create({'name': 'profittest.example.com'})
        server = self.env['saas.server'].sudo().create({
            'name': 'profit-test-srv', 'cost_per_cpu_month': 5.0,
            'cost_per_gb_ram_month': 2.0, 'cost_per_gb_storage_month': 0.1})
        instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'profittest', 'domain_id': domain.id, 'partner_id': partner.id,
            'saas_product_id': product.id, 'plan_id': plan.id,
            'docker_server_id': server.id, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': False, 'state': 'running',
        })
        engine = self.env['saas.pricing.engine']
        expected = engine.profitability(instance.monthly_revenue, instance.monthly_cost)
        self.assertAlmostEqual(instance.monthly_margin, expected['profit'], places=2)
        self.assertAlmostEqual(instance.margin_pct, expected['margin_pct'], places=2)
        self.assertEqual(instance.is_profitable, expected['is_profitable'])
