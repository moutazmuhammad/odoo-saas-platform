"""Characterization tests for hosting_db_upgrade_module — a customer-
facing recovery tool (stop -> one-shot `odoo -u <module>` -> bring the
container back up regardless of outcome), routed through ComputeDriver.

Originally written against the raw `docker compose` calls this method
had before (zero test coverage existed at all), then updated in the same
commit that routed stop/run/up through the already-existing
driver.stop()/run_once()/driver.start() methods — reusing driver.stop()
(`docker stop <container>`) instead of the original `docker compose stop
odoo` is a deliberate, understood command-string change (the two are
behaviorally equivalent for this single-service-per-project setup), so
assertions target the driver calls themselves rather than raw SSH
strings, the same approach test_redeploy_blue_green.py's zero-downtime
tests use for its already-routed canonical destroy()/start() calls."""
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestHostingDbUpgradeModule(TransactionCase):
    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or self.env['saas.product'].sudo().create(
            {'name': 'UM Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'UM Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.domain = self.env['saas.based.domain'].sudo().create({'name': 'um.example.com'})
        self.partner = self.env['res.partner'].sudo().create({'name': 'UM Cust'})
        self.server = self.env['saas.server'].sudo().create(
            {'name': 'um-srv', 'docker_base_path': '/home/odoo'})
        self.instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'umtest', 'domain_id': self.domain.id, 'partner_id': self.partner.id,
            'saas_product_id': self.product.id, 'plan_id': self.plan.id,
            'docker_server_id': self.server.id, 'is_hosting': True,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running'})
        self.db_full_name = self.instance._hosting_db_full_name('mydb')

    def _run(self, driver, module='sale', db_names=None):
        db_full_name = self.db_full_name
        names = db_names if db_names is not None else [db_full_name]
        with patch.object(type(self.server), '_get_ssh_connection',
                          lambda rec: MagicMock()), \
                patch.object(type(self.instance), 'hosting_db_list',
                             lambda rec: [{'name': n} for n in names]), \
                patch.object(type(self.instance), '_compute_driver',
                             return_value=driver):
            return self.instance.hosting_db_upgrade_module('mydb', module)

    @staticmethod
    def _exec_result(rc, stdout='', stderr=''):
        from odoo.addons.saas_core.drivers.base import ExecResult
        return ExecResult(rc=rc, stdout=stdout, stderr=stderr)

    def test_happy_path_stops_upgrades_and_restarts(self):
        driver = MagicMock()
        driver.run_once.return_value = self._exec_result(0, 'upgrade output ok')
        out = self._run(driver)
        driver.stop.assert_called_once()
        driver.run_once.assert_called_once()
        run_args = driver.run_once.call_args.args[1]
        self.assertIn('-u sale', run_args)
        self.assertIn('-d %s' % self.db_full_name, run_args)
        driver.start.assert_called_once()
        # stop() must happen before run_once(), which must happen before
        # start() — order matters (the one-shot run needs the persistent
        # service stopped first).
        call_order = [name for name, args, kwargs in driver.method_calls]
        self.assertEqual(
            [n for n in call_order if n in ('stop', 'run_once', 'start')],
            ['stop', 'run_once', 'start'])
        self.assertIn('upgrade output ok', out)

    def test_rejects_invalid_module_name(self):
        driver = MagicMock()
        with self.assertRaises(UserError):
            self._run(driver, module='not valid!')
        driver.stop.assert_not_called()

    def test_rejects_unknown_database(self):
        driver = MagicMock()
        with self.assertRaises(UserError):
            self._run(driver, db_names=['someone_else_db'])
        driver.stop.assert_not_called()

    def test_stop_failure_aborts_before_running_the_upgrade(self):
        driver = MagicMock()
        driver.stop.side_effect = RuntimeError('docker daemon unreachable')
        with self.assertRaises(UserError) as cm:
            self._run(driver)
        self.assertIn("Couldn't pause your instance", str(cm.exception))
        driver.run_once.assert_not_called()
        driver.start.assert_not_called()

    def test_upgrade_failure_still_brings_container_back_up(self):
        driver = MagicMock()
        driver.run_once.return_value = self._exec_result(1, 'traceback here')
        with self.assertRaises(UserError) as cm:
            self._run(driver)
        # The container-restart still happened despite the upgrade failure.
        driver.start.assert_called_once()
        self.assertTrue(hasattr(cm.exception, '_saas_upgrade_output'))
        self.assertIn('traceback here', cm.exception._saas_upgrade_output)

    def test_restart_failure_after_successful_upgrade_raises_distinct_error(self):
        driver = MagicMock()
        driver.run_once.return_value = self._exec_result(0, 'ok')
        driver.start.side_effect = RuntimeError('daemon unreachable')
        with self.assertRaises(UserError) as cm:
            self._run(driver)
        self.assertIn("didn't come back up automatically", str(cm.exception))

    def test_both_upgrade_and_restart_fail_reports_upgrade_error_first(self):
        driver = MagicMock()
        driver.run_once.return_value = self._exec_result(1, 'boom')
        driver.start.side_effect = RuntimeError('also broken')
        with self.assertRaises(UserError) as cm:
            self._run(driver)
        self.assertIn("didn't complete successfully", str(cm.exception))
