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
    # kubeconfig now lives on a dedicated saas.kubeconfig record
    # (region.kubeconfig_id), read via its _kubeconfig_yaml() accessor —
    # same upload-only/encrypted-at-rest shape as saas.ssh.key.pair.
    server.region_id.kubeconfig_id._kubeconfig_yaml.return_value = kubeconfig
    server.region_id.name = 'test-region'
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
    pod.status.phase = phase
    cs = MagicMock()
    cs.name = 'odoo'
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

        self.assertEqual(handle.server_id, server.id)
        self.assertEqual(handle.container_name, 'odoo_acme')
        self.assertEqual(handle.instance_path, 'odoo-tenant-odoo-acme')

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

    def test_create_with_replicas_2_requests_rwx_storage(self):
        """A compute tier above 1 replica (saas.instance.compute_tier_id,
        e.g. HA=2/Scale=4) — RWX is REQUIRED alongside it (the operator
        rejects RWO with replicas > 1, see FilestoreSpec's own comment in
        odoo_types.go) so this driver must always set both together."""
        from odoo.addons.saas_core.drivers.base import ComputeSpec
        driver, _server = _make_driver()
        custom_api = MagicMock()
        driver._custom_api = MagicMock(return_value=custom_api)
        spec = ComputeSpec(
            container_name='odoo_ha', image='odoo:18.0', instance_path='/x',
            http_port=8069, longpolling_port=8072, db_name='ha', db_host='db',
            env={'domain': 'ha.example.com', 'replicas': 2})
        driver.create(spec)
        body = custom_api.create_cluster_custom_object.call_args.args[3]
        self.assertEqual(body['spec']['replicas'], 2)
        self.assertEqual(
            body['spec']['storage']['filestore']['accessMode'], 'ReadWriteMany')

    # -------- scale() (compute tier replica count) --------------------------
    def test_scale_patches_replicas(self):
        driver, _server = _make_driver()
        custom_api = MagicMock()
        driver._custom_api = MagicMock(return_value=custom_api)
        driver.scale(_handle(), 2)
        custom_api.patch_cluster_custom_object.assert_called_once_with(
            'saas.odoo.example.com', 'v1alpha1', 'odooinstances', 'odoo-acme',
            {'spec': {'replicas': 2}})

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

    def test_scale_raises_on_api_error(self):
        driver, _server = _make_driver()
        custom_api = MagicMock()
        custom_api.patch_cluster_custom_object.side_effect = ApiException(
            status=422, reason='Invalid')
        driver._custom_api = MagicMock(return_value=custom_api)
        with self.assertRaises(RuntimeError):
            driver.scale(_handle(), 2)

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

    # -------- endpoint() ----------------------------------------------------
    def test_endpoint_returns_region_ingress_address(self):
        """endpoint() must return a CONNECT-TO address (the cluster's own
        ingress front door), not the tenant's own public domain — that
        hostname's DNS still points wherever it pointed before cutover, so
        "connecting" to it would loop back rather than reach the cluster."""
        driver, server = _make_driver()
        server.region_id.ingress_host = '192.168.1.15'
        server.region_id.ingress_port = 80
        host, port = driver.endpoint(_handle())
        self.assertEqual(host, '192.168.1.15')
        self.assertEqual(port, 80)

    def test_endpoint_defaults_port_80(self):
        driver, server = _make_driver()
        server.region_id.ingress_host = '192.168.1.15'
        server.region_id.ingress_port = False
        host, port = driver.endpoint(_handle())
        self.assertEqual(port, 80)

    def test_endpoint_empty_when_no_ingress_host_configured(self):
        driver, server = _make_driver()
        server.region_id.ingress_host = False
        self.assertEqual(driver.endpoint(_handle()), ('', 0))

    # -------- logs() ----------------------------------------------------
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

    # -------- usage metrics (Prometheus + in-pod storage) -----------------
    def _prom_driver(self, *responses):
        """Driver whose Prometheus proxy calls return ``responses`` in order
        (each a vector/matrix ``result``)."""
        import json
        driver, server = _make_driver()
        region = server.region_id.sudo.return_value
        region.prometheus_namespace = 'monitoring'
        region.prometheus_service = 'prometheus-server:80'
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
        server.region_id.sudo.return_value.prometheus_namespace = ''
        with self.assertRaises(PrometheusUnavailable):
            driver.prometheus_query('up')
        driver._api_client.call_api.assert_not_called()

    def test_prometheus_api_error_raises_unavailable(self):
        from odoo.addons.saas_core.drivers.kubernetes_driver import PrometheusUnavailable
        driver = self._prom_driver()
        driver._api_client.call_api.side_effect = ApiException(status=503)
        with self.assertRaises(PrometheusUnavailable):
            driver.prometheus_query('up')

    def test_usage_by_tenant_maps_namespaces_to_cr_names(self):
        driver = self._prom_driver(
            [{'metric': {'namespace': 'odoo-tenant-odoo-acme'}, 'value': [1, '0.25']},
             {'metric': {'namespace': 'kube-system'}, 'value': [1, '9']}],
            [{'metric': {'namespace': 'odoo-tenant-odoo-acme'}, 'value': [1, '1048576']}],
        )
        self.assertEqual(driver.usage_by_tenant(), {
            'odoo-acme': {'cpu_cores': 0.25, 'mem_bytes': 1048576.0}})
        cpu_query = dict(driver._api_client.call_api.call_args_list[0][1]
                         ['query_params'])['query']
        # Web pods of the Odoo container only — not cron/Job pods.
        self.assertIn('container="odoo"', cpu_query)
        self.assertIn('pod!~"odoo-(cron|init|update|restore)-.+"', cpu_query)

    def test_usage_by_tenant_scoped_to_one_handle(self):
        driver = self._prom_driver([], [])
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
