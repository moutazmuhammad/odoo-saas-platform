import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields, http
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
    and action_portal_start() end in saas.job.enqueue(...), and
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
    # action_restart()/action_portal_start() end in saas.job.enqueue(...);
    # action_stop() ends in run_in_background(). Patch those directly so no
    # real job row or background thread is ever created under HttpCase.

    def test_restart_success_enqueues_restart_job(self):
        mock_enqueue = MagicMock(
            side_effect=lambda *a, **kw: self.env['saas.job'].browse())
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with patch.object(type(self.env['saas.job']), 'enqueue', mock_enqueue):
            result = self._json_call('/my/instances/%d/restart' % self.instance.id)
        self.assertTrue((result or {}).get('success'))
        mock_enqueue.assert_called_once()
        self.assertEqual(mock_enqueue.call_args.args[1], '_do_restart')
        self.assertEqual(self.instance.sudo().state, 'provisioning')

    def test_stop_success_runs_in_background(self):
        mock_run = MagicMock()
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with patch(
                'odoo.addons.saas_core.models.saas_instance.run_in_background',
                mock_run):
            result = self._json_call('/my/instances/%d/stop' % self.instance.id)
        self.assertTrue((result or {}).get('success'))
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args.args[1], '_do_stop')
        self.assertEqual(self.instance.sudo().state, 'provisioning')

    # TestPortalDatabaseOps (the /my/instances/<id>/databases/* form-post
    # routes: create/duplicate/drop/upgrade-module/reset-admin-password)
    # was removed entirely: hosting_db_create_async/_duplicate_async/
    # _drop_async/_upgrade_module_async/_reset_admin_password (the
    # ssh_docker/docker-exec DB self-service family) were removed along
    # with that backend — DB self-service is gone for now (a later phase
    # reimplements it via driver.exec()).


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

    def _retained_snapshot(self):
        """The full-instance snapshot a cancellation retains."""
        return self.env['saas.instance.backup'].sudo().create({
            'instance_id': self.instance.id,
            'name': '20260101T000000Z',
            'is_full_instance': True,
            'format': 'operator',
            'bucket_path': 'backups/x/20260101T000000Z',
            'state': 'done',
        })

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
        self._retained_snapshot()
        self.env['ir.config_parameter'].sudo().set_param(
            'saas_master.support_email', '')
        self.authenticate('portalowner@example.com', 'ownerpass123')
        result = self._json_call(
            '/my/instances/%d/request-restore' % self.instance.id)
        self.assertIn('Support email is not configured', (result or {}).get('error', ''))

    def test_request_restore_success_sends_mail_and_logs(self):
        self._retained_snapshot()
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
        self._retained_snapshot()
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
        self._retained_snapshot()
        self.instance.sudo().write({'restore_banner_dismissed': False})
        self.authenticate('portalowner@example.com', 'ownerpass123')
        result = self._json_call(
            '/my/instances/%d/decline-restore' % self.instance.id)
        self.assertTrue((result or {}).get('success'))
        self.instance.invalidate_recordset()
        self.assertFalse(self.instance.sudo().restoration_backup_id)
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


