from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestDeploymentAwareNavigation(TransactionCase):
    """billing/pricing architecture redesign, Part 4/5/7: saas.instance's
    compute_driver convenience field and the Kubernetes Instances
    list-filter action.

    The sibling "Compose Instances" action/menu item (domain
    compute_driver='ssh_docker') was removed along with the ssh_docker
    backend — that Selection value no longer exists, so a second compute
    backend to filter by/switch between no longer exists either.
    """

    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().create(
            {'name': 'DeployNavSvc', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'DeployNavPlan', 'is_custom': True, 'workers': 1,
            'storage_limit': 5, 'cpu_limit': 1.0, 'ram_limit': '1g',
            'price': 10.0, 'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.partner = self.env['res.partner'].sudo().create({'name': 'DeployNav Cust'})
        self.domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create({'name': 'deploynav.example.com'})
        self.k8s_server = self.env['saas.server'].sudo().create(
            {'name': 'deploynav-k8s', 'compute_driver': 'kubernetes'})

    def _inst(self, sub, server):
        return self.env['saas.instance'].sudo().create({
            'subdomain': sub, 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'docker_server_id': server.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running',
        })

    def test_compute_driver_follows_the_server(self):
        k8s_inst = self._inst('deploynavk8s', self.k8s_server)
        self.assertEqual(k8s_inst.compute_driver, 'kubernetes')

    def test_kubernetes_instances_action_domain_filters_correctly(self):
        k8s_inst = self._inst('deploynavk8sfilter', self.k8s_server)
        action = self.env.ref('saas_core.saas_instance_action_kubernetes')
        found = self.env['saas.instance'].search(eval(action.domain))
        self.assertIn(k8s_inst, found)
