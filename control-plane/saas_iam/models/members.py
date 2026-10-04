import re
import secrets
from odoo import api, fields, models, _
from odoo.exceptions import AccessDenied, AccessError, ValidationError


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
        return self.active and self.user_id.active and not self.user_id.sudo().iam_initial_password

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

    @api.model
    def _eligible_ids(self, owner):
        profiles = self.sudo().with_context(active_test=False).search([('owner_id', '=', owner.id)])
        accepted = self.env['saas.iam.invitation'].sudo().search([
            ('owner_id', '=', owner.id), ('state', '=', 'accepted'), ('user_id', '!=', False)])
        return list(set(profiles.filtered(lambda m: m.active and m.user_id.active).mapped('user_id').ids +
                        (accepted.mapped('user_id') - profiles.mapped('user_id')).filtered('active').ids))

    @api.model
    def _create_profile(self, name, email, phone=None):
        self.env['saas.iam']._require_customer_owner()
        owner = self.env.user.partner_id
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValidationError(_('Enter the teammate’s name.'))
        if not isinstance(email, str) or not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', email.strip()) or len(email) > 254:
            raise ValidationError(_('Enter a valid email address.'))
        phone = self._phone(phone)
        email = email.strip().lower()
        User = self.env['res.users'].sudo().with_context(active_test=False, no_reset_password=True)
        user = User.search([('login', '=ilike', email)], limit=1)
        self._check_unique_phone(phone, user.partner_id if user else None)
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
                user.write({'active': True, 'password': password, 'iam_initial_password': True})
        else:
            password = secrets.token_urlsafe(18)
            user = User.create({'name': name.strip(), 'login': email, 'email': email,
                'phone': phone, 'password': password, 'iam_initial_password': True,
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
        return member._credentials(password)

    def _credentials(self, password):
        self.ensure_one()
        return {'id': self.id, 'email': self.sudo().user_id.login, 'temporary_password': password,
                'login_url': self.env['ir.config_parameter'].sudo().get_param('web.base.url', '') + '/login'}

    @api.model
    def _phone(self, phone):
        if phone in (None, False, ''):
            return False
        phone = re.sub(r'[ ()-]', '', phone or '') if isinstance(phone, str) else ''
        if not re.fullmatch(r'\+[1-9][0-9]{7,14}', phone):
            raise ValidationError(_('Enter a phone number with its country code, for example +201012345678.'))
        return phone

    @api.model
    def _check_unique_phone(self, phone, partner=None):
        if not phone:
            return
        from odoo.addons.phone_validation.tools.phone_validation import phone_format
        country = (partner and partner.country_id) or self.env.user.partner_id.country_id or self.env.company.country_id
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
        contacts = self.env['res.partner'].sudo().with_context(active_test=False).search(['|', ('phone', '!=', False), ('mobile', '!=', False)])
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

    def _reset_password(self):
        member = self._owner_profile()
        if not member._can_manage_login():
            raise AccessError(_('This is an existing or shared account. Its password is managed by the account holder.'))
        password = secrets.token_urlsafe(18)
        member.user_id.sudo().write({'password': password, 'iam_initial_password': True})
        member._invalidate_user_access()
        self.env['saas.iam']._audit('iam.member.password.reset', detail='Profile %s' % member.id)
        return member._credentials(password)

    def _invalidate_user_access(self):
        self.ensure_one()
        grants = self.env['saas.iam.grant'].sudo().search([
            '|', ('user_id', '=', self.sudo().user_id.id),
            '&', ('group_id.active', '=', True), ('group_id.user_ids', 'in', self.sudo().user_id.ids)])
        self.env['saas.iam']._invalidate_access(grants.mapped('project_id'))

    def _delete_profile(self):
        member = self._owner_profile()
        disable_login = member._can_manage_login()
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

    @api.model_create_multi
    def create(self, vals_list):
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
                   '/saas/api/v1/iam/password/change', '/saas/api/v1/iam/verification/finish'}
        managed = bool(request.env.uid and request.env['saas.iam']._is_managed_user())
        customer_paths = ('/hosting/order', '/services/order', '/services/custom-order',
                          '/saas/api/v1/hosting/order', '/saas/api/v1/wallet',
                          '/saas/api/v1/invoices', '/saas/api/v1/billing', '/my/billing')
        if managed and any(path == p or path.startswith(p + '/') for p in customer_paths):
            if endpoint.routing.get('type') == 'json':
                return {'ok': False, 'error': _('Only the customer owner can create projects or manage billing.'), 'code': 'access_denied'}
            raise AccessError(_('Only the customer owner can create projects or manage billing.'))
        pending = bool(request.env.uid and request.env.user.sudo().iam_initial_password)
        if pending and (path == '/my' or path.startswith('/my/') or path in ('/hosting/order', '/services/order')) and path not in ('/my/change-password', '/my/verify-profile'):
            return request.redirect('/my/change-password')
        if pending and path.startswith('/saas/api/v1/') and path not in allowed:
            message = _('Change your temporary password before accessing your workspace.')
            if endpoint.routing.get('type') == 'json':
                return {'ok': False, 'error': message, 'code': 'password_change_required'}
            raise AccessError(message)
        return super()._dispatch(endpoint)
