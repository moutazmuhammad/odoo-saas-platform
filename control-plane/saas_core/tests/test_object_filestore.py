from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestObjectFilestore(TransactionCase):
    """Phase 2.1.3: object-storage filestore — server capability flag, the
    computed JuiceFS path.

    The 2.1.4 (DataService.migrate_filestore_to_object_store), 2.1.6
    (_hosting_clone_filestore) and 2.2 (immutable tenant image build:
    _tenant_base_image/_build_and_push_tenant_image/_image_build_cmd/
    rollback_image) test groups that used to live here were removed with
    the ssh_docker backend, as were the Docker-host template tests.
    """

    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or self.env['saas.product'].sudo().create(
            {'name': 'OF Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'OF Plan', 'is_custom': True, 'workers': 2, 'storage_limit': 10,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 50.0, 'yearly_price': 480.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.partner = self.env['res.partner'].sudo().create({'name': 'OF Cust'})
        self.domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create({'name': 'of.example.com'})

    def _instance(self, sub, server):
        return self.env['saas.instance'].sudo().create({
            'subdomain': sub, 'domain_id': self.domain.id, 'partner_id': self.partner.id,
            'saas_product_id': self.product.id, 'plan_id': self.plan.id,
            'docker_server_id': server.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running'})

    def test_no_mount_when_server_local(self):
        srv = self.env['saas.server'].sudo().create({'name': 'of-local'})
        inst = self._instance('oflocal', srv)
        self.assertEqual(inst._get_filestore_mount(), '')

    def test_mount_path_when_server_has_object_store(self):
        srv = self.env['saas.server'].sudo().create(
            {'name': 'of-obj', 'object_filestore_mount': '/mnt/jfs'})
        inst = self._instance('ofobj', srv)
        path = inst._get_filestore_mount()
        # <mount>/<partner>/<sub>/filestore, and stays under the mount
        self.assertTrue(path.startswith('/mnt/jfs/'))
        self.assertTrue(path.endswith('/ofobj/filestore'))
