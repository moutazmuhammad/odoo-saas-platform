import hashlib
from datetime import timedelta
from unittest.mock import patch
from odoo import fields
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import AccessError, ValidationError, UserError
from .test_iam import IamFixture


@tagged('post_install', '-at_install')
class TestIamMembers(IamFixture, TransactionCase):
    def _profile(self, owner=None, user=None):
        return self.env['saas.iam.member'].sudo().create({'owner_id': (owner or self.owner).partner_id.id,
            'user_id': (user or self.team).id, 'name': 'Teammate', 'phone': '+201012345678'})

    def _challenge(self, profile, channel='email', code='123456'):
        return self.env['saas.iam.challenge'].sudo().create({'member_id': profile.id, 'channel': channel,
            'identifier': profile.user_id.login if channel == 'email' else profile.phone,
            'code_hash': hashlib.sha256(code.encode()).hexdigest(),
            'expires_at': fields.Datetime.now() + timedelta(minutes=10)})

    def test_create_profile_immediately_without_accepting_invitation(self):
        result = self.env['saas.iam.member'].with_user(self.owner)._create_profile('New Teammate', 'new-teammate@example.com', '+201012345678')
        member = self.env['saas.iam.member'].sudo().browse(result['id'])
        self.assertTrue(member.user_id.share)
        self.assertTrue(member.user_id.iam_initial_password)
        self.assertTrue(result['temporary_password'])
        self.assertIn(member.user_id.id, member._eligible_ids(self.owner.partner_id))
        self._grant('project_admin', user=member.user_id)
        self.assertFalse(self.iam._allowed(self.project, 'project.discover', member.user_id))
        self.assertFalse(self.iam._allowed(self.project, 'db.delete', member.user_id))
        self.assertNotIn(self.project.id, self.iam._visible_project_ids(member.user_id))
        member.write({'verified_email': member.user_id.login, 'email_verified': True, 'phone_verified': True})
        self.assertFalse(self.iam._allowed(self.project, 'db.delete', member.user_id))
        with self.assertRaises(ValidationError):
            member.with_user(member.user_id)._finish('short')
        member.with_user(member.user_id)._finish('MyOwnStrongPassword123!')
        self.assertTrue(self.iam._allowed(self.project, 'db.delete', member.user_id))

    def test_existing_account_credentials_and_contact_are_preserved(self):
        original = (self.team.name, self.team.partner_id.phone)
        result = self.env['saas.iam.member'].with_user(self.owner)._create_profile('Profile Name', self.team.login, '+201012345678')
        self.assertFalse(result['temporary_password'])
        self.assertEqual(original, (self.team.name, self.team.partner_id.phone))
        self.assertFalse(self.team.sudo().iam_initial_password)
        with self.assertRaises(ValidationError):
            self.env['saas.iam.member'].with_user(self.owner)._create_profile('Again', self.team.login, '+201012345678')

    def test_codes_are_single_use_account_channel_and_purpose_bound(self):
        profile = self._profile()
        challenge = self._challenge(profile)
        own = profile.with_user(self.team)
        with self.assertRaises(AccessError):
            profile.with_user(self.stranger)._verify_code('email', '123456')
        self.assertFalse(own._verify_code('phone', '123456')['verified'])
        self.assertFalse(self.env['saas.registration.otp'].sudo()._verify(self.team.login, '123456', 'email'))
        self.assertTrue(own._verify_code('email', '123456')['verified'])
        self.assertFalse(own._verify_code('email', '123456')['verified'])
        self.assertTrue(challenge.used)
        self.assertFalse(profile.phone_verified)
        with self.assertRaises(ValidationError):
            own._finish()

    def test_wrong_attempts_and_expired_or_changed_identifier_are_rejected(self):
        profile = self._profile()
        challenge = self._challenge(profile)
        for i in range(6):
            self.assertFalse(profile.with_user(self.team)._verify_code('email', '000000')['verified'])
        self.assertFalse(profile.with_user(self.team)._verify_code('email', '123456')['verified'])
        self.assertEqual(challenge.attempts, 6)
        challenge.write({'attempts': 0, 'expires_at': fields.Datetime.now() - timedelta(seconds=1)})
        self.assertFalse(profile.with_user(self.team)._verify_code('email', '123456')['verified'])
        challenge.write({'expires_at': fields.Datetime.now() + timedelta(minutes=1), 'identifier': 'changed@example.com'})
        self.assertFalse(profile.with_user(self.team)._verify_code('email', '123456')['verified'])

    def test_owner_deletes_profile_and_revokes_only_their_customer_access(self):
        profile = self._profile()
        other_profile = self._profile(owner=self.other)
        profile.write({'verified_email': profile.user_id.login, 'email_verified': True, 'phone_verified': True})
        other_profile.write({'verified_email': other_profile.user_id.login, 'email_verified': True, 'phone_verified': True})
        self._grant('viewer')
        self._grant('viewer', project=self.foreign)
        group = self.env['saas.iam.group'].sudo().create({'owner_id': self.owner.partner_id.id, 'name': 'Team', 'user_ids': [(4, self.team.id)]})
        self._grant('deploy', group=group)
        with self.assertRaises(AccessError):
            profile.with_user(self.other)._delete_profile()
        profile.with_user(self.owner)._delete_profile()
        self.assertFalse(profile.active)
        self.assertNotIn(self.team.id, group.user_ids.ids)
        self.assertTrue(self.team.active)
        self.assertFalse(self.iam._allowed(self.project, 'project.view', self.team))
        self.assertTrue(self.iam._allowed(self.foreign, 'project.view', self.team))
        self.assertNotIn(self.team.id, profile._eligible_ids(self.owner.partner_id))
        with self.assertRaises(AccessError):
            profile.with_user(self.team)._verify_code('email', '123456')

    def test_portal_cannot_write_verified_flags_or_challenges(self):
        profile = self._profile()
        with self.assertRaises(AccessError):
            profile.with_user(self.team).write({'phone_verified': True})
        with self.assertRaises(AccessError):
            self.env['saas.iam.challenge'].with_user(self.team).search([])
        with self.assertRaises(AccessError):
            self.team.with_user(self.team).write({'iam_initial_password': False})

    def test_new_codes_replace_old_codes_and_phone_change_resets_verification(self):
        profile = self._profile()
        profile.phone_verified = True
        old = self._challenge(profile, 'phone')
        def send(sms, **kwargs):
            sms.write({'state': 'pending'})
        with patch.object(type(self.env['sms.sms']), 'send', send):
            result = profile.with_user(self.team)._send_code('phone', '+201099999999')
        self.assertTrue(result['sent'])
        self.assertFalse(old.exists())
        self.assertFalse(profile.phone_verified)
        self.assertEqual(profile.phone, '+201099999999')
        self.assertFalse(profile.with_user(self.team)._verify_code('phone', '123456')['verified'])


