from unittest.mock import MagicMock, patch

from kubernetes.client.rest import ApiException

from odoo.tests.common import TransactionCase, tagged


def _make_driver(kubeconfig='apiVersion: v1\nkind: Config\n'):
    """A KubernetesDriver over a MagicMock saas.server, with the lazy
    Kubernetes API client already short-circuited (see _client()'s
    `self._api_client is not None` early-return) so tests never need a
    real kubeconfig/cluster — they instead control _custom_api()/_core_api()
    directly, the actual API-call boundary."""
    from odoo.addons.saas_core.drivers.kubernetes_driver import KubernetesDriver
    server = MagicMock()
    server.id = 7
    server.name = 'k8s-test-server'
    server._kubeconfig_yaml.return_value = kubeconfig
    server._registry_pull_auth.return_value = None  # public images only
    server._toolbox_image.return_value = 'alpine/k8s:1.35.6'
    driver = KubernetesDriver(server)
    driver._api_client = MagicMock()
    return driver, server


def _handle(namespace='odoo-tenant-odoo-acme'):
    from odoo.addons.saas_core.drivers.base import ComputeHandle
    return ComputeHandle(server_id=7, container_name='odoo_acme',
                         instance_path=namespace, http_port=8069)


def _fake_pod(phase='Running', restart_count=0, waiting_reason=None, name='odoo-acme-xyz'):
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.deletion_timestamp = None
    pod.status.conditions = [MagicMock(type="Ready", status="True")]
    pod.status.phase = phase
    cs = MagicMock()
    cs.name = 'odoo'
    cs.ready = True
    cs.restart_count = restart_count
    if waiting_reason:
        cs.state.waiting.reason = waiting_reason
    else:
        cs.state.waiting = None
    pod.status.container_statuses = [cs]
    return pod


