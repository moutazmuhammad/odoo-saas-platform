"""Regression tests for the four trial / order / quota eligibility bugs
found during the 2026-07 bug-hunt (see docs / commit history):

  #1  _saas_has_paid_instance counted unpaid draft/pending_payment orders
      as "paid", barring the free trial after an abandoned checkout.
  #2  A rejected trial (one-trial-per-client) leaked an orphan draft
      instance because the controller's cleanup re-raised the deferred
      constraint; fixed with a cr.savepoint() around create+deploy/bill.
  #3  A 'failed' trial deploy permanently locked the customer out of the
      trial (the constraint counted failed trials).
  #4  Cancelled instances counted toward the max-instances quota, so
      cancelling to free a slot didn't help (dead-end).
"""
import json

from odoo.exceptions import ValidationError
from odoo.tests.common import HttpCase, TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestTrialEligibilityFixes(TransactionCase):
    """Model-level fixes: #1 (paid detection) and #3 (failed-trial retry)."""

    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or \
            self.env['saas.product'].sudo().create({
                'name': 'ELIG Hosting', 'is_hosting': True,
                'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'ELIG Plan', 'is_custom': True, 'workers': 2,
            'storage_limit': 10, 'cpu_limit': 1.0, 'ram_limit': '1g',
            'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create(
                {'name': 'elig.example.com'})

    def _mk(self, partner, state, sub, is_trial=False):
        return self.env['saas.instance'].sudo().create({
            'subdomain': sub, 'domain_id': self.domain.id,
            'partner_id': partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': False,
            'state': state, 'is_trial': is_trial})

    # ---- Bug #1 --------------------------------------------------------
    def test_incomplete_orders_are_not_paid_instances(self):
        """draft / pending_payment are unpaid, incomplete orders — they must
        NOT count as owning a paid instance (else an abandoned checkout bars
        the free trial forever)."""
        p = self.env['res.partner'].sudo().create({'name': 'Elig Pending'})
        self._mk(p, 'pending_payment', 'eligpp')
        self.assertFalse(
            p._saas_has_paid_instance(hosting=True),
            "a pending_payment (unpaid) order must not count as paid")
        self._mk(p, 'draft', 'eligdr')
        self.assertFalse(
            p._saas_has_paid_instance(hosting=True),
            "a draft (never-provisioned) order must not count as paid")

    def test_running_instance_is_a_paid_instance(self):
        """A genuinely provisioned (running) non-trial instance DOES count."""
        p = self.env['res.partner'].sudo().create({'name': 'Elig Running'})
        self._mk(p, 'running', 'eligrun')
        self.assertTrue(p._saas_has_paid_instance(hosting=True))

    # ---- Bug #3 --------------------------------------------------------
    def test_failed_trial_does_not_block_a_new_trial(self):
        """A 'failed' trial deploy must not permanently lock the customer out
        — they can start a fresh trial."""
        p = self.env['res.partner'].sudo().create({'name': 'Elig Failed'})
        self._mk(p, 'failed', 'eligft1', is_trial=True)
        # Must NOT raise: the failed trial is excluded from the constraint.
        fresh = self._mk(p, 'draft', 'eligft2', is_trial=True)
        self.assertTrue(fresh.exists())

    def test_live_trial_still_blocks_a_second_trial(self):
        """The one-trial rule still holds for a live (non-failed) trial."""
        p = self.env['res.partner'].sudo().create({'name': 'Elig Live'})
        self._mk(p, 'running', 'eligrt1', is_trial=True)
        with self.assertRaises(ValidationError):
            self._mk(p, 'draft', 'eligrt2', is_trial=True)

    def test_cancelled_trial_does_not_block_a_new_trial(self):
        """A cancelled trial also frees the one-trial slot (pre-existing rule,
        guarded here alongside the failed-trial fix)."""
        p = self.env['res.partner'].sudo().create({'name': 'Elig Cancelled'})
        self._mk(p, 'cancelled_by_client', 'eligct1', is_trial=True)
        fresh = self._mk(p, 'draft', 'eligct2', is_trial=True)
        self.assertTrue(fresh.exists())


@tagged('post_install', '-at_install')
class TestOrderControllerFixes(HttpCase):
    """Controller-level fixes exercised through the live JSON order route:
    #2 (no orphan on a rejected trial) and #4 (cancelled excluded from quota).
    """

    def setUp(self):
        super().setUp()
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('saas_master.hosting_worker_price', '10.0')
        icp.set_param('saas_master.hosting_storage_price_per_gb', '0.3')
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or \
            self.env['saas.product'].sudo().create({
                'name': 'ORD Hosting', 'is_hosting': True,
                'is_published': True})
        self.trial_plan = self.env['saas.plan'].sudo().create({
            'name': 'ORD Trial', 'is_trial_plan': True, 'price': 0.0,
            'yearly_price': 0.0, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g',
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        # Region WITH capacity: one co-located proxy + docker + db host.
        self.region = self.env['saas.region'].sudo().create({
            'name': 'ORD Region', 'code': 'ord-reg'})
        self.env['saas.server'].sudo().create({
            'name': 'ord-host', 'is_proxy_server': True,
            'is_docker_host': True, 'is_db_server': True,
            'region_id': self.region.id, 'health_state': 'ok'})
        self.domain = self.env['saas.based.domain'].sudo().create({
            'name': 'ord.example.com', 'region_id': self.region.id})
        self.version = self.env['saas.odoo.version'].sudo().search(
            [('is_hosting_version', '=', True)], limit=1) or \
            self.env['saas.odoo.version'].sudo().create({
                'name': '18.0', 'docker_image': 'odoo',
                'docker_image_tag': '18.0', 'nginx_template': 'new',
                'is_hosting_version': True})
        # A portal user we can authenticate as.
        self.partner = self.env['res.partner'].sudo().create(
            {'name': 'Order Cust', 'email': 'ordcust@example.com'})
        self.user = self.env['res.users'].sudo().create({
            'name': 'Order Cust', 'login': 'ordcust@example.com',
            'password': 'ordpass123', 'partner_id': self.partner.id,
            'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])]})

    def _order(self, **params):
        params.setdefault('domain_id', self.domain.id)
        params.setdefault('odoo_version_id', self.version.id)
        params.setdefault('region_id', self.region.id)
        params.setdefault('workers', 1)
        params.setdefault('storage', 5)
        params.setdefault('billing_period', 'monthly')
        resp = self.url_open(
            '/saas/api/v1/hosting/order',
            data=json.dumps({'jsonrpc': '2.0', 'method': 'call',
                             'params': params}),
            headers={'Content-Type': 'application/json'})
        return resp.json().get('result')

    def _instance_count(self):
        return self.env['saas.instance'].sudo().search_count(
            [('partner_id', '=', self.partner.id)])

    def _calculate(self, route, **params):
        resp = self.url_open(
            route,
            data=json.dumps({'jsonrpc': '2.0', 'method': 'call',
                             'params': params}),
            headers={'Content-Type': 'application/json'})
        return resp.json().get('result')

    # ---- Bug #2 --------------------------------------------------------
    def test_rejected_trial_leaves_no_orphan(self):
        """A second trial (rejected by one-trial-per-client) must not leave an
        orphan draft instance — the controller's savepoint rolls the INSERT
        back."""
        # First trial, live (flag not set → the controller's pre-create flag
        # check passes and the create reaches the deferred constraint).
        self.env['saas.instance'].sudo().create({
            'subdomain': 'ordtrial1', 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.trial_plan.id, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': self.region.id,
            'state': 'running', 'is_trial': True})
        before = self._instance_count()
        self.authenticate('ordcust@example.com', 'ordpass123')
        res = self._order(project_name='T2', subdomain='ordtrial2',
                          is_trial='1')
        self.assertFalse(res.get('ok'),
                         "a second trial must be rejected: %s" % res)
        self.assertEqual(
            self._instance_count(), before,
            "rejected trial must not leak an instance (savepoint rollback)")

    # ---- Bug #4 --------------------------------------------------------
    def test_cancelled_instance_does_not_count_toward_quota(self):
        """With the quota at 1 and one CANCELLED instance, a new order must
        still be allowed — cancelled instances consume no infra and must not
        block the quota."""
        self.env['ir.config_parameter'].sudo().set_param(
            'saas_master.max_instances_per_user', '1')
        self.env['saas.instance'].sudo().create({
            'subdomain': 'ordcancelled', 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.trial_plan.id, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': self.region.id,
            'state': 'cancelled_by_client'})
        self.authenticate('ordcust@example.com', 'ordpass123')
        res = self._order(project_name='Q1', subdomain='ordquota')
        self.assertTrue(
            res.get('ok'),
            "cancelled instance must not block the quota; got: %s" % res)

    # ---- B.1.2: checkout path (previously only trial-specific edge cases
    # were covered through HTTP; nothing exercised the general paid-order
    # path or asserted where either outcome actually redirects) ----------

    def test_paid_order_redirects_to_checkout(self):
        # main.py's hosting_order sends a PAID order to
        # /my/instances/<id>/checkout (billing still owed) and a TRIAL
        # straight to the instance view — this distinction was never
        # actually asserted before, only res['ok'] truthiness.
        self.authenticate('ordcust@example.com', 'ordpass123')
        res = self._order(project_name='Paid1', subdomain='ordpaid',
                          workers=2, storage=10)
        self.assertTrue(res and res.get('ok'), res)
        redirect = res['data']['redirect_url']
        self.assertIn('/checkout', redirect,
                     "a paid order must redirect to checkout: %s" % redirect)

    def test_trial_order_redirects_to_instance_not_checkout(self):
        self.authenticate('ordcust@example.com', 'ordpass123')
        res = self._order(project_name='Trial1', subdomain='ordtrialview',
                          is_trial='1')
        self.assertTrue(res and res.get('ok'), res)
        redirect = res['data']['redirect_url']
        self.assertNotIn('/checkout', redirect,
                         "a trial must not be sent to checkout: %s" % redirect)
        self.assertIn('/my/instances/', redirect)
        # Proof it's actually past the payment gate: action_deploy() was
        # invoked (not action_confirm_and_bill()), so the state must already
        # be beyond draft/pending_payment — provisioning itself is
        # legitimately async (over SSH, in a background thread) and may
        # not have finished within the request, so don't assert a specific
        # post-deploy state, only that it isn't a payment-gated one.
        instance = self.env['saas.instance'].sudo().search(
            [('subdomain', '=', 'ordtrialview')], limit=1)
        self.assertNotIn(instance.state, ('draft', 'pending_payment'),
                         "a trial must skip the payment gate entirely: %s"
                         % instance.state)

    def test_hosting_calculate_returns_real_pricing(self):
        # Public route, no auth needed — exercises the SPA's live slider
        # quote end-to-end through saas.pricing.engine, not just unit-level.
        res = self._calculate('/saas/api/v1/hosting/calculate',
                              workers=2, storage=10, billing='monthly')
        self.assertTrue(res and res.get('ok'), res)
        data = res['data']
        self.assertEqual(data['workers'], 2)
        self.assertEqual(data['storage'], 10)
        self.assertGreater(data['total'], 0)

    def test_hosting_calculate_project_adds_environment_costs(self):
        base = self._calculate('/saas/api/v1/hosting/calculate',
                               workers=2, storage=10, billing='monthly')
        project = self._calculate('/saas/api/v1/hosting/calculate-project',
                                  workers=2, storage=10, billing='monthly',
                                  staging_count=1, dev_count=1)
        self.assertTrue(project and project.get('ok'), project)
        data = project['data']
        self.assertEqual(data['staging_count'], 1)
        self.assertEqual(data['dev_count'], 1)
        self.assertGreater(data['env_total'], 0,
                           "two extra environments must add real cost")
        self.assertAlmostEqual(
            data['project_total'], base['data']['total'] + data['env_total'],
            places=2,
            msg="project_total must be the base hosting quote plus env_total")

    def test_services_calculate_returns_real_pricing(self):
        res = self._calculate('/saas/api/v1/services/calculate',
                              workers=2, storage=10, billing='monthly')
        self.assertTrue(res and res.get('ok'), res)
        self.assertGreater(res['data']['total'], 0)
