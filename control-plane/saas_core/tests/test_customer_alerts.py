"""Customer email alerts: service down / restored, high CPU, high storage."""
import datetime
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.saas_core.drivers.kubernetes_driver import KubernetesDriver

GB = 1024 ** 3


@tagged('post_install', '-at_install')
class TestCustomerAlerts(TransactionCase):

    def setUp(self):
        super().setUp()
        env = self.env
        product = env['saas.product'].sudo().create({'name': 'AL Hosting', 'is_hosting': True})
        plan = env['saas.plan'].sudo().create({
            'name': 'AL Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 10,
            'cpu_limit': 2.0, 'ram_limit': '2g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': env.company.currency_id.id, 'saas_product_ids': [(6, 0, [product.id])]})
        self.server = env['saas.server'].sudo().create({'name': 'al-srv', 'compute_driver': 'kubernetes'})
        self.inst = env['saas.instance'].sudo().create({
            'subdomain': 'altest',
            'domain_id': (env['saas.based.domain'].sudo().search([], limit=1)
                          or env['saas.based.domain'].sudo().create({'name': 'al.example.com'})).id,
            'partner_id': env['res.partner'].sudo().create(
                {'name': 'AL Cust', 'email': 'al@example.com'}).id,
            'saas_product_id': product.id, 'plan_id': plan.id,
            'docker_server_id': self.server.id, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': False, 'state': 'running'})
        self.sent = []
        p = patch.object(type(self.inst), '_send_notification',
                         lambda rec, tmpl: self.sent.append(tmpl))
        p.start(); self.addCleanup(p.stop)
        self.observation = dict(runtime_state='online', runtime_reason='healthy', runtime_message='ok',
                                runtime_workload='running', runtime_reachable=True)
        p = patch('odoo.addons.saas_core.models.saas_instance_health.observe_runtime',
                  lambda driver, handle, url, lifecycle: dict(self.observation))
        p.start(); self.addCleanup(p.stop)
        Instance = type(self.inst)
        self.driver = MagicMock()
        self.driver._cr_name = KubernetesDriver._cr_name
        for name in ('_compute_driver', '_compute_handle'):
            p = patch.object(Instance, name, lambda rec, connection=None, _n=name: (
                self.driver if _n == '_compute_driver' else MagicMock()))
            p.start(); self.addCleanup(p.stop)

    # -- helpers ------------------------------------------------------------
    def _observe(self, state, reachable=None):
        self.observation.update(runtime_state=state, runtime_reachable=reachable)
        self.env['saas.instance.runtime'].sudo().search(
            [('instance_id', '=', self.inst.id)]).write(
            {'checked_at': fields.Datetime.now() - datetime.timedelta(seconds=30)})
        self.inst.invalidate_recordset()
        self.inst._refresh_runtime_health()

    def _backdate(self, field, seconds):
        self.inst.write({field: fields.Datetime.now() - datetime.timedelta(seconds=seconds)})

    # -- availability -------------------------------------------------------
    def test_service_down_email_after_grace_then_restored(self):
        self._observe('online')
        self.assertEqual(self.sent, [])
        self._observe('unreachable', False)
        self.assertTrue(self.inst.alert_down_since)
        self.assertEqual(self.sent, [], "one failed check is not an outage")
        self._backdate('alert_down_since', 61)
        self._observe('unavailable', True)
        self.assertEqual(self.sent, ['saas_core.mail_template_saas_service_down'])
        self.assertTrue(self.inst.alert_down_notified)
        self._observe('unreachable', False)
        self.assertEqual(len(self.sent), 1, "one email per outage")
        self._observe('online', True)
        self.assertEqual(self.sent[-1], 'saas_core.mail_template_saas_service_restored')
        self.assertFalse(self.inst.alert_down_since)
        self.assertFalse(self.inst.alert_down_notified)

    def test_short_blip_sends_nothing(self):
        self._observe('unreachable', False)
        self._observe('online', True)
        self.assertEqual(self.sent, [])
        self.assertFalse(self.inst.alert_down_since)

    def test_unknown_observation_keeps_the_outage_clock(self):
        self._observe('unreachable', False)
        since = self.inst.alert_down_since
        self._observe('unknown')
        self.assertEqual(self.inst.alert_down_since, since)
        self.assertEqual(self.sent, [])

    def test_stopped_server_is_never_reported_down(self):
        self.inst.state = 'stopped'
        self.observation.update(runtime_lifecycle='stopped')
        self._observe('unavailable', False)
        self._backdate('alert_down_since', 120) if self.inst.alert_down_since else None
        self._observe('unavailable', False)
        self.assertEqual(self.sent, [])

    # -- usage --------------------------------------------------------------
    def _usage(self, cpu_cores, storage_gb):
        cr = KubernetesDriver._cr_name(self.inst._compute_handle())
        self.driver.usage_by_tenant.return_value = {cr: {'cpu_cores': cpu_cores, 'mem_bytes': 1 * GB}}
        self.driver.measure_storage.return_value = {'filestore_bytes': storage_gb * GB, 'db_bytes': 0}
        self.inst._refresh_usage()

    def test_high_cpu_email_once_per_day_after_sustained_load(self):
        self._usage(cpu_cores=3.9, storage_gb=1)   # ~97% of the package cores
        self.assertGreaterEqual(self.inst.cpu_usage_pct, 90)
        self.assertTrue(self.inst.alert_cpu_since)
        self.assertEqual(self.sent, [], "a single high reading is not sustained")
        self._backdate('alert_cpu_since', 11 * 60)
        self._usage(cpu_cores=3.9, storage_gb=1)
        self.assertEqual(self.sent, ['saas_core.mail_template_saas_high_cpu'])
        self._usage(cpu_cores=3.9, storage_gb=1)
        self.assertEqual(len(self.sent), 1, "not repeated within 24h")
        self._usage(cpu_cores=0.2, storage_gb=1)
        self.assertFalse(self.inst.alert_cpu_since, "load dropped: clock reset")

    def test_high_storage_email_once_per_day(self):
        self._usage(cpu_cores=0.2, storage_gb=9.5)   # 95% of 10 GB
        self.assertEqual(self.sent, ['saas_core.mail_template_saas_high_storage'])
        self._usage(cpu_cores=0.2, storage_gb=9.6)
        self.assertEqual(len(self.sent), 1)
        self._backdate('alert_storage_last_sent', 25 * 3600)
        self._usage(cpu_cores=0.2, storage_gb=9.6)
        self.assertEqual(len(self.sent), 2)

    def test_thresholds_are_configurable(self):
        self.env['ir.config_parameter'].sudo().set_param('saas_master.alert_storage_pct', '50')
        self._usage(cpu_cores=0.2, storage_gb=6)
        self.assertEqual(self.sent, ['saas_core.mail_template_saas_high_storage'])