from odoo.tests.common import HttpCase
import json


@tagged('post_install', '-at_install')
class TestIamMemberApi(IamFixture, HttpCase):
    def _rpc(self, path, **params):
        return self.url_open(path, data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params, 'id': 1}),
            headers={'Content-Type': 'application/json'}).json()['result']

    def test_owner_add_assign_verify_and_delete_profile(self):
        self.authenticate(self.owner.login, 'iam-test-pass')
        result = self._rpc('/saas/api/v1/iam/members', name='New Person', email='iamnewprofile@example.com', phone='+201012345678')
        self.assertTrue(result['ok'], result)
        profile = self.env['saas.iam.member'].sudo().browse(result['data']['id'])
        password = result['data']['temporary_password']
        catalog = self._rpc('/saas/api/v1/iam')['data']
        self.assertEqual(catalog['profiles'][0]['id'], profile.id)
        self.assertFalse(catalog['profiles'][0]['verified'])
        self.assertIn(profile.user_id.id, [m['id'] for m in catalog['members']])
        granted = self._rpc('/saas/api/v1/iam/grant', project_ids=[self.project.id], roles=['viewer'], environments=['all'], user_id=profile.user_id.id)
        self.assertTrue(granted['ok'], granted)
        group = self._rpc('/saas/api/v1/iam/groups', name='Before Login', user_ids=[profile.user_id.id])
        self.assertTrue(group['ok'], group)
        self.authenticate(profile.user_id.login, password)
        pending = self._rpc('/saas/api/v1/me')['data']['teammate_verification']
        self.assertEqual(pending[0]['id'], profile.id)
        self.assertTrue(pending[0]['needs_password'])
        self.assertFalse(self._rpc('/saas/api/v1/instances/%s/status' % self.project.id)['ok'])
        for channel in ('email', 'phone'):
            self.env['saas.iam.challenge'].sudo().create({'member_id': profile.id, 'channel': channel,
                'identifier': profile.user_id.login if channel == 'email' else profile.phone,
                'code_hash': hashlib.sha256(b'123456').hexdigest(),
                'expires_at': fields.Datetime.now() + timedelta(minutes=10)})
            verified = self._rpc('/saas/api/v1/iam/verification/verify', member_id=profile.id, channel=channel, code='123456')
            self.assertTrue(verified['data']['verified'], verified)
        completed = self._rpc('/saas/api/v1/iam/verification/finish', member_id=profile.id, password='MyNewOwnPassword123!')
        self.assertTrue(completed['ok'], completed)
        # Password replacement retains the current authenticated session.
        self.assertEqual(self._rpc('/saas/api/v1/me')['data']['teammate_verification'], [])
        self.assertTrue(self._rpc('/saas/api/v1/instances/%s/status' % self.project.id)['ok'])
        self.authenticate(self.other.login, 'iam-test-pass')
        self.assertFalse(self._rpc('/saas/api/v1/iam/members', member_id=profile.id, delete=True)['ok'])
        self.authenticate(self.owner.login, 'iam-test-pass')
        removed = self._rpc('/saas/api/v1/iam/members', member_id=profile.id, delete=True)
        self.assertTrue(removed['ok'], removed)
        self.assertEqual(self._rpc('/saas/api/v1/iam')['data']['profiles'], [])
        self.authenticate(profile.user_id.login, 'MyNewOwnPassword123!')
        self.assertFalse(self._rpc('/saas/api/v1/instances/%s/status' % self.project.id)['ok'])
