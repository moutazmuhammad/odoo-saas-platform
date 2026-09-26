import json
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


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
            'name': 'Build Region', 'code': 'build-region',
            'registry_host': 'localhost:32000',
            'registry_push_host': 'registry.container-registry.svc:5000',
            'registry_prefix': 'acme', 'registry_insecure': True})
        self.server = self.env['saas.server'].sudo().create(
            {'name': 'build-k8s', 'compute_driver': 'kubernetes', 'region_id': self.region.id})
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
        for p in (patch.object(Instance, '_compute_driver', return_value=self.driver),
                  patch.object(Instance, '_compute_handle', return_value='HANDLE'),
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

    def test_region_without_registry_refuses(self):
        self.region.registry_host = False
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

    def test_nothing_to_bake_deploys_the_plain_version_image(self):
        self.repo.unlink()
        build = self._build(repo_id=False)
        self.instance._job_start_build(build.id)
        self.driver.start_image_build.assert_not_called()
        kw = self.driver.deploy_image.call_args.kwargs
        self.assertEqual((kw['repository'], kw['tag'], kw['addons_paths'], kw['modules']),
                         ('odoo', '18.0', [], []))
        self.assertIsNone(kw['registry_username'])

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
            registry_username='u', registry_password='p')
        ns, job = self.batch.create_namespaced_job.call_args.args
        self.assertEqual(ns, 'odoo-builds')
        spec = job['spec']['template']['spec']
        self.assertEqual([c['name'] for c in spec['initContainers']], ['fetch', 'inspect'])
        self.assertEqual(spec['containers'][0]['name'], 'build')
        self.assertFalse(spec['automountServiceAccountToken'])
        self.assertEqual(job['spec']['backoffLimit'], 0)
        # The token-bearing URL only ever comes from the Secret.
        self.assertNotIn('https://t@g/x.git', json.dumps(job))
        secret = self.core.create_namespaced_secret.call_args.args[1]
        self.assertEqual(secret.string_data['REPO_URL_0'], 'https://t@g/x.git')
        self.assertIn('reg.ns.svc:5000', secret.string_data['config.json'])
        # Secret + ConfigMap are owned by the Job (garbage-collected with it).
        owner = self.core.patch_namespaced_secret.call_args.args[2]['metadata']['ownerReferences'][0]
        self.assertEqual(owner['uid'], 'uid-1')
        # Egress policy allows the in-cluster registry's namespace.
        policy = self.net.replace_namespaced_network_policy.call_args.args[2]
        self.assertIn({'kubernetes.io/metadata.name': 'ns'},
                      [r['to'][0].get('namespaceSelector', {}).get('matchLabels')
                       for r in policy['spec']['egress']])

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
        self.assertEqual(patch_body['spec']['update'], {'token': 'build-1', 'modules': ['m']})
        self.assertEqual(patch_body['spec']['addonsPaths'], ['/opt/tenant-addons/x'])
        self.core.replace_namespaced_secret.assert_called_once()