@tagged('post_install', '-at_install')
class TestKubernetesDriver(TransactionCase):
    """Phase 2.1: a REAL Kubernetes-API-backed ComputeDriver, managing the
    Compute Service operator's OdooInstance CRD — not raw kubectl/YAML
    strings over SSH (the prior stub, never run against a live cluster).
    Mocked at the kubernetes-client API boundary (_custom_api()/_core_api()),
    not at a shell/SSH layer, since there is no shell layer anymore."""

    # -------- the interface is fully satisfied (ABC enforces this) --------
    def test_implements_full_compute_driver_interface(self):
        from odoo.addons.saas_core.drivers.kubernetes_driver import KubernetesDriver
        from odoo.addons.saas_core.drivers.base import ComputeDriver
        driver, _server = _make_driver()
        self.assertIsInstance(driver, ComputeDriver)

    # -------- kubeconfig loading ---------------------------------------
    def test_missing_kubeconfig_raises_clear_error(self):
        driver, _server = _make_driver(kubeconfig='')
        driver._api_client = None  # force the lazy-load path to actually run
        with self.assertRaises(RuntimeError) as cm:
            driver.stop(_handle())
        self.assertIn('kubeconfig', str(cm.exception))

    def test_loads_kubeconfig_via_real_client_config(self):
        """Exercises the actual (non-bypassed) client-construction path,
        proving the YAML->kubernetes.client.Configuration wiring itself
        works, not just the tests that bypass it."""
        driver, _server = _make_driver(
            kubeconfig='apiVersion: v1\nclusters: []\ncontexts: []\n'
                      'current-context: ""\nkind: Config\nusers: []\n')
        driver._api_client = None
        with patch('odoo.addons.saas_core.drivers.kubernetes_driver.k8s_config'
                  '.load_kube_config_from_dict') as load_mock:
            client = driver._client()
        self.assertTrue(load_mock.called)
        self.assertIsNotNone(client)

    # -------- create() --------------------------------------------------
    def test_create_builds_and_posts_odoo_instance_cr(self):
        from odoo.addons.saas_core.drivers.base import ComputeSpec
        driver, server = _make_driver()
        custom_api = MagicMock()
        driver._custom_api = MagicMock(return_value=custom_api)

        spec = ComputeSpec(
            container_name='odoo_acme', image='registry.example.com/odoo:18.0-abc123',
            instance_path='/unused', http_port=8069, longpolling_port=8072,
            db_name='acme', db_host='db',
            env={'domain': 'acme.example.com', 'odoo_version': '18.0',
                'tls_enabled': True, 'filestore_size': '10Gi'})
        handle = driver.create(spec)

        custom_api.create_cluster_custom_object.assert_called_once()
        args = custom_api.create_cluster_custom_object.call_args.args
        self.assertEqual(args[0], 'saas.odoo.example.com')
        self.assertEqual(args[1], 'v1alpha1')
        self.assertEqual(args[2], 'odooinstances')
        body = args[3]
        self.assertEqual(body['metadata']['name'], 'odoo-acme')
        self.assertEqual(body['spec']['version'], '18.0')
        self.assertEqual(body['spec']['image'],
                         {'repository': 'registry.example.com/odoo', 'tag': '18.0-abc123'})
        self.assertEqual(body['spec']['domain']['hostname'], 'acme.example.com')
        self.assertTrue(body['spec']['domain']['tls']['enabled'])
        self.assertEqual(body['spec']['storage']['filestore']['size'], '10Gi')
        self.assertTrue(body['spec']['storage']['sharedWithDatabase'])

        self.assertEqual(handle.server_id, server.id)
        self.assertEqual(handle.container_name, 'odoo_acme')
        self.assertEqual(handle.instance_path, 'odoo-tenant-odoo-acme')

    # -------- private registry: tenant pull Secret --------------------------
    def _spec(self):
        from odoo.addons.saas_core.drivers.base import ComputeSpec
        return ComputeSpec(
            container_name='odoo_acme', image='reg.example.com/odoo:18.0', instance_path='/x',
            http_port=8069, longpolling_port=8072, db_name='acme', db_host='db',
            env={'domain': 'acme.example.com'})

    def test_create_without_registry_credentials_sets_no_pull_secret(self):
        driver, _server = _make_driver()
        custom, core = MagicMock(), MagicMock()
        driver._custom_api = MagicMock(return_value=custom)
        driver._core_api = MagicMock(return_value=core)
        driver.create(self._spec())
        body = custom.create_cluster_custom_object.call_args.args[3]
        self.assertNotIn('pullSecretRefs', body['spec']['image'])
        core.replace_namespaced_secret.assert_not_called()
        core.create_namespaced_secret.assert_not_called()

    def test_create_with_registry_credentials_writes_pull_secret_once_namespace_exists(self):
        driver, server = _make_driver()
        server._registry_pull_auth.return_value = ('reg.example.com', 'u', 'p')
        custom, core = MagicMock(), MagicMock()
        driver._custom_api = MagicMock(return_value=custom)
        driver._core_api = MagicMock(return_value=core)
        # Right after create the operator hasn't made the namespace yet.
        core.replace_namespaced_secret.side_effect = ApiException(status=404)
        core.create_namespaced_secret.side_effect = ApiException(status=404)
        handle = driver.create(self._spec())
        body = custom.create_cluster_custom_object.call_args.args[3]
        self.assertEqual(body['spec']['image']['pullSecretRefs'], [{'name': 'tenant-registry'}])
        core.create_namespaced_secret.assert_called_once()

        # The provisioning wait loop's health() polls write it once it can.
        core.replace_namespaced_secret.side_effect = None
        driver._get_cr = MagicMock(return_value={'status': {'phase': 'Provisioning'}})
        driver._first_pod = MagicMock(return_value=None)
        driver.health(handle)
        name, ns, secret = core.replace_namespaced_secret.call_args.args
        self.assertEqual((name, ns), ('tenant-registry', 'odoo-tenant-odoo-acme'))
        self.assertEqual(secret.type, 'kubernetes.io/dockerconfigjson')
        self.assertIn('reg.example.com', secret.string_data['.dockerconfigjson'])
        driver.health(handle)
        self.assertEqual(core.replace_namespaced_secret.call_count, 2)  # not again

    def test_health_rewrites_pull_secret_on_image_pull_error(self):
        driver, server = _make_driver()
        server._registry_pull_auth.return_value = ('reg.example.com', 'u', 'p')
        core = MagicMock()
        driver._core_api = MagicMock(return_value=core)
        driver._get_cr = MagicMock(return_value={'status': {'phase': 'Provisioning'}})
        driver._first_pod = MagicMock(return_value=_fake_pod(waiting_reason='ImagePullBackOff'))
        driver.health(_handle())
        driver.health(_handle())
        self.assertEqual(core.replace_namespaced_secret.call_count, 2)

    def test_health_pull_secret_failure_does_not_break_health(self):
        driver, server = _make_driver()
        server._registry_pull_auth.return_value = ('reg.example.com', 'u', 'p')
        core = MagicMock()
        core.replace_namespaced_secret.side_effect = ApiException(status=403)
        driver._core_api = MagicMock(return_value=core)
        driver._get_cr = MagicMock(return_value={'status': {'phase': 'Provisioning'}})
        driver._first_pod = MagicMock(return_value=None)
        self.assertEqual(driver.health(_handle()).status, 'missing')

    def test_create_sets_database_filter_when_given(self):
        from odoo.addons.saas_core.drivers.base import ComputeSpec
        driver, _server = _make_driver()
        custom_api = MagicMock()
        driver._custom_api = MagicMock(return_value=custom_api)
        base_env = {'domain': 'acme.example.com'}
        for env, expected in ((base_env, None),
                              (dict(base_env, database_filter='^acme_.+$'), '^acme_.+$')):
            driver.create(ComputeSpec(
                container_name='odoo_acme', image='odoo:18.0', instance_path='/x',
                http_port=8069, longpolling_port=8072, db_name='acme', db_host='db',
                env=env))
            body = custom_api.create_cluster_custom_object.call_args.args[3]
            self.assertEqual(body['spec'].get('databaseFilter'), expected)

    def test_create_turns_on_hosting_access_when_given(self):
        from odoo.addons.saas_core.drivers.base import ComputeSpec
        driver, _server = _make_driver()
        custom_api = MagicMock()
        driver._custom_api = MagicMock(return_value=custom_api)
        for env, dbm, shell in (
                ({'domain': 'acme.example.com'}, None, None),
                ({'domain': 'acme.example.com', 'db_manager_prefix': 'acme_', 'shell': True},
                 {'prefix': 'acme_', 'maxDatabases': 0}, True)):
            driver.create(ComputeSpec(
                container_name='odoo_acme', image='odoo:18.0', instance_path='/x',
                http_port=8069, longpolling_port=8072, db_name='acme', db_host='db',
                env=env))
            spec = custom_api.create_cluster_custom_object.call_args.args[3]['spec']
            self.assertEqual(spec.get('databaseManager'), dbm)
            self.assertEqual(spec.get('shell'), shell)

    def test_create_defaults_to_one_replica_and_rwo_storage(self):
        from odoo.addons.saas_core.drivers.base import ComputeSpec
        driver, _server = _make_driver()
        custom_api = MagicMock()
        driver._custom_api = MagicMock(return_value=custom_api)
        spec = ComputeSpec(
            container_name='odoo_solo', image='odoo:18.0', instance_path='/x',
            http_port=8069, longpolling_port=8072, db_name='solo', db_host='db',
            env={'domain': 'solo.example.com'})
        driver.create(spec)
        body = custom_api.create_cluster_custom_object.call_args.args[3]
        self.assertEqual(body['spec']['replicas'], 1)
        self.assertNotIn('accessMode', body['spec']['storage']['filestore'])

    def test_create_rejects_horizontal_scaling(self):
        from odoo.addons.saas_core.drivers.base import ComputeSpec
        driver, _server = _make_driver()
        driver._custom_api = MagicMock()
        with self.assertRaises(ValueError):
            driver._build_odoo_instance('odoo-acme', ComputeSpec(
                container_name='odoo-acme', image='odoo:18.0', instance_path='',
                http_port=8069, longpolling_port=8072, db_name='acme', db_host='db',
                env={'domain': 'acme.example.com', 'replicas': 2}))
        driver._custom_api().create_cluster_custom_object.assert_not_called()

    def test_trigger_backup_waits_for_the_operator_cronjob(self):
        """A just-enabled backup's CronJob appears asynchronously: wait for
        it instead of failing (this made every final snapshot fail on a
        tenant that never had backups)."""
        driver, _server = _make_driver()
        batch = MagicMock()
        cron = MagicMock()
        batch.read_namespaced_cron_job.side_effect = [
            ApiException(status=404), ApiException(status=404), cron]
        driver._batch_api = MagicMock(return_value=batch)
        with patch('odoo.addons.saas_core.drivers.kubernetes_driver.time.sleep'):
            name = driver.trigger_backup_now(_handle())
        self.assertTrue(name.startswith('odoo-backup-manual-'))
        batch.create_namespaced_job.assert_called_once()

    def test_trigger_backup_rejects_suspended_schedule(self):
        driver, _server = _make_driver()
        batch = MagicMock()
        cron = MagicMock()
        cron.spec.suspend = True
        batch.read_namespaced_cron_job.return_value = cron
        driver._batch_api = MagicMock(return_value=batch)
        with self.assertRaisesRegex(RuntimeError, 'Backups are paused'):
            driver.trigger_backup_now(_handle())
        batch.create_namespaced_job.assert_not_called()

    def test_trigger_backup_gives_up_after_the_wait(self):
        driver, _server = _make_driver()
        batch = MagicMock()
        batch.read_namespaced_cron_job.side_effect = ApiException(status=404)
        driver._batch_api = MagicMock(return_value=batch)
        with self.assertRaises(RuntimeError):
            driver.trigger_backup_now(_handle(), wait_seconds=0)

    def test_set_package_patches_everything_in_one_go(self):
        driver, _server = _make_driver()
        custom = MagicMock()
        driver._custom_api = MagicMock(return_value=custom)
        driver.set_package(_handle(), {
            'cpu_request': '250m', 'cpu_limit': '1000m', 'mem_request': '256Mi',
            'mem_limit': '1024Mi', 'workers': 2,
            'db_cpu_request': '100m', 'db_cpu_limit': '500m',
            'db_mem_request': '128Mi', 'db_mem_limit': '512Mi',
            'filestore_size': '10Gi', 'db_storage_size': '10Gi',
            'quota': {'limits.cpu': '5000m'}})
        spec = custom.patch_cluster_custom_object.call_args.args[4]['spec']
        self.assertEqual(spec['resources']['limits'], {'cpu': '1000m', 'memory': '1024Mi'})
        self.assertEqual(spec['workers'], {'count': 2})
        self.assertEqual(spec['database'], {
            'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'},
                          'limits': {'cpu': '500m', 'memory': '512Mi'}},
            'storage': {'size': '10Gi'}})
        self.assertEqual(spec['storage'], {'filestore': {'size': '10Gi'}})
        self.assertEqual(spec['tenancy'], {'resourceQuota': {'limits.cpu': '5000m'}})

    def test_set_resources_patches_resources_and_workers(self):
        driver, _server = _make_driver()
        custom_api = MagicMock()
        driver._custom_api = MagicMock(return_value=custom_api)
        driver.set_resources(_handle(), cpu_request='250m', cpu_limit='1000m',
                             mem_request='256Mi', mem_limit='1024Mi', workers=40)
        custom_api.patch_cluster_custom_object.assert_called_once_with(
            'saas.odoo.example.com', 'v1alpha1', 'odooinstances', 'odoo-acme',
            {'spec': {
                'resources': {
                    'requests': {'cpu': '250m', 'memory': '256Mi'},
                    'limits': {'cpu': '1000m', 'memory': '1024Mi'}},
                # clamped to the CRD maximum
                'workers': {'count': 32}}})

    def test_create_sets_explicit_workers_including_zero(self):
        from odoo.addons.saas_core.drivers.base import ComputeSpec
        driver, _server = _make_driver()
        custom_api = MagicMock()
        driver._custom_api = MagicMock(return_value=custom_api)
        driver.create(ComputeSpec(
            container_name='odoo_w', image='odoo:18.0', instance_path='/x',
            http_port=8069, longpolling_port=8072, db_name='w', db_host='db',
            env={'domain': 'w.example.com', 'workers': 0, 'cpu_limit': '500m'}))
        spec = custom_api.create_cluster_custom_object.call_args.args[3]['spec']
        self.assertEqual(spec['workers'], {'count': 0, 'maxCronThreads': 1})
        self.assertEqual(spec['resources']['limits']['cpu'], '500m')


    def test_create_requires_domain(self):
        from odoo.addons.saas_core.drivers.base import ComputeSpec
        driver, _server = _make_driver()
        driver._custom_api = MagicMock()
        spec = ComputeSpec(
            container_name='odoo_nodom', image='odoo:18.0', instance_path='/x',
            http_port=8069, longpolling_port=8072, db_name='x', db_host='db')
        with self.assertRaises(RuntimeError):
            driver.create(spec)

    def test_create_raises_on_api_error(self):
        """A non-409 API error always raises — no idempotency special-case."""
        from odoo.addons.saas_core.drivers.base import ComputeSpec
        driver, _server = _make_driver()
        custom_api = MagicMock()
        custom_api.create_cluster_custom_object.side_effect = ApiException(status=500, reason='InternalError')
        driver._custom_api = MagicMock(return_value=custom_api)
        spec = ComputeSpec(
            container_name='odoo_dup', image='odoo:18.0', instance_path='/x',
            http_port=8069, longpolling_port=8072, db_name='x', db_host='db',
            env={'domain': 'dup.example.com'})
        with self.assertRaises(RuntimeError):
            driver.create(spec)

    def _dup_spec(self):
        from odoo.addons.saas_core.drivers.base import ComputeSpec
        return ComputeSpec(
            container_name='odoo_dup', image='odoo:18.0', instance_path='/x',
            http_port=8069, longpolling_port=8072, db_name='x', db_host='db',
            env={'domain': 'dup.example.com'})

    def test_create_is_idempotent_when_cr_already_exists(self):
        """A retried create() (e.g. after Odoo's own cron-worker time
        limit orphaned the original attempt mid-poll — see
        KubernetesDriver.create()'s own docstring) must succeed, not
        raise, when the CR it's retrying already exists for real —
        verified by actually reading it back, not just trusting the
        409's wording."""
        driver, _server = _make_driver()
        custom_api = MagicMock()
        custom_api.create_cluster_custom_object.side_effect = ApiException(status=409, reason='AlreadyExists')
        custom_api.get_cluster_custom_object.return_value = {
            'status': {'phase': 'Ready'},
        }
        driver._custom_api = MagicMock(return_value=custom_api)
        handle = driver.create(self._dup_spec())
        self.assertEqual(handle.container_name, 'odoo_dup')

    def test_create_raises_on_409_when_cr_does_not_actually_exist(self):
        """A 409 whose follow-up read finds nothing (raced with a delete,
        or a genuine name collision with an unrelated object) must still
        raise — the idempotent-retry path only kicks in when the CR is
        actually there."""
        driver, _server = _make_driver()
        custom_api = MagicMock()
        custom_api.create_cluster_custom_object.side_effect = ApiException(status=409, reason='AlreadyExists')
        custom_api.get_cluster_custom_object.side_effect = ApiException(status=404, reason='NotFound')
        driver._custom_api = MagicMock(return_value=custom_api)
        with self.assertRaises(RuntimeError):
            driver.create(self._dup_spec())

    # -------- create() with a restore source ------------------------------
    def test_create_with_restore_source_sets_spec_restore(self):
        """A caller can hand a restore source through spec.env['restore']
        (see _build_odoo_instance's docstring) — this must translate
        exactly onto RestoreSourceSpec's field names
        (compute/operator/api/v1alpha1/odooinstance_types.go). The
        ssh_docker -> Kubernetes migrate_to_kubernetes() caller that used
        to build such a spec was removed along with that backend, but this
        driver-level capability is still real infrastructure — the
        planned CR-based backup/restore replacement (see the removal
        plan's Phase 5) is expected to reuse exactly this mapping."""
        from odoo.addons.saas_core.drivers.base import ComputeSpec
        driver, _server = _make_driver()
        custom_api = MagicMock()
        driver._custom_api = MagicMock(return_value=custom_api)

        spec = ComputeSpec(
            container_name='odoo_acme_k8s', image='odoo:18.0',
            instance_path='/unused', http_port=8069, longpolling_port=8072,
            db_name='acme-k8s', db_host='',
            env={
                'domain': 'acme-k8s.example.com',
                'restore': {
                    'bucket': 'saas-backups',
                    'prefix': 'acme-k8s',
                    'secret_name': 'odoo-acme-k8s-restore-creds',
                    'backup_id': '20260101T000000Z',
                },
            })
        driver.create(spec)

        body = custom_api.create_cluster_custom_object.call_args.args[3]
        self.assertEqual(body['spec']['restore'], {'source': {
            'type': 'ObjectStorage',
            'bucket': 'saas-backups',
            'prefix': 'acme-k8s',
            'objectStorageSecretRef': {'name': 'odoo-acme-k8s-restore-creds'},
            'backupId': '20260101T000000Z',
        }})

    def test_create_without_restore_source_omits_spec_restore(self):
        from odoo.addons.saas_core.drivers.base import ComputeSpec
        driver, _server = _make_driver()
        custom_api = MagicMock()
        driver._custom_api = MagicMock(return_value=custom_api)
        spec = ComputeSpec(
            container_name='odoo_acme', image='odoo:18.0', instance_path='/x',
            http_port=8069, longpolling_port=8072, db_name='acme', db_host='db',
            env={'domain': 'acme.example.com'})
        driver.create(spec)
        body = custom_api.create_cluster_custom_object.call_args.args[3]
        self.assertNotIn('restore', body['spec'])

    # -------- destroy() ---------------------------------------------------
    def test_destroy_deletes_cr(self):
        driver, _server = _make_driver()
        custom_api = MagicMock()
        driver._custom_api = MagicMock(return_value=custom_api)
        driver.destroy(_handle())
        custom_api.delete_cluster_custom_object.assert_called_once_with(
            'saas.odoo.example.com', 'v1alpha1', 'odooinstances', 'odoo-acme')

    def test_destroy_tolerates_already_gone(self):
        driver, _server = _make_driver()
        custom_api = MagicMock()
        custom_api.delete_cluster_custom_object.side_effect = ApiException(status=404)
        driver._custom_api = MagicMock(return_value=custom_api)
        driver.destroy(_handle())  # must not raise

    def test_destroy_raises_on_real_error(self):
        driver, _server = _make_driver()
        custom_api = MagicMock()
        custom_api.delete_cluster_custom_object.side_effect = ApiException(status=500)
        driver._custom_api = MagicMock(return_value=custom_api)
        with self.assertRaises(RuntimeError):
            driver.destroy(_handle())

    # -------- start()/stop()/restart() ------------------------------------
    def test_start_and_stop_patch_suspended(self):
        driver, _server = _make_driver()
        custom_api = MagicMock()
        driver._custom_api = MagicMock(return_value=custom_api)
        h = _handle()

        driver.stop(h)
        custom_api.patch_cluster_custom_object.assert_called_with(
            'saas.odoo.example.com', 'v1alpha1', 'odooinstances', 'odoo-acme',
            {'spec': {'suspended': True}})

        driver.start(h)
        custom_api.patch_cluster_custom_object.assert_called_with(
            'saas.odoo.example.com', 'v1alpha1', 'odooinstances', 'odoo-acme',
            {'spec': {'suspended': False}})

    def test_restart_is_a_rolling_restart_not_stop_start(self):
        """Zero downtime: stamps the pod template instead of suspending."""
        driver, _server = _make_driver()
        driver._get_cr = MagicMock(return_value={'spec': {'suspended': False}})
        driver.stop = MagicMock()
        apps = MagicMock()
        dep = MagicMock()
        dep.metadata.name = 'odoo-acme'
        apps.list_namespaced_deployment.return_value.items = [dep]
        driver._apps_api = MagicMock(return_value=apps)
        driver.restart(_handle())
        driver.stop.assert_not_called()
        apps.list_namespaced_deployment.assert_called_once_with(
            'odoo-tenant-odoo-acme',
            label_selector='app.kubernetes.io/name=odoo,app.kubernetes.io/instance=odoo-acme')
        name, ns, patch = apps.patch_namespaced_deployment.call_args.args
        self.assertEqual((name, ns), ('odoo-acme', 'odoo-tenant-odoo-acme'))
        self.assertIn('kubectl.kubernetes.io/restartedAt',
                      patch['spec']['template']['metadata']['annotations'])

    def test_restart_resumes_a_suspended_instance(self):
        driver, _server = _make_driver()
        driver._get_cr = MagicMock(return_value={'spec': {'suspended': True}})
        driver._patch_suspended = MagicMock()
        driver._apps_api = MagicMock()
        driver.restart(_handle())
        driver._patch_suspended.assert_called_once_with(_handle(), False)
        driver._apps_api.assert_not_called()

    def test_restart_raises_without_deployment(self):
        driver, _server = _make_driver()
        driver._get_cr = MagicMock(return_value={'spec': {}})
        apps = MagicMock()
        apps.list_namespaced_deployment.return_value.items = []
        driver._apps_api = MagicMock(return_value=apps)
        with self.assertRaises(RuntimeError):
            driver.restart(_handle())

    # -------- health() ------------------------------------------------------
    def test_health_maps_ready_phase_to_running(self):
        driver, _server = _make_driver()
        driver._get_cr = MagicMock(return_value={'status': {'phase': 'Ready'}})
        driver._first_pod = MagicMock(return_value=_fake_pod(restart_count=2))
        hs = driver.health(_handle())
        self.assertTrue(hs.running)
        self.assertEqual(hs.status, 'running')
        self.assertEqual(hs.restart_count, 2)

    def test_health_ready_phase_with_missing_pod_is_not_running(self):
        driver, _server = _make_driver()
        driver._get_cr = MagicMock(return_value={'status': {'phase': 'Ready'}})
        driver._first_pod = MagicMock(return_value=None)
        # 'missing', not 'not_found': the operator recreates the pod, so the
        # reconciler must not call start() for an evicted/rescheduled pod.
        self.assertEqual(driver.health(_handle()).status, 'missing')

    def test_health_ready_phase_with_unready_pod_is_not_running(self):
        driver, _server = _make_driver()
        driver._get_cr = MagicMock(return_value={'status': {'phase': 'Ready'}})
        pod = _fake_pod()
        pod.status.container_statuses[0].ready = False
        driver._first_pod = MagicMock(return_value=pod)
        health = driver.health(_handle())
        self.assertFalse(health.running)
        self.assertEqual(health.status, 'starting')

    def test_health_serving_pod_overrides_stale_phase(self):
        driver, _server = _make_driver()
        driver._get_cr = MagicMock(return_value={'status': {'phase': 'Degraded'}})
        driver._first_pod = MagicMock(return_value=_fake_pod())
        self.assertTrue(driver.health(_handle()).running)

    def test_health_terminating_pod_is_not_running(self):
        driver, _server = _make_driver()
        driver._get_cr = MagicMock(return_value={'status': {'phase': 'Ready'}})
        pod = _fake_pod()
        pod.metadata.deletion_timestamp = '2026-10-06T00:00:00Z'
        driver._first_pod = MagicMock(return_value=pod)
        health = driver.health(_handle())
        self.assertFalse(health.running)
        self.assertEqual(health.status, 'terminating')

    def test_health_suspension_waits_for_live_pod_to_stop(self):
        driver, _server = _make_driver()
        driver._get_cr = MagicMock(return_value={'spec': {'suspended': True}, 'status': {'phase': 'Suspended'}})
        driver._first_pod = MagicMock(return_value=_fake_pod())
        self.assertTrue(driver.health(_handle()).running)
        driver._first_pod.return_value = None
        self.assertEqual(driver.health(_handle()).status, 'exited')

    def test_first_pod_prefers_ready_replica_during_rollout(self):
        driver, _server = _make_driver()
        new, serving = _fake_pod(), _fake_pod(name='serving')
        new.status.container_statuses[0].ready = False
        driver._core_api = MagicMock()
        driver._core_api().list_namespaced_pod.return_value.items = [new, serving]
        self.assertIs(driver._first_pod('tenant', 'acme'), serving)

    def test_health_not_found_when_cr_missing(self):
        driver, _server = _make_driver()
        driver._get_cr = MagicMock(return_value=None)
        hs = driver.health(_handle())
        self.assertFalse(hs.running)
        self.assertEqual(hs.status, 'not_found')

    def test_health_crash_loop_backoff_overrides_ready_phase(self):
        """A pod can be in CrashLoopBackOff while the CR's own coarser
        phase still says Ready (e.g. right after a bad redeploy, before
        the operator's own status catches up) — the pod-level signal must
        win, since this drives real crash-loop auto-stop logic
        (saas_instance.py's _CRASH_LOOP_THRESHOLD check)."""
        driver, _server = _make_driver()
        driver._get_cr = MagicMock(return_value={'status': {'phase': 'Ready'}})
        driver._first_pod = MagicMock(
            return_value=_fake_pod(restart_count=5, waiting_reason='CrashLoopBackOff'))
        hs = driver.health(_handle())
        self.assertEqual(hs.status, 'restarting')
        self.assertFalse(hs.running)
        self.assertEqual(hs.restart_count, 5)

    # -------- logs() ----------------------------------------------------
    def test_health_detects_cron_sidecar_crash_loop(self):
        driver, _server = _make_driver()
        driver._get_cr = MagicMock(return_value={'status': {'phase': 'Ready'}})
        pod = _fake_pod(restart_count=1)
        cron_pod = _fake_pod(restart_count=3, waiting_reason='CrashLoopBackOff')
        cron_status = cron_pod.status.container_statuses[0]
        cron_status.name = 'cron'
        pod.status.container_statuses.append(cron_status)
        driver._first_pod = MagicMock(return_value=pod)
        health = driver.health(_handle())
        self.assertEqual(health.status, 'restarting')
        self.assertFalse(health.running)
        self.assertEqual(health.restart_count, 4)

    def test_logs_reads_pod_log(self):
        driver, _server = _make_driver()
        driver._first_pod = MagicMock(return_value=_fake_pod())
        core_api = MagicMock()
        # Raw response (urllib3) body: decoded as text, newlines intact.
        core_api.read_namespaced_pod_log.return_value = MagicMock(data=b'hello\nfrom odoo\n')
        driver._core_api = MagicMock(return_value=core_api)
        self.assertEqual(driver.logs(_handle(), tail=50), 'hello\nfrom odoo\n')
        core_api.read_namespaced_pod_log.assert_called_once_with(
            'odoo-acme-xyz', 'odoo-tenant-odoo-acme', container='odoo', tail_lines=50,
            _preload_content=False)

    def test_logs_empty_when_no_pod(self):
        driver, _server = _make_driver()
        driver._first_pod = MagicMock(return_value=None)
        self.assertEqual(driver.logs(_handle()), '')

    # -------- exec() ------------------------------------------------------
    def test_exec_no_pod_returns_nonzero(self):
        driver, _server = _make_driver()
        driver._first_pod = MagicMock(return_value=None)
        result = driver.exec(_handle(), 'echo hi')
        self.assertFalse(result.ok)
        self.assertEqual(result.rc, 127)

    def test_exec_success(self):
        driver, _server = _make_driver()
        driver._first_pod = MagicMock(return_value=_fake_pod())
        driver._core_api = MagicMock(return_value=MagicMock())
        fake_resp = MagicMock()
        fake_resp.read_stdout.return_value = 'ok\n'
        fake_resp.read_stderr.return_value = ''
        fake_resp.returncode = 0
        with patch('odoo.addons.saas_core.drivers.kubernetes_driver.k8s_stream',
                  return_value=fake_resp):
            result = driver.exec(_handle(), 'echo ok')
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout, 'ok\n')

    # -------- database filter (hosting customer databases) ----------------
    # -------- staff cluster terminal (toolbox pod) -------------------------
    def _toolbox_driver(self, pods):
        """Driver whose read_namespaced_pod returns ``pods`` in turn."""
        from types import SimpleNamespace as NS
        from kubernetes.client.rest import ApiException
        driver, _server = _make_driver()
        core = MagicMock()

        def read_pod(*a, **k):
            phase = pods.pop(0)
            if phase is None:
                raise ApiException(status=404)
            return NS(status=NS(phase=phase))
        core.read_namespaced_pod.side_effect = read_pod
        driver._core_api = MagicMock(return_value=core)
        return driver, core

    def test_toolbox_requires_the_hand_made_service_account(self):
        from kubernetes.client.rest import ApiException
        from odoo.addons.saas_core.drivers.kubernetes_driver import ToolboxNotSetUp
        driver, core = self._toolbox_driver([])
        core.read_namespaced_service_account.side_effect = ApiException(status=404)
        with self.assertRaises(ToolboxNotSetUp):
            driver._ensure_toolbox_pod()
        core.create_namespaced_pod.assert_not_called()

    def test_toolbox_pod_started_when_missing_and_replaced_when_finished(self):
        with patch('odoo.addons.saas_core.drivers.kubernetes_driver.time.sleep'):
            driver, core = self._toolbox_driver([None, 'Pending', 'Running'])
            self.assertEqual(driver._ensure_toolbox_pod(), 'saas-toolbox')
            body = core.create_namespaced_pod.call_args.args[1]
            self.assertEqual(body['spec']['serviceAccountName'], 'saas-toolbox')
            self.assertTrue(body['spec']['activeDeadlineSeconds'] > 0)

            driver, core = self._toolbox_driver(['Succeeded', None, 'Running'])
            driver._ensure_toolbox_pod()
            core.delete_namespaced_pod.assert_called_once()
            core.create_namespaced_pod.assert_called_once()

    def test_toolbox_uses_cluster_image_and_registry_credentials(self):
        with patch('odoo.addons.saas_core.drivers.kubernetes_driver.time.sleep'):
            driver, core = self._toolbox_driver([None, 'Running'])
            driver.server._toolbox_image.return_value = 'reg.example.com/k8s:1.35.6'
            driver._ensure_toolbox_pod()
            body = core.create_namespaced_pod.call_args.args[1]
            self.assertEqual(body['spec']['containers'][0]['image'], 'reg.example.com/k8s:1.35.6')
            self.assertNotIn('imagePullSecrets', body['spec'])
            core.replace_namespaced_secret.assert_not_called()

            driver, core = self._toolbox_driver([None, 'Running'])
            driver.server._registry_pull_auth.return_value = ('reg.example.com', 'u', 'p')
            core.replace_namespaced_secret.side_effect = ApiException(status=404)
            driver._ensure_toolbox_pod()
            body = core.create_namespaced_pod.call_args.args[1]
            self.assertEqual(body['spec']['imagePullSecrets'], [{'name': 'saas-toolbox-registry'}])
            ns, secret = core.create_namespaced_secret.call_args.args
            self.assertEqual(ns, 'saas-toolbox')
            self.assertEqual(secret.metadata.name, 'saas-toolbox-registry')
            self.assertEqual(secret.type, 'kubernetes.io/dockerconfigjson')

    def test_toolbox_never_grants_permissions(self):
        """Role bindings are an operator's manual step (setup/03a step 12.4, 03b step 7.2)."""
        import inspect
        from odoo.addons.saas_core.drivers import kubernetes_driver
        src = inspect.getsource(kubernetes_driver.KubernetesDriver._ensure_toolbox_pod) + \
            inspect.getsource(kubernetes_driver.KubernetesDriver._toolbox_pod_body)
        for forbidden in ('RbacAuthorization', 'role_binding', 'create_namespace('):
            self.assertNotIn(forbidden, src)

    def test_set_hosting_access_patches_one_spec(self):
        driver, _server = _make_driver()
        custom = MagicMock()
        driver._custom_api = MagicMock(return_value=custom)
        driver.set_hosting_access(_handle(), '^acme_.+$', 'acme_')
        body = custom.patch_cluster_custom_object.call_args.args[4]
        self.assertEqual(body, {'spec': {
            'databaseFilter': '^acme_.+$', 'databaseManager': {'prefix': 'acme_', 'maxDatabases': 0}, 'shell': True}})

    def test_hosting_database_limit_is_passed_to_operator(self):
        driver, _server = _make_driver()
        custom = MagicMock()
        driver._custom_api = MagicMock(return_value=custom)
        driver.set_hosting_access(_handle(), '^acme_.+$', 'acme_', max_databases=1)
        body = custom.patch_cluster_custom_object.call_args.args[4]
        self.assertEqual(body['spec']['databaseManager']['maxDatabases'], 1)

    def test_database_manager_key_reads_the_operator_secret(self):
        import base64
        driver, _server = _make_driver()
        core = MagicMock()
        core.read_namespaced_secret.return_value.data = {
            'dbmanager-key': base64.b64encode(b'k3y').decode()}
        driver._core_api = MagicMock(return_value=core)
        self.assertEqual(driver.database_manager_key(_handle()), 'k3y')
        self.assertEqual(core.read_namespaced_secret.call_args.args,
                         ('odoo-admin-credentials', 'odoo-tenant-odoo-acme'))

    def test_hosting_access_ready_needs_shell_and_addon(self):
        from types import SimpleNamespace as NS
        driver, _server = _make_driver()
        odoo_plain = NS(name='odoo', args=['-c', '/etc/odoo/odoo.conf'])
        odoo_dbm = NS(name='odoo', args=['-c', '/etc/odoo/odoo.conf', '--load=base,web,saas_tenant_dbm'])
        shell = NS(name='shell', args=None)
        for containers, ready in (([odoo_plain], False), ([odoo_dbm], False), ([odoo_dbm, shell], True)):
            pod = NS(status=NS(phase='Running'), spec=NS(containers=containers))
            driver._resolve_pod = MagicMock(return_value=('ns', pod))
            self.assertEqual(driver.hosting_access_ready(_handle()), ready)

    def test_set_database_filter_patches_the_cr(self):
        driver, _server = _make_driver()
        custom = MagicMock()
        driver._custom_api = MagicMock(return_value=custom)
        driver.set_database_filter(_handle(), '^acme_.+$')
        body = custom.patch_cluster_custom_object.call_args.args[4]
        self.assertEqual(body, {'spec': {'databaseFilter': '^acme_.+$'}})

    # -------- usage metrics (Prometheus + in-pod storage) -----------------
    def _prom_driver(self, *responses):
        """Driver whose Prometheus proxy calls return ``responses`` in order
        (each a vector/matrix ``result``)."""
        import json
        driver, server = _make_driver()
        cluster = server.sudo.return_value
        cluster.prometheus_namespace = 'monitoring'
        cluster.prometheus_service = 'prometheus-server:80'
        replies = []
        for result in responses:
            reply = MagicMock()
            reply.data = json.dumps({'status': 'success', 'data': {'result': result}})
            replies.append(reply)
        driver._api_client.call_api.side_effect = replies
        return driver

    def test_prometheus_goes_through_the_api_service_proxy(self):
        driver = self._prom_driver([])
        driver.prometheus_query('up')
        args, kwargs = driver._api_client.call_api.call_args
        self.assertIn('/services/{service}/proxy/api/v1/query', args[0])
        self.assertEqual(kwargs['path_params'], {
            'namespace': 'monitoring', 'service': 'prometheus-server:80'})
        self.assertIn(('query', 'up'), kwargs['query_params'])

    def test_prometheus_unconfigured_raises_unavailable(self):
        from odoo.addons.saas_core.drivers.kubernetes_driver import PrometheusUnavailable
        driver, server = _make_driver()
        server.sudo.return_value.prometheus_namespace = ''
        with self.assertRaises(PrometheusUnavailable):
            driver.prometheus_query('up')
        driver._api_client.call_api.assert_not_called()

    def test_prometheus_api_error_raises_unavailable(self):
        from odoo.addons.saas_core.drivers.kubernetes_driver import PrometheusUnavailable
        driver = self._prom_driver()
        driver._api_client.call_api.side_effect = ApiException(status=503)
        with self.assertRaises(PrometheusUnavailable):
            driver.prometheus_query('up')

    def test_usage_by_tenant_sums_odoo_and_database(self):
        ns = 'odoo-tenant-odoo-acme'
        driver = self._prom_driver(
            [{'metric': {'namespace': ns}, 'value': [1, '0.25']},
             {'metric': {'namespace': 'kube-system'}, 'value': [1, '9']}],   # odoo cpu
            [{'metric': {'namespace': ns}, 'value': [1, '1048576']}],         # odoo mem
            [{'metric': {'namespace': ns}, 'value': [1, '0.05']}],            # db cpu
            [{'metric': {'namespace': ns}, 'value': [1, '524288']}],          # db mem
        )
        usage = driver.usage_by_tenant()
        self.assertEqual(list(usage), ['odoo-acme'])
        expected = {'odoo_cpu_cores': 0.25, 'odoo_mem_bytes': 1048576.0,
                    'db_cpu_cores': 0.05, 'db_mem_bytes': 524288.0,
                    'cpu_cores': 0.3, 'mem_bytes': 1572864.0}
        for key, value in expected.items():
            self.assertAlmostEqual(usage['odoo-acme'][key], value, msg=key)
        queries = [dict(c[1]['query_params'])['query']
                   for c in driver._api_client.call_api.call_args_list]
        # Odoo: web and cron pods, not one-off Jobs; the database separately.
        self.assertIn('container=~"odoo|cron"', queries[0])
        self.assertIn('pod!~"odoo-(init|update|restore)-.+"', queries[0])
        self.assertTrue(queries[0].startswith('sum by (namespace)'))
        self.assertIn('container=~"postgresql|postgres"', queries[2])

    def test_usage_by_tenant_scoped_to_one_handle(self):
        driver = self._prom_driver([], [], [], [])
        driver.usage_by_tenant(_handle())
        cpu_query = dict(driver._api_client.call_api.call_args_list[0][1]
                         ['query_params'])['query']
        self.assertIn('namespace=~"odoo-tenant-odoo-acme"', cpu_query)

    def test_measure_storage_parses_exec_output(self):
        from odoo.addons.saas_core.drivers.base import ExecResult
        driver, _server = _make_driver()
        driver.exec = MagicMock(return_value=ExecResult(
            rc=0, stdout='filestore_bytes=123\ndb_bytes=456\n', stderr=''))
        self.assertEqual(driver.measure_storage(_handle()),
                         {'filestore_bytes': 123, 'db_bytes': 456})

    def test_measure_storage_raises_on_failure(self):
        from odoo.addons.saas_core.drivers.base import ExecResult
        driver, _server = _make_driver()
        driver.exec = MagicMock(return_value=ExecResult(
            rc=1, stdout='filestore_bytes=123\n', stderr='psql: connection refused'))
        with self.assertRaises(RuntimeError):
            driver.measure_storage(_handle())

    # -------- the SEAM: _compute_driver() resolves to KubernetesDriver -----
    def test_compute_driver_selected_by_server_type(self):
        # Kubernetes is the only compute backend left (ssh_docker removed);
        # this just proves the god-model always resolves to KubernetesDriver.
        from odoo.addons.saas_core.drivers.kubernetes_driver import KubernetesDriver
        product = self.env['saas.product'].sudo().search([('is_hosting', '=', True)], limit=1) \
            or self.env['saas.product'].sudo().create({'name': 'K Host', 'is_hosting': True})
        plan = self.env['saas.plan'].sudo().create({
            'name': 'K Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [product.id])]})
        partner = self.env['res.partner'].sudo().create({'name': 'K Cust'})
        domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create({'name': 'k.example.com'})
        k8s_srv = self.env['saas.server'].sudo().create(
            {'name': 'k8s', 'compute_driver': 'kubernetes'})

        def mk(sub, srv):
            return self.env['saas.instance'].sudo().create({
                'subdomain': sub, 'domain_id': domain.id, 'partner_id': partner.id,
                'saas_product_id': product.id, 'plan_id': plan.id,
                'docker_server_id': srv.id, 'billing_period': 'monthly',
                'environment': 'production', 'region_id': False, 'state': 'running'})

        self.assertIsInstance(mk('konk8s', k8s_srv)._compute_driver(), KubernetesDriver)

    def test_do_stop_routes_through_kubernetes_unchanged(self):
        """The god-model's _do_stop is NOT changed for K8s — it just calls
        self._compute_driver().stop(); on a k8s server that patches
        spec.suspended=true on the real OdooInstance CR. This is the
        no-rewrite proof, now against the real-API driver."""
        from odoo.addons.saas_core.drivers.kubernetes_driver import KubernetesDriver
        product = self.env['saas.product'].sudo().search([('is_hosting', '=', True)], limit=1) \
            or self.env['saas.product'].sudo().create({'name': 'K Host2', 'is_hosting': True})
        plan = self.env['saas.plan'].sudo().create({
            'name': 'K Plan2', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [product.id])]})
        partner = self.env['res.partner'].sudo().create({'name': 'K Cust2'})
        domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create({'name': 'k2.example.com'})
        k8s_srv = self.env['saas.server'].sudo().create(
            {'name': 'k8s2', 'compute_driver': 'kubernetes'})
        inst = self.env['saas.instance'].sudo().create({
            'subdomain': 'konstop', 'domain_id': domain.id, 'partner_id': partner.id,
            'saas_product_id': product.id, 'plan_id': plan.id,
            'docker_server_id': k8s_srv.id, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': False, 'state': 'running'})

        custom_api = MagicMock()
        with patch.object(KubernetesDriver, '_custom_api', return_value=custom_api):
            inst._do_stop()
        self.assertEqual(inst.state, 'stopped')
        custom_api.patch_cluster_custom_object.assert_called_with(
            'saas.odoo.example.com', 'v1alpha1', 'odooinstances', 'odoo-konstop',
            {'spec': {'suspended': True}})
