"""Characterization tests for _hosting_build_template_db — the one-time
per-instance template DB bootstrap (pause the live container, run a
one-shot `odoo -i base` init on a core-only addons path, always resume
the container regardless of outcome, verify at the PG level and clean up
on failure). All three container operations (destroy/run_once/start) are
routed through ComputeDriver — this file originally characterized the
pre-swap raw `docker compose run` init call, then was updated in the
same pass that routed it through driver.run_once(), matching the
established pattern from test_hosting_db_upgrade_module.py."""
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestHostingBuildTemplateDb(TransactionCase):
    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or self.env['saas.product'].sudo().create(
            {'name': 'TD Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'TD Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.domain = self.env['saas.based.domain'].sudo().create({'name': 'td.example.com'})
        self.partner = self.env['res.partner'].sudo().create({'name': 'TD Cust'})
        self.server = self.env['saas.server'].sudo().create(
            {'name': 'td-srv', 'docker_base_path': '/home/odoo'})
        self.instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'tdtest', 'domain_id': self.domain.id, 'partner_id': self.partner.id,
            'saas_product_id': self.product.id, 'plan_id': self.plan.id,
            'docker_server_id': self.server.id, 'is_hosting': True,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running'})

    @staticmethod
    def _ssh_mock():
        m = MagicMock()
        m.__enter__ = MagicMock(return_value=m)
        m.__exit__ = MagicMock(return_value=False)
        return m

    @staticmethod
    def _exec_result(rc, stdout='', stderr=''):
        from odoo.addons.saas_core.drivers.base import ExecResult
        return ExecResult(rc=rc, stdout=stdout, stderr=stderr)

    def _run(self, driver, pg_initialized=True, drop_db=None, drop_filestore=None):
        Inst = type(self.instance)
        with patch.object(type(self.server), '_get_ssh_connection',
                          lambda rec: self._ssh_mock()), \
                patch.object(Inst, '_compute_driver', return_value=driver), \
                patch.object(Inst, '_pg_ensure_db_with_grants', lambda rec, t: None), \
                patch.object(Inst, '_pg_mark_template', lambda rec, t, flag=True: None), \
                patch.object(Inst, '_hosting_core_addons_path', lambda rec, ssh: '/opt/odoo/addons'), \
                patch.object(Inst, '_pg_db_initialized', lambda rec, t: pg_initialized), \
                patch.object(Inst, '_pg_drop_db', drop_db or (lambda rec, t: None)), \
                patch.object(Inst, '_hosting_drop_filestore',
                             drop_filestore or (lambda rec, t: None)):
            return self.instance._hosting_build_template_db('tmpl_18_0')

    def test_happy_path_pauses_inits_and_resumes(self):
        driver = MagicMock()
        driver.run_once.return_value = self._exec_result(0, 'init ok')
        result = self._run(driver)
        self.assertEqual(result, 'tmpl_18_0')
        driver.destroy.assert_called_once()
        driver.run_once.assert_called_once()
        run_args = driver.run_once.call_args.args[1]
        self.assertIn('-i base', run_args)
        self.assertIn('-d tmpl_18_0', run_args)
        self.assertIn('--addons-path=/opt/odoo/addons', run_args)
        driver.start.assert_called_once()
        call_order = [name for name, args, kwargs in driver.method_calls]
        self.assertEqual(
            [n for n in call_order if n in ('destroy', 'run_once', 'start')],
            ['destroy', 'run_once', 'start'])

    def test_pause_failure_is_tolerated_and_init_still_runs(self):
        # Pausing is best-effort (the comment: "best-effort pause before
        # the one-time template build") — a destroy() failure must not
        # abort the whole build.
        driver = MagicMock()
        driver.destroy.side_effect = RuntimeError('already stopped')
        driver.run_once.return_value = self._exec_result(0, 'init ok')
        result = self._run(driver)
        self.assertEqual(result, 'tmpl_18_0')
        driver.run_once.assert_called_once()
        driver.start.assert_called_once()

    def test_container_always_resumed_even_when_init_fails(self):
        driver = MagicMock()
        driver.run_once.return_value = self._exec_result(1, 'init traceback')
        with self.assertRaises(UserError) as cm:
            self._run(driver, pg_initialized=False)
        driver.start.assert_called_once()
        self.assertIn('init traceback', str(cm.exception))

    def test_resume_failure_is_logged_not_raised(self):
        # A failed resume must not mask (or replace) the real init-failure
        # error, and must not itself blow up the method — best-effort,
        # matching the original `except Exception as e: self._append_log(...)`.
        driver = MagicMock()
        driver.run_once.return_value = self._exec_result(0, 'init ok')
        driver.start.side_effect = RuntimeError('daemon unreachable')
        result = self._run(driver)  # must not raise
        self.assertEqual(result, 'tmpl_18_0')

    def test_pg_verification_failure_cleans_up_and_raises(self):
        driver = MagicMock()
        driver.run_once.return_value = self._exec_result(0, 'init ok, but PG disagrees')
        drop_mock = MagicMock()
        fs_mock = MagicMock()
        with self.assertRaises(UserError) as cm:
            self._run(driver, pg_initialized=False,
                     drop_db=drop_mock, drop_filestore=fs_mock)
        self.assertIn("Couldn't prepare the database template", str(cm.exception))
        drop_mock.assert_called_once()
        fs_mock.assert_called_once()
