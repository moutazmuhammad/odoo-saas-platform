import datetime
import json
from unittest.mock import MagicMock, patch

import requests
from psycopg2 import OperationalError

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

from ..drivers.base import HealthStatus
from ..drivers.runtime_health import observe_runtime


@tagged("post_install", "-at_install")
class TestRuntimeHealth(TransactionCase):
    def setUp(self):
        super().setUp()
        self.driver = MagicMock()
        self.driver.health.return_value = HealthStatus(running=True, status='running')

    def observe(self, payload=None, code=200, lifecycle='running'):
        response = MagicMock()
        response.status_code = code
        response.iter_content.return_value = [json.dumps(payload or {'status': 'pass', 'db_server_status': True}).encode()]
        response.__enter__.return_value = response
        with patch('odoo.addons.saas_core.drivers.runtime_health.requests.get', return_value=response) as get:
            result = observe_runtime(self.driver, object(), 'https://tenant.example.com', lifecycle)
        self.driver.start.assert_not_called()
        self.driver.stop.assert_not_called()
        return result, get

    def test_online_requires_workload_endpoint_and_database(self):
        result, get = self.observe()
        self.assertEqual(result['runtime_state'], 'online')
        self.assertTrue(result['runtime_reachable'])
        self.assertEqual(get.call_args.args[0], 'https://tenant.example.com/web/health?db_server_status=1')
        self.assertFalse(get.call_args.kwargs['allow_redirects'])

    def test_http_200_without_database_confirmation_is_not_online(self):
        result, _ = self.observe({'status': 'pass'})
        self.assertEqual(result['runtime_state'], 'unavailable')

    def test_gateway_errors_and_redirects_are_not_online(self):
        for code in (301, 302, 401, 404, 500, 502, 503):
            result, _ = self.observe(code=code)
            self.assertEqual(result['runtime_state'], 'unavailable')
            self.assertTrue(result['runtime_reachable'])

    def test_ready_workload_with_unreachable_url_is_not_online(self):
        with patch('odoo.addons.saas_core.drivers.runtime_health.requests.get', side_effect=requests.ConnectionError):
            result = observe_runtime(self.driver, object(), 'https://tenant.example.com', 'running')
        self.assertEqual(result['runtime_state'], 'unreachable')
        self.assertFalse(result['runtime_reachable'])

    def test_cluster_failure_is_unknown_not_a_confirmed_tenant_outage(self):
        self.driver.health.side_effect = RuntimeError('private kubeconfig details')
        result, get = self.observe()
        self.assertEqual(result['runtime_state'], 'unknown')
        self.assertEqual(result['runtime_reason'], 'cluster_unreachable')
        self.assertNotIn('private', result['runtime_message'])
        get.assert_not_called()

    def test_missing_workload_never_inherits_running_lifecycle(self):
        self.driver.health.return_value = HealthStatus(running=False, status='not_found')
        result, get = self.observe()
        self.assertEqual(result['runtime_state'], 'unavailable')
        self.assertEqual(result['runtime_reason'], 'workload_missing')
        get.assert_not_called()

    def test_provisioning_is_starting_not_unavailable(self):
        """While a deploy runs, a missing or not-ready workload is a phase of
        provisioning, not an outage; the message names the phase."""
        self.driver.health.return_value = HealthStatus(running=False, status='not_found')
        result, get = self.observe(lifecycle='provisioning')
        self.assertEqual(result['runtime_state'], 'starting')
        self.assertEqual(result['runtime_reason'], 'provisioning')
        self.assertIn('creating', result['runtime_message'])
        get.assert_not_called()
        self.driver.health.return_value = HealthStatus(running=True, status='running')
        result, _ = self.observe(code=503, lifecycle='provisioning')
        self.assertEqual(result['runtime_state'], 'starting')
        self.assertIn('not answering', result['runtime_message'])
        result, _ = self.observe(lifecycle='provisioning')
        self.assertEqual(result['runtime_state'], 'online')

    def test_crash_loop_is_unavailable(self):
        self.driver.health.return_value = HealthStatus(running=False, status='restarting', detail='CrashLoopBackOff')
        result, _ = self.observe()
        self.assertEqual(result['runtime_state'], 'unavailable')

    def test_suspension_is_not_reported_complete_while_pod_runs(self):
        result, get = self.observe(lifecycle='suspended')
        self.assertEqual(result['runtime_state'], 'stopping')
        get.assert_not_called()
        self.driver.health.return_value = HealthStatus(running=False, status='exited')
        result, _ = self.observe(lifecycle='suspended')
        self.assertEqual(result['runtime_state'], 'suspended')

    def test_image_pull_failure_is_unavailable(self):
        self.driver.health.return_value = HealthStatus(running=False, status='starting', detail='ImagePullBackOff')
        result, get = self.observe()
        self.assertEqual(result['runtime_state'], 'unavailable')
        get.assert_not_called()

    def test_unready_workload_is_starting(self):
        self.driver.health.return_value = HealthStatus(running=False, status='restarting')
        result, get = self.observe()
        self.assertEqual(result['runtime_state'], 'starting')
        get.assert_not_called()

    def test_unrelated_or_oversized_http_body_is_not_online(self):
        for body in (b'<html>proxy default page</html>', b'x' * 8193):
            response = MagicMock(status_code=200)
            response.iter_content.return_value = [body]
            response.__enter__.return_value = response
            with patch('odoo.addons.saas_core.drivers.runtime_health.requests.get', return_value=response):
                result = observe_runtime(self.driver, object(), 'https://tenant.example.com', 'running')
            self.assertEqual(result['runtime_state'], 'unavailable')

    def test_missing_pod_and_terminating_pod(self):
        self.driver.health.return_value = HealthStatus(running=False, status='missing')
        result, get = self.observe()
        self.assertEqual(result['runtime_reason'], 'workload_missing')
        get.assert_not_called()
        self.driver.health.return_value = HealthStatus(running=False, status='terminating')
        result, _ = self.observe(lifecycle='stopped')
        self.assertEqual(result['runtime_state'], 'stopping')


