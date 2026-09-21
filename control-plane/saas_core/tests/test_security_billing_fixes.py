import json
from datetime import date, timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import HttpCase, TransactionCase, tagged

from odoo.addons.saas_core.models.saas_instance_repo import (
    assert_safe_git_url, _extract_git_host)


@tagged('post_install', '-at_install')
class TestSsrfGuard(TransactionCase):
    """SSRF: user-supplied Git URLs must never resolve to internal hosts."""

    def test_extract_host_https_and_scp(self):
        self.assertEqual(
            _extract_git_host('https://gitea.example.com/o/r.git'),
            'gitea.example.com')
        self.assertEqual(
            _extract_git_host('git@github.com:o/r.git'), 'github.com')

    def test_loopback_rejected(self):
        for url in ('https://127.0.0.1/o/r.git',
                    'https://localhost/o/r.git',
                    'git@127.0.0.1:o/r.git'):
            with self.assertRaises(UserError):
                assert_safe_git_url(url)

    def test_private_ranges_rejected(self):
        for url in ('https://10.0.0.5/o/r.git',
                    'https://192.168.1.10/o/r.git',
                    'https://172.16.4.4/o/r.git'):
            with self.assertRaises(UserError):
                assert_safe_git_url(url)

    def test_metadata_endpoint_rejected(self):
        # The cloud metadata service is link-local (169.254.0.0/16).
        with self.assertRaises(UserError):
            assert_safe_git_url('https://169.254.169.254/latest/meta-data')

    def test_missing_host_rejected(self):
        with self.assertRaises(UserError):
            assert_safe_git_url('not-a-url')

    def test_public_host_allowed(self):
        # A public IP literal must NOT raise (no DNS needed → offline-safe).
        try:
            assert_safe_git_url('https://8.8.8.8/o/r.git')
        except UserError as e:
            self.fail("public host wrongly rejected: %s" % e)


@tagged('post_install', '-at_install')
class TestRateLimit(TransactionCase):
    """DB-backed fixed-window rate limiter."""

    def test_allows_then_blocks_within_window(self):
        rl = self.env['saas.rate.limit']
        allowed = []
        for _i in range(5):
            ok, _retry = rl._hit('utest', 'k1', 3, 3600)
            allowed.append(ok)
        # First 3 allowed, the rest blocked (same fixed window).
        self.assertEqual(allowed, [True, True, True, False, False])

    def test_independent_keys(self):
        rl = self.env['saas.rate.limit']
        self.assertTrue(rl._hit('utest', 'a', 1, 3600)[0])
        self.assertFalse(rl._hit('utest', 'a', 1, 3600)[0])
        # A different key has its own bucket.
        self.assertTrue(rl._hit('utest', 'b', 1, 3600)[0])

    def test_retry_after_is_positive_when_blocked(self):
        rl = self.env['saas.rate.limit']
        rl._hit('utest', 'c', 1, 3600)
        ok, retry = rl._hit('utest', 'c', 1, 3600)
        self.assertFalse(ok)
        self.assertGreater(retry, 0)