@tagged('post_install', '-at_install')
class TestPortalBackups(_PortalTestBase):
    """B.1.5 continued: /my/instances/<id>/{backups/ondemand,
    backup(s)/<id>/{discard,download,restore}}.

    On-demand backup (/backups/ondemand) validated the requested db_name
    via hosting_db_list() (ssh_docker/docker-exec) before creating the
    backup — that whole family was removed along with ssh_docker, so
    on-demand backup is gone for now (a later phase reimplements it via
    the OdooInstance CR's spec.backup, see the removal plan's Phase 5).
    Only the auth/state-guard tests (which reject before ever reaching
    hosting_db_list) remain below; the "requires db_name"/"rejects
    unknown db"/"blocked while running"/"success" tests and the
    _mock_hosting_db_list helper were removed.

    action_restore_backup()/action_restore_full_instance() are similarly
    gone (see TestPortalBackups' restore tests further down) — both were
    async, ssh_docker-heavy model methods.
    """

    def test_ondemand_denies_non_owner(self):
        self.authenticate('portalintruder@example.com', 'intruderpass123')
        resp = self._form_post(
            '/my/instances/%d/backups/ondemand' % self.instance.id,
            {'db_name': 'proddb'})
        self.assertTrue(resp.headers['Location'].endswith('/my/instances'))

    def test_ondemand_rejects_non_hosting_instance(self):
        self.instance.sudo().write({'is_hosting': False})
        self.authenticate('portalowner@example.com', 'ownerpass123')
        resp = self._form_post(
            '/my/instances/%d/backups/ondemand' % self.instance.id,
            {'db_name': 'proddb'})
        self.assertIn('/databases?error=', resp.headers['Location'])

    def test_ondemand_rejects_instance_not_running(self):
        self.instance.sudo().write({'state': 'stopped'})
        self.authenticate('portalowner@example.com', 'ownerpass123')
        resp = self._form_post(
            '/my/instances/%d/backups/ondemand' % self.instance.id,
            {'db_name': 'proddb'})
        self.assertIn('/databases?error=', resp.headers['Location'])

    def test_discard_denies_non_owner(self):
        backup = self.env['saas.instance.backup'].sudo().create({
            'instance_id': self.instance.id, 'db_name': 'proddb',
            'name': 'ondemand_x', 'state': 'done', 'ephemeral': True})
        self.authenticate('portalintruder@example.com', 'intruderpass123')
        resp = self._form_post(
            '/my/instances/%d/backups/%d/discard'
            % (self.instance.id, backup.id))
        self.assertTrue(resp.headers['Location'].endswith('/my/instances'))

    def test_discard_not_found_for_non_ephemeral_backup(self):
        backup = self.env['saas.instance.backup'].sudo().create({
            'instance_id': self.instance.id, 'db_name': 'proddb',
            'name': 'daily_snapshot', 'state': 'done', 'ephemeral': False})
        self.authenticate('portalowner@example.com', 'ownerpass123')
        resp = self._form_post(
            '/my/instances/%d/backups/%d/discard'
            % (self.instance.id, backup.id))
        self.assertIn('error=', resp.headers['Location'])

    def test_discard_running_backup_marks_failed(self):
        backup = self.env['saas.instance.backup'].sudo().create({
            'instance_id': self.instance.id, 'db_name': 'proddb',
            'name': 'ondemand_running', 'state': 'running', 'ephemeral': True})
        self.authenticate('portalowner@example.com', 'ownerpass123')
        resp = self._form_post(
            '/my/instances/%d/backups/%d/discard'
            % (self.instance.id, backup.id))
        self.assertIn('notice=', resp.headers['Location'])
        self.assertEqual(backup.state, 'failed')

    def test_discard_done_backup_shrinks_expiry(self):
        backup = self.env['saas.instance.backup'].sudo().create({
            'instance_id': self.instance.id, 'db_name': 'proddb',
            'name': 'ondemand_done', 'state': 'done', 'ephemeral': True,
            'expires_at': fields.Datetime.now() + timedelta(hours=8)})
        self.authenticate('portalowner@example.com', 'ownerpass123')
        self._form_post(
            '/my/instances/%d/backups/%d/discard'
            % (self.instance.id, backup.id))
        self.assertLessEqual(backup.expires_at, fields.Datetime.now())

    def test_download_denies_non_owner(self):
        backup = self.env['saas.instance.backup'].sudo().create({
            'instance_id': self.instance.id, 'db_name': 'proddb',
            'name': 'ondemand_x', 'state': 'done', 'ephemeral': True})
        self.authenticate('portalintruder@example.com', 'intruderpass123')
        resp = self.url_open(
            '/my/instances/%d/backups/%d/download'
            % (self.instance.id, backup.id), allow_redirects=False)
        self.assertTrue(resp.headers['Location'].endswith('/my/instances'))

    def test_download_not_found_for_pending_backup(self):
        backup = self.env['saas.instance.backup'].sudo().create({
            'instance_id': self.instance.id, 'db_name': 'proddb',
            'name': 'ondemand_pending', 'state': 'running', 'ephemeral': True})
        self.authenticate('portalowner@example.com', 'ownerpass123')
        resp = self.url_open(
            '/my/instances/%d/backups/%d/download'
            % (self.instance.id, backup.id), allow_redirects=False)
        self.assertIn('error=', resp.headers['Location'])

    def test_download_surfaces_refresh_failure(self):
        backup = self.env['saas.instance.backup'].sudo().create({
            'instance_id': self.instance.id, 'db_name': 'proddb',
            'name': 'ondemand_x', 'state': 'done', 'ephemeral': True})
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with patch.object(
                type(backup), '_refresh_download_url',
                lambda self: None):
            resp = self.url_open(
                '/my/instances/%d/backups/%d/download'
                % (self.instance.id, backup.id), allow_redirects=False)
        location = resp.headers['Location'].replace('%27', "'").replace('%20', ' ')
        self.assertIn("couldn't generate", location)

    def test_download_success_redirects_to_download_url(self):
        backup = self.env['saas.instance.backup'].sudo().create({
            'instance_id': self.instance.id, 'db_name': 'proddb',
            'name': 'ondemand_x', 'state': 'done', 'ephemeral': True})

        def _fake_refresh(self):
            self.download_url = 'https://bucket.example.com/fake-signed-url'
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with patch.object(type(backup), '_refresh_download_url', _fake_refresh):
            resp = self.url_open(
                '/my/instances/%d/backups/%d/download'
                % (self.instance.id, backup.id), allow_redirects=False)
        self.assertIn(resp.status_code, (301, 302, 303))
        self.assertEqual(
            resp.headers['Location'], 'https://bucket.example.com/fake-signed-url')

    def test_restore_denies_non_owner(self):
        backup = self.env['saas.instance.backup'].sudo().create({
            'instance_id': self.instance.id, 'db_name': 'proddb',
            'name': 'ondemand_x', 'state': 'done', 'ephemeral': True})
        self.authenticate('portalintruder@example.com', 'intruderpass123')
        resp = self._form_post(
            '/my/instances/%d/backup/%d/restore'
            % (self.instance.id, backup.id))
        self.assertTrue(resp.headers['Location'].endswith('/my/instances'))

    def test_restore_rejects_wrong_instance_state(self):
        backup = self.env['saas.instance.backup'].sudo().create({
            'instance_id': self.instance.id, 'db_name': 'proddb',
            'name': 'ondemand_x', 'state': 'done', 'ephemeral': True})
        self.instance.sudo().write({'state': 'suspended'})
        self.authenticate('portalowner@example.com', 'ownerpass123')
        resp = self._form_post(
            '/my/instances/%d/backup/%d/restore'
            % (self.instance.id, backup.id))
        self.assertIn('error=', resp.headers['Location'])

    def test_restore_rejects_backup_not_done(self):
        backup = self.env['saas.instance.backup'].sudo().create({
            'instance_id': self.instance.id, 'db_name': 'proddb',
            'name': 'ondemand_pending', 'state': 'running', 'ephemeral': True})
        self.authenticate('portalowner@example.com', 'ownerpass123')
        resp = self._form_post(
            '/my/instances/%d/backup/%d/restore'
            % (self.instance.id, backup.id))
        self.assertIn('error=', resp.headers['Location'])

    def test_restore_from_backups_page_requires_exact_confirmation(self):
        backup = self.env['saas.instance.backup'].sudo().create({
            'instance_id': self.instance.id, 'db_name': 'proddb',
            'name': 'ondemand_x', 'state': 'done', 'ephemeral': True})
        self.authenticate('portalowner@example.com', 'ownerpass123')
        resp = self._form_post(
            '/my/instances/%d/backup/%d/restore'
            % (self.instance.id, backup.id),
            {'return_to': 'backups', 'confirm': 'wrong-name'})
        self.assertIn('error=', resp.headers['Location'])

    # test_restore_success_calls_model_method was removed:
    # action_restore_backup/action_restore_full_instance (the restic/SSH
    # restore pipeline) were removed along with ssh_docker — there is no
    # real success path to test any more (a later phase reimplements
    # restore via the OdooInstance CR's spec.restore, see the removal
    # plan's Phase 5).

    def test_restore_failure_is_caught_and_reported(self):
        """With the model-side restore method gone, hitting this route on
        an otherwise-valid backup now always fails — and the portal route
        still catches that (whatever the underlying exception) and
        reports it as an error redirect rather than a 500."""
        backup = self.env['saas.instance.backup'].sudo().create({
            'instance_id': self.instance.id, 'db_name': 'proddb',
            'name': 'ondemand_x', 'state': 'done', 'ephemeral': True})
        self.authenticate('portalowner@example.com', 'ownerpass123')
        resp = self._form_post(
            '/my/instances/%d/backup/%d/restore'
            % (self.instance.id, backup.id))
        self.assertIn('error=', resp.headers['Location'])


