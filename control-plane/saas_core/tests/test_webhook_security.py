from odoo.tests.common import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestWebhookSecurity(HttpCase):
    """SEC-011: webhook auth failures are indistinguishable (no valid-secret
    oracle) and a known secret is rate-limited against deploy fan-out."""

    def setUp(self):
        super().setUp()
        product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or \
            self.env['saas.product'].sudo().create(
                {'name': 'WH Hosting', 'is_hosting': True, 'is_published': True})
        plan = self.env['saas.plan'].sudo().create({
            'name': 'WH Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [product.id])]})
        domain = self.env['saas.based.domain'].sudo().search([], limit=1) or \
            self.env['saas.based.domain'].sudo().create({'name': 'wh.example.com'})
        partner = self.env['res.partner'].sudo().create({'name': 'WH Cust'})
        self.instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'whinst', 'domain_id': domain.id, 'partner_id': partner.id,
            'saas_product_id': product.id, 'plan_id': plan.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running', 'is_hosting': True})
        self.secret = 'whsecret123abc'
        self.env['saas.instance.repo'].sudo().create({
            'instance_id': self.instance.id,
            'repo_url': 'https://github.com/acme/widgets.git',
            'branch': 'main', 'webhook_enabled': True,
            'webhook_secret': self.secret})

    def _post(self, secret):
        return self.url_open('/saas/webhook/%s' % secret, data=b'{}')

    def test_unknown_and_unsigned_are_indistinguishable(self):
        # Unknown secret and valid-secret-without-signature must look identical
        # (both 404) so the endpoint isn't a valid-secret oracle.
        unknown = self._post('definitely-not-a-secret')
        unsigned = self._post(self.secret)
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unsigned.status_code, 404)

    def test_known_secret_is_rate_limited(self):
        from odoo.addons.saas_core.controllers.webhook import SaasWebhookController
        limit = SaasWebhookController._WEBHOOK_RATE_LIMIT
        codes = [self._post(self.secret).status_code for _ in range(limit + 5)]
        self.assertIn(429, codes,
                      "a known secret must be rate-limited (got %s)" % set(codes))
