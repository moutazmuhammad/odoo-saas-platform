import importlib.util
import json
from pathlib import Path
from unittest.mock import patch
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

    def test_normal_portal_signup_cannot_reuse_teammate_phone(self):
        self.env['saas.iam.member'].with_user(self.owner)._create_profile('Phone Owner', 'phone-owner@example.com', '+201012345678')
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.env['res.users'].sudo().with_context(no_reset_password=True).create({
                'name': 'Phone Duplicate', 'login': 'phone-duplicate@example.com',
                'phone': '+20 10 1234 5678',
                'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])],
            })

    def test_managed_user_cannot_become_customer_owner_after_password_change(self):
        member, _ = self._new_profile()
        user = member.user_id
        self.env['res.users'].with_user(user)._change_initial_password('MyOwnStrongPassword123!')
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

    def test_first_login_only_requires_own_password_and_blocks_temporary_password(self):
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
        self.assertTrue(self.iam._allowed(self.project, 'project.view', member.user_id))

    def test_shared_account_cannot_be_reset_or_disabled_by_one_customer(self):
        member, result = self._new_profile()
        self.env['res.users'].with_user(member.user_id)._change_initial_password('MyOwnStrongPassword123!')
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