@tagged("post_install", "-at_install")
class TestRuntimeHealthModel(TransactionCase):
    """Observation storage, cache, staleness and the sweep cron."""

    def setUp(self):
        super().setUp()
        env = self.env
        product = env['saas.product'].sudo().create({'name': 'RH Hosting', 'is_hosting': True})
        self.plan = env['saas.plan'].sudo().create({
            'name': 'RH Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 10.0, 'yearly_price': 96.0,
            'currency_id': env.company.currency_id.id, 'saas_product_ids': [(6, 0, [product.id])]})
        self.vals = {
            'domain_id': (env['saas.based.domain'].sudo().search([], limit=1)
                          or env['saas.based.domain'].sudo().create({'name': 'rh.example.com'})).id,
            'partner_id': env['res.partner'].sudo().create({'name': 'RH Cust'}).id,
            'saas_product_id': product.id, 'plan_id': self.plan.id,
            'docker_server_id': env['saas.server'].sudo().create({'name': 'rh-srv'}).id,
            'environment': 'production', 'region_id': False, 'state': 'running',
        }
        self.observed = []
        online = dict(runtime_state='online', runtime_reason='healthy', runtime_message='ok',
                      runtime_workload='running', runtime_reachable=True)

        def fake_observe(driver, handle, url, lifecycle):
            self.observed.append(url)
            return dict(online)
        p = patch('odoo.addons.saas_core.models.saas_instance_health.observe_runtime', fake_observe)
        p.start()
        self.addCleanup(p.stop)
        Instance = type(env['saas.instance'])
        for name in ('_compute_driver', '_compute_handle'):
            p = patch.object(Instance, name, lambda rec, connection=None: MagicMock())
            p.start()
            self.addCleanup(p.stop)

    def _inst(self, sub, **kw):
        return self.env['saas.instance'].sudo().create(dict(self.vals, subdomain=sub, **kw))

    def _set_checked(self, inst, seconds_ago):
        self.env['saas.instance.runtime'].sudo().search([('instance_id', '=', inst.id)]).write({
            'checked_at': fields.Datetime.now() - datetime.timedelta(seconds=seconds_ago)})
        inst.invalidate_recordset()

    def test_observation_stored_outside_the_tenant_row(self):
        inst = self._inst('rhstore')
        before = inst.write_date
        inst._refresh_runtime_health()
        row = self.env['saas.instance.runtime'].sudo().search([('instance_id', '=', inst.id)])
        self.assertEqual(row.state, 'online')
        self.assertEqual(inst.runtime_state, 'online')
        self.assertEqual(inst.write_date, before)
        self.assertEqual(inst._runtime_status_dict()['runtime_state'], 'online')

    def test_fresh_observation_is_cached_for_20_seconds(self):
        inst = self._inst('rhcache')
        inst._refresh_runtime_health()
        inst._refresh_runtime_health()
        self.assertEqual(len(self.observed), 1)
        self._set_checked(inst, 21)
        inst._refresh_runtime_health()
        self.assertEqual(len(self.observed), 2)

    def test_observation_older_than_120_seconds_is_unknown(self):
        inst = self._inst('rhstale')
        inst._refresh_runtime_health()
        self._set_checked(inst, 121)
        status = inst._runtime_status_dict()
        self.assertEqual(status['runtime_state'], 'unknown')
        self.assertTrue(status['runtime_stale'])

    def test_lifecycle_change_invalidates_observation(self):
        inst = self._inst('rhlife')
        inst._refresh_runtime_health()
        inst.state = 'stopped'
        self.assertEqual(inst._runtime_status_dict()['runtime_state'], 'unknown')

    def test_locked_observation_is_skipped_not_waited_for(self):
        inst = self._inst('rhlock')
        inst._refresh_runtime_health()
        self._set_checked(inst, 30)
        lock = OperationalError('could not obtain lock')
        real_execute = type(self.env.cr).execute

        def execute(cr, query, params=None, log_exceptions=True):
            if 'FOR UPDATE NOWAIT' in str(query):
                raise lock
            return real_execute(cr, query, params, log_exceptions)
        with patch.object(type(self.env.cr), 'execute', execute):
            inst._refresh_runtime_health()   # must not raise
        self.assertEqual(len(self.observed), 2)

    def test_cron_checks_never_observed_tenants_first(self):
        checked = self._inst('rhcron1')
        checked._refresh_runtime_health()
        self._set_checked(checked, 60)
        fresh = self._inst('rhcron2')
        # Other tenants in the database count as freshly checked here.
        others = self.env['saas.instance'].sudo().search([('id', 'not in', (checked | fresh).ids)])
        self.env['saas.instance.runtime'].sudo().create([
            {'instance_id': i.id, 'checked_at': fields.Datetime.now(), 'lifecycle': i.state} for i in others])
        self.observed.clear()
        seen = []
        Instance = type(self.env['saas.instance'])
        real = Instance._refresh_runtime_health

        def spy(rec):
            seen.append(rec.id)
            return real(rec)
        with patch.object(Instance, '_refresh_runtime_health', spy):
            self.env['saas.instance']._cron_observe_runtime_health()
        self.assertLess(seen.index(fresh.id), seen.index(checked.id))
        self.assertEqual(self.env['saas.instance.runtime'].sudo().search_count(
            [('instance_id', 'in', (checked | fresh).ids)]), 2)
