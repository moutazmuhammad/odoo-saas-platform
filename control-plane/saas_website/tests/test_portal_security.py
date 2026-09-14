import json
from unittest.mock import MagicMock, patch

from odoo import http
from odoo.exceptions import UserError
from odoo.tests.common import HttpCase, tagged


class _PortalTestBase(HttpCase):
    """Shared fixture for /my/instances/* portal-route tests: one hosting
    instance owned by `self.owner`, plus an unrelated `self.intruder`
    portal user to probe the ownership boundary."""

    def setUp(self):
        super().setUp()
        product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or \
            self.env['saas.product'].sudo().create(
                {'name': 'Portal Hosting', 'is_hosting': True, 'is_published': True})
        plan = self.env['saas.plan'].sudo().create({
            'name': 'Portal Plan', 'is_custom': True, 'workers': 1,
            'storage_limit': 5, 'cpu_limit': 1.0, 'ram_limit': '1g',
            'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [product.id])]})
        domain = self.env['saas.based.domain'].sudo().search([], limit=1) or \
            self.env['saas.based.domain'].sudo().create({'name': 'portal.example.com'})

        self.owner = self.env['res.users'].sudo().create({
            'name': 'Portal Owner', 'login': 'portalowner@example.com',
            'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])]})
        self.owner.password = 'ownerpass123'
        self.intruder = self.env['res.users'].sudo().create({
            'name': 'Portal Intruder', 'login': 'portalintruder@example.com',
            'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])]})
        self.intruder.password = 'intruderpass123'

        self.instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'portalinst', 'domain_id': domain.id,
            'partner_id': self.owner.partner_id.id,
            'saas_product_id': product.id, 'plan_id': plan.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running', 'is_hosting': True})

    def _json_call(self, route, params=None):
        resp = self.url_open(
            route,
            data=json.dumps({'jsonrpc': '2.0', 'method': 'call',
                             'params': params or {}}),
            headers={'Content-Type': 'application/json'})
        return resp.json().get('result')

    def _form_post(self, route, data=None):
        """POST to a csrf=True, type='http' portal route. Odoo's own test
        suite computes the token the same way: csrf_token() only needs
        self.env + self.session.sid, both of which HttpCase already sets,
        so the unbound Request method can be called with the TestCase
        itself standing in for the request (see e.g. odoo/addons/account's
        test_portal_attachment.py)."""
        payload = {'csrf_token': http.Request.csrf_token(self)}
        payload.update(data or {})
        return self.url_open(route, data=payload, allow_redirects=False)


@tagged('post_install', '-at_install')
class TestPortalInstanceSecurity(_PortalTestBase):
    """B.1.5: /my/instances/* portal routes (saas_website/controllers/portal.py)
    had zero test coverage. This first slice covers the one property shared
    by every route in the file — the access_token/ownership boundary via
    CustomerPortal._document_check_access() — plus the highest-consequence
    self-service actions (start/stop/restart).

    Same standing rule as saas_core's job-queue tests: action_restart()
    and action_portal_start() end in saas.job._enqueue(...), and
    action_stop()/action_portal_stop() end in run_in_background() — neither
    may be allowed to run for real inside a live HttpCase request, so their
    success paths patch those functions directly (never _spawn_worker),
    exactly like test_webhook_security.py's _patched_enqueue().
    """

    # ---- Auth boundary: shared by every route in portal.py ----------------

    def test_status_route_denies_non_owner(self):
        self.authenticate('portalintruder@example.com', 'intruderpass123')
        result = self._json_call('/my/instances/%d/status' % self.instance.id)
        self.assertEqual(result.get('error'), 'Access denied')

    def test_status_route_allows_owner(self):
        self.authenticate('portalowner@example.com', 'ownerpass123')
        result = self._json_call('/my/instances/%d/status' % self.instance.id)
        self.assertNotIn('error', result or {})

    def test_upgrade_page_denies_non_owner(self):
        self.authenticate('portalintruder@example.com', 'intruderpass123')
        resp = self.url_open('/my/instances/%d/upgrade' % self.instance.id)
        self.assertIn(resp.status_code, (200, 303))
        self.assertTrue(
            resp.url.endswith('/my/instances'),
            "non-owner must be redirected away, landed on %s" % resp.url)

    def test_restart_route_denies_non_owner(self):
        self.authenticate('portalintruder@example.com', 'intruderpass123')
        result = self._json_call('/my/instances/%d/restart' % self.instance.id)
        self.assertEqual(result.get('error'), 'Access denied')

    def test_start_and_stop_route_deny_non_owner(self):
        self.authenticate('portalintruder@example.com', 'intruderpass123')
        for route in ('start', 'stop'):
            result = self._json_call(
                '/my/instances/%d/%s' % (self.instance.id, route))
            self.assertEqual(result.get('error'), 'Access denied')

    # ---- Self-service lifecycle actions: state guards ----------------------

    def test_restart_rejects_non_running_instance(self):
        self.instance.sudo().write({'state': 'stopped'})
        self.authenticate('portalowner@example.com', 'ownerpass123')
        result = self._json_call('/my/instances/%d/restart' % self.instance.id)
        self.assertIn('must be running', (result or {}).get('error', ''))

    def test_stop_rejects_non_running_instance(self):
        self.instance.sudo().write({'state': 'stopped'})
        self.authenticate('portalowner@example.com', 'ownerpass123')
        result = self._json_call('/my/instances/%d/stop' % self.instance.id)
        self.assertIn('must be running', (result or {}).get('error', ''))

    def test_start_rejects_running_instance(self):
        # Already running (setUp default) — action_portal_start requires 'stopped'.
        self.authenticate('portalowner@example.com', 'ownerpass123')
        result = self._json_call('/my/instances/%d/start' % self.instance.id)
        self.assertIn('must be stopped', (result or {}).get('error', ''))

    def test_start_rejects_overdue_invoices(self):
        self.instance.sudo().write({'state': 'stopped'})
        with patch.object(
                type(self.instance), '_has_overdue_invoices_past_grace',
                lambda self: True):
            self.authenticate('portalowner@example.com', 'ownerpass123')
            result = self._json_call('/my/instances/%d/start' % self.instance.id)
        self.assertIn('overdue invoices', (result or {}).get('error', ''))

    # ---- Self-service lifecycle actions: success paths ---------------------
    # action_restart()/action_portal_start() end in saas.job._enqueue(...);
    # action_stop() ends in run_in_background(). Patch those directly so no
    # real job row or background thread is ever created under HttpCase.

    def test_restart_success_enqueues_restart_job(self):
        mock_enqueue = MagicMock(
            side_effect=lambda *a, **kw: self.env['saas.job'].browse())
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with patch.object(type(self.instance), '_ensure_can_ssh', lambda self: None), \
                patch.object(type(self.env['saas.job']), '_enqueue', mock_enqueue):
            result = self._json_call('/my/instances/%d/restart' % self.instance.id)
        self.assertTrue((result or {}).get('success'))
        mock_enqueue.assert_called_once()
        self.assertEqual(mock_enqueue.call_args.args[1], '_do_restart')
        self.assertEqual(self.instance.sudo().state, 'provisioning')

    def test_stop_success_runs_in_background(self):
        mock_run = MagicMock()
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with patch.object(type(self.instance), '_ensure_can_ssh', lambda self: None), \
                patch(
                    'odoo.addons.saas_core.models.saas_instance.run_in_background',
                    mock_run):
            result = self._json_call('/my/instances/%d/stop' % self.instance.id)
        self.assertTrue((result or {}).get('success'))
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args.args[1], '_do_stop')
        self.assertEqual(self.instance.sudo().state, 'provisioning')


@tagged('post_install', '-at_install')
class TestPortalDatabaseOps(_PortalTestBase):
    """B.1.5 continued: /my/instances/<id>/databases/* form-post routes.

    These are thin wrappers: real create/duplicate/drop/upgrade success
    paths (SSH, saas.job._enqueue vs. run_in_background) are already
    covered at the model layer in test_job_queue.py. Here the model
    methods themselves are mocked out entirely so these tests exercise
    only what's actually new at this layer: the ownership boundary, the
    portal-specific form validation (confirm-name-matches, both-fields-
    required), correct param pass-through, and redirect/notice wiring —
    exactly the "thin HTTP smoke test" approach flagged as remaining
    work in this plan's B.1.3 entry.
    """

    def _mock_method(self, name, return_value=None, side_effect=None):
        mock = MagicMock(return_value=return_value, side_effect=side_effect)
        return patch.object(type(self.instance), name, mock), mock

    def test_db_create_denies_non_owner(self):
        self.authenticate('portalintruder@example.com', 'intruderpass123')
        resp = self._form_post(
            '/my/instances/%d/databases/create' % self.instance.id,
            {'name': 'newdb', 'login': 'admin', 'password': 'adminpass1'})
        self.assertIn(resp.status_code, (301, 302, 303))
        self.assertTrue(resp.headers['Location'].endswith('/my/instances'))

    def test_db_create_success_redirects_with_notice(self):
        op = MagicMock(db_name='portalinst_newdb')
        p, mock = self._mock_method('hosting_db_create_async', return_value=op)
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with p:
            resp = self._form_post(
                '/my/instances/%d/databases/create' % self.instance.id,
                {'name': 'newdb', 'login': 'admin', 'password': 'adminpass1'})
        self.assertIn(resp.status_code, (301, 302, 303))
        location = resp.headers['Location']
        self.assertIn('/databases?notice=', location)
        mock.assert_called_once_with(
            name='newdb', login='admin', password='adminpass1',
            lang='en_US', country_code=None)

    def test_db_create_propagates_model_rejection(self):
        p, mock = self._mock_method(
            'hosting_db_create_async',
            side_effect=UserError("Database 'x' already exists."))
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with p:
            resp = self._form_post(
                '/my/instances/%d/databases/create' % self.instance.id,
                {'name': 'dup', 'login': 'admin', 'password': 'adminpass1'})
        self.assertIn('/databases?error=', resp.headers['Location'])

    def test_db_duplicate_success_redirects_with_notice(self):
        op = MagicMock(db_name='portalinst_copy')
        p, mock = self._mock_method('hosting_db_duplicate_async', return_value=op)
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with p:
            resp = self._form_post(
                '/my/instances/%d/databases/duplicate' % self.instance.id,
                {'source': 'prod', 'new_name': 'copy'})
        self.assertIn('/databases?notice=', resp.headers['Location'])
        mock.assert_called_once_with(source='prod', new_name='copy')

    def test_db_drop_requires_exact_confirmation(self):
        self.authenticate('portalowner@example.com', 'ownerpass123')
        resp = self._form_post(
            '/my/instances/%d/databases/drop' % self.instance.id,
            {'name': 'proddb', 'confirm': 'proddb-typo'})
        self.assertIn('/databases?error=', resp.headers['Location'])

    def test_db_drop_success_redirects_with_notice(self):
        op = MagicMock(db_name='portalinst_proddb')
        p, mock = self._mock_method('hosting_db_drop_async', return_value=op)
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with p:
            resp = self._form_post(
                '/my/instances/%d/databases/drop' % self.instance.id,
                {'name': 'proddb', 'confirm': 'proddb'})
        self.assertIn('/databases?notice=', resp.headers['Location'])
        mock.assert_called_once_with(name='proddb')

    def test_db_upgrade_module_requires_both_fields(self):
        self.authenticate('portalowner@example.com', 'ownerpass123')
        resp = self._form_post(
            '/my/instances/%d/databases/upgrade-module' % self.instance.id,
            {'name': '', 'module': 'sale'})
        self.assertIn('/databases?error=', resp.headers['Location'])

    def test_db_upgrade_module_success_redirects_with_notice(self):
        op = MagicMock(db_name='portalinst_proddb', module_name='sale')
        p, mock = self._mock_method(
            'hosting_db_upgrade_module_async', return_value=op)
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with p:
            resp = self._form_post(
                '/my/instances/%d/databases/upgrade-module' % self.instance.id,
                {'name': 'proddb', 'module': 'sale'})
        self.assertIn('/databases?notice=', resp.headers['Location'])
        mock.assert_called_once_with(name='proddb', module='sale')

    def test_reset_admin_password_requires_matching_confirmation(self):
        self.authenticate('portalowner@example.com', 'ownerpass123')
        resp = self._form_post(
            '/my/instances/%d/databases/reset-admin-password' % self.instance.id,
            {'name': 'proddb', 'new_password': 'newpass1',
             'confirm_password': 'different1'})
        self.assertIn('/databases?error=', resp.headers['Location'])

    def test_reset_admin_password_success_redirects_with_notice(self):
        p, mock = self._mock_method(
            'hosting_db_reset_admin_password', return_value='admin')
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with p:
            resp = self._form_post(
                '/my/instances/%d/databases/reset-admin-password' % self.instance.id,
                {'name': 'proddb', 'new_password': 'newpass1',
                 'confirm_password': 'newpass1'})
        self.assertIn('/databases?notice=', resp.headers['Location'])
        mock.assert_called_once_with(name='proddb', new_password='newpass1')

    def test_db_op_dismiss_ignores_op_from_another_instance(self):
        other = self.env['saas.instance'].sudo().create({
            'subdomain': 'otherportalinst', 'domain_id': self.instance.domain_id.id,
            'partner_id': self.owner.partner_id.id,
            'saas_product_id': self.instance.saas_product_id.id,
            'plan_id': self.instance.plan_id.id, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': False,
            'state': 'running', 'is_hosting': True})
        op = self.env['saas.instance.db.operation'].sudo().create({
            'instance_id': other.id, 'db_name': 'otherdb', 'operation': 'create'})
        self.authenticate('portalowner@example.com', 'ownerpass123')
        resp = self._form_post(
            '/my/instances/%d/databases/op/%d/dismiss'
            % (self.instance.id, op.id))
        self.assertIn(resp.status_code, (301, 302, 303))
        self.assertTrue(op.exists(), "op belonging to a different instance must survive")

    def test_db_op_dismiss_deletes_own_op(self):
        op = self.env['saas.instance.db.operation'].sudo().create({
            'instance_id': self.instance.id, 'db_name': 'proddb', 'operation': 'create'})
        self.authenticate('portalowner@example.com', 'ownerpass123')
        self._form_post(
            '/my/instances/%d/databases/op/%d/dismiss'
            % (self.instance.id, op.id))
        self.assertFalse(op.exists())


@tagged('post_install', '-at_install')
class TestPortalChangePlan(_PortalTestBase):
    """B.1.5 continued: /my/instances/<id>/{change-plan,do-change-plan,
    cancel-upgrade,cancel-downgrade} — real portal-layer business logic
    (storage-reduction block, no-change detection, config clamping,
    upgrade-vs-downgrade branch selection), unlike the databases/* routes
    which are thin wrappers.

    action_request_plan_change()/_request_downgrade() are themselves
    synchronous, SSH-free, ORM/billing-only methods (no saas.job/
    run_in_background involved) and already have dedicated model-layer
    coverage (test_billing_overhaul.py), so — same "thin HTTP smoke
    test" principle as TestPortalDatabaseOps — they're mocked here too,
    to isolate what's actually new at this layer: which of the two the
    portal route picks (based on new_workers vs. current_workers) and
    how it turns the return value into a redirect (checkout for a
    charged upgrade, back to the instance otherwise).
    """

    def setUp(self):
        super().setUp()
        # A bigger current plan than the base fixture's (workers=1) so a
        # downgrade (fewer workers, still >0) is reachable.
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'Portal Plan (current)', 'is_custom': True, 'workers': 4,
            'storage_limit': 10, 'cpu_limit': 2.0, 'ram_limit': '2g',
            'price': 40.0, 'yearly_price': 384.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.instance.saas_product_id.id])]})
        self.instance.sudo().write({'plan_id': self.plan.id, 'billing_period': 'monthly'})

    def _do_change_plan(self, workers, storage, billing_period='monthly'):
        return self._form_post(
            '/my/instances/%d/do-change-plan' % self.instance.id,
            {'workers': str(workers), 'storage': str(storage),
             'billing_period': billing_period})

    def test_change_plan_page_denies_non_owner(self):
        self.authenticate('portalintruder@example.com', 'intruderpass123')
        resp = self.url_open(
            '/my/instances/%d/change-plan' % self.instance.id,
            allow_redirects=False)
        self.assertIn(resp.status_code, (301, 302, 303))
        self.assertTrue(resp.headers['Location'].endswith('/my/instances'))

    def test_change_plan_page_redirects_for_trial_instance(self):
        self.instance.sudo().write({'is_trial': True})
        self.authenticate('portalowner@example.com', 'ownerpass123')
        resp = self.url_open(
            '/my/instances/%d/change-plan' % self.instance.id,
            allow_redirects=False)
        self.assertEqual(
            resp.headers['Location'], '/my/instances/%d' % self.instance.id)

    def test_change_plan_page_renders_for_eligible_instance(self):
        self.authenticate('portalowner@example.com', 'ownerpass123')
        resp = self.url_open('/my/instances/%d/change-plan' % self.instance.id)
        self.assertEqual(resp.status_code, 200)

    def test_do_change_plan_denies_non_owner(self):
        self.authenticate('portalintruder@example.com', 'intruderpass123')
        resp = self._do_change_plan(8, 20)
        self.assertTrue(resp.headers['Location'].endswith('/my/instances'))

    def test_do_change_plan_blocks_storage_reduction(self):
        self.authenticate('portalowner@example.com', 'ownerpass123')
        resp = self._do_change_plan(4, 5)  # storage 5 < current 10
        self.assertIn('/change-plan?error=', resp.headers['Location'])
        self.assertIn(
            'Storage cannot be reduced',
            resp.headers['Location'].replace('%20', ' '))

    def test_do_change_plan_requires_both_fields(self):
        self.authenticate('portalowner@example.com', 'ownerpass123')
        resp = self._do_change_plan(0, 10)
        self.assertIn('/change-plan?error=', resp.headers['Location'])

    def test_do_change_plan_rejects_no_actual_change(self):
        self.authenticate('portalowner@example.com', 'ownerpass123')
        resp = self._do_change_plan(4, 10, 'monthly')
        self.assertIn('/change-plan?error=', resp.headers['Location'])
        self.assertIn(
            'No changes selected',
            resp.headers['Location'].replace('%20', ' '))

    def test_do_change_plan_downgrade_calls_request_downgrade_and_redirects_home(self):
        # The recordset args a mock captures live on the HTTP request's own
        # cursor, which is closed by the time this test method regains
        # control — reading a field off it afterwards raises "Cannot use a
        # closed cursor". Pull out the plain values inside the side_effect,
        # while the cursor is still open, instead of keeping the recordset.
        seen = {}

        def _capture(plan, period):
            seen['workers'] = plan.workers
            seen['period'] = period
            return 'scheduled'

        mock_downgrade = MagicMock(side_effect=_capture)
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with patch.object(type(self.instance), '_request_downgrade', mock_downgrade):
            resp = self._do_change_plan(2, 10)  # fewer workers = downgrade
        self.assertEqual(
            resp.headers['Location'], '/my/instances/%d' % self.instance.id)
        mock_downgrade.assert_called_once()
        self.assertEqual(seen.get('workers'), 2)
        self.assertEqual(seen.get('period'), 'monthly')

    def test_do_change_plan_upgrade_with_charge_redirects_to_checkout(self):
        mock_upgrade = MagicMock(return_value=MagicMock(amount_total=25.0))
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with patch.object(
                type(self.instance), 'action_request_plan_change', mock_upgrade):
            resp = self._do_change_plan(8, 20)  # more workers = upgrade
        self.assertEqual(
            resp.headers['Location'],
            '/my/instances/%d/checkout' % self.instance.id)
        mock_upgrade.assert_called_once()
        self.assertEqual(mock_upgrade.call_args.kwargs.get('billing_period'), 'monthly')

    def test_do_change_plan_zero_charge_upgrade_redirects_home(self):
        mock_upgrade = MagicMock(return_value=True)
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with patch.object(
                type(self.instance), 'action_request_plan_change', mock_upgrade):
            resp = self._do_change_plan(8, 20)
        self.assertEqual(
            resp.headers['Location'], '/my/instances/%d' % self.instance.id)

    def test_do_change_plan_propagates_model_rejection(self):
        mock_upgrade = MagicMock(side_effect=UserError("A downgrade is already scheduled."))
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with patch.object(
                type(self.instance), 'action_request_plan_change', mock_upgrade):
            resp = self._do_change_plan(8, 20)
        self.assertIn('/change-plan?error=', resp.headers['Location'])

    def test_cancel_upgrade_denies_non_owner(self):
        self.authenticate('portalintruder@example.com', 'intruderpass123')
        resp = self._form_post(
            '/my/instances/%d/cancel-upgrade' % self.instance.id)
        self.assertTrue(resp.headers['Location'].endswith('/my/instances'))

    def test_cancel_upgrade_cancels_pending_plan_change(self):
        mock_cancel = MagicMock()
        self.instance.sudo().write({'pending_plan_id': self.plan.id})
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with patch.object(type(self.instance), '_cancel_pending_upgrade', mock_cancel):
            resp = self._form_post(
                '/my/instances/%d/cancel-upgrade' % self.instance.id)
        mock_cancel.assert_called_once()
        self.assertEqual(
            resp.headers['Location'], '/my/instances/%d' % self.instance.id)

    def test_cancel_upgrade_is_a_noop_with_no_pending_change(self):
        mock_cancel = MagicMock()
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with patch.object(type(self.instance), '_cancel_pending_upgrade', mock_cancel):
            self._form_post('/my/instances/%d/cancel-upgrade' % self.instance.id)
        mock_cancel.assert_not_called()

    def test_cancel_downgrade_denies_non_owner(self):
        self.authenticate('portalintruder@example.com', 'intruderpass123')
        resp = self._form_post(
            '/my/instances/%d/cancel-downgrade' % self.instance.id)
        self.assertTrue(resp.headers['Location'].endswith('/my/instances'))

    def test_cancel_downgrade_calls_model_method(self):
        mock_cancel = MagicMock()
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with patch.object(
                type(self.instance), 'action_cancel_scheduled_downgrade', mock_cancel):
            resp = self._form_post(
                '/my/instances/%d/cancel-downgrade' % self.instance.id)
        mock_cancel.assert_called_once()
        self.assertEqual(
            resp.headers['Location'], '/my/instances/%d' % self.instance.id)


