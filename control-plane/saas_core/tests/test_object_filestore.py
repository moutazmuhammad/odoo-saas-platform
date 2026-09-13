from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestObjectFilestore(TransactionCase):
    """Phase 2.1.3: object-storage filestore — server capability flag, the
    computed JuiceFS path, and the conditional docker-compose volume."""

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

    # -------- 2.1.4 DataService.migrate_filestore_to_object_store --------
    def test_migrate_raises_without_object_mount(self):
        inst = self._instance('ofnomnt', self.env['saas.server'].sudo().create(
            {'name': 'of-nomnt'}))
        with self.assertRaises(RuntimeError):
            inst._data_service().migrate_filestore_to_object_store(inst, recreate=False)

    def test_migrate_copies_local_to_object_store(self):
        srv = self.env['saas.server'].sudo().create(
            {'name': 'of-mig', 'object_filestore_mount': '/mnt/jfs',
             'docker_base_path': '/home/odoo'})
        inst = self._instance('ofmig', srv)

        cmds = []

        class FakeSSH:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, cmd, timeout=None):
                cmds.append(cmd); return (0, '', '')

        with patch.object(type(srv), '_get_ssh_connection', return_value=FakeSSH()), \
             patch.object(type(inst), '_get_container_uid', return_value='101'):
            dst = inst._data_service().migrate_filestore_to_object_store(
                inst, recreate=False)
        self.assertTrue(dst.endswith('/ofmig/filestore'))
        joined = '\n'.join(cmds)
        # copies CONTENTS of the local filestore into the object mount + chowns
        self.assertIn('cp -a', joined)
        self.assertIn('/data/odoo/filestore/.', joined)
        self.assertIn('chown -R 101:101', joined)

    # -------- 2.1.6 clone uses JuiceFS CoW on an object-store host --------
    def _clone_cmds(self, server):
        inst = self._instance('ofclone', server)
        cmds = []

        class FakeSSH:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, cmd, timeout=None):
                cmds.append(cmd); return (0, '', '')

        with patch.object(type(server), '_get_ssh_connection', return_value=FakeSSH()), \
             patch.object(type(inst), '_get_container_uid', return_value='101'):
            inst._hosting_clone_filestore('__tmpl', 'newdb')
        return inst, '\n'.join(cmds)

    def test_clone_uses_juicefs_cow_on_object_host(self):
        srv = self.env['saas.server'].sudo().create(
            {'name': 'of-clone-obj', 'object_filestore_mount': '/mnt/jfs',
             'docker_base_path': '/home/odoo'})
        inst, joined = self._clone_cmds(srv)
        self.assertIn('juicefs clone', joined)
        self.assertNotIn('cp -a', joined)
        # paths resolve under the object mount, not the local data dir
        self.assertIn('/mnt/jfs/', joined)
        self.assertEqual(inst._hosting_filestore_path('newdb'),
                         inst._get_filestore_mount() + '/newdb')

    def test_clone_uses_cp_on_local_host(self):
        srv = self.env['saas.server'].sudo().create(
            {'name': 'of-clone-local', 'docker_base_path': '/home/odoo'})
        inst, joined = self._clone_cmds(srv)
        self.assertIn('cp -a', joined)
        self.assertNotIn('juicefs clone', joined)
        self.assertIn('/data/odoo/filestore/newdb', inst._hosting_filestore_path('newdb'))

    # -------- 2.2: immutable tenant image — base ref + Dockerfile render -----
    def test_tenant_base_image_ref(self):
        srv = self.env['saas.server'].sudo().create(
            {'name': 'reg-srv', 'registry_host': '127.0.0.1:5000'})
        inst = self._instance('regtest', srv)
        # uses the version's image tag from saas.odoo.version
        ref = inst._tenant_base_image()
        self.assertTrue(ref.startswith('127.0.0.1:5000/odoo-base:'))
        # no registry configured -> legacy mode (empty)
        srv2 = self.env['saas.server'].sudo().create({'name': 'noreg-srv'})
        self.assertEqual(self._instance('noregtest', srv2)._tenant_base_image(), '')

    def test_render_tenant_dockerfile(self):
        srv = self.env['saas.server'].sudo().create(
            {'name': 'reg-srv2', 'registry_host': '127.0.0.1:5000'})
        inst = self._instance('dftest', srv)
        df = inst._render_tenant_dockerfile()
        self.assertIn('FROM 127.0.0.1:5000/odoo-base:', df)
        self.assertIn('USER odoo', df)

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
            {'name': 'adn-srv', 'registry_host': '127.0.0.1:5000'}))
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

    def test_build_cmd_uses_egress_restricted_sandbox(self):
        inst = self._instance('sbx', self.env['saas.server'].sudo().create(
            {'name': 'sbx-srv', 'registry_host': '127.0.0.1:5000'}))
        cmd = inst._image_build_cmd('/ctx', '127.0.0.1:5000/tenant-sbx:abc')
        # untrusted RUN steps confined to the egress-restricted network,
        # legacy builder (BuildKit can't take a custom --network)
        self.assertIn('--network saas-build', cmd)
        self.assertIn('DOCKER_BUILDKIT=0', cmd)

    def test_rollback_requires_successful_build_with_image(self):
        inst = self._instance('rbk', self.env['saas.server'].sudo().create(
            {'name': 'rbk-srv', 'registry_host': '127.0.0.1:5000'}))
        bad = self.env['saas.build'].sudo().create(
            {'instance_id': inst.id, 'source': 'redeploy', 'state': 'failed'})
        from odoo.exceptions import UserError
        with self.assertRaises(UserError):
            inst.rollback_image(bad)