@tagged('post_install', '-at_install')
class TestPortalComputeTiers(_PortalTestBase):
    """/my/instances/<id>/compute-tier/{change (JSON),checkout (GET)} —
    same shape as TestPortalBackups' daily-backup coverage would be, had
    one existed (see saas_core's action_change_compute_tier, which this
    JSON route just calls)."""

    def _k8s_server(self):
        region = self.env['saas.region'].sudo().create(
            {'name': 'Portal Tier Region', 'code': 'portal-tier-region'})
        return self.env['saas.server'].sudo().create(
            {'name': 'portal-tier-k8s', 'compute_driver': 'kubernetes',
             'region_id': region.id})

    def _ha_tier(self):
        return self.env['saas.compute.tier'].sudo().create(
            {'name': 'HA', 'code': 'ha-portaltest', 'replicas': 2,
             'monthly_price': 15.0})

    def test_change_denies_non_owner(self):
        self.instance.sudo().docker_server_id = self._k8s_server()
        tier = self._ha_tier()
        self.authenticate('portalintruder@example.com', 'intruderpass123')
        result = self._json_call(
            '/saas/api/v1/instances/%d/compute-tier/change'
            % self.instance.id, {'tier_id': tier.id})
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'not_found')

    def test_change_rejects_when_no_kubernetes_backend_assigned(self):
        # No docker_server_id assigned at all here (never an ssh_docker
        # value now that Kubernetes is the only backend) — the guard in
        # action_change_compute_tier still rejects an empty/non-Kubernetes
        # backend the same way.
        tier = self._ha_tier()
        self.authenticate('portalowner@example.com', 'ownerpass123')
        result = self._json_call(
            '/saas/api/v1/instances/%d/compute-tier/change'
            % self.instance.id, {'tier_id': tier.id})
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'change_failed')
        self.assertIn('Kubernetes', result['error'])
        self.assertFalse(self.instance.compute_tier_pending_invoice_id)

    def test_change_rejects_instance_not_running(self):
        tier = self._ha_tier()
        self.instance.sudo().write({
            'docker_server_id': self._k8s_server().id, 'state': 'stopped'})
        self.authenticate('portalowner@example.com', 'ownerpass123')
        result = self._json_call(
            '/saas/api/v1/instances/%d/compute-tier/change'
            % self.instance.id, {'tier_id': tier.id})
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'change_failed')

    def test_change_to_priced_tier_creates_invoice_and_returns_checkout_url(self):
        self.instance.sudo().docker_server_id = self._k8s_server()
        tier = self._ha_tier()
        self.authenticate('portalowner@example.com', 'ownerpass123')
        result = self._json_call(
            '/saas/api/v1/instances/%d/compute-tier/change'
            % self.instance.id, {'tier_id': tier.id})
        self.assertTrue(result['ok'])
        self.assertEqual(
            result['data'].get('checkout_url'),
            '/my/instances/%d/compute-tier/checkout' % self.instance.id)
        self.assertTrue(self.instance.compute_tier_pending_invoice_id)

    def test_change_to_free_tier_applies_immediately(self):
        """A free (or downgrade) tier change is applied via a background
        job with no checkout — see action_change_compute_tier."""
        self.instance.sudo().docker_server_id = self._k8s_server()
        free_tier = self.env['saas.compute.tier'].sudo().create(
            {'name': 'Free2', 'code': 'free2-portaltest', 'replicas': 2,
             'monthly_price': 0.0})
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with patch.object(type(self.env['saas.job']), '_spawn_worker',
                          lambda self: None):
            result = self._json_call(
                '/saas/api/v1/instances/%d/compute-tier/change'
                % self.instance.id, {'tier_id': free_tier.id})
        self.assertTrue(result['ok'])
        self.assertTrue(result['data'].get('applied'))
        self.assertFalse(self.instance.compute_tier_pending_invoice_id)

    def test_checkout_page_renders_with_pending_invoice(self):
        self.instance.sudo().docker_server_id = self._k8s_server()
        tier = self._ha_tier()
        self.instance.sudo().action_change_compute_tier(tier.id)
        self.authenticate('portalowner@example.com', 'ownerpass123')
        resp = self.url_open(
            '/my/instances/%d/compute-tier/checkout' % self.instance.id)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Upgrade to the', resp.content)

    def test_checkout_page_redirects_when_no_pending_invoice(self):
        self.authenticate('portalowner@example.com', 'ownerpass123')
        resp = self.url_open(
            '/my/instances/%d/compute-tier/checkout' % self.instance.id,
            allow_redirects=False)
        self.assertIn(resp.status_code, (301, 302, 303))
        self.assertIn('/my/instances/%d?notice=' % self.instance.id,
                      resp.headers['Location'])


