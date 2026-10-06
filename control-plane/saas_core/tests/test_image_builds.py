import datetime
import json
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from .test_kubernetes_driver import _handle


@tagged('post_install', '-at_install')
class TestImageBuildPipeline(TransactionCase):
    """Customer Git repos → in-cluster image build → zero-downtime rollout
    (saas_instance_build.py), with the Kubernetes driver mocked."""

    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().create(
            {'name': 'Build Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'Build Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.region = self.env['saas.region'].sudo().create({
            'name': 'Build Region', 'code': 'build-region'})
        self.server = self.env['saas.server'].sudo().create({
            'name': 'build-k8s', 'compute_driver': 'kubernetes', 'region_id': self.region.id,
            'registry_host': 'localhost:32000',
            'registry_push_host': 'registry.container-registry.svc:5000',
            'registry_prefix': 'acme', 'registry_insecure': True})
        self.version = self.env['saas.odoo.version'].sudo().create({
            'name': '18.0', 'docker_image': 'odoo', 'docker_image_tag': '18.0',
            'nginx_template': 'new', 'is_hosting_version': True})
        domain = self.env['saas.based.domain'].sudo().create({'name': 'build.example.com'})
        partner = self.env['res.partner'].sudo().create({'name': 'Build Cust'})
        self.instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'bldinst', 'domain_id': domain.id, 'partner_id': partner.id,
            'saas_product_id': self.product.id, 'plan_id': self.plan.id,
            'docker_server_id': self.server.id, 'odoo_version_id': self.version.id,
            'environment': 'production', 'region_id': False, 'state': 'running'})
        self.repo = self.env['saas.instance.repo'].sudo().create({
            'instance_id': self.instance.id, 'repo_url': 'https://github.com/acme/app.git',
            'branch': 'main', 'github_token': 'tok123'})
        self.driver = MagicMock()
        self.driver.cleanup_build.return_value = None
        Instance = type(self.instance)
        self.customer_dbs = [{'name': 'bldinst_prod', 'admin_login': 'admin'},
                             {'name': 'bldinst_test', 'admin_login': 'admin'}]
        for p in (patch.object(Instance, '_compute_driver', return_value=self.driver),
                  patch.object(Instance, '_compute_handle', return_value='HANDLE'),
                  patch.object(Instance, 'hosting_db_list', lambda rec: self.customer_dbs),
                  patch.object(type(self.env['saas.job']), '_spawn_worker', lambda s: None)):
            p.start()
            self.addCleanup(p.stop)

    def _jobs(self, method):
        return self.env['saas.job'].sudo().search(
            [('model', '=', 'saas.instance'), ('res_id', '=', self.instance.id),
             ('method', '=', method)])

    def _build(self, **vals):
        return self.env['saas.build'].sudo().create({
            'instance_id': self.instance.id, 'repo_id': self.repo.id, 'branch': 'main',
            'source': 'push', 'state': 'running', **vals})

    def _succeeded(self, modules=None, subdir='addons'):
        return {'state': 'succeeded', 'digest': 'sha256:abc', 'log': 'ok',
                'result': {'repos': [{'dir': 'app', 'subdir': subdir, 'sha': 'deadbeef'}],
                           'modules': modules or {'my_mod': '18.0.1.0'}}}

    # ---- entry point --------------------------------------------------
    def test_build_and_deploy_queues_the_start_step(self):
        build = self.instance.action_build_and_deploy('redeploy')
        self.assertEqual(build.state, 'running')
        self.assertEqual(build.stage, 'queued')
        job = self._jobs('_job_start_build')
        self.assertEqual(json.loads(job.args_json), [build.id])
        self.assertEqual(job.channel, 'deploy')
        self.assertEqual(job.lock_key, 'build:%s' % self.instance.id)
        self.assertTrue(job.idempotent, "a dead worker must retry, not fail, the build")

    def test_cluster_without_registry_refuses(self):
        self.server.registry_host = False
        with self.assertRaises(UserError):
            self.instance.action_build_and_deploy('redeploy')

    def test_not_running_records_nothing(self):
        self.instance.state = 'stopped'
        self.assertFalse(self.instance.action_build_and_deploy('redeploy'))

    def test_new_build_supersedes_a_running_one(self):
        old = self._build(stage='building', job_name='build-old')
        new = self.instance.action_build_and_deploy('redeploy')
        self.assertEqual(old.state, 'failed')
        self.assertIn('#%d' % new.id, old.log)
        self.driver.cleanup_build.assert_called_once_with('build-old')

    # ---- build step ---------------------------------------------------
    def test_start_build_creates_the_job(self):
        build = self._build(commit_sha='c0ffee')
        self.instance._job_start_build(build.id)
        kw = self.driver.start_image_build.call_args.kwargs
        tag = '18.0-b%d' % build.id
        self.assertEqual(kw['image_ref'],
                         'registry.container-registry.svc:5000/acme/tenant-bldinst:%s' % tag)
        self.assertTrue(kw['registry_insecure'])
        self.assertEqual(kw['base_image'], 'odoo:18.0')
        self.assertIn('FROM odoo:18.0', kw['dockerfile'])
        self.assertEqual(kw['repos'], [{
            'url': 'https://x-access-token:tok123@github.com/acme/app.git',
            'ref': 'c0ffee', 'branch': 'main', 'dir': 'app'}])
        self.assertEqual(build.stage, 'building')
        self.assertEqual(build.image_ref, 'localhost:32000/acme/tenant-bldinst:%s' % tag)
        self.assertTrue(self._jobs('_job_poll_build').run_after)

    def test_modules_to_update_only_changed(self):
        """With a previous successful build on record, the next deploy's
        ``odoo -u`` list is small and surgical — unchanged modules are
        not upgraded again (expensive on large customer DBs)."""
        prev = self._build(
            state='success',
            module_versions=json.dumps({
                'base': '18.0.1.0', 'sale': '18.0.1.0', 'stock': '18.0.1.0'}))
        build = self._build(
            module_versions=json.dumps({
                'base': '18.0.1.0', 'sale': '18.0.2.0', 'stock': '18.0.1.0'}))
        modules = self.instance._modules_to_update(build, {
            'base': '18.0.1.0', 'sale': '18.0.2.0', 'stock': '18.0.1.0'})
        self.assertEqual(modules, ['sale'])

    def test_build_does_not_request_registry_cache(self):
        self._build(state='success', image_digest='localhost:32000/acme/tenant-bldinst@sha256:abc')
        build = self._build()
        self.instance._job_start_build(build.id)
        self.assertNotIn('cache_ref', self.driver.start_image_build.call_args.kwargs)

    def test_modules_to_update_empty_when_unchanged(self):
        """Identical module_versions as the last success → no module
        upgrade, the tenant updates just pick the new image."""
        prev = self._build(
            state='success',
            module_versions=json.dumps({
                'base': '18.0.1.0', 'sale': '18.0.1.0'}))
        build = self._build(
            module_versions=json.dumps({
                'base': '18.0.1.0', 'sale': '18.0.1.0'}))
        modules = self.instance._modules_to_update(build, {
            'base': '18.0.1.0', 'sale': '18.0.1.0'})
        self.assertEqual(modules, [])

    def test_only_source_changed_module_is_upgraded_without_version_bump(self):
        versions = {
            'sale_custom': {'version': '18.0.1.0', 'sha256': 'old-source'},
            'stock_custom': {'version': '18.0.1.0', 'sha256': 'unchanged-source'},
        }
        self._build(state='success', module_versions=json.dumps(versions))
        build = self._build()
        current = {**versions, 'sale_custom': {'version': '18.0.1.0', 'sha256': 'new-source'}}
        self.assertEqual(self.instance._modules_to_update(build, current), ['sale_custom'])
        self.assertEqual(self.instance._modules_to_update(build, versions), [])

    def test_legacy_build_compares_serving_source_before_upgrading(self):
        from ..drivers.base import ExecResult
        self._build(state='success', addons_paths='["/opt/tenant-addons/app"]',
                    module_versions=json.dumps({'sale_custom': '18.0.1.0', 'stock_custom': '18.0.1.0'}))
        self.driver.exec.return_value = ExecResult(
            rc=0, stdout=json.dumps({'sale_custom': 'old', 'stock_custom': 'same'}), stderr='')
        build = self._build()
        versions = {'sale_custom': {'version': '18.0.1.0', 'sha256': 'new'},
                    'stock_custom': {'version': '18.0.1.0', 'sha256': 'same'}}
        self.assertEqual(self.instance._modules_to_update(build, versions), ['sale_custom'])
        self.assertIn('module_fingerprint', self.driver.exec.call_args.args[1])

    def test_upgrade_fingerprint_skips_logic_assets_and_bytecode(self):
        import tempfile
        from pathlib import Path
        from ..models.saas_instance_build import _BUILD_TEMPLATES
        namespace = {}
        exec(_BUILD_TEMPLATES.get_template('module_fingerprint.py').render(), namespace)
        fingerprint = namespace['module_fingerprint']
        with tempfile.TemporaryDirectory() as temp:
            module = Path(temp)
            (module / '__manifest__.py').write_text("{'version': '18.0.1.0', 'data': ['views.xml']}")
            (module / 'models.py').write_text('value = 1')
            original = fingerprint(temp)
            (module / '__pycache__').mkdir()
            (module / '__pycache__' / 'models.pyc').write_bytes(b'generated')
            self.assertEqual(fingerprint(temp), original)
            (module / 'models.py').write_text('value = 2')
            self.assertEqual(fingerprint(temp), original)
            (module / 'static').mkdir()
            (module / 'static' / 'app.js').write_text('console.log(1)')
            self.assertEqual(fingerprint(temp), original)
            (module / 'models.py').write_text(
                "from odoo import models, fields\nclass Record(models.Model):\n"
                "    _inherit = 'sale.order'\n    custom_name = fields.Char()\n"
                "    def business_logic(self):\n        return 1\n")
            changed = fingerprint(temp)
            self.assertNotEqual(changed, original)
            (module / 'models.py').write_text((module / 'models.py').read_text().replace('return 1', 'return 2'))
            self.assertEqual(fingerprint(temp), changed)
            (module / 'views.xml').write_text('<odoo/>')
            self.assertNotEqual(fingerprint(temp), changed)

    def test_upgrade_fingerprint_detects_field_changes_and_migrations(self):
        import tempfile
        from pathlib import Path
        from ..models.saas_instance_build import _BUILD_TEMPLATES
        namespace = {}
        exec(_BUILD_TEMPLATES.get_template('module_fingerprint.py').render(), namespace)
        fingerprint = namespace['module_fingerprint']
        with tempfile.TemporaryDirectory() as temp:
            module = Path(temp)
            (module / '__manifest__.py').write_text("{'version': '18.0.1.0'}")
            model = module / 'models.py'
            model.write_text("from odoo import fields as f\nclass Model:\n    value = f.Char()")
            previous = fingerprint(temp)
            model.write_text(model.read_text().replace('f.Char()', 'f.Integer()'))
            changed = fingerprint(temp)
            self.assertNotEqual(changed, previous)
            (module / 'migrations').mkdir()
            (module / 'migrations' / 'post.py').write_text('def migrate(cr, version):\n    pass')
            self.assertNotEqual(fingerprint(temp), changed)

    def test_nothing_to_bake_deploys_the_plain_version_image(self):
        self.repo.unlink()
        build = self._build(repo_id=False)
        self.instance._job_start_build(build.id)
        self.driver.start_image_build.assert_not_called()
        kw = self.driver.deploy_image.call_args.kwargs
        self.assertEqual((kw['repository'], kw['tag'], kw['addons_paths'], kw['modules']),
                         ('odoo', '18.0', [], []))
        self.assertIsNone(kw['registry_username'])

    def test_deploy_upgrades_hosting_customer_databases(self):
        build = self._build(image_ref='localhost:32000/acme/tenant-bldinst:18.0-b1')
        self.instance._deploy_build(
            build, self.driver, repository='localhost:32000/acme/tenant-bldinst',
            tag='18.0-b1', addons_paths=[], module_versions={}, modules=['my_mod'])
        kw = self.driver.deploy_image.call_args.kwargs
        self.assertEqual(kw['databases'], ['bldinst_prod', 'bldinst_test'])
        self.driver.set_hosting_access.assert_called_once_with('HANDLE', '^bldinst_.+$', 'bldinst_', max_databases=1)

    def test_deploy_without_modules_lists_no_databases(self):
        build = self._build(image_ref='localhost:32000/acme/tenant-bldinst:18.0-b1')
        self.instance._deploy_build(
            build, self.driver, repository='localhost:32000/acme/tenant-bldinst',
            tag='18.0-b1', addons_paths=[], module_versions={}, modules=[])
        self.assertEqual(self.driver.deploy_image.call_args.kwargs['databases'], [])

    def test_poll_build_reschedules_while_running(self):
        build = self._build(stage='building', job_name='j1')
        self.driver.build_status.return_value = {'state': 'running'}
        self.instance._job_poll_build(build.id)
        self.assertEqual(build.state, 'running')
        self.assertTrue(self._jobs('_job_poll_build'))

    def test_failed_build_leaves_the_instance_alone(self):
        self.repo.state = 'pending'
        build = self._build(stage='building', job_name='j1')
        self.driver.build_status.return_value = {'state': 'failed', 'log': 'pip: no such package'}
        self.instance._job_poll_build(build.id)
        self.assertEqual(build.state, 'failed')
        self.assertIn('pip: no such package', build.log)
        self.assertEqual(self.repo.state, 'error')
        self.driver.deploy_image.assert_not_called()
        self.driver.cleanup_build.assert_called_with('j1')

    def test_succeeded_build_hands_off_to_the_operator(self):
        build = self._build(stage='building', job_name='j1',
                            image_ref='localhost:32000/acme/tenant-bldinst:18.0-b9')
        self.driver.build_status.return_value = self._succeeded()
        self.instance._job_poll_build(build.id)
        kw = self.driver.deploy_image.call_args.kwargs
        self.assertEqual(kw['repository'], 'localhost:32000/acme/tenant-bldinst')
        self.assertEqual(kw['tag'], '18.0-b9')
        self.assertEqual(kw['addons_paths'], ['/opt/tenant-addons/app/addons'])
        self.assertEqual(kw['update_token'], 'build-%d' % build.id)
        # No previous successful build: upgrade everything in the repos.
        self.assertEqual(kw['modules'], ['my_mod'])
        self.assertEqual(build.stage, 'deploying')
        self.assertEqual(build.commit_sha, 'deadbeef')
        self.assertEqual(build.image_digest, 'localhost:32000/acme/tenant-bldinst@sha256:abc')
        self.assertEqual((self.repo.state, self.repo.addons_subdir), ('cloned', 'addons'))

    def test_only_changed_modules_are_upgraded(self):
        self._build(state='success', module_versions=json.dumps(
            {'my_mod': '18.0.1.0', 'other': '18.0.1.0'}))
        build = self._build(stage='building', job_name='j2', image_ref='r:t')
        self.driver.build_status.return_value = self._succeeded(
            modules={'my_mod': '18.0.1.0', 'other': '18.0.1.1', 'brand_new': '1.0'})
        self.instance._job_poll_build(build.id)
        self.assertEqual(self.driver.deploy_image.call_args.kwargs['modules'],
                         ['brand_new', 'other'])

    # ---- rollout step -------------------------------------------------
    def test_rollout_success_when_the_operator_applied_it(self):
        build = self._build(stage='deploying', image_ref='localhost:32000/acme/tenant-bldinst:18.0-b1')
        self.driver.update_status.return_value = {
            'state': 'applied', 'applied_token': 'build-%d' % build.id, 'message': '',
            'phase': 'Ready', 'observed_image': build.image_ref}
        self.instance._job_poll_rollout(build.id)
        self.assertEqual(build.state, 'success')
        self.assertEqual(self.instance.deploy_image, build.image_ref)

    def test_rollout_waits_for_the_new_pods(self):
        build = self._build(stage='deploying', image_ref='img:new')
        self.driver.update_status.return_value = {
            'state': 'applied', 'applied_token': 'build-%d' % build.id, 'message': '',
            'phase': 'Updating', 'observed_image': 'img:new'}
        self.instance._job_poll_rollout(build.id)
        self.assertEqual(build.state, 'running')
        self.assertTrue(self._jobs('_job_poll_rollout'))

    def test_failed_module_upgrade_fails_the_build(self):
        build = self._build(stage='deploying', image_ref='img:new')
        self.driver.update_status.return_value = {
            'state': 'failed', 'applied_token': 'build-0', 'message': 'update Job failed',
            'phase': 'Ready', 'observed_image': 'img:old'}
        self.driver.update_job_log.return_value = 'ParseError in my_mod/views.xml'
        self.instance._job_poll_rollout(build.id)
        self.assertEqual(build.state, 'failed')
        self.assertIn('ParseError', build.log)
        self.assertIn('previous version is still serving', build.log)

    # ---- rollback / webhook / validation ---------------------------------
    def test_rollback_redeploys_an_old_image_without_upgrades(self):
        good = self._build(state='success', image_ref='localhost:32000/acme/tenant-bldinst:18.0-b3',
                           addons_paths=json.dumps(['/opt/tenant-addons/app']),
                           module_versions='{}')
        rb = good.action_rollback()
        self.assertEqual(rb.source, 'rollback')
        kw = self.driver.deploy_image.call_args.kwargs
        self.assertEqual((kw['tag'], kw['modules'], kw['addons_paths']),
                         ('18.0-b3', [], ['/opt/tenant-addons/app']))
        self.assertEqual(kw['update_token'], 'build-%d' % rb.id)

    def test_webhook_push_to_stopped_instance_fails_the_build(self):
        self.instance.state = 'stopped'
        build = self._build(commit_sha='abc')
        self.repo._run_webhook_deploy(build.id)
        self.assertEqual(build.state, 'failed')

    def test_webhook_push_starts_the_pipeline_for_the_pushed_commit(self):
        build = self._build(commit_sha='abc')
        self.repo._run_webhook_deploy(build.id)
        self.assertEqual(build.state, 'running')
        self.assertTrue(self._jobs('_job_start_build'))

    def test_validate_repo_rejects_missing_branch(self):
        proc = MagicMock(returncode=0, stdout='abc\trefs/heads/main\n', stderr='')
        with patch('odoo.addons.saas_core.models.saas_instance_build.subprocess.run',
                   return_value=proc) as run:
            self.instance._validate_repo_before_change(
                'https://github.com/acme/app.git', 'main', 'tok')
            with self.assertRaises(UserError):
                self.instance._validate_repo_before_change(
                    'https://github.com/acme/app.git', 'nope', 'tok')
        self.assertIn('https://x-access-token:tok@github.com/acme/app.git', run.call_args.args[0])

    def test_validate_repo_hides_the_token_on_error(self):
        proc = MagicMock(returncode=128, stdout='', stderr='fatal: https://x:tok@x denied')
        with patch('odoo.addons.saas_core.models.saas_instance_build.subprocess.run',
                   return_value=proc):
            with self.assertRaises(UserError) as cm:
                self.instance._validate_repo_before_change(
                    'https://github.com/acme/app.git', 'main', 'tok')
        self.assertNotIn('tok', str(cm.exception).replace('token', ''))


    def test_history_retention_removes_old_records_but_keeps_rollback_and_active(self):
        Build = self.env['saas.build'].sudo()
        old = fields.Datetime.now() - datetime.timedelta(days=40)
        expired = self._build(state='failed', date_done=old)
        baseline = self._build(state='success', date_done=old, image_digest='registry@sha256:previous', module_versions='{}')
        current = self._build(state='success', date_done=old, image_digest='registry@sha256:current', module_versions='{}')
        repeated = self._build(state='success', date_done=old, image_digest='registry@sha256:current')
        active = self._build(date_start=old)
        Build._cron_cleanup_history()
        self.assertFalse(expired.exists())
        self.assertTrue(current.exists())  # Latest module fingerprint baseline.
        self.assertTrue(baseline.exists())
        self.assertTrue(repeated.exists())
        self.assertTrue(active.exists())

    def test_history_retention_caps_completed_records_per_instance(self):
        self.env['ir.config_parameter'].sudo().set_param('saas_master.build_history_limit', 3)
        builds = [self._build(state='failed', date_done=fields.Datetime.now()) for _ in range(6)]
        self.env['saas.build']._cron_cleanup_history()
        self.assertFalse(any(build.exists() for build in builds[:3]))
        self.assertTrue(all(build.exists() for build in builds[3:]))
        self.assertEqual(self.env['saas.build']._cron_cleanup_history(), 0)

    def test_history_retention_preserves_pending_worker_references(self):
        old = fields.Datetime.now() - datetime.timedelta(days=40)
        build = self._build(state='failed', date_done=old)
        self.env['saas.job'].enqueue(self.instance, '_job_poll_rollout', args=(build.id,), run_now=False)
        self.env['saas.build']._cron_cleanup_history()
        self.assertTrue(build.exists())

    def test_history_retention_invalid_settings_use_safe_defaults(self):
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('saas_master.build_history_days', '-1')
        params.set_param('saas_master.build_history_limit', 'invalid')
        self.assertEqual(self.env['saas.build']._history_retention_policy(), (30, 50))