@tagged('post_install', '-at_install')
class TestPortalDataRestoreRequests(_PortalTestBase):
    """B.1.5 continued: /my/instances/<id>/{request-restore,
    dismiss-restore-banner,decline-restore} — all synchronous JSON
    routes, no SSH/job queue involved, so no infra mocking hazard here.
    request-restore sends a real odoo mail.mail; that's exercised for
    real (outgoing mail is captured by Odoo's test-mode mail queue, it
    never actually leaves the process) since it's the one meaningfully
    new behaviour at this layer (the "email delivery failed" branch)."""

    def test_request_restore_denies_non_owner(self):
        self.authenticate('portalintruder@example.com', 'intruderpass123')
        result = self._json_call(
            '/my/instances/%d/request-restore' % self.instance.id)
        self.assertTrue((result or {}).get('error'))

    def test_request_restore_rejects_no_retained_backup(self):
        self.authenticate('portalowner@example.com', 'ownerpass123')
        result = self._json_call(
            '/my/instances/%d/request-restore' % self.instance.id)
        self.assertIn('No backup available', (result or {}).get('error', ''))

    def test_request_restore_rejects_missing_support_email(self):
        self.instance.sudo().write({'retained_backup_path': '/backups/x.tar.gz'})
        self.env['ir.config_parameter'].sudo().set_param(
            'saas_master.support_email', '')
        self.authenticate('portalowner@example.com', 'ownerpass123')
        result = self._json_call(
            '/my/instances/%d/request-restore' % self.instance.id)
        self.assertIn('Support email is not configured', (result or {}).get('error', ''))

    def test_request_restore_success_sends_mail_and_logs(self):
        self.instance.sudo().write({'retained_backup_path': '/backups/x.tar.gz'})
        self.env['ir.config_parameter'].sudo().set_param(
            'saas_master.support_email', 'support@example.com')
        self.authenticate('portalowner@example.com', 'ownerpass123')
        result = self._json_call(
            '/my/instances/%d/request-restore' % self.instance.id)
        self.assertTrue((result or {}).get('success'))
        mail = self.env['mail.mail'].sudo().search([
            ('email_to', '=', 'support@example.com'),
            ('subject', 'like', self.instance.subdomain)],
            limit=1, order='id desc')
        self.assertTrue(mail, "a real mail.mail record must be created")

    def test_request_restore_surfaces_delivery_failure(self):
        self.instance.sudo().write({'retained_backup_path': '/backups/x.tar.gz'})
        self.env['ir.config_parameter'].sudo().set_param(
            'saas_master.support_email', 'support@example.com')
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with patch.object(
                type(self.env['mail.mail']), 'send',
                lambda self, *a, **kw: self.write({
                    'state': 'exception', 'failure_reason': 'boom'})):
            result = self._json_call(
                '/my/instances/%d/request-restore' % self.instance.id)
        self.assertTrue((result or {}).get('error'))
        self.assertNotIn('success', result or {})

    def test_dismiss_restore_banner_denies_non_owner(self):
        self.authenticate('portalintruder@example.com', 'intruderpass123')
        result = self._json_call(
            '/my/instances/%d/dismiss-restore-banner' % self.instance.id)
        self.assertTrue((result or {}).get('error'))

    def test_dismiss_restore_banner_success(self):
        self.authenticate('portalowner@example.com', 'ownerpass123')
        result = self._json_call(
            '/my/instances/%d/dismiss-restore-banner' % self.instance.id)
        self.assertTrue((result or {}).get('success'))
        self.assertTrue(self.instance.sudo().restore_banner_dismissed)

    def test_decline_restore_denies_non_owner(self):
        self.authenticate('portalintruder@example.com', 'intruderpass123')
        result = self._json_call(
            '/my/instances/%d/decline-restore' % self.instance.id)
        self.assertTrue((result or {}).get('error'))

    def test_decline_restore_clears_restore_state(self):
        self.instance.sudo().write({
            'retained_backup_path': '/backups/x.tar.gz',
            'restore_banner_dismissed': False,
        })
        self.authenticate('portalowner@example.com', 'ownerpass123')
        result = self._json_call(
            '/my/instances/%d/decline-restore' % self.instance.id)
        self.assertTrue((result or {}).get('success'))
        self.instance.invalidate_recordset()
        self.assertFalse(self.instance.sudo().retained_backup_path)
        self.assertTrue(self.instance.sudo().restore_banner_dismissed)


