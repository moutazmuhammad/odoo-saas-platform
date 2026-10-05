import importlib.util
import json
from pathlib import Path
from unittest.mock import patch
from unittest import TestCase
from odoo.tests.common import TransactionCase, HttpCase, tagged
from odoo.exceptions import AccessError, ValidationError
from .test_iam import IamFixture


@tagged('post_install', '-at_install')
class TestIamMembers(IamFixture, TransactionCase):
    def _profile(self, owner=None, user=None):
        return self.env['saas.iam.member'].sudo().create({'owner_id': (owner or self.owner).partner_id.id,
            'user_id': (user or self.team).id, 'name': 'Teammate'})

    def _new_profile(self):
        result = self.env['saas.iam.member'].with_user(self.owner)._create_profile('New Teammate', 'iam-new-member@example.com')
        return self.env['saas.iam.member'].sudo().browse(result['id']), result

    def _verify_member_phone(self, member, phone='01012345678'):
        User = self.env['res.users'].with_user(member.user_id)
        with patch.object(type(User), '_deliver_phone_code'):
            User._send_phone_code(phone, self.env.ref('base.eg').id)
        return User._verify_phone_code(member.user_id.sudo().iam_phone_code)

    def test_phone_uniqueness_normalizes_mobile_and_inactive_contacts(self):
        egypt = self.env.ref('base.eg')
        self.owner.partner_id.country_id = egypt
        self.other.partner_id.write({'country_id': egypt.id, 'mobile': '010 1234 5678'})
        Member = self.env['saas.iam.member'].with_user(self.owner)
        with self.assertRaises(ValidationError):
            Member._create_profile('Duplicate Phone', 'duplicate-phone@example.com', '+20 (10) 1234-5678')
        self.other.active = False
        self.other.partner_id.write({'mobile': False, 'phone': '00201012345678', 'active': False})
        with self.assertRaises(ValidationError):
            Member._create_profile('Duplicate Phone', 'duplicate-phone@example.com', '+201012345678')

    def test_local_phone_uses_owner_country_automatically(self):
        self.owner.partner_id.country_id = self.env.ref('base.eg')
        result = self.env['saas.iam.member'].with_user(self.owner)._create_profile(
            'Local Phone', 'local-phone@example.com', '010 1234 5678')
        member = self.env['saas.iam.member'].sudo().browse(result['id'])
        self.assertEqual(member.phone, '+201012345678')
        self.assertEqual(member.user_id.partner_id.country_id, self.env.ref('base.eg'))

    def test_phone_country_can_differ_from_owner_country(self):
        self.owner.partner_id.country_id = self.env.ref('base.us')
        result = self.env['saas.iam.member'].with_user(self.owner)._create_profile(
            'Egypt Phone', 'egypt-phone@example.com', '01012345678', self.env.ref('base.eg').id)
        member = self.env['saas.iam.member'].sudo().browse(result['id'])
        self.assertEqual(member.phone, '+201012345678')
        self.assertEqual(member.user_id.partner_id.country_id, self.env.ref('base.eg'))

    def test_local_phone_cannot_bypass_uniqueness(self):
        self.owner.partner_id.country_id = self.env.ref('base.eg')
        self.other.partner_id.phone = '+201012345678'
        with self.assertRaises(ValidationError):
            self.env['saas.iam.member'].with_user(self.owner)._create_profile(
                'Duplicate Local', 'duplicate-local@example.com', '01012345678')
        self.assertFalse(self.env['res.users'].sudo().search_count([('login', '=', 'duplicate-local@example.com')]))

    def test_invalid_local_phone_or_country_rejected(self):
        Member = self.env['saas.iam.member'].with_user(self.owner)
        with self.assertRaises(ValidationError):
            Member._create_profile('Invalid Phone', 'invalid-phone@example.com', '123', self.env.ref('base.eg').id)
        with self.assertRaises(ValidationError):
            Member._create_profile('Invalid Country', 'invalid-country@example.com', '01012345678', 'invalid')

    def test_phone_setup_cannot_skip_password_change(self):
        member, _ = self._new_profile()
        with self.assertRaises(AccessError):
            self.env['res.users'].with_user(member.user_id)._send_phone_code('01012345678', self.env.ref('base.eg').id)

    def _configure_whatsapp(self):
        self.env.company.sudo().write({
            'saas_whatsapp_phone_id': '123456789', 'saas_whatsapp_access_token': 'test-token',
            'saas_whatsapp_template': 'veltnex_verification', 'saas_whatsapp_language': 'en_US',
            'saas_whatsapp_api_version': 'v25.0'})

    def test_whatsapp_sends_authentication_template_with_copy_code_button(self):
        self._configure_whatsapp()
        with patch('odoo.addons.saas_iam.models.whatsapp.requests.post') as post:
            post.return_value.ok = True
            post.return_value.json.return_value = {'messages': [{'id': 'wamid.test'}]}
            self.env['saas.iam.whatsapp']._send_code('+201012345678', '123456')
        self.assertEqual(post.call_args.args[0], 'https://graph.facebook.com/v25.0/123456789/messages')
        payload = post.call_args.kwargs['json']
        self.assertEqual(payload['to'], '201012345678')
        self.assertEqual(payload['template']['components'][1]['parameters'][0]['text'], '123456')
        self.assertFalse(post.call_args.kwargs['allow_redirects'])

    def test_whatsapp_provider_rejection_never_reports_delivery_success(self):
        from odoo.exceptions import UserError
        self._configure_whatsapp()
        with patch('odoo.addons.saas_iam.models.whatsapp.requests.post') as post:
            post.return_value.ok = False
            with self.assertRaises(UserError):
                self.env['saas.iam.whatsapp']._send_code('+201012345678', '123456')

    def test_registration_uses_same_whatsapp_sender(self):
        with patch.object(type(self.env['saas.iam.whatsapp']), '_send_code') as send:
            otp = self.env['saas.registration.otp'].sudo()._generate_and_send_phone('+201012345678')
        send.assert_called_once_with('+201012345678', otp.code)
        self.assertFalse(otp.verified)

    def test_whatsapp_settings_secrets_are_not_readable_by_teammates(self):
        self._configure_whatsapp()
        with self.assertRaises(AccessError):
            self.env.company.with_user(self.team).read(['saas_whatsapp_access_token'])

    def test_wrong_expired_or_replayed_code_never_unlocks_access(self):
        from odoo import fields
        from datetime import timedelta
        member, _ = self._new_profile()
        User = self.env['res.users'].with_user(member.user_id)
        User._change_initial_password('MyOwnStrongPassword123!')
        with patch.object(type(User), '_deliver_phone_code'):
            User._send_phone_code('01012345678', self.env.ref('base.eg').id)
        code = member.user_id.sudo().iam_phone_code
        wrong = '000000' if code != '000000' else '111111'
        for _ in range(5):
            with TestCase.assertRaises(self, ValidationError):
                User._verify_phone_code(wrong)
        with self.assertRaises(ValidationError):
            User._verify_phone_code(code)
        self.assertFalse(member._ready())
        with patch.object(type(User), '_deliver_phone_code'):
            User._send_phone_code('01012345678', self.env.ref('base.eg').id)
        member.user_id.sudo().iam_phone_code_expires = fields.Datetime.now() - timedelta(seconds=1)
        with self.assertRaises(ValidationError):
            User._verify_phone_code(member.user_id.sudo().iam_phone_code)
        self._verify_member_phone(member)
        with self.assertRaises(AccessError):
            User._verify_phone_code(code)

    def test_phone_confirmation_rechecks_duplicate_claim(self):
        member, _ = self._new_profile()
        User = self.env['res.users'].with_user(member.user_id)
        User._change_initial_password('MyOwnStrongPassword123!')
        with patch.object(type(User), '_deliver_phone_code'):
            User._send_phone_code('01012345678', self.env.ref('base.eg').id)
        self.other.partner_id.phone = '+201012345678'
        with self.assertRaises(ValidationError):
            User._verify_phone_code(member.user_id.sudo().iam_phone_code)
        self.assertFalse(member._ready())

    def test_changing_phone_invalidates_previous_code(self):
        member, _ = self._new_profile()
        User = self.env['res.users'].with_user(member.user_id)
        User._change_initial_password('MyOwnStrongPassword123!')
        with patch.object(type(User), '_deliver_phone_code'), \
                patch('odoo.addons.saas_iam.models.members.secrets.randbelow', side_effect=[123456, 654321]):
            User._send_phone_code('01012345678', self.env.ref('base.eg').id)
            User._send_phone_code('01112345678', self.env.ref('base.eg').id)
        with self.assertRaises(ValidationError):
            User._verify_phone_code('123456')
        User._verify_phone_code('654321')
        self.assertEqual(member.user_id.partner_id.phone, '+201112345678')
        self.assertTrue(member._ready())

    def test_whatsapp_failure_keeps_account_locked_and_destroys_old_code(self):
        from odoo.exceptions import UserError
        member, _ = self._new_profile()
        User = self.env['res.users'].with_user(member.user_id)
        User._change_initial_password('MyOwnStrongPassword123!')
        with patch.object(type(User), '_deliver_phone_code'):
            User._send_phone_code('01012345678', self.env.ref('base.eg').id)
        with patch.object(type(User), '_deliver_phone_code', side_effect=UserError('WhatsApp unavailable')):
            with TestCase.assertRaises(self, UserError):
                User._send_phone_code('01112345678', self.env.ref('base.eg').id)
        self.assertFalse(member.user_id.sudo().iam_phone_code)
        self.assertFalse(member._ready())

    def test_verified_phone_cannot_be_changed_without_reverification(self):
        member, _ = self._new_profile()
        User = self.env['res.users'].with_user(member.user_id)
        User._change_initial_password('MyOwnStrongPassword123!')
        self._verify_member_phone(member)
        self.assertTrue(member._ready())
        member.user_id.partner_id.phone = '+201112345678'
        self.assertFalse(member._ready())
        with self.assertRaises(AccessError):
            member.user_id.with_user(member.user_id).write({'iam_verified_phone': '+201112345678'})

    def test_normal_portal_signup_cannot_reuse_teammate_phone(self):
        self.env['saas.iam.member'].with_user(self.owner)._create_profile('Phone Owner', 'phone-owner@example.com', '+201012345678')
        with self.assertRaises(ValidationError):
            self.env['res.users'].sudo().with_context(no_reset_password=True).create({
                'name': 'Phone Duplicate', 'login': 'phone-duplicate@example.com',
                'phone': '+20 10 1234 5678',
                'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])],
            })
        self.assertFalse(self.env['res.users'].sudo().search_count([('login', '=', 'phone-duplicate@example.com')]))

    def test_managed_user_cannot_become_customer_owner_after_password_change(self):
        member, _ = self._new_profile()
        user = member.user_id
        self.env['res.users'].with_user(user)._change_initial_password('MyOwnStrongPassword123!')
        self._verify_member_phone(member)
        self.assertTrue(self.iam._is_managed_user(user))
        with self.assertRaises(AccessError):
            self.project.with_user(user).sudo().copy({'subdomain': 'iamunauthorized', 'partner_id': user.partner_id.id})
        with self.assertRaises(AccessError):
            self.env['saas.iam.member'].with_user(user)._create_profile('Nested', 'nested@example.com')
        # Legacy projects created through the old checkout bypass never imply ownership.
        legacy = self.project.copy({'subdomain': 'iamlegacy', 'partner_id': user.partner_id.id})
        self.assertFalse(self.iam._is_owner(legacy, user))
        self.assertFalse(self.iam._allowed(legacy, 'db.delete', user))
        self.assertFalse(self.iam._allowed(legacy, 'billing.manage', user))
        self.assertEqual(self.iam.with_user(user)._project_domain(), [('id', 'in', [])])

    def test_managed_roles_enforce_action_and_environment_boundaries(self):
        member, _ = self._new_profile()
        user = member.user_id
        self.env['res.users'].with_user(user)._change_initial_password('MyOwnStrongPassword123!')
        self._verify_member_phone(member)
        self._grant('viewer', 'staging', user=user)
        grant = self._grant('sql', 'staging', user=user)
        self.assertTrue(self.iam._allowed(self.stage, 'sql.execute', user))
        self.assertFalse(self.iam._allowed(self.project, 'sql.execute', user))
        self.assertFalse(self.iam._allowed(self.dev, 'sql.execute', user))
        self.assertFalse(self.iam._allowed(self.stage, 'db.delete', user))
        with self.assertRaises(AccessError):
            self.project.with_user(user).sudo().hosting_sql_query('example', 'SELECT 1')
        with self.assertRaises(AccessError):
            self.stage.with_user(user).sudo().hosting_db_drop_async('example')
        with self.assertRaises(AccessError):
            self.stage.with_user(user).sudo().copy({'subdomain': 'iamdeniedchild'})
        self._grant('environment_creator', 'staging', user=user)
        child = self.stage.with_user(user).sudo().copy({'subdomain': 'iamallowedchild', 'parent_id': self.project.id, 'environment': 'staging', 'partner_id': self.owner.partner_id.id})
        self.assertEqual(child.parent_id, self.project)
        grant.active = False
        self.assertFalse(self.iam._allowed(self.stage, 'sql.execute', user))

    def test_first_login_requires_password_and_mobile_verification(self):
        member, result = self._new_profile()
        self.assertTrue(member.user_id.share)
        self.assertEqual(member.user_id.iam_managed_owner_id, self.owner.partner_id)
        self.assertIn(member.user_id.id, member._eligible_ids(self.owner.partner_id))
        self._grant('project_admin', user=member.user_id)
        self.assertFalse(member.phone)
        self.assertFalse(member._ready())
        self.assertFalse(self.iam._allowed(self.project, 'project.discover', member.user_id))
        self.assertFalse(self.iam._allowed(self.project, 'db.delete', member.user_id))
        self.assertNotIn(self.project.id, self.iam._visible_project_ids(member.user_id))
        User = self.env['res.users'].with_user(member.user_id)
        for password in ('short', result['temporary_password'], '  padded password123  '):
            with self.assertRaises(ValidationError):
                User._change_initial_password(password)
        User._change_initial_password('MyOwnStrongPassword123!')
        self.assertFalse(member._ready())
        self.assertFalse(self.iam._allowed(self.project, 'db.delete', member.user_id))
        self._verify_member_phone(member)
        self.assertTrue(member._ready())
        self.assertTrue(self.iam._allowed(self.project, 'db.delete', member.user_id))
        with self.assertRaises(AccessError):
            User._change_initial_password('AnotherStrongPassword123!')

    def test_existing_account_password_and_contact_are_preserved(self):
        original = (self.team.name, self.team.partner_id.phone)
        result = self.env['saas.iam.member'].with_user(self.owner)._create_profile('Profile Name', self.team.login)
        member = self.env['saas.iam.member'].sudo().browse(result['id'])
        self.assertFalse(result['temporary_password'])
        self.assertTrue(member._ready())
        self.assertFalse(member._can_manage_login())
        self.assertEqual(original, (self.team.name, self.team.partner_id.phone))
        with self.assertRaises(AccessError):
            member.with_user(self.owner)._reset_password()
        with self.assertRaises(ValidationError):
            self.env['saas.iam.member'].with_user(self.owner)._create_profile('Again', self.team.login)

    def test_owner_reset_requires_password_change_again_and_cancels_pending_work(self):
        member, result = self._new_profile()
        self.env['res.users'].with_user(member.user_id)._change_initial_password('MyOwnStrongPassword123!')
        self._verify_member_phone(member)
        self._grant('viewer', user=member.user_id)
        self.assertTrue(self.iam._allowed(self.project, 'project.view', member.user_id))
        with self.assertRaises(AccessError):
            member.with_user(self.other)._reset_password()
        with patch.object(type(self.iam), '_invalidate_access') as invalidated:
            reset = member.with_user(self.owner)._reset_password()
        self.assertTrue(invalidated.called)
        self.assertNotEqual(reset['temporary_password'], result['temporary_password'])
        self.assertTrue(member.user_id.iam_initial_password)
        self.assertFalse(self.iam._allowed(self.project, 'project.view', member.user_id))
        self.env['res.users'].with_user(member.user_id)._change_initial_password('MyReplacementPassword123!')
        self.assertFalse(self.iam._allowed(self.project, 'project.view', member.user_id))
        self._verify_member_phone(member)
        self.assertTrue(self.iam._allowed(self.project, 'project.view', member.user_id))

    def test_shared_account_cannot_be_reset_or_disabled_by_one_customer(self):
        member, result = self._new_profile()
        self.env['res.users'].with_user(member.user_id)._change_initial_password('MyOwnStrongPassword123!')
        self._verify_member_phone(member)
        with self.assertRaises(ValidationError):
            self.env['saas.iam.member'].with_user(self.other)._create_profile('Unrelated profile', member.user_id.login)
        # Existing shared relationships remain protected across upgrades.
        self._profile(owner=self.other, user=member.user_id)
        self._grant('viewer', user=member.user_id)
        self._grant('viewer', project=self.foreign, user=member.user_id)
        self.assertFalse(member._can_manage_login())
        with self.assertRaises(AccessError):
            member.with_user(self.owner)._reset_password()
        deleted = member.with_user(self.owner)._delete_profile()
        self.assertFalse(deleted['account_disabled'])
        self.assertTrue(member.user_id.active)
        self.assertFalse(self.iam._allowed(self.project, 'project.view', member.user_id))
        self.assertTrue(self.iam._allowed(self.foreign, 'project.view', member.user_id))

    def test_delete_managed_user_disables_login_and_recreation_issues_new_password(self):
        member, original = self._new_profile()
        self._grant('viewer', user=member.user_id)
        group = self.env['saas.iam.group'].sudo().create({'owner_id': self.owner.partner_id.id, 'name': 'Team', 'user_ids': [(4, member.user_id.id)]})
        self._grant('deploy', group=group)
        result = member.with_user(self.owner)._delete_profile()
        self.assertTrue(result['account_disabled'])
        self.assertFalse(member.user_id.active)
        self.assertFalse(member.active)
        self.assertNotIn(member.user_id.id, group.user_ids.ids)
        self.assertNotIn(member.user_id.id, member._eligible_ids(self.owner.partner_id))
        with self.assertRaises(AccessError):
            member.with_user(self.owner)._reset_password()
        recreated = self.env['saas.iam.member'].with_user(self.owner)._create_profile('Returned teammate', member.user_id.login)
        self.assertEqual(recreated['id'], member.id)
        self.assertTrue(member.user_id.active)
        self.assertTrue(member.user_id.iam_initial_password)
        self.assertNotEqual(original['temporary_password'], recreated['temporary_password'])
        self.assertFalse(self.env['saas.iam.grant'].sudo().search_count([('user_id', '=', member.user_id.id), ('active', '=', True)]))

    def test_account_with_own_projects_cannot_be_taken_over(self):
        profile = self._profile(user=self.other)
        self.other.sudo().iam_managed_owner_id = self.owner.partner_id
        self.assertFalse(profile._can_manage_login())
        with self.assertRaises(AccessError):
            profile.with_user(self.owner)._reset_password()
        self.assertFalse(profile.with_user(self.owner)._delete_profile()['account_disabled'])
        self.assertTrue(self.other.active)

    def test_portal_cannot_set_password_flags_or_managed_owner(self):
        profile = self._profile()
        with self.assertRaises(AccessError):
            profile.with_user(self.team).write({'active': False})
        with self.assertRaises(AccessError):
            self.team.with_user(self.team).write({'iam_initial_password': False})
        with self.assertRaises(AccessError):
            self.team.with_user(self.team).write({'iam_managed_owner_id': self.owner.partner_id.id})

    def test_migration_identifies_only_owner_created_accounts(self):
        legacy = self.env['res.users'].with_user(self.owner).sudo().with_context(no_reset_password=True).create({
            'name': 'Legacy teammate', 'login': 'iam-legacy-created@example.com', 'password': 'legacy-pass',
            'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])]})
        self._profile(user=legacy)
        self._profile(user=self.team)
        path = Path(__file__).parents[1] / 'migrations/18.0.1.2.0/post-migration.py'
        spec = importlib.util.spec_from_file_location('iam_members_migration', path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        self.env.flush_all()
        migration.migrate(self.env.cr, '18.0.1.1.0')
        (legacy | self.team).invalidate_recordset()
        self.assertEqual(legacy.iam_managed_owner_id, self.owner.partner_id)
        self.assertFalse(self.team.iam_managed_owner_id)


@tagged('post_install', '-at_install')
class TestIamMemberApi(IamFixture, HttpCase):
    def _rpc(self, path, **params):
        return self.url_open(path, data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params, 'id': 1}),
            headers={'Content-Type': 'application/json'}).json()['result']

    def test_owner_create_password_change_reset_and_delete_managed_account(self):
        self.authenticate(self.owner.login, 'iam-test-pass')
        result = self._rpc('/saas/api/v1/iam/members', name='New Person', email='iamnewprofile@example.com')
        self.assertTrue(result['ok'], result)
        profile = self.env['saas.iam.member'].sudo().browse(result['data']['id'])
        password = result['data']['temporary_password']
        catalog = self._rpc('/saas/api/v1/iam')['data']
        self.assertFalse(catalog['profiles'][0]['ready'])
        self.assertTrue(catalog['profiles'][0]['can_reset_password'])
        self.assertIn(profile.user_id.id, [m['id'] for m in catalog['members']])
        self.assertTrue(self._rpc('/saas/api/v1/iam/grant', project_ids=[self.project.id], roles=['viewer'], environments=['all'], user_id=profile.user_id.id)['ok'])
        self.assertTrue(self._rpc('/saas/api/v1/iam/groups', name='Before Login', user_ids=[profile.user_id.id])['ok'])
        login = self._rpc('/saas/api/v1/auth/login', login=profile.user_id.login, password=password)
        self.assertTrue(login['data']['must_change_password'], login)
        # HttpCase.authenticate seeds a domain-less cookie; browsers replace
        # the host cookie when the real login API rotates the session.
        self.opener.cookies.clear(domain='', path='/', name='session_id')
        redirect = self.url_open('/my/access', allow_redirects=False)
        self.assertEqual(redirect.status_code, 303)
        self.assertIn('/my/change-password', redirect.headers['Location'])
        self.assertTrue(self._rpc('/saas/api/v1/me')['data']['must_change_password'])
        for route in ('/saas/api/v1/instances', '/saas/api/v1/dashboard', '/saas/api/v1/iam/members', '/saas/api/v1/instances/%s/status' % self.project.id):
            result = self._rpc(route)
            self.assertFalse(result['ok'], result)
            self.assertEqual(result['code'], 'password_change_required')
        completed = self._rpc('/saas/api/v1/iam/password/change', password='MyNewOwnPassword123!')
        self.assertTrue(completed['ok'], completed)
        self.assertFalse(self._rpc('/saas/api/v1/me')['data']['must_change_password'])
        self.assertTrue(self._rpc('/saas/api/v1/me')['data']['must_verify_phone'])
        denied = self._rpc('/saas/api/v1/instances/%s/status' % self.project.id)
        self.assertEqual(denied['code'], 'phone_verification_required')
        with patch.object(type(self.env['res.users']), '_deliver_phone_code'):
            sent = self._rpc('/saas/api/v1/iam/phone/send', phone='01012345678', country_id=self.env.ref('base.eg').id)
        self.assertTrue(sent['ok'], sent)
        self.assertNotIn('test_otp', sent['data'])
        profile.user_id.invalidate_recordset()
        code = profile.user_id.sudo().iam_phone_code
        wrong = '000000' if code != '000000' else '111111'
        self.assertFalse(self._rpc('/saas/api/v1/iam/phone/verify', code=wrong)['ok'])
        profile.user_id.invalidate_recordset()
        self.assertEqual(profile.user_id.sudo().iam_phone_code_attempts, 1)
        self.assertEqual(self._rpc('/saas/api/v1/instances')['code'], 'phone_verification_required')
        verified = self._rpc('/saas/api/v1/iam/phone/verify', code=profile.user_id.sudo().iam_phone_code)
        self.assertTrue(verified['ok'], verified)
        self.assertFalse(self._rpc('/saas/api/v1/me')['data']['must_verify_phone'])
        self.assertTrue(self._rpc('/saas/api/v1/instances/%s/status' % self.project.id)['ok'])
        me = self._rpc('/saas/api/v1/me')['data']
        self.assertTrue(me['is_managed_teammate'])
        self.assertFalse(me['can_create_projects'])
        for route in ('/saas/api/v1/hosting/order', '/saas/api/v1/wallet', '/saas/api/v1/invoices', '/saas/api/v1/iam/members', '/saas/api/v1/iam/groups'):
            denied = self._rpc(route)
            self.assertFalse(denied['ok'], denied)
            self.assertEqual(denied['code'], 'access_denied')
        old_session = self.opener.cookies.copy()
        self.authenticate(self.other.login, 'iam-test-pass')
        self.assertFalse(self._rpc('/saas/api/v1/iam/members', member_id=profile.id, reset_password=True)['ok'])
        self.authenticate(self.owner.login, 'iam-test-pass')
        reset = self._rpc('/saas/api/v1/iam/members', member_id=profile.id, reset_password=True)
        self.assertTrue(reset['ok'], reset)
        # A password reset invalidates the old authenticated session.
        current_cookies = self.opener.cookies.copy()
        self.opener.cookies.clear()
        self.opener.cookies.update(old_session)
        self.assertFalse(self._rpc('/saas/api/v1/me')['ok'])
        self.opener.cookies.clear()
        self.opener.cookies.update(current_cookies)
        self.authenticate(profile.user_id.login, reset['data']['temporary_password'])
        self.assertTrue(self._rpc('/saas/api/v1/me')['data']['must_change_password'])
        self.assertTrue(self._rpc('/saas/api/v1/iam/password/change', password='MyReplacementPassword123!')['ok'])
        self.authenticate(self.owner.login, 'iam-test-pass')
        removed = self._rpc('/saas/api/v1/iam/members', member_id=profile.id, delete=True)
        self.assertTrue(removed['data']['account_disabled'], removed)
        self.assertEqual(self._rpc('/saas/api/v1/iam')['data']['profiles'], [])
        profile.user_id.invalidate_recordset()
        self.assertFalse(profile.user_id.active)


@tagged('post_install', '-at_install')
class TestRegistrationAccountIdentity(IamFixture, HttpCase):
    def _rpc(self, path, **params):
        return self.url_open(path, data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params, 'id': 1}),
            headers={'Content-Type': 'application/json'}).json()['result']

    def _details(self):
        return {'name': 'New Customer', 'email': 'registration-orphan@example.com',
                'phone': '+201012345678', 'country_id': self.env.ref('base.eg').id,
                'city': 'Cairo', 'password': 'RegistrationPassword123!', 'confirm_password': 'RegistrationPassword123!'}

    def test_registration_reuses_identifiers_but_not_an_orphan_customer_identity(self):
        from types import SimpleNamespace
        from odoo.addons.saas_website.controllers.api import SaasApi
        from odoo.addons.saas_website.controllers.registration import SaasRegistration
        details = self._details()
        orphan = self.env['res.partner'].create({'name': 'Historical Customer', 'email': details['email'],
            'phone': details['phone'], 'country_id': details['country_id']})
        order = self.env['sale.order'].create({'partner_id': orphan.id})
        request = SimpleNamespace(env=self.env)
        with patch('odoo.addons.saas_website.controllers.api.request', request):
            self.assertFalse(SaasApi()._validate_registration(details))
        with patch('odoo.addons.saas_website.controllers.registration.request', request):
            self.assertFalse(SaasRegistration()._validate_registration_fields(details))
        OTP = type(self.env['saas.registration.otp'])
        with patch.object(OTP, '_generate_and_send_phone', return_value=SimpleNamespace(code='123456')):
            started = self._rpc('/saas/api/v1/auth/register/start', **details)
        self.assertTrue(started['ok'], started)
        # No account is created before a verified phone code.
        with patch.object(OTP, '_verify', return_value=False):
            rejected = self._rpc('/saas/api/v1/auth/register/verify', otp='000000', **details)
        self.assertEqual(rejected['code'], 'otp_invalid')
        self.assertFalse(self.env['res.users'].search_count([('login', '=', details['email'])]))
        with patch.object(OTP, '_verify', return_value=True):
            registered = self._rpc('/saas/api/v1/auth/register/verify', otp='123456', **details)
        self.assertTrue(registered['ok'], registered)
        user = self.env['res.users'].search([('login', '=', details['email'])])
        self.assertTrue(user)
        self.assertNotEqual(user.partner_id, orphan)
        self.assertEqual(order.partner_id, orphan)
        self.assertFalse(user.iam_managed_owner_id)

    def test_registration_blocks_inactive_login_and_normalized_account_phone(self):
        from odoo.addons.saas_website.controllers.registration import _registration_identity_error
        details = self._details()
        self.other.partner_id.write({'country_id': details['country_id'], 'mobile': '010 1234 5678'})
        self.other.active = False
        self.assertTrue(_registration_identity_error(self.env, details['email'], details['phone'], details['country_id']))
        self.assertTrue(_registration_identity_error(self.env, self.other.login.upper(), '+201099988877', details['country_id']))
        denied = self._rpc('/saas/api/v1/auth/register/start', **details)
        self.assertFalse(denied['ok'], denied)

    def test_registration_blocks_account_email_when_login_is_different(self):
        from odoo.addons.saas_website.controllers.registration import _registration_identity_error
        self.other.partner_id.email = 'account-contact@example.com'
        self.assertTrue(_registration_identity_error(self.env, 'ACCOUNT-CONTACT@example.com', '+201099988877', self.env.ref('base.eg').id))