@tagged('post_install', '-at_install')
class TestImageBuildDriver(TransactionCase):
    """KubernetesDriver's build-Job / deploy_image primitives against a mocked
    Kubernetes API."""

    def setUp(self):
        super().setUp()
        from odoo.addons.saas_core.tests.test_kubernetes_driver import _make_driver
        self.driver, _server = _make_driver()
        self.core, self.batch, self.custom, self.net = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        self.driver._core_api = MagicMock(return_value=self.core)
        self.driver._batch_api = MagicMock(return_value=self.batch)
        self.driver._custom_api = MagicMock(return_value=self.custom)
        self.driver._networking_api = MagicMock(return_value=self.net)
        self.batch.create_namespaced_job.return_value.metadata.uid = 'uid-1'

    def test_start_image_build_job_shape(self):
        self.driver.start_image_build(
            name='build-1-acme', repos=[{'url': 'https://t@g/x.git', 'ref': 'abc',
                                         'branch': 'main', 'dir': 'x'}],
            dockerfile='FROM odoo:18.0', requirements='requests', base_image='odoo:18.0',
            image_ref='reg.ns.svc:5000/tenant-acme:18.0-b1',
            builder_image='moby/buildkit:rootless', git_image='alpine/git',
            registry_host='reg', registry_push_host='reg.ns.svc:5000',
            registry_username='u', registry_password='p',
            tenant_key='Acme-Test', registry_insecure=True)
        ns, job = self.batch.create_namespaced_job.call_args.args
        self.assertEqual(ns, 'odoo-builds')
        spec = job['spec']['template']['spec']
        self.assertEqual([c['name'] for c in spec['initContainers']], ['fetch', 'inspect'])
        self.assertEqual(spec['containers'][0]['name'], 'build')
        self.assertFalse(spec['automountServiceAccountToken'])
        self.assertEqual(spec['securityContext']['fsGroup'], 1000)
        self.assertEqual(spec['securityContext']['fsGroupChangePolicy'], 'OnRootMismatch')
        self.assertEqual(job['spec']['backoffLimit'], 0)
        build_env = {e['name']: e['value'] for e in spec['containers'][0]['env']}
        self.assertNotIn('CACHE_IMAGE_REF', build_env)
        config = self.core.create_namespaced_config_map.call_args.args[1].data
        import tomllib
        settings = tomllib.loads(config['buildkitd.toml'])
        self.assertEqual(settings['registry']['reg.ns.svc:5000']['http'], True)
        self.assertEqual(settings['root'], '/home/user/.local/share/buildkit')
        self.assertEqual(settings['worker']['oci']['gckeepstorage'], '8GB')
        self.assertNotIn('--export-cache', config['build.sh'])
        self.assertNotIn('--import-cache', config['build.sh'])
        self.assertIn('--opt no-cache', config['build.sh'])
        # Init containers run sequentially, but their largest request counts
        # for the pod's entire lifetime. They must not reserve their CPU limits
        # and prevent a build fitting into a node with 500m CPU available.
        containers = spec['initContainers'] + spec['containers']
        cpu_requests = [int(c['resources']['requests']['cpu'].rstrip('m'))
                        for c in containers]
        self.assertLessEqual(max(cpu_requests), 500)
        for c in spec['initContainers']:
            self.assertEqual(c['resources']['requests']['memory'], '128Mi')
        # The token-bearing URL only ever comes from the Secret.
        self.assertNotIn('https://t@g/x.git', json.dumps(job))
        secret = self.core.create_namespaced_secret.call_args.args[1]
        self.assertEqual(secret.string_data['REPO_URL_0'], 'https://t@g/x.git')
        self.assertIn('reg.ns.svc:5000', secret.string_data['config.json'])
        # Secret + ConfigMap are owned by the Job (garbage-collected with it).
        owner = self.core.patch_namespaced_secret.call_args.args[2]['metadata']['ownerReferences'][0]
        self.assertEqual(owner['uid'], 'uid-1')
        # Build scratch is temporary; no cache disk is provisioned.
        buildkit_vol = next(v for v in spec['volumes'] if v['name'] == 'buildkit')
        self.assertEqual(buildkit_vol['emptyDir']['sizeLimit'], '20Gi')
        self.assertFalse(any('persistentVolumeClaim' in v for v in spec['volumes']))
        self.core.create_namespaced_persistent_volume_claim.assert_not_called()
        self.assertEqual(spec['containers'][0]['resources']['requests']['ephemeral-storage'], '4Gi')
        self.assertEqual(spec['containers'][0]['resources']['limits']['ephemeral-storage'], '24Gi')
        # Egress policy allows the in-cluster registry's namespace.
        policy = self.net.replace_namespaced_network_policy.call_args.args[2]
        self.assertIn({'kubernetes.io/metadata.name': 'ns'},
                      [r['to'][0].get('namespaceSelector', {}).get('matchLabels')
                       for r in policy['spec']['egress']])

    def test_build_pod_pulls_with_registry_credentials(self):
        self.driver.start_image_build(
            name='build-3-acme', repos=[], dockerfile='FROM x', requirements='',
            base_image='registry.example.com/odoo:18.0', image_ref='reg.ns.svc:5000/t:1',
            builder_image='registry.example.com/buildkit', git_image='registry.example.com/git',
            registry_host='registry.example.com', registry_push_host='reg.ns.svc:5000',
            registry_username='u', registry_password='p')
        job = self.batch.create_namespaced_job.call_args.args[1]
        spec = job['spec']['template']['spec']
        self.assertEqual(spec['imagePullSecrets'], [{'name': 'build-3-acme-pull'}])
        secrets = {c.args[1].metadata.name: c.args[1]
                   for c in self.core.create_namespaced_secret.call_args_list}
        pull = secrets['build-3-acme-pull']
        self.assertEqual(pull.type, 'kubernetes.io/dockerconfigjson')
        self.assertEqual(set(json.loads(pull.string_data['.dockerconfigjson'])['auths']),
                         {'registry.example.com'})
        # BuildKit can push to the push host and pull FROM the pull host.
        auths = json.loads(secrets['build-3-acme'].string_data['config.json'])['auths']
        self.assertEqual(set(auths), {'reg.ns.svc:5000', 'registry.example.com'})
        # Both Secrets are cleaned up with the Job.
        self.assertEqual({c.args[0] for c in self.core.patch_namespaced_secret.call_args_list},
                         {'build-3-acme', 'build-3-acme-pull'})

    def test_build_pod_without_credentials_has_no_pull_secret(self):
        self.driver.start_image_build(
            name='build-4-acme', repos=[], dockerfile='FROM x', requirements='',
            base_image='x', image_ref='r/x:t', builder_image='b', git_image='g',
            registry_host='r')
        spec = self.batch.create_namespaced_job.call_args.args[1]['spec']['template']['spec']
        self.assertNotIn('imagePullSecrets', spec)
        self.core.create_namespaced_secret.assert_called_once()

    def test_start_image_build_is_idempotent_on_retry(self):
        from kubernetes.client.rest import ApiException
        self.core.create_namespaced_secret.side_effect = ApiException(status=409)
        self.core.create_namespaced_config_map.side_effect = ApiException(status=409)
        self.batch.create_namespaced_job.side_effect = ApiException(status=409)
        self.batch.read_namespaced_job.return_value.metadata.uid = 'uid-existing'
        self.driver.start_image_build(
            name='build-2-acme', repos=[], dockerfile='FROM x', requirements='',
            base_image='x', image_ref='r/x:t', builder_image='b', git_image='g')
        self.core.replace_namespaced_secret.assert_called_once()
        self.core.replace_namespaced_config_map.assert_called_once()
        owner = self.core.patch_namespaced_secret.call_args.args[2]['metadata']['ownerReferences'][0]
        self.assertEqual(owner['uid'], 'uid-existing')

    def test_build_status_parses_the_markers(self):
        job = MagicMock()
        job.status.succeeded = 1
        self.batch.read_namespaced_job.return_value = job
        pod = MagicMock()
        pod.metadata.name = 'build-1-pod'
        self.core.list_namespaced_pod.return_value.items = [pod]
        self.core.read_namespaced_pod_log.return_value = (
            'noise\nSAAS_BUILD_DIGEST sha256:ff\n'
            'SAAS_BUILD_RESULT {"repos":[{"dir":"x","subdir":"","sha":"a"}],"modules":{"m":"1"}}\n')
        st = self.driver.build_status('build-1')
        self.assertEqual(st['state'], 'succeeded')
        self.assertEqual(st['digest'], 'sha256:ff')
        self.assertEqual(st['result']['modules'], {'m': '1'})

    def test_deploy_image_patch(self):
        from odoo.addons.saas_core.drivers.base import ComputeHandle
        handle = ComputeHandle(server_id=7, container_name='odoo_acme',
                               instance_path='odoo-tenant-odoo-acme', http_port=8069)
        self.driver.deploy_image(handle, repository='reg/tenant-acme', tag='18.0-b1',
                                 addons_paths=['/opt/tenant-addons/x'], update_token='build-1',
                                 modules=['m'], registry_host='reg', registry_username='u',
                                 registry_password='p')
        patch_body = self.custom.patch_cluster_custom_object.call_args.args[4]
        self.assertEqual(patch_body['spec']['image'], {
            'repository': 'reg/tenant-acme', 'tag': '18.0-b1',
            'pullSecretRefs': [{'name': 'tenant-registry'}]})
        self.assertEqual(patch_body['spec']['update'],
                         {'token': 'build-1', 'modules': ['m'], 'databases': None})
        self.assertEqual(patch_body['spec']['addonsPaths'], ['/opt/tenant-addons/x'])
        self.core.replace_namespaced_secret.assert_called_once()

    def test_plain_image_deploy_uses_the_cluster_credentials(self):
        """A rollback to the plain version image keeps the pull Secret when
        the cluster has a private registry, and drops it otherwise."""
        self.driver.server._registry_pull_auth.return_value = ('reg.example.com', 'u', 'p')
        self.driver.deploy_image(_handle(), repository='reg.example.com/odoo', tag='18.0',
                                 addons_paths=[], update_token='build-2', modules=[])
        patch_body = self.custom.patch_cluster_custom_object.call_args.args[4]
        self.assertEqual(patch_body['spec']['image']['pullSecretRefs'], [{'name': 'tenant-registry'}])
        secret = self.core.replace_namespaced_secret.call_args.args[2]
        self.assertIn('reg.example.com', secret.string_data['.dockerconfigjson'])

        self.driver.server._registry_pull_auth.return_value = None
        self.driver.deploy_image(_handle(), repository='odoo', tag='18.0',
                                 addons_paths=[], update_token='build-3', modules=[])
        patch_body = self.custom.patch_cluster_custom_object.call_args.args[4]
        self.assertIsNone(patch_body['spec']['image']['pullSecretRefs'])

    def _cr(self, generation, applied, cond_reason, cond_generation):
        self.custom.get_cluster_custom_object.return_value = {
            'metadata': {'generation': generation},
            'spec': {'update': {'token': 'build-2'}},
            'status': {'appliedUpdateToken': applied, 'conditions': [{
                'type': 'UpdateReady', 'reason': cond_reason,
                'observedGeneration': cond_generation, 'message': 'boom'}]}}

    def test_update_status_ignores_a_previous_updates_failure(self):
        # build-1 failed at generation 4; build-2 was just requested (gen 5)
        # and the operator hasn't reconciled it yet.
        self._cr(5, 'build-0', 'UpdateFailed', 4)
        st = self.driver.update_status(_handle())
        self.assertEqual(st['state'], 'running')
        self.assertEqual(st['message'], '')

    def test_update_status_reports_a_current_failure(self):
        self._cr(5, 'build-0', 'UpdateFailed', 5)
        self.assertEqual(self.driver.update_status(_handle())['state'], 'failed')

    def test_update_status_applied(self):
        self._cr(5, 'build-2', 'UpdateApplied', 5)
        self.assertEqual(self.driver.update_status(_handle())['state'], 'applied')


@tagged('post_install', '-at_install')
class TestDockerConfigJson(TransactionCase):
    def test_docker_hub_gets_legacy_index_key(self):
        from ..drivers.k8s_builds import docker_config_json
        auths = json.loads(docker_config_json('docker.io', 'u', 'p'))['auths']
        self.assertEqual(set(auths), {'docker.io', 'https://index.docker.io/v1/'})
        self.assertEqual(set(json.loads(docker_config_json('ghcr.io', 'u', 'p'))['auths']), {'ghcr.io'})

    def test_several_hosts_share_the_credentials(self):
        from ..drivers.k8s_builds import docker_config_json
        auths = json.loads(docker_config_json(['docker.io', 'ghcr.io', None], 'u', 'p'))['auths']
        self.assertEqual(set(auths), {'docker.io', 'ghcr.io', 'https://index.docker.io/v1/'})
        self.assertEqual(auths['docker.io'], auths['ghcr.io'])
