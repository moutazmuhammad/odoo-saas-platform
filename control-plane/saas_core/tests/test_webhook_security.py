import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

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
        self.repo = self.env['saas.instance.repo'].sudo().create({
            'instance_id': self.instance.id,
            'repo_url': 'https://github.com/acme/widgets.git',
            'branch': 'main', 'webhook_enabled': True,
            'webhook_secret': self.secret, 'state': 'cloned'})

    def _post(self, secret):
        return self.url_open('/saas/webhook/%s' % secret, data=b'{}')

    def _signed_post(self, body, headers=None, secret=None):
        secret = secret if secret is not None else self.secret
        body_bytes = json.dumps(body).encode() if not isinstance(body, bytes) else body
        sig = 'sha256=' + hmac.new(
            secret.encode(), body_bytes, hashlib.sha256).hexdigest()
        all_headers = {'X-Hub-Signature-256': sig, 'X-GitHub-Event': 'push'}
        all_headers.update(headers or {})
        return self.url_open('/saas/webhook/%s' % self.secret,
                             data=body_bytes, headers=all_headers)

    def _push_payload(self, ref='refs/heads/main'):
        return {'ref': ref, 'after': 'abc123def456',
                'head_commit': {'message': 'fix stuff', 'author': {'name': 'Dev'}},
                'commits': [{'id': 'abc123def456', 'message': 'fix stuff',
                            'author': {'name': 'Dev'}}]}

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

    # ---- B.1.4: previously untested branches — every one of these
    # returns BEFORE the controller ever reaches saas.job._enqueue(...),
    # so they're all safe to drive through a real HttpCase request (no
    # risk of the background-worker-thread hazard documented in
    # PRODUCTION-READINESS-PLAN.md's Definition of Done). ---------------

    def test_sha1_signature_explicitly_rejected(self):
        # Deprecated-but-still-sent by some old integrations; must not be
        # accepted as a downgrade path even though a real SHA-1 HMAC of the
        # correct secret is presented.
        body = json.dumps(self._push_payload()).encode()
        sha1_sig = 'sha1=' + hmac.new(
            self.secret.encode(), body, hashlib.sha1).hexdigest()
        resp = self.url_open('/saas/webhook/%s' % self.secret, data=body,
                             headers={'X-Hub-Signature': sha1_sig,
                                      'X-GitHub-Event': 'push'})
        self.assertEqual(resp.status_code, 404)

    def test_invalid_json_body_rejected(self):
        body = b'{not valid json'
        sig = 'sha256=' + hmac.new(
            self.secret.encode(), body, hashlib.sha256).hexdigest()
        resp = self.url_open('/saas/webhook/%s' % self.secret, data=body,
                             headers={'X-Hub-Signature-256': sig,
                                      'X-GitHub-Event': 'push'})
        self.assertEqual(resp.status_code, 400)

    def test_non_push_event_ignored(self):
        # Must NOT reuse _push_payload(): _is_push_event() has a header-less
        # fallback that treats any payload shaped like {ref, commits: [...]}
        # as a push regardless of X-GitHub-Event (for providers that don't
        # send an event header) — a real PR payload has neither key, so use
        # one that doesn't accidentally match that fallback.
        payload = {'action': 'opened',
                   'pull_request': {'number': 1, 'title': 'do a thing'}}
        resp = self._signed_post(payload, headers={'X-GitHub-Event': 'pull_request'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get('reason'), 'not a push event')

    def test_branch_mismatch_ignored(self):
        resp = self._signed_post(self._push_payload(ref='refs/heads/other-branch'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get('reason'), 'branch mismatch')

    def test_instance_not_running_ignored(self):
        self.instance.write({'state': 'suspended'})
        resp = self._signed_post(self._push_payload())
        self.assertEqual(resp.json().get('reason'), 'instance not running')

    def test_repo_not_cloned_ignored(self):
        self.repo.state = 'pending'
        resp = self._signed_post(self._push_payload())
        self.assertEqual(resp.json().get('reason'), 'repo not cloned')

    def _patched_enqueue(self):
        """A MagicMock standing in for saas.job._enqueue: records call args
        for assertions, returns an empty recordset (the controller discards
        the return value anyway), and — critically — never creates a real
        job row or spawns saas.job._spawn_worker's background thread. Same
        rule as the databases/* tests: never let a live HttpCase request
        actually trigger that (see PRODUCTION-READINESS-PLAN.md).

        Assigning a bare MagicMock as a class attribute does NOT get
        auto-bound like a real method would (MagicMock isn't a descriptor),
        so the call arrives here as (record, method, ...) with no implicit
        `self` — side_effect is written to match that, not "self, *a".
        Entering this (an explicit `new=` was given to patch.object) yields
        the MagicMock itself, so callers get it via
        `with self._patched_enqueue() as m:`."""
        mock = MagicMock(
            side_effect=lambda *a, **kw: self.env['saas.job'].browse())
        return patch.object(type(self.env['saas.job']), '_enqueue', mock)

    def test_duplicate_delivery_ignored(self):
        with self._patched_enqueue():
            first = self._signed_post(
                self._push_payload(), headers={'X-GitHub-Delivery': 'dup-1'})
            self.assertEqual(first.json().get('status'), 'ok')
            second = self._signed_post(
                self._push_payload(), headers={'X-GitHub-Delivery': 'dup-1'})
        self.assertEqual(second.json().get('reason'), 'duplicate delivery')

    def test_successful_push_creates_build_and_enqueues_deploy(self):
        with self._patched_enqueue() as mock_enqueue:
            resp = self._signed_post(
                self._push_payload(), headers={'X-GitHub-Delivery': 'push-1'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get('status'), 'ok')
        build = self.env['saas.build'].sudo().search(
            [('instance_id', '=', self.instance.id)], limit=1)
        self.assertTrue(build, "a successful push must create a build record")
        self.assertEqual(build.commit_sha, 'abc123def456')
        self.assertEqual(build.branch, 'main')
        self.assertEqual(build.source, 'push')
        mock_enqueue.assert_called_once()
        call_kwargs = mock_enqueue.call_args.kwargs
        self.assertEqual(call_kwargs.get('channel'), 'deploy')
        self.assertEqual(call_kwargs.get('lock_key'),
                         'instance:%s' % self.instance.id)
