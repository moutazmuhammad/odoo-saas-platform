from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestObjectFilestore(TransactionCase):
    """Phase 2.1.3: object-storage filestore — server capability flag, the
    computed JuiceFS path, and the conditional docker-compose volume.

    The 2.1.4 (DataService.migrate_filestore_to_object_store), 2.1.6
    (_hosting_clone_filestore) and 2.2 (immutable tenant image build:
    _tenant_base_image/_build_and_push_tenant_image/_image_build_cmd/
    rollback_image) test groups that used to live here were removed: all
    of those methods were ssh_docker-only (docker exec / SSH-based image
    build+push) and were removed along with that backend. The Jinja
    template-rendering tests (docker-compose.yml.jinja / odoo.conf.jinja /
    Dockerfile.tenant.jinja) are kept — they exercise _render_template
    directly with an explicit context, not the deleted orchestration
    methods, so they still pass and still document the templates'
    immutable-vs-legacy-mode behavior for whenever that pipeline is
    rebuilt for Kubernetes.
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

    def test_compose_includes_filestore_volume_only_when_set(self):
        inst = self._instance('ofrender', self.env['saas.server'].sudo().create(
            {'name': 'of-render'}))
        with_mount = inst._render_template('docker-compose.yml.jinja', {
            'odoo_version': '18.0', 'subdomain': 'ofrender', 'xmlrpc_port': 8069,
            'longpolling_port': 8072, 'filestore_mount': '/mnt/jfs/p/ofrender/filestore'})
        self.assertIn('/mnt/jfs/p/ofrender/filestore:/var/lib/odoo/filestore', with_mount)
        without = inst._render_template('docker-compose.yml.jinja', {
            'odoo_version': '18.0', 'subdomain': 'ofrender', 'xmlrpc_port': 8069,
            'longpolling_port': 8072, 'filestore_mount': ''})
        self.assertNotIn(':/var/lib/odoo/filestore', without)

    def test_compose_immutable_mode_skips_mounts(self):
        inst = self._instance('immut', self.env['saas.server'].sudo().create(
            {'name': 'immut-srv'}))
        img = '127.0.0.1:5000/tenant-immut@sha256:abc'
        immutable = inst._render_template('docker-compose.yml.jinja', {
            'odoo_version': '18.0', 'subdomain': 'immut', 'xmlrpc_port': 8069,
            'longpolling_port': 8072, 'tenant_image': img})
        self.assertIn('image: %s' % img, immutable)
        self.assertNotIn('/opt/odoo-source/', immutable)        # no source mount
        self.assertNotIn(':/mnt/extra-addons', immutable)       # no addons mount
        self.assertNotIn('requirements.txt:/etc/odoo', immutable)
        # legacy mode keeps the mounts
        legacy = inst._render_template('docker-compose.yml.jinja', {
            'odoo_version': '18.0', 'subdomain': 'immut', 'xmlrpc_port': 8069,
            'longpolling_port': 8072, 'odoo_image': 'odoo-light', 'tenant_image': ''})
        self.assertIn('/opt/odoo-source/18.0:/opt/odoo', legacy)
        self.assertIn(':/mnt/extra-addons', legacy)

    def test_immutable_addons_path_and_bake_dir(self):
        inst = self._instance('adn', self.env['saas.server'].sudo().create(
            {'name': 'adn-srv'}))
        # odoo.conf: immutable mode points addons_path at the baked /opt/tenant-addons
        conf_immut = inst._render_template('odoo.conf.jinja', {
            'immutable': True, 'repo_addons_paths': ['/opt/tenant-addons/myrepo']})
        self.assertIn('/opt/tenant-addons', conf_immut)
        self.assertNotIn('/mnt/extra-addons', conf_immut)
        conf_legacy = inst._render_template('odoo.conf.jinja', {
            'immutable': False, 'repo_addons_paths': []})
        self.assertIn('/mnt/extra-addons', conf_legacy)
        # Dockerfile bakes custom modules to the non-VOLUME /opt/tenant-addons
        df = inst._render_template('Dockerfile.tenant.jinja', {
            'base_image': 'b', 'pip_packages': False, 'has_addons': True})
        self.assertIn('/opt/tenant-addons', df)
        self.assertNotIn(':/mnt/extra-addons', df)
