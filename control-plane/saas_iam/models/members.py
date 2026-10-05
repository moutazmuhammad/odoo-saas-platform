import re
import secrets
import hmac
from html import escape
from datetime import timedelta
from odoo import api, fields, models, _
from odoo.exceptions import AccessDenied, AccessError, ValidationError
from odoo.addons.saas_core.fields import EncryptedChar


class IamMember(models.Model):
    _name = 'saas.iam.member'
    _description = 'Customer teammate profile'

    owner_id = fields.Many2one('res.partner', required=True, index=True, ondelete='cascade')
    user_id = fields.Many2one('res.users', required=True, index=True, ondelete='cascade')
    name = fields.Char(required=True)
    phone = fields.Char()
    active = fields.Boolean(default=True)
    _sql_constraints = [('owner_user_unique', 'unique(owner_id,user_id)', 'This teammate already has a profile.')]

    def _ready(self):
        self.ensure_one()
        return self.active and self.user_id.active and not self.user_id.sudo()._onboarding_pending()

    def _can_manage_login(self):
        """Never let a customer take over an independently owned/shared login."""
        self.ensure_one()
        member = self.sudo()
        user = member.user_id
        if not user.share or user.iam_managed_owner_id != member.owner_id:
            return False
        if self.sudo().search_count([('user_id', '=', user.id), ('owner_id', '!=', member.owner_id.id)]):
            return False
        grants = self.env['saas.iam.grant'].sudo().search_count([
            ('owner_id', '!=', member.owner_id.id), ('active', '=', True),
            '|', ('user_id', '=', user.id), '&', ('group_id.active', '=', True), ('group_id.user_ids', 'in', user.ids)])
        invitations = self.env['saas.iam.invitation'].sudo().search_count([
            ('owner_id', '!=', member.owner_id.id), ('user_id', '=', user.id), ('state', '=', 'accepted')])
        owned_projects = self.env['saas.instance'].sudo().with_context(active_test=False).search_count([('partner_id', '=', user.partner_id.id)])
        orders = self.env['sale.order'].sudo().search_count([('partner_id', '=', user.partner_id.id)])
        invoices = self.env['account.move'].sudo().search_count([('partner_id', '=', user.partner_id.id)])
        return not (grants or invitations or owned_projects or orders or invoices)

    def _can_disable_login(self):
        self.ensure_one()
        return self._can_manage_login() and not self.sudo().user_id.iam_account_verified

    @api.model
    def _eligible_ids(self, owner):
        profiles = self.sudo().with_context(active_test=False).search([('owner_id', '=', owner.id)])
        accepted = self.env['saas.iam.invitation'].sudo().search([
            ('owner_id', '=', owner.id), ('state', '=', 'accepted'), ('user_id', '!=', False)])
        return list(set(profiles.filtered(lambda m: m.active and m.user_id.active).mapped('user_id').ids +
                        (accepted.mapped('user_id') - profiles.mapped('user_id')).filtered('active').ids))

    @api.model
    def _create_profile(self, name, email, phone=None, country_id=None, send_credentials=False):
        self.env['saas.iam']._require_customer_owner()
        owner = self.env.user.partner_id
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValidationError(_('Enter the teammate’s name.'))
        if not isinstance(email, str) or not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', email.strip()) or len(email) > 254:
            raise ValidationError(_('Enter a valid email address.'))
        country = self._phone_country(country_id, owner.country_id)
        phone = self._phone(phone, country)
        email = email.strip().lower()
        User = self.env['res.users'].sudo().with_context(active_test=False, no_reset_password=True)
        user = User.search([('login', '=ilike', email)], limit=1)
        self._check_unique_phone(phone, user.partner_id if user else None, country)
        password = None
        if user:
            self.env.cr.execute('SELECT id FROM res_users WHERE id=%s FOR UPDATE', [user.id])
            user.invalidate_recordset()
            if user.iam_managed_owner_id and user.iam_managed_owner_id != owner:
                raise ValidationError(_('This account is managed by another customer. Invite the teammate to your projects instead.'))
            if not user.share or user == self.env.user:
                raise ValidationError(_('This account cannot be added as a teammate.'))
            old = self.sudo().with_context(active_test=False).search([('owner_id', '=', owner.id), ('user_id', '=', user.id)], limit=1)
            if old and old.active:
                raise ValidationError(_('This teammate already has a profile.'))
            if not user.active:
                if not old or not old._can_manage_login():
                    raise ValidationError(_('This account cannot be added as a teammate.'))
                password = secrets.token_urlsafe(18)
                user.write({'active': True, 'password': password, 'iam_initial_password': True,
                            'iam_verified_phone': False})
        else:
            password = secrets.token_urlsafe(18)
            user = User.create({'name': name.strip(), 'login': email, 'email': email,
                'phone': phone, 'password': password, 'iam_initial_password': True,
                'country_id': country.id if country else False,
                'iam_managed_owner_id': owner.id,
                'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])]})
            old = self.browse()
        values = {'owner_id': owner.id, 'user_id': user.id, 'name': name.strip(), 'phone': phone, 'active': True}
        if old:
            old.write(values)
            member = old
        else:
            member = self.sudo().create(values)
        self.env['saas.iam']._audit('iam.member.create', detail='Profile %s' % member.id)
        return member._credentials(password, send_credentials=send_credentials)

    def _credentials(self, password, send_credentials=False):
        self.ensure_one()
        result = {'id': self.id, 'email': self.sudo().user_id.login, 'temporary_password': password,
                'login_url': self.env['ir.config_parameter'].sudo().get_param('web.base.url', '') + '/login'}
        if send_credentials:
            # Recheck ownership before queuing sensitive sign-in details. Never accept
            # a recipient supplied by the browser.
            member = self._owner_profile()
            body = '<p>Hello %s,</p><p>Your teammate profile is ready.</p>' % escape(member.name)
            body += '<p>Sign-in page: %s<br>Email: %s</p>' % (
                escape(result['login_url']), escape(result['email']))
            if password:
                body += '<p>Temporary password: <strong>%s</strong></p>' % escape(password)
                body += '<p>On first login, replace your temporary password, then verify your mobile number through WhatsApp before accessing projects.</p>'
            else:
                body += '<p>Use your existing password to sign in.</p>'
            self.env['mail.mail'].sudo().create({
                'subject': _('Your teammate sign-in details'),
                'email_to': result['email'], 'body_html': body,
                'auto_delete': True,
            })
            result['email_queued'] = True
        return result

    def _email_credentials(self, password=None):
        member = self._owner_profile()
        user = member.user_id
        if password is not None:
            if (not member._can_manage_login() or not user.iam_initial_password
                    or not isinstance(password, str) or not 12 <= len(password) <= 256):
                raise ValidationError(_('These temporary credentials are no longer valid. Reset the password to generate new ones.'))
            try:
                user.with_user(user)._check_credentials(
                    {'login': user.login, 'password': password, 'type': 'password'}, {'interactive': True})
            except AccessDenied:
                raise ValidationError(_('These temporary credentials are no longer valid. Reset the password to generate new ones.')) from None
        self._credentials(password, send_credentials=True)
        return {'email_queued': True}

    @api.model
    def _phone_country(self, country_id=None, default_country=None):
        country = default_country or self.env.company.country_id
        if country_id:
            if isinstance(country_id, bool) or not str(country_id).isdigit():
                raise ValidationError(_('Choose a valid phone country.'))
            country = self.env['res.country'].sudo().browse(int(country_id)).exists()
            if not country:
                raise ValidationError(_('Choose a valid phone country.'))
        return country

    @api.model
    def _phone(self, phone, country=None):
        if phone in (None, False, ''):
            return False
        phone = re.sub(r'[ ()-]', '', phone or '') if isinstance(phone, str) else ''
        if phone.startswith('00'):
            phone = '+' + phone[2:]
        if phone and not phone.startswith('+'):
            from odoo.addons.phone_validation.tools.phone_validation import phone_format
            if not country or not country.phone_code:
                raise ValidationError(_('Choose the country for this phone number.'))
            phone = phone_format(phone, country.code, country.phone_code,
                                 force_format='E164', raise_exception=False) or ''
        if not re.fullmatch(r'\+[1-9][0-9]{7,14}', phone):
            raise ValidationError(_('Enter a valid phone number for the selected country.'))
        return phone

    @api.model
    def _check_unique_phone(self, phone, partner=None, country=None):
        if not phone:
            return
        from odoo.addons.phone_validation.tools.phone_validation import phone_format
        country = country or (partner and partner.country_id) or self.env.user.partner_id.country_id or self.env.company.country_id
        def canonical(value, contact_country=None):
            value = re.sub(r'[^0-9+]', '', value or '')
            if value.startswith('00'):
                value = '+' + value[2:]
            c = contact_country or country
            formatted = phone_format(value, c.code, c.phone_code, force_format='E164', raise_exception=False) if c else value
            if formatted and formatted.startswith('+'):
                return formatted
            if c and c.phone_code and value:
                prefix = str(c.phone_code)
                if value.startswith(prefix):
                    return '+' + value
                national = value if c.code == 'IT' else value.lstrip('0')
                return '+' + prefix + national
            return value
        key = canonical(phone)
        self.env.cr.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ['saas.phone:' + key])
        contacts = self.env['res.partner'].sudo().with_context(active_test=False).search([('user_ids', '!=', False), '|', ('phone', '!=', False), ('mobile', '!=', False)])
        for contact in contacts:
            if partner and contact.id == partner.id:
                continue
            if any(value and canonical(value, contact.country_id) == key for value in (contact.phone, contact.mobile)):
                raise ValidationError(_('This phone number is already in use.'))
        profiles = self.sudo().with_context(active_test=False).search([('phone', '!=', False)])
        if any((not partner or m.user_id.partner_id != partner) and canonical(m.phone, m.user_id.partner_id.country_id) == key for m in profiles):
            raise ValidationError(_('This phone number is already in use.'))

    def _owner_profile(self):
        self.env['saas.iam']._require_customer_owner()
        if len(self) != 1:
            raise AccessError(_('Teammate profile not found.'))
        # Use the same lock order for create, reset, delete and password changes.
        self.env.cr.execute('SELECT id FROM res_users WHERE id=%s FOR UPDATE', [self.sudo().user_id.id])
        self.env.cr.execute('SELECT id FROM saas_iam_member WHERE id=%s FOR UPDATE', [self.id])
        self.invalidate_recordset()
        self.sudo().user_id.invalidate_recordset()
        if not self.active or self.owner_id != self.env.user.partner_id:
            raise AccessError(_('Only the customer owner can manage this profile.'))
        return self.sudo()

    def _reset_password(self, send_credentials=False):
        member = self._owner_profile()
        if not member._can_manage_login():
            raise AccessError(_('This is an existing or shared account. Its password is managed by the account holder.'))
        password = secrets.token_urlsafe(18)
        member.user_id.sudo().write({'password': password, 'iam_initial_password': True,
                                    'iam_verified_phone': False, 'iam_phone_code': False})
        member._invalidate_user_access()
        self.env['saas.iam']._audit('iam.member.password.reset', detail='Profile %s' % member.id)
        return member._credentials(password, send_credentials=send_credentials)

    def _invalidate_user_access(self):
        self.ensure_one()
        grants = self.env['saas.iam.grant'].sudo().search([
            '|', ('user_id', '=', self.sudo().user_id.id),
            '&', ('group_id.active', '=', True), ('group_id.user_ids', 'in', self.sudo().user_id.ids)])
        self.env['saas.iam']._invalidate_access(grants.mapped('project_id'))

    def _delete_profile(self):
        member = self._owner_profile()
        disable_login = member._can_disable_login()
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
        if disable_login:
            member.user_id.sudo().write({'active': False})
        self.env['saas.iam']._invalidate_access(projects)
        self.env['saas.iam']._audit('iam.member.delete', detail='Profile %s' % member.id)
        return {'removed': True, 'account_disabled': disable_login}


class IamUser(models.Model):
    _inherit = 'res.users'
    iam_initial_password = fields.Boolean(default=False, groups='base.group_system')
    iam_managed_owner_id = fields.Many2one('res.partner', groups='base.group_system', ondelete='restrict', copy=False)
    iam_account_verified = fields.Boolean(default=False, groups='base.group_system', copy=False)
    iam_verified_phone = fields.Char(groups='base.group_system', copy=False)
    iam_phone_pending = fields.Char(groups='base.group_system', copy=False)
    iam_phone_country_id = fields.Many2one('res.country', groups='base.group_system', copy=False)
    iam_phone_code = EncryptedChar(groups='base.group_system', copy=False)
    iam_phone_code_expires = fields.Datetime(groups='base.group_system', copy=False)
    iam_phone_code_attempts = fields.Integer(groups='base.group_system', copy=False)

    def _needs_phone_verification(self):
        self.ensure_one()
        user = self.sudo()
        skip = self.env['ir.config_parameter'].sudo().get_param(
            'saas_iam.skip_teammate_phone_verification', 'False')
        if str(skip).lower() in ('true', '1'):
            return False
        return bool(user.iam_managed_owner_id and (
            not user.iam_verified_phone or user.iam_verified_phone != user.partner_id.phone))

    def _onboarding_pending(self):
        self.ensure_one()
        return bool(self.sudo().iam_initial_password or self._needs_phone_verification())

    @api.model
    def _phone_setup_user(self):
        user = self.env.user.sudo()
        self.env.cr.execute('SELECT id FROM res_users WHERE id=%s FOR UPDATE', [user.id])
        user.invalidate_recordset()
        if not user.active or user._is_public() or user.iam_initial_password or not user._needs_phone_verification():
            raise AccessError(_('Change your temporary password first, then verify your mobile number.'))
        return user

    def _deliver_phone_code(self, phone, code):
        """Use the shared WhatsApp sender for every phone verification."""
        self.env['saas.iam.whatsapp']._send_code(phone, code)

    @api.model
    def _send_phone_code(self, phone, country_id=None):
        user = self._phone_setup_user()
        Member = self.env['saas.iam.member']
        country = Member._phone_country(country_id, user.partner_id.country_id or user.iam_managed_owner_id.country_id)
        phone = Member._phone(phone, country)
        if not phone:
            raise ValidationError(_('Enter your mobile phone number.'))
        Member._check_unique_phone(phone, user.partner_id, country)
        code = '%06d' % secrets.randbelow(1000000)
        # Invalidate any previous challenge even if the new delivery fails.
        user.write({'iam_phone_code': False, 'iam_phone_pending': False})
        user._deliver_phone_code(phone, code)
        user.write({'iam_phone_pending': phone, 'iam_phone_country_id': country.id,
                    'iam_phone_code': code, 'iam_phone_code_attempts': 0,
                    'iam_phone_code_expires': fields.Datetime.now() + timedelta(minutes=10)})
        return {'otp_sent': True, 'phone': phone}

    @api.model
    def _verify_phone_code(self, code):
        user = self._phone_setup_user()
        if (not user.iam_phone_code or not user.iam_phone_pending or
                not user.iam_phone_code_expires or user.iam_phone_code_expires < fields.Datetime.now() or
                user.iam_phone_code_attempts >= 5):
            raise ValidationError(_('The verification code has expired. Request a new code.'))
        user.iam_phone_code_attempts += 1
        if not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code) or not hmac.compare_digest(user.iam_phone_code, code):
            raise ValidationError(_('The verification code is incorrect.'))
        # Recheck uniqueness at confirmation, including another account that
        # claimed the number while the WhatsApp was in transit.
        self.env['saas.iam.member']._check_unique_phone(
            user.iam_phone_pending, user.partner_id, user.iam_phone_country_id)
        phone = user.iam_phone_pending
        with self.env.cr.savepoint():
            user.partner_id.write({'phone': phone, 'mobile': False, 'country_id': user.iam_phone_country_id.id})
            user.write({'iam_verified_phone': phone, 'iam_account_verified': True, 'iam_phone_code': False,
                        'iam_phone_pending': False, 'iam_phone_code_expires': False})
            profiles = self.env['saas.iam.member'].sudo().search([('user_id', '=', user.id)])
            profiles.write({'phone': phone})
            for profile in profiles:
                profile._invalidate_user_access()
            self.env['saas.iam']._audit('iam.member.phone.verify', detail='User %s' % user.id)
        return {'verified': True}

    @api.model_create_multi
    def create(self, vals_list):
        # A caller may catch validation errors and still commit its request.
        # Never leave a newly created login behind after a rejected phone claim.
        with self.env.cr.savepoint():
            users = super().create(vals_list)
            for user in users.filtered('share'):
                for phone in (user.partner_id.phone, user.partner_id.mobile):
                    self.env['saas.iam.member']._check_unique_phone(phone, user.partner_id)
            return users

    @api.model
    def _change_initial_password(self, password):
        user = self.env.user
        self.env.cr.execute('SELECT id FROM res_users WHERE id=%s FOR UPDATE', [user.id])
        user.invalidate_recordset()
        if not user.active or user._is_public() or not user.sudo().iam_initial_password:
            raise AccessError(_('No initial password change is required for this account.'))
        if not isinstance(password, str) or len(password) < 12 or len(password) > 256 or password != password.strip():
            raise ValidationError(_('Choose a new password with at least 12 characters, without leading or trailing spaces.'))
        try:
            user._check_credentials({'login': user.login, 'password': password, 'type': 'password'}, {'interactive': True})
        except AccessDenied:
            pass
        else:
            raise ValidationError(_('Choose a password different from your temporary password.'))
        user.sudo().write({'password': password, 'iam_initial_password': False})
        self.env['saas.iam']._audit('iam.member.password.change', detail='User %s' % user.id)
        return {'changed': True}


