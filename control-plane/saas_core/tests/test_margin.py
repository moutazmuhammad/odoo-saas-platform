from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestTenantMargin(TransactionCase):
    """Phase 4: per-tenant margin = monthly revenue − infra cost (rate card)."""

    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or self.env['saas.product'].sudo().create(
            {'name': 'M Hosting', 'is_hosting': True, 'is_published': True})
        cur = self.env.company.currency_id
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'M Plan', 'is_custom': True, 'workers': 2, 'storage_limit': 10,
            'cpu_limit': 2.0, 'ram_limit': '4g', 'price': 100.0, 'yearly_price': 960.0,
            'currency_id': cur.id, 'saas_product_ids': [(6, 0, [self.product.id])]})
        self.partner = self.env['res.partner'].sudo().create({'name': 'M Cust'})
        self.domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create({'name': 'm.example.com'})
        # rate card: $5/core, $2/GB-RAM, $0.1/GB-storage per month
        self.server = self.env['saas.server'].sudo().create({
            'name': 'm-srv', 'cost_per_cpu_month': 5.0,
            'cost_per_gb_ram_month': 2.0, 'cost_per_gb_storage_month': 0.1})

    def _inst(self, sub, **kw):
        vals = {'subdomain': sub, 'domain_id': self.domain.id, 'partner_id': self.partner.id,
                'saas_product_id': self.product.id, 'plan_id': self.plan.id,
                'docker_server_id': self.server.id, 'billing_period': 'monthly',
                'environment': 'production', 'region_id': False, 'state': 'running'}
        vals.update(kw)
        return self.env['saas.instance'].sudo().create(vals)

    def test_cost_revenue_margin(self):
        inst = self._inst('mtest')
        # cost = 2 cores*5 + 4 GB*2 + 0 storage*0.1 = 10 + 8 = 18
        self.assertAlmostEqual(inst.monthly_cost, 18.0, places=2)
        # revenue = monthly plan price 100
        self.assertAlmostEqual(inst.monthly_revenue, 100.0, places=2)
        self.assertAlmostEqual(inst.monthly_margin, 82.0, places=2)
        self.assertAlmostEqual(inst.margin_pct, 82.0, places=1)
        self.assertTrue(inst.is_profitable)

    def test_yearly_revenue_normalized(self):
        inst = self._inst('myear', billing_period='yearly')
        # yearly 960 -> 80/mo
        self.assertAlmostEqual(inst.monthly_revenue, 80.0, places=2)

    def test_unprofitable_when_cost_exceeds_revenue(self):
        cheap = self.env['saas.plan'].sudo().create({
            'name': 'M Cheap', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 2.0, 'ram_limit': '4g', 'price': 5.0, 'yearly_price': 48.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        inst = self._inst('mloss', plan_id=cheap.id)
        self.assertAlmostEqual(inst.monthly_cost, 18.0, places=2)
        self.assertAlmostEqual(inst.monthly_margin, 5.0 - 18.0, places=2)
        self.assertFalse(inst.is_profitable)

    def test_unprofitable_alert_cron_flags_losses(self):
        cheap = self.env['saas.plan'].sudo().create({
            'name': 'M Loss', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 2.0, 'ram_limit': '4g', 'price': 5.0, 'yearly_price': 48.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        loss = self._inst('mloss2', plan_id=cheap.id)   # cost 18 > rev 5
        self._inst('mwin2')                              # profitable
        flagged = self.env['saas.instance'].sudo()._cron_flag_unprofitable_tenants()
        self.assertGreaterEqual(flagged, 1)
        self.assertFalse(loss.is_profitable)

    def test_child_cost_rolls_up_to_parent(self):
        prod = self._inst('mprod')
        child = self._inst('mstg', environment='staging', parent_id=prod.id)
        # parent cost includes its own + child's infra cost
        self.assertAlmostEqual(prod.monthly_cost, 36.0, places=2)  # 18 own + 18 child
        # child bills via parent -> own revenue 0
        self.assertAlmostEqual(child.monthly_revenue, 0.0, places=2)
        # parent revenue is the plan price (covers both)
        self.assertAlmostEqual(prod.monthly_revenue, 100.0, places=2)