@tagged('post_install', '-at_install')
class TestPortalInstanceFolders(_PortalTestBase):
    """B.1.5 continued: /my/instances/folder/* and /my/instances/move —
    pure ORM CRUD, partner-scoped by hand (no _document_check_access;
    every query filters by request.env.user.partner_id itself), so the
    ownership boundary is tested the same way the routes enforce it:
    trying to touch a folder or instance that belongs to someone else
    and confirming the route can't find it (not: confirming an
    AccessError, since none of these routes raise one)."""

    def _create_folder(self, name, parent_id=None, owner_login=None,
                       owner_pass=None):
        self.authenticate(
            owner_login or 'portalowner@example.com',
            owner_pass or 'ownerpass123')
        params = {'name': name}
        if parent_id:
            params['parent_id'] = parent_id
        return self._json_call('/my/instances/folder/create', params)

    def test_folder_create_requires_name(self):
        result = self._create_folder('')
        self.assertTrue((result or {}).get('error'))

    def test_folder_create_success(self):
        result = self._create_folder('My Folder')
        self.assertTrue((result or {}).get('success'))
        folder = self.env['saas.instance.folder'].sudo().browse(result['folder_id'])
        self.assertEqual(folder.partner_id, self.owner.partner_id)

    def test_folder_create_ignores_parent_owned_by_someone_else(self):
        foreign = self._create_folder(
            'Intruder Folder', owner_login='portalintruder@example.com',
            owner_pass='intruderpass123')
        result = self._create_folder('Mine', parent_id=foreign['folder_id'])
        folder = self.env['saas.instance.folder'].sudo().browse(result['folder_id'])
        self.assertFalse(
            folder.parent_id,
            "a parent_id owned by another partner must be silently ignored")

    def test_folder_rename_requires_name(self):
        created = self._create_folder('Original')
        result = self._json_call(
            '/my/instances/folder/%d/rename' % created['folder_id'],
            {'name': ''})
        self.assertTrue((result or {}).get('error'))

    def test_folder_rename_denies_foreign_folder(self):
        foreign = self._create_folder(
            'Intruder Folder', owner_login='portalintruder@example.com',
            owner_pass='intruderpass123')
        self.authenticate('portalowner@example.com', 'ownerpass123')
        result = self._json_call(
            '/my/instances/folder/%d/rename' % foreign['folder_id'],
            {'name': 'Stolen'})
        self.assertIn('not found', (result or {}).get('error', '').lower())

    def test_folder_rename_success(self):
        created = self._create_folder('Original')
        result = self._json_call(
            '/my/instances/folder/%d/rename' % created['folder_id'],
            {'name': 'Renamed'})
        self.assertTrue((result or {}).get('success'))
        folder = self.env['saas.instance.folder'].sudo().browse(created['folder_id'])
        self.assertEqual(folder.name, 'Renamed')

    def test_folder_delete_blocked_when_it_has_subfolders(self):
        parent = self._create_folder('Parent')
        self._create_folder('Child', parent_id=parent['folder_id'])
        result = self._json_call(
            '/my/instances/folder/%d/delete' % parent['folder_id'])
        self.assertTrue((result or {}).get('error'))
        self.assertTrue(
            self.env['saas.instance.folder'].sudo().browse(
                parent['folder_id']).exists())

    def test_folder_delete_moves_instances_to_unfiled(self):
        folder = self._create_folder('Holds an instance')
        self.instance.sudo().write({'folder_id': folder['folder_id']})
        result = self._json_call(
            '/my/instances/folder/%d/delete' % folder['folder_id'])
        self.assertTrue((result or {}).get('success'))
        self.assertFalse(self.instance.sudo().folder_id)

    def test_move_instance_to_folder_denies_foreign_instance(self):
        folder = self._create_folder(
            'Intruder Folder', owner_login='portalintruder@example.com',
            owner_pass='intruderpass123')
        # still authenticated as the intruder from _create_folder above
        result = self._json_call('/my/instances/move', {
            'instance_ids': [self.instance.id],
            'folder_id': folder['folder_id']})
        self.assertTrue((result or {}).get('error'))
        self.assertFalse(self.instance.sudo().folder_id)

    def test_move_instance_to_folder_success(self):
        folder = self._create_folder('Target')
        result = self._json_call('/my/instances/move', {
            'instance_ids': [self.instance.id],
            'folder_id': folder['folder_id']})
        self.assertTrue((result or {}).get('success'))
        self.assertEqual(self.instance.sudo().folder_id.id, folder['folder_id'])

    def test_move_instance_to_unfiled(self):
        folder = self._create_folder('Target')
        self.instance.sudo().write({'folder_id': folder['folder_id']})
        result = self._json_call('/my/instances/move', {
            'instance_ids': [self.instance.id], 'folder_id': False})
        self.assertTrue((result or {}).get('success'))
        self.assertFalse(self.instance.sudo().folder_id)