@tagged('post_install', '-at_install')
class TestPortalRepoManagement(_PortalTestBase):
    """B.1.5 continued: /my/instances/<id>/{update-repo,remove-repo,
    pull-repo}. All three redirect to the same '/my/instances/<id>'
    URL whether they succeed or silently no-op internally (unlike most
    of this file's other routes, there's no notice=/error=
    querystring), so what's actually testable — and what these tests
    check — is the resulting saas.instance.repo state and which model
    method got called, not the redirect target (except for the auth
    boundary, which redirects to the bare '/my/instances' listing
    instead and so IS distinguishable).

    Repo changes rebuild the instance's image (action_build_and_deploy,
    which queues a saas.job): the job's worker thread must never run for
    real inside a live HttpCase request, so _spawn_worker is patched out
    for the whole class. run_in_background() (pull-repo, a fresh local
    import inside the route body) is patched at its source module
    attribute, same standing rule as every other run_in_background()
    route in this plan.
    """

    def setUp(self):
        super().setUp()
        self.instance.sudo().write({'is_hosting': True, 'state': 'running'})
        spawn = patch.object(type(self.env['saas.job']), '_spawn_worker', lambda self: None)
        spawn.start()
        self.addCleanup(spawn.stop)

    def test_update_repo_denies_non_owner(self):
        self.authenticate('portalintruder@example.com', 'intruderpass123')
        resp = self._form_post(
            '/my/instances/%d/update-repo' % self.instance.id,
            {'repo_url': 'https://github.com/acme/widgets.git'})
        self.assertEqual(resp.headers['Location'], '/my/instances')

    def test_update_repo_ignored_when_instance_not_running(self):
        self.instance.sudo().write({'state': 'stopped'})
        self.authenticate('portalowner@example.com', 'ownerpass123')
        self._form_post(
            '/my/instances/%d/update-repo' % self.instance.id,
            {'repo_url': 'https://github.com/acme/widgets.git'})
        self.assertFalse(self.instance.sudo().repo_ids)

    def test_update_repo_creates_new_repo_and_redeploys(self):
        self.authenticate('portalowner@example.com', 'ownerpass123')
        resp = self._form_post(
            '/my/instances/%d/update-repo' % self.instance.id,
            {'repo_url': 'https://github.com/acme/widgets.git',
             'repo_branch': 'main', 'git_token': 'ghp_secret'})
        self.assertEqual(
            resp.headers['Location'], '/my/instances/%d' % self.instance.id)
        repo = self.instance.sudo().repo_ids
        self.assertEqual(len(repo), 1)
        self.assertEqual(repo.repo_url, 'https://github.com/acme/widgets.git')
        self.assertEqual(repo.branch, 'main')
        self.assertTrue(repo.webhook_enabled)

    def test_update_repo_updates_existing_repo(self):
        repo = self.env['saas.instance.repo'].sudo().create({
            'instance_id': self.instance.id,
            'repo_url': 'https://github.com/acme/old.git', 'branch': 'main'})
        self.authenticate('portalowner@example.com', 'ownerpass123')
        self._form_post(
            '/my/instances/%d/update-repo' % self.instance.id,
            {'repo_url': 'https://github.com/acme/new.git',
             'repo_branch': 'develop'})
        self.assertEqual(repo.repo_url, 'https://github.com/acme/new.git')
        self.assertEqual(repo.branch, 'develop')

    def test_update_repo_with_empty_url_removes_existing_repo(self):
        repo = self.env['saas.instance.repo'].sudo().create({
            'instance_id': self.instance.id,
            'repo_url': 'https://github.com/acme/old.git', 'branch': 'main'})
        mock_build = MagicMock()
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with patch.object(type(self.instance), 'action_build_and_deploy', mock_build):
            self._form_post(
                '/my/instances/%d/update-repo' % self.instance.id,
                {'repo_url': ''})
        self.assertFalse(repo.exists())
        # Removing the repo rebuilds the image without it (rolling rollout).
        mock_build.assert_called_once_with('redeploy')

    def test_remove_repo_denies_non_owner(self):
        self.authenticate('portalintruder@example.com', 'intruderpass123')
        resp = self._form_post(
            '/my/instances/%d/remove-repo' % self.instance.id)
        self.assertEqual(resp.headers['Location'], '/my/instances')

    def test_remove_repo_is_a_noop_with_no_repo(self):
        self.authenticate('portalowner@example.com', 'ownerpass123')
        resp = self._form_post(
            '/my/instances/%d/remove-repo' % self.instance.id)
        self.assertEqual(
            resp.headers['Location'], '/my/instances/%d' % self.instance.id)

    def test_remove_repo_deletes_and_redeploys(self):
        repo = self.env['saas.instance.repo'].sudo().create({
            'instance_id': self.instance.id,
            'repo_url': 'https://github.com/acme/widgets.git', 'branch': 'main'})
        self.authenticate('portalowner@example.com', 'ownerpass123')
        self._form_post('/my/instances/%d/remove-repo' % self.instance.id)
        self.assertFalse(repo.exists())

    def test_pull_repo_denies_non_owner(self):
        self.authenticate('portalintruder@example.com', 'intruderpass123')
        resp = self._form_post(
            '/my/instances/%d/pull-repo' % self.instance.id)
        self.assertEqual(resp.headers['Location'], '/my/instances')

    def test_pull_repo_ignored_when_no_cloned_repo(self):
        mock_run = MagicMock()
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with patch(
                'odoo.addons.saas_core.utils.run_in_background', mock_run):
            self._form_post('/my/instances/%d/pull-repo' % self.instance.id)
        mock_run.assert_not_called()

    def test_pull_repo_success_runs_in_background(self):
        self.env['saas.instance.repo'].sudo().create({
            'instance_id': self.instance.id,
            'repo_url': 'https://github.com/acme/widgets.git',
            'branch': 'main', 'state': 'cloned'})
        mock_run = MagicMock()
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with patch(
                'odoo.addons.saas_core.utils.run_in_background', mock_run):
            resp = self._form_post('/my/instances/%d/pull-repo' % self.instance.id)
        self.assertEqual(
            resp.headers['Location'], '/my/instances/%d' % self.instance.id)
        mock_run.assert_called_once()
        self.assertEqual(
            mock_run.call_args.args[1], '_do_webhook_pull_and_restart')


