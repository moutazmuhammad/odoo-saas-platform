from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestDataService(TransactionCase):
    """Phase 1: DataService primitives (snapshot/materialize) delegate to the
    proven restic backup + restore. Unit-level (mocked); the real restic→DO-Spaces
    round-trip is verified separately against the live test tenant."""

    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or self.env['saas.product'].sudo().create(
            {'name': 'DS Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'DS Plan', 'is_custom': True, 'workers': 2, 'storage_limit': 10,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 50.0, 'yearly_price': 480.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.partner = self.env['res.partner'].sudo().create({'name': 'DS Cust'})
        self.domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create({'name': 'ds.example.com'})

    def _instance(self, sub):
        return self.env['saas.instance'].sudo().create({
            'subdomain': sub, 'domain_id': self.domain.id, 'partner_id': self.partner.id,
            'saas_product_id': self.product.id, 'plan_id': self.plan.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running'})

    def _done_backup(self, inst, name):
        return self.env['saas.instance.backup'].sudo().create({
            'instance_id': inst.id, 'name': name, 'state': 'done', 'is_full_instance': True})

    def test_snapshot_delegates_and_returns_done_backup(self):
        inst = self._instance('dssnap')
        bk = self._done_backup(inst, 'snap1')
        Backup = self.env['saas.instance.backup']
        with patch.object(type(Backup), '_perform_full_instance_backup', return_value=None) as m:
            snap = inst._data_service().snapshot(inst)
        m.assert_called_once()
        self.assertEqual(snap, bk)

    def test_snapshot_raises_when_no_backup_produced(self):
        inst = self._instance('dsnone')
        Backup = self.env['saas.instance.backup']
        with patch.object(type(Backup), '_perform_full_instance_backup', return_value=None):
            with self.assertRaises(RuntimeError):
                inst._data_service().snapshot(inst)

    def test_materialize_routes_to_restore(self):
        inst = self._instance('dsmat')
        bk = self._done_backup(inst, 'snap2')
        with patch.object(type(inst), '_do_restore_full_instance', return_value=None) as m:
            out = inst._data_service().materialize(bk)
        m.assert_called_once_with(bk.id)
        self.assertEqual(out, inst)

    def test_materialize_neutralize_not_implemented(self):
        inst = self._instance('dsneu')
        bk = self._done_backup(inst, 'snap3')
        with self.assertRaises(NotImplementedError):
            inst._data_service().materialize(bk, neutralize=True)

    # -- Phase 2 (/ROADMAP.md §5): migrate_to_kubernetes cleanup ------------
    def test_cleanup_failed_migration_unlinks_even_if_destroy_raises(self):
        """A failed migration must never leave an orphaned CR/namespace OR
        an orphaned saas.instance row lying around — and a failure tearing
        down the Kubernetes side must not prevent cleaning up the DB side
        (best-effort, matches SaasInstanceBackup.unlink()'s own pattern for
        cloud-object cleanup)."""
        inst = self._instance('dscleanup')
        ds = inst._data_service()

        target = MagicMock()
        driver = MagicMock()
        driver.destroy.side_effect = RuntimeError('cluster unreachable')
        target._compute_driver.return_value = driver

        ds._cleanup_failed_migration(target, handle='some-handle')

        driver.destroy.assert_called_once_with('some-handle')
        target.sudo.return_value.unlink.assert_called_once()

    def test_cleanup_failed_migration_without_handle_still_unlinks(self):
        inst = self._instance('dscleanup2')
        ds = inst._data_service()
        target = MagicMock()
        ds._cleanup_failed_migration(target, handle=None)
        target._compute_driver.assert_not_called()
        target.sudo.return_value.unlink.assert_called_once()

    # -- Phase 2: _wait_for_restore_ready must not treat an in-progress
    # restore (status=False, reason=RestoreRunning/RestorePending) as a
    # failure — only reason=RestoreFailed is terminal (a real local
    # microk8s run surfaced this: reconcileRestore reports status=False
    # while genuinely still running, compute/operator/internal/controller
    # /restore.go). --------------------------------------------------------
    def test_wait_for_restore_ready_returns_on_true(self):
        inst = self._instance('dswait1')
        ds = inst._data_service()
        driver = MagicMock()
        driver._get_cr.return_value = {'status': {'conditions': [
            {'type': 'RestoreReady', 'status': 'True', 'reason': 'RestoreSucceeded'}]}}
        ds._wait_for_restore_ready(driver, 'odoo-x', timeout=5)  # must not raise

    def test_wait_for_restore_ready_tolerates_running_state(self):
        inst = self._instance('dswait2')
        ds = inst._data_service()
        driver = MagicMock()
        calls = {'n': 0}

        def _get_cr(name):
            calls['n'] += 1
            if calls['n'] < 3:
                return {'status': {'conditions': [
                    {'type': 'RestoreReady', 'status': 'False',
                     'reason': 'RestoreRunning',
                     'message': 'restoring database and filestore from backup'}]}}
            return {'status': {'conditions': [
                {'type': 'RestoreReady', 'status': 'True', 'reason': 'RestoreSucceeded'}]}}
        driver._get_cr.side_effect = _get_cr

        with patch('odoo.addons.saas_core.dataservice.service.time.sleep'):
            ds._wait_for_restore_ready(driver, 'odoo-x', timeout=5)  # must not raise
        self.assertEqual(calls['n'], 3)

    def test_wait_for_restore_ready_raises_on_restore_failed(self):
        inst = self._instance('dswait3')
        ds = inst._data_service()
        driver = MagicMock()
        driver._get_cr.return_value = {'status': {'conditions': [
            {'type': 'RestoreReady', 'status': 'False', 'reason': 'RestoreFailed',
             'message': 'restore Job failed after exhausting retries'}]}}
        with self.assertRaises(RuntimeError) as cm:
            ds._wait_for_restore_ready(driver, 'odoo-x', timeout=5)
        self.assertIn('exhausting retries', str(cm.exception))

    # -- Phase 2: _ensure_restore_secret must not write into a namespace
    # that still exists but is mid-termination from a prior failed attempt
    # reusing the same (deterministic) namespace name — a real 403
    # (NamespaceTerminating) was observed against a live microk8s cluster
    # on an immediate retry. -------------------------------------------------
    def test_ensure_restore_secret_waits_out_terminating_namespace(self):
        from kubernetes.client.rest import ApiException
        inst = self._instance('dsns1')
        ds = inst._data_service()
        driver = MagicMock()
        core_api = MagicMock()
        driver._core_api.return_value = core_api

        terminating_ns = MagicMock()
        terminating_ns.status.phase = 'Terminating'
        active_ns = MagicMock()
        active_ns.status.phase = 'Active'
        core_api.read_namespace.side_effect = [
            terminating_ns,
            ApiException(status=404),
            active_ns,
        ]

        with patch('odoo.addons.saas_core.dataservice.service.time.sleep'):
            ds._ensure_restore_secret(
                driver, 'odoo-tenant-odoo-x', 'odoo-x-restore-creds',
                {'endpoint': 'http://minio:9000', 'access_key': 'a', 'secret_key': 'b'},
                timeout=5)

        self.assertEqual(core_api.read_namespace.call_count, 3)
        core_api.create_namespaced_secret.assert_called_once()

    # -- Phase 2: _wait_until_healthy must tolerate the post-restore web
    # Deployment rollout window (RestoreReady=True but the Deployment
    # hasn't rolled out yet — observed against a real cluster as
    # status='restarting'/phase='Provisioning' right after restore). ------
    def test_wait_until_healthy_tolerates_post_restore_rollout(self):
        from odoo.addons.saas_core.drivers.base import HealthStatus
        inst = self._instance('dshealth1')
        ds = inst._data_service()
        driver = MagicMock()
        driver.health.side_effect = [
            HealthStatus(running=False, status='restarting', detail='Provisioning'),
            HealthStatus(running=False, status='restarting', detail='Provisioning'),
            HealthStatus(running=True, status='running', detail='Ready'),
        ]
        with patch('odoo.addons.saas_core.dataservice.service.time.sleep'):
            ds._wait_until_healthy(driver, 'handle', timeout=5)  # must not raise
        self.assertEqual(driver.health.call_count, 3)

    def test_wait_until_healthy_raises_on_timeout(self):
        from odoo.addons.saas_core.drivers.base import HealthStatus
        inst = self._instance('dshealth2')
        ds = inst._data_service()
        driver = MagicMock()
        driver.health.return_value = HealthStatus(
            running=False, status='restarting', detail='Provisioning')
        with patch('odoo.addons.saas_core.dataservice.service.time.sleep'):
            with self.assertRaises(RuntimeError):
                ds._wait_until_healthy(driver, 'handle', timeout=-1)