class IamPasswordGate(models.AbstractModel):
    _inherit = 'ir.http'

    @classmethod
    def _dispatch(cls, endpoint):
        from odoo.http import request
        path = request.httprequest.path
        allowed = {'/saas/api/v1/me', '/saas/api/v1/auth/login', '/saas/api/v1/auth/logout',
                   '/saas/api/v1/iam/password/change', '/saas/api/v1/iam/verification/finish',
                   '/saas/api/v1/iam/phone/setup', '/saas/api/v1/iam/phone/send', '/saas/api/v1/iam/phone/verify'}
        managed = bool(request.env.uid and request.env['saas.iam']._is_managed_user())
        customer_paths = ('/hosting/order', '/services/order', '/services/custom-order',
                          '/saas/api/v1/hosting/order', '/saas/api/v1/wallet',
                          '/saas/api/v1/invoices', '/saas/api/v1/billing', '/my/billing')
        if managed and any(path == p or path.startswith(p + '/') for p in customer_paths):
            if endpoint.routing.get('type') == 'json':
                return {'ok': False, 'error': _('Only the customer owner can create projects or manage billing.'), 'code': 'access_denied'}
            raise AccessError(_('Only the customer owner can create projects or manage billing.'))
        pending = bool(request.env.uid and request.env.user.sudo()._onboarding_pending())
        if pending and (path == '/my' or path.startswith('/my/') or path in ('/hosting/order', '/services/order')) and path not in ('/my/change-password', '/my/verify-profile'):
            return request.redirect('/my/change-password')
        if pending and path.startswith('/saas/api/v1/') and path not in allowed:
            password_pending = request.env.user.sudo().iam_initial_password
            message = (_('Change your temporary password before accessing your workspace.') if password_pending
                       else _('Verify your mobile number before accessing your workspace.'))
            if endpoint.routing.get('type') == 'json':
                return {'ok': False, 'error': message, 'code': 'password_change_required' if password_pending else 'phone_verification_required'}
            raise AccessError(message)
        return super()._dispatch(endpoint)
