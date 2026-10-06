from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestManagedPackage(TransactionCase):
    """saas.plan._package: what one instance reserves (Odoo pods +
    PostgreSQL + volumes + quota), the single source for deploy, capacity,
    cost and customer metrics."""

    def setUp(self):
        super().setUp()
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'Pkg Plan', 'is_custom': True, 'workers': 4, 'storage_limit': 20,
            'cpu_limit': 2.0, 'ram_limit': '2g', 'price': 40.0,
            'currency_id': self.env.company.currency_id.id})

    def test_database_sized_per_worker_with_floors(self):
        pkg = self.plan._package()
        self.assertEqual((pkg['db_cpu_m'], pkg['db_mem_mi']), (1000, 1024))  # 4 × 0.25 / 4 × 256
        self.plan.workers = 1
        pkg = self.plan._package()
        self.assertEqual((pkg['db_cpu_m'], pkg['db_mem_mi']), (250, 512))    # floors

    def test_package_totals_count_every_pod(self):
        pkg = self.plan._package()
        self.assertEqual((pkg['total_cpu_m'], pkg['total_mem_mi']), (3500, 3584))
        self.assertEqual(pkg['odoo_pods'], 1)
        self.assertEqual(pkg['replicas'], 1)
        self.plan.cpu_limit = 4.0
        self.plan.ram_limit = '4g'
        larger = self.plan._package()
        self.assertEqual(larger['odoo_pods'], 1)
        self.assertEqual((larger['total_cpu_m'], larger['total_mem_mi']), (5500, 5632))

    def test_quota_leaves_room_for_rollout_surge_and_jobs(self):
        pkg = self.plan._package(storage_gb=20)
        quota_cpu = int(pkg['quota']['limits.cpu'].rstrip('m'))
        # package + one surge Odoo pod + shells + one Job's headroom
        self.assertEqual(quota_cpu, 3500 + 2500 + 500 * 2 + 2000)
        self.assertEqual(pkg['quota']['requests.storage'], '21Gi')

    def test_settings_change_database_share(self):
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('saas_master.db_cpu_per_worker', '0.5')
        icp.set_param('saas_master.db_ram_per_worker', '512')
        pkg = self.plan._package()
        self.assertEqual((pkg['db_cpu_m'], pkg['db_mem_mi']), (2000, 2048))

    def test_capacity_counts_the_whole_package(self):
        server = self.env['saas.server'].sudo().create(
            {'name': 'pkg-srv', 'compute_driver': 'kubernetes', 'max_cpu_cores': 5.0})
        product = self.env['saas.product'].sudo().create({'name': 'Pkg Prod', 'is_hosting': True})
        domain = self.env['saas.based.domain'].sudo().create({'name': 'pkg.example.com'})
        partner = self.env['res.partner'].sudo().create({'name': 'Pkg Cust'})
        self.env['saas.instance'].sudo().create({
            'subdomain': 'pkgone', 'domain_id': domain.id, 'partner_id': partner.id,
            'saas_product_id': product.id, 'plan_id': self.plan.id,
            'docker_server_id': server.id, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': False, 'state': 'running'})
        server.invalidate_recordset()
        self.assertEqual(server.allocated_cpu, 3.5)       # Odoo 2 + cron 0.5 + database 1
        self.assertEqual(server.allocated_ram_gb, 3.5)
        # A second 3.5-core package would exceed the 5-core cluster.
        self.assertFalse(server._has_capacity_for(self.plan))

    def test_cron_limits_are_smaller_and_capped(self):
        pkg = self.plan._package()
        self.assertEqual((pkg['cron_cpu_m'], pkg['cron_mem_mi']), (500, 512))
        self.plan.cpu_limit = 8.0
        self.plan.ram_limit = '16g'
        larger = self.plan._package()
        self.assertEqual((larger['cron_cpu_m'], larger['cron_mem_mi']), (500, 512))
        self.assertEqual(larger['odoo_pods'], 1)
