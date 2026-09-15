"""Characterization tests for _hosting_build_template_db — the one-time
per-instance template DB bootstrap (pause the live container, run a
one-shot `odoo -i base` init on a core-only addons path, always resume
the container regardless of outcome, verify at the PG level and clean up
on failure). destroy()/start() for the pause/resume were already routed
through ComputeDriver; only the middle `docker compose run` init call
was still raw, with zero test coverage of its own before this file —
same "characterize first" discipline as the other Phase 2.3 clusters."""
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
    def _ssh_mock(execute_return=(0, '', '')):
        """A MagicMock usable as `with ... as ssh:` whose execute() returns
        a real 3-tuple — a bare MagicMock() doesn't, and this method's
        pre-routing raw ssh.execute() call for the init command (still
        present until the swap lands in the same pass) needs one."""
        m = MagicMock()
        m.__enter__ = MagicMock(return_value=m)
        m.__exit__ = MagicMock(return_value=False)
        m.execute.return_value = execute_return
        return m

    def _run(self, driver, pg_initialized=True, drop_db=None, drop_filestore=None,
             ssh_execute_return=(0, '', '')):
        Inst = type(self.instance)
        self._ssh = self._ssh_mock(ssh_execute_return)
        with patch.object(type(self.server), '_get_ssh_connection',
                          lambda rec: self._ssh), \
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
        # NOTE: today's implementation still runs the init via a raw
        # ssh.execute('docker compose run ...') call, not driver.run_once()
        # — only destroy()/start() (pause/resume) are routed so far. This
        # asserts on the CURRENT behavior; it gets updated once the init
        # call itself is routed through driver.run_once().
        driver = MagicMock()
        result = self._run(driver, ssh_execute_return=(0, 'init ok', ''))
        self.assertEqual(result, 'tmpl_18_0')
        driver.destroy.assert_called_once()
        self._ssh.execute.assert_called_once()
        init_cmd = self._ssh.execute.call_args.args[0]
        self.assertIn('-i base', init_cmd)
        self.assertIn('-d tmpl_18_0', init_cmd)
        self.assertIn('--addons-path=/opt/odoo/addons', init_cmd)
        driver.start.assert_called_once()

    def test_pause_failure_is_tolerated_and_init_still_runs(self):
        # Pausing is best-effort (the comment: "best-effort pause before
        # the one-time template build") — a destroy() failure must not
        # abort the whole build.
        driver = MagicMock()
        driver.destroy.side_effect = RuntimeError('already stopped')
        result = self._run(driver, ssh_execute_return=(0, 'init ok', ''))
        self.assertEqual(result, 'tmpl_18_0')
        self._ssh.execute.assert_called_once()
        driver.start.assert_called_once()

    def test_container_always_resumed_even_when_init_fails(self):
        driver = MagicMock()
        with self.assertRaises(UserError) as cm:
            self._run(driver, pg_initialized=False,
                     ssh_execute_return=(1, 'init traceback', ''))
        driver.start.assert_called_once()
        self.assertIn('init traceback', str(cm.exception))

    def test_resume_failure_is_logged_not_raised(self):
        # A failed resume must not mask (or replace) the real init-failure
        # error, and must not itself blow up the method — best-effort,
        # matching the original `except Exception as e: self._append_log(...)`.
        driver = MagicMock()
        driver.start.side_effect = RuntimeError('daemon unreachable')
        result = self._run(driver, ssh_execute_return=(0, 'init ok', ''))  # must not raise
        self.assertEqual(result, 'tmpl_18_0')

    def test_pg_verification_failure_cleans_up_and_raises(self):
        driver = MagicMock()
        drop_mock = MagicMock()
        fs_mock = MagicMock()
        with self.assertRaises(UserError) as cm:
            self._run(driver, pg_initialized=False,
                     ssh_execute_return=(0, 'init ok, but PG disagrees', ''),
                     drop_db=drop_mock, drop_filestore=fs_mock)
        self.assertIn("Couldn't prepare the database template", str(cm.exception))
        drop_mock.assert_called_once()
        fs_mock.assert_called_once()
