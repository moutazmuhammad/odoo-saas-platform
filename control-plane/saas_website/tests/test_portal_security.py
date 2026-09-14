import json
from unittest.mock import MagicMock, patch

from odoo.tests.common import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestPortalInstanceSecurity(HttpCase):
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

    def _json_call(self, route):
        resp = self.url_open(
            route,
            data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': {}}),
            headers={'Content-Type': 'application/json'})
        return resp.json().get('result')

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