@tagged('post_install', '-at_install')
class TestPortalSubscribeAndCheckout(_PortalTestBase):
    """B.1.5 (final slice): /my/instances/<id>/{subscribe,checkout} — the
    trial->paid conversion form-post and the invoice payment page. The
    last remaining piece of portal.py's route coverage.

    action_subscribe_from_trial() is a synchronous, SSH-free,
    billing-only ORM method already covered at the model layer in
    test_billing_overhaul.py, so — same "thin HTTP smoke test"
    principle as change-plan — it's mocked here to isolate what's new
    at this layer: form validation, config clamping, and turning the
    return value into a checkout-vs-instance-page redirect.
    _get_or_create_hosting_plan()'s pricing-engine lookup is left
    unmocked (pure ORM/pricing logic, same as the change-plan tests).

    checkout is a render-only GET route; it's exercised for real
    (a real posted account.move, real payment.provider compatibility
    lookup) since there's no heavier async work to isolate away from.
    """

    def _subscribe(self, workers, storage, billing_period='monthly'):
        return self._form_post(
            '/my/instances/%d/subscribe' % self.instance.id,
            {'workers': str(workers), 'storage': str(storage),
             'billing_period': billing_period})

    def test_subscribe_denies_non_owner(self):
        self.authenticate('portalintruder@example.com', 'intruderpass123')
        resp = self._subscribe(4, 10)
        self.assertTrue(resp.headers['Location'].endswith('/my/instances'))

    def test_subscribe_requires_both_fields(self):
        self.authenticate('portalowner@example.com', 'ownerpass123')
        resp = self._subscribe(0, 10)
        self.assertIn('/upgrade?error=', resp.headers['Location'])

    def test_subscribe_charged_upgrade_redirects_to_checkout(self):
        mock_subscribe = MagicMock(return_value=MagicMock(amount_total=20.0))
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with patch.object(
                type(self.instance), 'action_subscribe_from_trial',
                mock_subscribe):
            resp = self._subscribe(4, 10)
        self.assertEqual(
            resp.headers['Location'],
            '/my/instances/%d/checkout' % self.instance.id)
        mock_subscribe.assert_called_once()
        self.assertEqual(
            mock_subscribe.call_args.kwargs.get('billing_period'), 'monthly')

    def test_subscribe_zero_charge_redirects_to_instance(self):
        mock_subscribe = MagicMock(return_value=True)
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with patch.object(
                type(self.instance), 'action_subscribe_from_trial',
                mock_subscribe):
            resp = self._subscribe(4, 10)
        self.assertEqual(
            resp.headers['Location'], '/my/instances/%d' % self.instance.id)

    def test_subscribe_propagates_model_rejection(self):
        mock_subscribe = MagicMock(
            side_effect=UserError("This instance is not on a trial plan."))
        self.authenticate('portalowner@example.com', 'ownerpass123')
        with patch.object(
                type(self.instance), 'action_subscribe_from_trial',
                mock_subscribe):
            resp = self._subscribe(4, 10)
        self.assertIn('/upgrade?error=', resp.headers['Location'])

    def test_subscribe_clamps_workers_to_config_limits(self):
        seen = {}

        def _capture(instance, plan_id, billing_period='monthly'):
            seen['workers'] = instance.env['saas.plan'].browse(plan_id).workers
            return True

        self.authenticate('portalowner@example.com', 'ownerpass123')
        with patch.object(
                type(self.instance), 'action_subscribe_from_trial', _capture):
            # default hosting config max_workers is 8 — ask for way more.
            self._subscribe(999, 10)
        self.assertEqual(seen.get('workers'), 8)

    def test_checkout_denies_non_owner(self):
        self.authenticate('portalintruder@example.com', 'intruderpass123')
        resp = self.url_open(
            '/my/instances/%d/checkout' % self.instance.id,
            allow_redirects=False)
        self.assertTrue(resp.headers['Location'].endswith('/my/instances'))

    def test_checkout_redirects_when_no_unpaid_invoice(self):
        self.authenticate('portalowner@example.com', 'ownerpass123')
        resp = self.url_open(
            '/my/instances/%d/checkout' % self.instance.id,
            allow_redirects=False)
        self.assertEqual(
            resp.headers['Location'], '/my/instances/%d' % self.instance.id)

    def test_checkout_renders_with_unpaid_restoration_invoice(self):
        product = self.instance._get_billing_product()
        invoice = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice',
            'partner_id': self.owner.partner_id.id,
            'invoice_date': fields.Date.today(),
            'invoice_line_ids': [(0, 0, {
                'product_id': product.id, 'name': 'Restoration charge',
                'quantity': 1, 'price_unit': 20.0})],
        })
        invoice.action_post()
        self.instance.sudo().write({'restoration_invoice_id': invoice.id})
        self.authenticate('portalowner@example.com', 'ownerpass123')
        resp = self.url_open('/my/instances/%d/checkout' % self.instance.id)
        self.assertEqual(resp.status_code, 200)
