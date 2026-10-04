import hashlib
import hmac
import re
import secrets
from datetime import timedelta
from markupsafe import escape
from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError, UserError


class IamMember(models.Model):
    _name = 'saas.iam.member'
    _description = 'Customer teammate profile'

    owner_id = fields.Many2one('res.partner', required=True, index=True, ondelete='cascade')
    user_id = fields.Many2one('res.users', required=True, index=True, ondelete='cascade')
    name = fields.Char(required=True)
    phone = fields.Char(required=True)
    active = fields.Boolean(default=True)
    email_verified = fields.Boolean(default=False)
    verified_email = fields.Char()
    phone_verified = fields.Boolean(default=False)
    _sql_constraints = [('owner_user_unique', 'unique(owner_id,user_id)', 'This teammate already has a profile.')]

    def _ready(self):
        self.ensure_one()
        return self.active and self.email_verified and self.verified_email == self.user_id.login and self.phone_verified and not self.user_id.sudo().iam_initial_password

    @api.model
    def _eligible_ids(self, owner):
        profiles = self.sudo().with_context(active_test=False).search([('owner_id', '=', owner.id)])
        accepted = self.env['saas.iam.invitation'].sudo().search([
            ('owner_id', '=', owner.id), ('state', '=', 'accepted'), ('user_id', '!=', False)])
        # A removed profile overrides a legacy invitation; it cannot restore access.
        return list(set(profiles.filtered('active').mapped('user_id').ids +
                        (accepted.mapped('user_id') - profiles.mapped('user_id')).ids))

    @api.model
    def _create_profile(self, name, email, phone):
        owner = self.env.user.partner_id
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValidationError(_('Enter the teammate’s name.'))
        if not isinstance(email, str) or not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', email.strip()) or len(email) > 254:
            raise ValidationError(_('Enter a valid email address.'))
        phone = self._phone(phone)
        email = email.strip().lower()
        User = self.env['res.users'].sudo().with_context(active_test=False, no_reset_password=True)
        user = User.search([('login', '=ilike', email)], limit=1)
        password = None
        if user:
            if not user.active or not user.share or user == self.env.user:
                raise ValidationError(_('This account cannot be added as a teammate.'))
        else:
            password = secrets.token_urlsafe(18)
            user = User.create({'name': name.strip(), 'login': email, 'email': email,
                'phone': phone, 'password': password, 'iam_initial_password': True,
                'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])]})
        old = self.sudo().with_context(active_test=False).search([('owner_id', '=', owner.id), ('user_id', '=', user.id)], limit=1)
        if old and old.active:
            raise ValidationError(_('This teammate already has a profile.'))
        values = {'owner_id': owner.id, 'user_id': user.id, 'name': name.strip(), 'phone': phone,
                  'active': True, 'email_verified': False, 'verified_email': False, 'phone_verified': False}
        if old:
            old.write(values)
            old.env['saas.iam.challenge'].sudo().search([('member_id', '=', old.id)]).unlink()
            member = old
        else:
            member = self.sudo().create(values)
        self.env['saas.iam']._audit('iam.member.create', detail='Profile %s' % member.id)
        return {'id': member.id, 'email': email, 'temporary_password': password,
                'login_url': self.env['ir.config_parameter'].sudo().get_param('web.base.url', '') + '/login'}

    @api.model
    def _phone(self, phone):
        phone = re.sub(r'[ ()-]', '', phone or '') if isinstance(phone, str) else ''
        if not re.fullmatch(r'\+[1-9][0-9]{7,14}', phone):
            raise ValidationError(_('Enter a phone number with its country code, for example +201012345678.'))
        return phone

    def _delete_profile(self):
        self.ensure_one()
        if self.owner_id != self.env.user.partner_id:
            raise AccessError(_('Only the customer owner can remove this profile.'))
        member = self.sudo()
        grants = self.env['saas.iam.grant'].sudo().search([('owner_id', '=', member.owner_id.id), ('user_id', '=', member.user_id.id)])
        groups = self.env['saas.iam.group'].sudo().search([('owner_id', '=', member.owner_id.id), ('user_ids', 'in', member.user_id.ids)])
        projects = grants.mapped('project_id') | self.env['saas.iam.grant'].sudo().search([('group_id', 'in', groups.ids)]).mapped('project_id')
        grants.write({'active': False})
        for group in groups:
            group.write({'user_ids': [(3, member.user_id.id)]})
        invitations = self.env['saas.iam.invitation'].sudo().search([('owner_id', '=', member.owner_id.id), '|', ('user_id', '=', member.user_id.id), ('email', '=ilike', member.user_id.login)])
        invitations.mapped('grant_ids').write({'active': False})
        invitations.write({'state': 'revoked'})
        member.write({'active': False})
        self.env['saas.iam.challenge'].sudo().search([('member_id', '=', member.id)]).unlink()
        self.env['saas.iam']._invalidate_access(projects)
        self.env['saas.iam']._audit('iam.member.delete', detail='Profile %s' % member.id)

    def _self_profile(self):
        if len(self) != 1:
            raise AccessError(_('Teammate profile not found.'))
        self.env.cr.execute('SELECT id FROM saas_iam_member WHERE id=%s FOR UPDATE', [self.id])
        self.invalidate_recordset()
        if not self.active or self.user_id != self.env.user:
            raise AccessError(_('Teammate profile not found.'))
        return self.sudo()

    def _send_code(self, channel, phone=None):
        member = self._self_profile()
        if channel not in ('email', 'phone'):
            raise ValidationError(_('Select email or phone verification.'))
        if channel == 'phone' and phone is not None:
            phone = self._phone(phone)
            if phone != member.phone:
                member.write({'phone': phone, 'phone_verified': False})
        identifier = member.user_id.login if channel == 'email' else member.phone
        code = '%06d' % secrets.randbelow(1000000)
        Challenge = self.env['saas.iam.challenge'].sudo()
        # These challenges are separate from signup/password reset OTPs.
        Challenge.search([('member_id', '=', member.id), ('channel', '=', channel)]).unlink()
        Challenge.create({'member_id': member.id, 'channel': channel, 'identifier': identifier,
            'code_hash': hashlib.sha256(code.encode()).hexdigest(),
            'expires_at': fields.Datetime.now() + timedelta(minutes=10)})
        if channel == 'email':
            try:
                self.env['mail.mail'].sudo().create({'subject': _('Verify your teammate email'),
                    'email_to': identifier, 'body_html': '<p>%s</p><p><strong>%s</strong></p><p>%s</p>' % (
                        escape(_('Your email verification code is:')), code, escape(_('This code expires in 10 minutes.')))}).send(raise_exception=True)
            except Exception:
                raise UserError(_('We could not send your email code. Contact the site administrator to check outgoing email.')) from None
        else:
            if 'sms.sms' not in self.env:
                raise UserError(_('SMS verification is unavailable. Contact the site administrator to configure SMS delivery.'))
            sms = self.env['sms.sms'].sudo().create({'number': identifier, 'body': _('Your phone verification code is: %s', code)})
            try:
                sms.send(unlink_sent=False, raise_exception=True)
            except Exception:
                raise UserError(_('The SMS provider could not send your code. Please contact the site administrator.')) from None
            if sms.state not in ('sent', 'pending', 'process'):
                raise UserError(_('The SMS provider could not send your code. Please contact the site administrator.'))
        return {'sent': True}

    def _verify_code(self, channel, code):
        member = self._self_profile()
        if channel not in ('email', 'phone') or not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code):
            return {'verified': False}
        Challenge = self.env['saas.iam.challenge'].sudo()
        challenge = Challenge.search([('member_id', '=', member.id), ('channel', '=', channel)], limit=1)
        if not challenge:
            return {'verified': False}
        self.env.cr.execute('SELECT id FROM saas_iam_challenge WHERE id=%s FOR UPDATE', [challenge.id])
        challenge.invalidate_recordset()
        identifier = member.user_id.login if channel == 'email' else member.phone
        if challenge.used or challenge.attempts >= 6 or challenge.expires_at < fields.Datetime.now() or challenge.identifier != identifier:
            return {'verified': False}
        challenge.attempts += 1
        if not hmac.compare_digest(challenge.code_hash, hashlib.sha256(code.encode()).hexdigest()):
            return {'verified': False}
        challenge.used = True
        member.write({channel + '_verified': True, **({'verified_email': identifier} if channel == 'email' else {})})
        return {'verified': True}

    def _finish(self, password=None):
        member = self._self_profile()
        if not member.email_verified or member.verified_email != member.user_id.login or not member.phone_verified:
            raise ValidationError(_('Verify your email and phone first.'))
        user = member.user_id.sudo()
        if user.iam_initial_password:
            if not isinstance(password, str) or len(password) < 12 or len(password) > 256:
                raise ValidationError(_('Choose a new password with at least 12 characters.'))
            user.write({'password': password, 'iam_initial_password': False})
        return {'verified': True}


class IamChallenge(models.Model):
    _name = 'saas.iam.challenge'
    _description = 'Account-bound teammate verification challenge'
    member_id = fields.Many2one('saas.iam.member', required=True, index=True, ondelete='cascade')
    channel = fields.Selection([('email', 'Email'), ('phone', 'Phone')], required=True)
    identifier = fields.Char(required=True)
    code_hash = fields.Char(required=True)
    expires_at = fields.Datetime(required=True)
    attempts = fields.Integer(default=0)
    used = fields.Boolean(default=False)

    @api.autovacuum
    def _gc_expired(self):
        self.sudo().search([('expires_at', '<', fields.Datetime.now())]).unlink()


class IamUser(models.Model):
    _inherit = 'res.users'
    iam_initial_password = fields.Boolean(default=False, groups='base.group_system')