@tagged('post_install', '-at_install')
class TestEnvSlotBilling(TransactionCase):
    """Deleting an extra environment must stop billing its recurring slot."""

    def setUp(self):
        super().setUp()
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('saas_master.hosting_worker_price', '10.0')
        icp.set_param('saas_master.hosting_storage_price_per_gb', '0.3')
        icp.set_param('saas_master.env_price_factor', '1.0')
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or \
            self.env['saas.product'].sudo().create({
                'name': 'TEST Hosting Sec', 'is_hosting': True,
                'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'TEST Sec Plan', 'is_custom': True, 'workers': 4,
            'storage_limit': 50, 'cpu_limit': 2.0, 'ram_limit': '2g',
            'price': 55.0, 'yearly_price': 528.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.partner = self.env['res.partner'].sudo().create(
            {'name': 'Sec Cust'})
        self.domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create(
                {'name': 'sec.example.com'})

    def _mk_prod(self, sub):
        today = date.today()
        return self.env['saas.instance'].sudo().create({
            'subdomain': sub, 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': False, 'state': 'running',
            'next_invoice_date': today + timedelta(days=15),
            'last_invoice_date': today - timedelta(days=15)})

    def _add_repo(self, prod):
        return self.env['saas.instance.repo'].sudo().create({
            'instance_id': prod.id, 'repo_url': 'https://github.com/x/y.git',
            'branch': 'main'})

    def _mk_child(self, prod, sub):
        return self.env['saas.instance'].sudo().create({
            'subdomain': sub, 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'billing_period': 'monthly',
            'environment': 'staging', 'parent_id': prod.id,
            'region_id': False, 'state': 'running'})

    def test_delete_keeps_reserved_slot(self):
        # Deleting a server frees the slot for REUSE — the reserved count and
        # the recurring charge are unchanged (he paid for the capacity).
        prod = self._mk_prod('secslot')
        prod.write({'staging_slots': 2})
        child = self._mk_child(prod, 'secslot-stg')
        self.assertEqual(prod._env_used_for('staging'), 1)
        self.assertEqual(len(prod._environment_order_lines('monthly')), 2)
        child.action_delete_environment(delete_branch=False)
        prod.invalidate_recordset(['staging_slots'])
        # Slot count unchanged; usage freed so another can be created.
        self.assertEqual(prod.staging_slots, 2)
        self.assertEqual(prod._env_used_for('staging'), 0)
        self.assertEqual(len(prod._environment_order_lines('monthly')), 2)

    def test_create_requires_repo(self):
        prod = self._mk_prod('secrepo')
        prod.write({'staging_slots': 1})
        with self.assertRaises(UserError):
            prod.action_create_environment('staging', name='s1')

    def test_create_capped_at_reserved_count(self):
        # One slot, already used by a live child → creating another must be
        # refused (reserve more first). (We seed the child directly because the
        # real create path starts a background deploy that commits.)
        prod = self._mk_prod('seccap')
        self._add_repo(prod)
        prod.write({'staging_slots': 1})
        self._mk_child(prod, 'seccap-stg')  # used = 1 = slots
        with self.assertRaises(UserError):
            prod.action_create_environment('staging', name='s2')

    def test_activate_reserved_slots_grants(self):
        prod = self._mk_prod('secgrant')
        prod.write({'reserved_staging_pending': 2, 'staging_slots': 0})
        prod._activate_reserved_slots()
        prod.invalidate_recordset(['staging_slots', 'reserved_staging_pending'])
        self.assertEqual(prod.staging_slots, 2)
        self.assertEqual(prod.reserved_staging_pending, 0)

    def test_release_free_slot_refunds(self):
        prod = self._mk_prod('secrel')
        prod.write({'staging_slots': 2})
        wallet = prod._wallet(create=True)
        before = wallet.balance
        prod.action_release_environment_slots('staging', qty=1)
        prod.invalidate_recordset(['staging_slots'])
        wallet = prod._wallet(create=True)
        self.assertEqual(prod.staging_slots, 1)
        self.assertGreater(wallet.balance, before)

    def test_release_blocked_when_slot_in_use(self):
        prod = self._mk_prod('secrelblock')
        prod.write({'staging_slots': 1})
        self._mk_child(prod, 'secrelblock-stg')  # uses the only slot
        with self.assertRaises(UserError):
            prod.action_release_environment_slots('staging', qty=1)


@tagged('post_install', '-at_install')
class TestApiSecurityHttp(HttpCase):
    """End-to-end checks against the live JSON routes: brute-force throttling
    and the access-token's read-only scope."""

    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or \
            self.env['saas.product'].sudo().create({
                'name': 'HTTP Hosting', 'is_hosting': True,
                'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'HTTP Plan', 'is_custom': True, 'workers': 2,
            'storage_limit': 5, 'cpu_limit': 1.0, 'ram_limit': '1g',
            'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create(
                {'name': 'http.example.com'})
        self.partner = self.env['res.partner'].sudo().create(
            {'name': 'HTTP Cust', 'email': 'httpcust@example.com'})

    def _call(self, route, params):
        resp = self.url_open(
            route,
            data=json.dumps({'jsonrpc': '2.0', 'method': 'call',
                             'params': params}),
            headers={'Content-Type': 'application/json'})
        return resp.json().get('result')

    def test_login_is_rate_limited(self):
        # Hammer with wrong credentials; the per-account window is 7/300s, so
        # the throttle must kick in well before the 12th try.
        codes = []
        for _i in range(12):
            res = self._call('/saas/api/v1/auth/login',
                             {'login': 'nobody@example.com',
                              'password': 'wrong'})
            codes.append((res or {}).get('code'))
        self.assertIn('rate_limited', codes,
                      "login endpoint was never throttled: %s" % codes)

    def test_access_token_is_read_only(self):
        inst = self.env['saas.instance'].sudo().create({
            'subdomain': 'httptok', 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': False, 'state': 'running',
            'is_hosting': True})
        token = inst._portal_ensure_token()
        # READ with the token works (share-link semantics preserved).
        read = self._call('/saas/api/v1/instances/%s' % inst.id,
                          {'access_token': token})
        self.assertTrue(read and read.get('ok'),
                        "token read should succeed: %s" % read)
        # WRITE with the SAME token is refused — destructive ops need the
        # authenticated owner, not a bearer token.
        write = self._call('/saas/api/v1/instances/%s/action' % inst.id,
                           {'access_token': token, 'action': 'restart'})
        self.assertFalse(write.get('ok'),
                         "token must NOT authorize a write: %s" % write)
        self.assertEqual(write.get('code'), 'not_found')
        # And the token must no longer be echoed back in the read payload.
        self.assertNotIn('access_token', read.get('data', {}))

    def test_register_start_never_echoes_otp(self):
        # SEC-001: the verification code is delivered out-of-band and must
        # never appear in the register/start (or resend) JSON response.
        country = self.env.ref('base.us', raise_if_not_found=False) \
            or self.env['res.country'].sudo().search([], limit=1)
        res = self._call('/saas/api/v1/auth/register/start', {
            'name': 'OTP Probe', 'email': 'otpprobe@example.com',
            'phone': '+12025550147', 'country_id': country.id,
            'city': 'Springfield', 'password': 'sup3rsecret'})
        self.assertTrue(res and res.get('ok'),
                        "register/start should succeed: %s" % res)
        self.assertTrue(res['data'].get('otp_sent'))
        self.assertNotIn('debug_otp', res['data'],
                         "OTP code must NOT be returned to the client")
        resend = self._call('/saas/api/v1/auth/register/resend',
                            {'phone': '+12025550147'})
        self.assertTrue(resend and resend.get('ok'), resend)
        self.assertNotIn('debug_otp', resend['data'],
                         "OTP code must NOT be returned on resend")

    def _make_portal_user(self, login):
        user = self.env['res.users'].sudo().create({
            'name': 'Reset User', 'login': login,
            'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])]})
        user.password = 'origpassword1'
        return user

    def test_reset_start_is_enumeration_safe(self):
        # CX-001: the start response is identical whether or not an account
        # exists, so it can't be used to probe which emails are registered.
        self._make_portal_user('resetexists@example.com')
        r1 = self._call('/saas/api/v1/auth/reset/start',
                        {'email': 'resetexists@example.com'})
        self.assertTrue(r1 and r1.get('ok'), r1)
        self.assertTrue(r1['data'].get('sent'))
        self.assertNotIn('debug_otp', r1['data'])
        r2 = self._call('/saas/api/v1/auth/reset/start',
                        {'email': 'noaccount-xyz@example.com'})
        self.assertTrue(r2 and r2.get('ok'), r2)
        self.assertEqual(set(r1['data']), set(r2['data']),
                         "existing vs non-existing email must be indistinguishable")

    def test_reset_verify_rejects_bad_inputs(self):
        bad_email = self._call('/saas/api/v1/auth/reset/verify',
                               {'email': 'nope', 'otp': '123456',
                                'password': 'longenough1'})
        self.assertEqual(bad_email.get('code'), 'invalid')
        short_pw = self._call('/saas/api/v1/auth/reset/verify',
                              {'email': 'x@y.com', 'otp': '123456',
                               'password': 'short'})
        self.assertEqual(short_pw.get('code'), 'invalid')
        wrong_code = self._call('/saas/api/v1/auth/reset/verify',
                                {'email': 'x@y.com', 'otp': '000000',
                                 'password': 'longenough1'})
        self.assertEqual(wrong_code.get('code'), 'otp_invalid')

    def test_reset_verify_sets_new_password_end_to_end(self):
        self._make_portal_user('resetok@example.com')
        self.env['saas.registration.otp'].sudo().create({
            'identifier': 'resetok@example.com', 'channel': 'email',
            'code': '654321', 'verified': False,
            'expires_at': fields.Datetime.now() + timedelta(minutes=10)})
        res = self._call('/saas/api/v1/auth/reset/verify', {
            'email': 'resetok@example.com', 'otp': '654321',
            'password': 'brandnew99'})
        self.assertTrue(res and res.get('ok'), res)
        # Proof the password was actually changed: a fresh login with the NEW
        # password succeeds (and the old one no longer does).
        good = self._call('/saas/api/v1/auth/login',
                          {'login': 'resetok@example.com',
                           'password': 'brandnew99'})
        self.assertTrue(good and good.get('ok'), good)
        bad = self._call('/saas/api/v1/auth/login',
                         {'login': 'resetok@example.com',
                          'password': 'origpassword1'})
        self.assertFalse(bad.get('ok'),
                         "the old password must no longer work")

    def test_register_verify_completes_signup_end_to_end(self):
        # Distinct from test_register_start_never_echoes_otp: that test only
        # covers start/resend (sending the code). This is the actual account
        # creation completion, the one auth route with no prior HTTP-level
        # coverage at all.
        country = self.env.ref('base.us', raise_if_not_found=False) \
            or self.env['res.country'].sudo().search([], limit=1)
        phone = '+12025550199'
        self.env['saas.registration.otp'].sudo().create({
            'identifier': phone, 'channel': 'phone',
            'code': '111222', 'verified': False,
            'expires_at': fields.Datetime.now() + timedelta(minutes=10)})
        res = self._call('/saas/api/v1/auth/register/verify', {
            'name': 'Verify Complete', 'email': 'verifycomplete@example.com',
            'phone': phone, 'country_id': country.id, 'city': 'Springfield',
            'password': 'brandnew99', 'otp': '111222'})
        self.assertTrue(res and res.get('ok'),
                        "register/verify should complete signup: %s" % res)
        # Proof the account was actually created and signed in: a fresh
        # login with the new credentials succeeds.
        good = self._call('/saas/api/v1/auth/login',
                          {'login': 'verifycomplete@example.com',
                           'password': 'brandnew99'})
        self.assertTrue(good and good.get('ok'), good)

    def test_register_verify_rejects_wrong_code(self):
        phone = '+12025550188'
        self.env['saas.registration.otp'].sudo().create({
            'identifier': phone, 'channel': 'phone',
            'code': '333444', 'verified': False,
            'expires_at': fields.Datetime.now() + timedelta(minutes=10)})
        res = self._call('/saas/api/v1/auth/register/verify', {
            'name': 'Wrong Code', 'email': 'wrongcode@example.com',
            'phone': phone, 'country_id': 1, 'city': 'Springfield',
            'password': 'brandnew99', 'otp': '000000'})
        self.assertEqual(res.get('code'), 'otp_invalid')
        # And no account was created by the rejected attempt.
        self.assertFalse(
            self.env['res.users'].sudo().search(
                [('login', '=', 'wrongcode@example.com')]),
            "a rejected register/verify must not create an account")

    def test_logout_ends_the_session_end_to_end(self):
        # No prior HTTP-level coverage of logout at all: login tests exist,
        # but nothing proved the session is actually invalidated afterwards.
        user = self._make_portal_user('logmeout@example.com')
        login = self._call('/saas/api/v1/auth/login',
                           {'login': 'logmeout@example.com',
                            'password': 'origpassword1'})
        self.assertTrue(login and login.get('ok'), login)
        me_before = self._call('/saas/api/v1/me', {})
        self.assertTrue(me_before and me_before.get('ok'),
                        "should be signed in right after login: %s" % me_before)
        logout = self._call('/saas/api/v1/auth/logout', {})
        self.assertTrue(logout and logout.get('ok'), logout)
        me_after = self._call('/saas/api/v1/me', {})
        self.assertFalse(me_after.get('ok'),
                         "session must no longer be authenticated after logout")
        self.assertEqual(me_after.get('code'), 'auth_required')

    def test_environments_payload_exposes_scaling(self):
        # The workspace needs the Production server's resources + slot usage to
        # offer "Scale resources" and "add test environment" CTAs.
        prod = self.env['saas.instance'].sudo().create({
            'subdomain': 'httpscale', 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': False, 'state': 'running',
            'is_hosting': True, 'staging_slots': 2})
        token = prod._portal_ensure_token()
        res = self._call('/saas/api/v1/instances/%s/environments' % prod.id,
                         {'access_token': token})
        self.assertTrue(res.get('ok'), res)
        data = res['data']
        self.assertEqual(data['production_plan']['workers'], self.plan.workers)
        self.assertEqual(data['production_plan']['storage_gb'],
                         int(self.plan.storage_limit))
        self.assertFalse(data['production_plan']['is_trial'])
        self.assertEqual(data['slots']['staging']['total'], 2)
        self.assertEqual(data['slots']['staging']['used'], 0)

    # ---- B.1.3: instances/<id>/databases/* — destructive ops must refuse
    # a bare access_token exactly like /action already does (SEC pattern
    # from test_access_token_is_read_only, extended to this route family) --

    def _running_hosting_instance(self, sub, **extra):
        vals = {
            'subdomain': sub, 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': False,
            'state': 'running', 'is_hosting': True,
        }
        vals.update(extra)
        return self.env['saas.instance'].sudo().create(vals)

    def test_databases_create_refuses_token_only_access(self):
        inst = self._running_hosting_instance('dbtoktest1')
        token = inst._portal_ensure_token()
        res = self._call(
            '/saas/api/v1/instances/%s/databases/create' % inst.id,
            {'access_token': token, 'name': 'shouldnotbecreated',
             'login': 'admin', 'password': 'whatever12'})
        self.assertFalse(res.get('ok'),
                         "a bare token must not authorize db create: %s" % res)
        self.assertEqual(res.get('code'), 'not_found')

    def test_databases_drop_refuses_token_only_access(self):
        inst = self._running_hosting_instance('dbtoktest2')
        token = inst._portal_ensure_token()
        res = self._call(
            '/saas/api/v1/instances/%s/databases/drop' % inst.id,
            {'access_token': token, 'name': 'production'})
        self.assertFalse(res.get('ok'),
                         "a bare token must not authorize db drop: %s" % res)
        self.assertEqual(res.get('code'), 'not_found')

    def test_databases_duplicate_refuses_token_only_access(self):
        inst = self._running_hosting_instance('dbtoktest3')
        token = inst._portal_ensure_token()
        res = self._call(
            '/saas/api/v1/instances/%s/databases/duplicate' % inst.id,
            {'access_token': token, 'source': 'production', 'name': 'copy1'})
        self.assertFalse(res.get('ok'),
                         "a bare token must not authorize db duplicate: %s" % res)
        self.assertEqual(res.get('code'), 'not_found')

    # ---- B.1.3 follow-up: databases/{create,drop,duplicate} REJECTION
    # paths only was removed (test_databases_duplicate_rejects_missing_source
    # + the _mock_db_ops_infra fixture): hosting_db_list/hosting_db_create_
    # async/_drop_async/_duplicate_async (the ssh_docker/docker-exec DB
    # self-service family) were removed along with that backend — DB
    # self-service is gone for now (a later phase reimplements it via
    # driver.exec()). The token-only-access rejection tests above are
    # unaffected: they never reach hosting_db_list, since the access check
    # rejects the request first.

    # ---- B.1.3: instances/<id>/environments/{reserve,release} — pure
    # billing/model logic, no SSH/infra dependency, so the full success
    # path is cheap to verify end-to-end (unlike databases/*, which needs
    # a mocked compute layer — left for a follow-up, see the plan) -------

    def _authenticated_project_owner(self, login, sub, **instance_extra):
        """A portal user + Production instance they actually own, with a
        real authenticated session (environments/* has no access_token
        param at all — session ownership only)."""
        partner = self.env['res.partner'].sudo().create(
            {'name': 'Env Owner', 'email': login})
        user = self.env['res.users'].sudo().create({
            'name': 'Env Owner', 'login': login, 'partner_id': partner.id,
            'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])]})
        user.password = 'envpass123'
        vals = {
            'subdomain': sub, 'domain_id': self.domain.id,
            'partner_id': partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': False,
            'state': 'running', 'is_hosting': True,
        }
        vals.update(instance_extra)
        instance = self.env['saas.instance'].sudo().create(vals)
        self.authenticate(login, 'envpass123')
        return instance

    def test_environment_release_credits_wallet_and_lowers_slots(self):
        today = fields.Date.today()
        instance = self._authenticated_project_owner(
            'envrelease@example.com', 'envrelease', staging_slots=3,
            last_invoice_date=today - timedelta(days=10),
            next_invoice_date=today + timedelta(days=20))
        env_price = instance._env_server_price('monthly')
        res = self._call(
            '/saas/api/v1/instances/%s/environments/release' % instance.id,
            {'type': 'staging', 'qty': 1})
        self.assertTrue(res and res.get('ok'), res)
        self.assertEqual(res['data']['released'], 1)
        self.assertEqual(res['data']['slots'], 2)
        self.assertEqual(instance.staging_slots, 2)
        # Proof the unused portion was actually credited, not just logged:
        # 20 of 30 days remain -> 2/3 of one slot's price, to the cent.
        expected_credit = round(env_price * 20 / 30, 2)
        wallet = self.env['saas.wallet'].sudo().search(
            [('partner_id', '=', instance.partner_id.id)], limit=1)
        self.assertTrue(wallet, "releasing a slot must create/credit a wallet")
        self.assertAlmostEqual(wallet.balance, expected_credit, places=2)

    def test_environment_release_rejects_releasing_in_use_slots(self):
        instance = self._authenticated_project_owner(
            'envoverrelease@example.com', 'envoverrelease', staging_slots=1)
        # Occupy the one slot with a child staging server so 0 are free.
        self.env['saas.instance'].sudo().create({
            'subdomain': 'envoverrelease-s1', 'domain_id': self.domain.id,
            'partner_id': instance.partner_id.id,
            'saas_product_id': self.product.id, 'plan_id': self.plan.id,
            'billing_period': 'monthly', 'environment': 'staging',
            'region_id': False, 'state': 'running', 'is_hosting': True,
            'parent_id': instance.id})
        res = self._call(
            '/saas/api/v1/instances/%s/environments/release' % instance.id,
            {'type': 'staging', 'qty': 1})
        self.assertFalse(res.get('ok'),
                         "releasing an in-use slot must be refused: %s" % res)
        self.assertEqual(instance.staging_slots, 1,
                         "a rejected release must not change the slot count")

    def test_environment_reserve_refuses_trial_instances(self):
        instance = self._authenticated_project_owner(
            'envtrialres@example.com', 'envtrialreserve', is_trial=True)
        res = self._call(
            '/saas/api/v1/instances/%s/environments/reserve' % instance.id,
            {'type': 'staging', 'qty': 1})
        self.assertFalse(res.get('ok'),
                         "a trial must not be able to reserve paid slots: %s"
                         % res)
        self.assertEqual(instance.staging_slots, 0)
