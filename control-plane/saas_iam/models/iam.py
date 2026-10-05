import hashlib
import secrets
from datetime import timedelta
from html import escape

from odoo import api, fields, models, _, SUPERUSER_ID
from odoo.exceptions import AccessError, ValidationError
from ..roles import ENVIRONMENTS, ROLE_SELECTION, ROLE_DEFINITIONS, PROJECT_PERMISSIONS, permissions_for


class IamGroup(models.Model):
    _name = 'saas.iam.group'
    _description = 'Customer Team Group'
    name = fields.Char(required=True)
    owner_id = fields.Many2one('res.partner', required=True, index=True, ondelete='cascade')
    user_ids = fields.Many2many('res.users', string='Accepted team members')
    active = fields.Boolean(default=True)


class IamInvitation(models.Model):
    _name = 'saas.iam.invitation'
    _description = 'Project Access Invitation'
    email = fields.Char(required=True, index=True)
    owner_id = fields.Many2one('res.partner', required=True, index=True, ondelete='cascade')
    token_hash = fields.Char(required=True, index=True, groups='saas_core.group_saas_manager')
    expires_at = fields.Datetime(required=True)
    state = fields.Selection([('pending', 'Pending'), ('accepted', 'Accepted'), ('revoked', 'Revoked')], default='pending', required=True)
    user_id = fields.Many2one('res.users', ondelete='set null')
    group_id = fields.Many2one('saas.iam.group', ondelete='cascade')
    grant_ids = fields.One2many('saas.iam.grant', 'invitation_id')


class IamGrant(models.Model):
    _name = 'saas.iam.grant'
    _description = 'Fixed Project Role Grant'
    _order = 'project_id, user_id, role, environment'
    project_id = fields.Many2one('saas.instance', required=True, index=True, ondelete='cascade')
    owner_id = fields.Many2one(related='project_id.partner_id', store=True, index=True)
    user_id = fields.Many2one('res.users', index=True, ondelete='cascade')
    group_id = fields.Many2one('saas.iam.group', index=True, ondelete='cascade')
    invitation_id = fields.Many2one('saas.iam.invitation', ondelete='cascade')
    role = fields.Selection(ROLE_SELECTION, required=True)
    environment = fields.Selection([('all', 'All environments')] + [(e, e.title()) for e in ENVIRONMENTS], required=True, default='all', index=True)
    active = fields.Boolean(default=True, index=True)

    @api.constrains('project_id', 'user_id', 'group_id', 'invitation_id')
    def _check_scope(self):
        for grant in self:
            if grant.project_id.parent_id:
                raise ValidationError(_('Roles must be assigned to a top-level project.'))
            if bool(grant.user_id) + bool(grant.group_id) + bool(grant.invitation_id) != 1:
                raise ValidationError(_('Select exactly one user, team group or pending invitation.'))
            if grant.group_id and grant.group_id.owner_id != grant.owner_id:
                raise ValidationError(_('The team group belongs to another customer.'))
            if grant.invitation_id and grant.invitation_id.owner_id != grant.owner_id:
                raise ValidationError(_('The invitation belongs to another customer.'))


class IamService(models.AbstractModel):
    _name = 'saas.iam'
    _description = 'Project IAM Policy Evaluation'

    def _project(self, instance):
        return instance.sudo().parent_id or instance.sudo()

    def _is_platform_admin(self, user):
        return user.id == SUPERUSER_ID or user.has_group('base.group_system') or user.has_group('saas_core.group_saas_manager')

    def _is_managed_user(self, user=None):
        user = user or self.env.user
        return bool(user.sudo().iam_managed_owner_id and not self._is_platform_admin(user))

    def _require_customer_owner(self):
        user = self.env.user
        if not user.active or user._is_public() or user.sudo()._onboarding_pending():
            raise AccessError(_('Complete your account setup before creating projects or managing teammates.'))

    def _project_domain(self):
        shared = [('id', 'in', self._visible_project_ids())]
        return ['|', ('partner_id', '=', self.env.user.partner_id.id)] + shared

    def _is_owner(self, instance, user=None):
        user = user or self.env.user
        if user.id == SUPERUSER_ID:
            return True
        if user.sudo()._onboarding_pending():
            return False
        return bool(user.active and not user._is_public() and (self._is_platform_admin(user) or (self._project(instance).partner_id == user.partner_id)))

    def _grants(self, project, user=None):
        user = user or self.env.user
        if not user.active or user._is_public() or user.sudo()._onboarding_pending():
            return self.env['saas.iam.grant']
        profiles = self.env['saas.iam.member'].sudo().with_context(active_test=False).search([
            ('owner_id', '=', self._project(project).partner_id.id), ('user_id', '=', user.id)])
        if profiles and not profiles._ready():
            return self.env['saas.iam.grant']
        return self.env['saas.iam.grant'].sudo().search([
            ('project_id', '=', self._project(project).id), ('active', '=', True),
            '|', ('user_id', '=', user.id), '&', ('group_id.active', '=', True), ('group_id.user_ids', 'in', [user.id]),
        ])

    def _permissions(self, instance, user=None, environment=None):
        user = user or self.env.user
        if self._is_owner(instance, user):
            return PROJECT_PERMISSIONS | {'billing.manage', 'database.manager'}
        scope = environment or instance.sudo().environment
        grants = self._grants(instance, user).filtered(lambda g: g.environment in ('all', scope))
        permissions = frozenset().union(*(permissions_for(g.role) for g in grants))
        if user.has_group('base.group_user') or user.has_group('saas_website.group_saas_support'):
            permissions |= {'project.view', 'build.view', 'logs.view', 'backup.view', 'db.view'}
        return permissions

    def _allowed(self, instance, permission, user=None, environment=None):
        user = user or self.env.user
        if not instance.exists():
            return False
        if user.id == SUPERUSER_ID:
            return True
        if not user.active or user._is_public() or user.sudo()._onboarding_pending():
            return False
        if permission == 'project.discover':
            return self._is_owner(instance, user) or bool(self._grants(instance, user)) or user.has_group('base.group_user') or user.has_group('saas_website.group_saas_support')
        if permission in self._permissions(instance, user, environment):
            return True
        # Platform support retains its existing read access, never customer IAM powers.
        return permission in ('project.view', 'build.view', 'logs.view', 'backup.view', 'db.view') and (
            user.has_group('base.group_user') or user.has_group('saas_website.group_saas_support'))

    def _require(self, instance, permission, user=None, environment=None):
        user = user or self.env.user
        if not self._allowed(instance, permission, user, environment):
            raise AccessError(_('You do not have permission to perform this action on this environment.'))
        return instance.sudo()

    def _visible_project_ids(self, user=None):
        user = user or self.env.user
        if not user.active or user._is_public() or user.sudo()._onboarding_pending():
            return []
        grants = self.env['saas.iam.grant'].sudo().search([
            ('active', '=', True), '|', ('user_id', '=', user.id),
            '&', ('group_id.active', '=', True), ('group_id.user_ids', 'in', [user.id]),
        ])
        profiles = self.env['saas.iam.member'].sudo().with_context(active_test=False).search([('user_id', '=', user.id)])
        blocked = set(profiles.filtered(lambda m: not m._ready()).mapped('owner_id').ids)
        return grants.filtered(lambda g: g.owner_id.id not in blocked).mapped('project_id').ids

    def _visible_instance_ids(self, user=None):
        user = user or self.env.user
        projects = self.env['saas.instance'].sudo().browse(self._visible_project_ids(user))
        children = projects.mapped('child_env_ids').filtered(lambda i: self._allowed(i, 'project.view', user))
        return (projects | children).ids

    def _validate_assignment(self, project, roles, environments):
        for environment in environments:
            scopes = ENVIRONMENTS if environment == 'all' else (environment,)
            for scope in scopes:
                self._require(project, 'iam.manage', environment=scope)
                effective = self._permissions(project, environment=scope)
                for role in roles:
                    if not permissions_for(role).issubset(effective):
                        raise AccessError(_('You can only grant roles whose permissions you hold in the same environment scope.'))

    def _validate_payload(self, project_ids, roles, environments):
        if not isinstance(project_ids, list) or not project_ids or len(project_ids) > 100:
            raise ValidationError(_('Select one or several projects.'))
        if not isinstance(roles, list) or not roles or any(r not in ROLE_DEFINITIONS for r in roles):
            raise ValidationError(_('Select predefined roles.'))
        if not isinstance(environments, list) or not environments or any(e not in ('all',) + ENVIRONMENTS for e in environments):
            raise ValidationError(_('Select valid environment scopes.'))
        projects = self.env['saas.instance'].sudo().browse([int(p) for p in project_ids]).exists()
        if len(projects) != len(set(project_ids)) or any(p.parent_id for p in projects):
            raise ValidationError(_('Select existing top-level projects.'))
        if len(projects.mapped('partner_id')) != 1:
            raise ValidationError(_('Projects must belong to the same customer.'))
        for project in projects:
            self._validate_assignment(project, roles, environments)
        return projects

    def _audit(self, action, project=None, detail=''):
        self.env['saas.audit.log'].saas_audit(action, model='saas.instance' if project else None,
            res_id=project.id if project else None, detail=detail)

    def _assign(self, projects, roles, environments, **subject):
        Grant = self.env['saas.iam.grant'].sudo()
        for project in projects:
            for role in set(roles):
                for environment in set(environments):
                    values = {'project_id': project.id, 'role': role, 'environment': environment, **subject}
                    domain = [(key, '=', value) for key, value in values.items()]
                    old = Grant.search(domain, limit=1)
                    if old:
                        old.write({'active': True})
                    else:
                        Grant.create(values)
            self._audit('iam.grant', project, 'Roles: %s; environments: %s' % (','.join(roles), ','.join(environments)))

    def _invite(self, email, project_ids, roles, environments, group_id=None):
        projects = self._validate_payload(project_ids, roles, environments)
        if not isinstance(email, str):
            raise ValidationError(_('Enter a valid email address.'))
        email = (email or '').strip().lower()
        if len(email) > 254 or '@' not in email or any(c in email for c in '\r\n <>'):
            raise ValidationError(_('Enter a valid email address.'))
        owner = projects[0].partner_id
        group = self.env['saas.iam.group'].sudo().browse(int(group_id or 0)).exists()
        if group_id and (not group or group.owner_id != owner or not self._is_owner(projects[0])):
            raise AccessError(_('Only the customer owner can manage team group membership.'))
        token = secrets.token_urlsafe(32)
        invitation = self.env['saas.iam.invitation'].sudo().create({
            'email': email, 'owner_id': owner.id, 'token_hash': hashlib.sha256(token.encode()).hexdigest(),
            'expires_at': fields.Datetime.now() + timedelta(days=7), 'group_id': group.id,
        })
        self._assign(projects, roles, environments, invitation_id=invitation.id)
        origin = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '').rstrip('/')
        url = origin + '/my/access/accept?token=' + token
        # Queue email; the owner can also copy the invite link if SMTP isn't configured.
        self.env['mail.mail'].sudo().create({
            'subject': 'Invitation to project access on VELTNEX', 'email_to': email,
            'body_html': '<p>You have been invited to access projects on VELTNEX.</p><p><a href="%s">Accept invitation</a></p><p>Sign in or register using the invited email address. This invitation expires in seven days.</p>' % escape(url, quote=True),
            'auto_delete': True,
        })
        return {'id': invitation.id, 'invite_url': url}

    def _accept(self, token):
        user = self.env.user
        if user._is_public() or not user.active:
            raise AccessError(_('Please sign in to accept this invitation.'))
        if not isinstance(token, str) or len(token) > 200:
            raise AccessError(_('Invalid invitation.'))
        invitation = self.env['saas.iam.invitation'].sudo().search([
            ('token_hash', '=', hashlib.sha256(token.encode()).hexdigest()), ('state', '=', 'pending'),
            ('expires_at', '>', fields.Datetime.now()),
        ], limit=1)
        if not invitation or (user.login or '').strip().lower() != invitation.email:
            raise AccessError(_('Sign in using the email address this invitation was sent to.'))
        self.env.cr.execute('SELECT id FROM saas_iam_invitation WHERE id=%s FOR UPDATE', [invitation.id])
        invitation.invalidate_recordset()
        if invitation.state != 'pending':
            raise AccessError(_('This invitation has already been used.'))
        projects = invitation.grant_ids.filtered('active').mapped('project_id')
        if not projects:
            raise AccessError(_('This invitation no longer grants access.'))
        invitation.grant_ids.write({'invitation_id': False, 'user_id': user.id})
        if invitation.group_id:
            invitation.group_id.write({'user_ids': [(4, user.id)]})
        invitation.write({'state': 'accepted', 'user_id': user.id})
        for project in projects:
            self._audit('iam.invitation.accept', project)
        return {'project_ids': projects.ids}

    def _revoke(self, grant_ids):
        if not isinstance(grant_ids, list) or not grant_ids or len(grant_ids) > 1000:
            raise ValidationError(_('Select role grants to remove.'))
        grants = self.env['saas.iam.grant'].sudo().browse([int(g) for g in grant_ids]).exists()
        for grant in grants:
            self._validate_assignment(grant.project_id, [grant.role], [grant.environment])
        grants.write({'active': False})
        self._invalidate_access(grants.mapped('project_id'))
        for project in grants.mapped('project_id'):
            self._audit('iam.revoke', project)

    def _invalidate_access(self, projects):
        # Stop sessions whose access has actually disappeared; other role grants may still allow it.
        targets = projects | projects.mapped('child_env_ids')
        sessions = self.env['saas.terminal.session'].sudo().search([('server_model', '=', 'saas.instance'), ('server_id', 'in', targets.ids)])
        from odoo.addons.saas_core.controllers.ssh_terminal import _close_ch
        for session in sessions:
            instance = targets.filtered(lambda i: i.id == session.server_id)
            user = self.env['res.users'].sudo().browse(session.uid)
            if not self._allowed(instance, 'terminal.open', user):
                self.env.cr.execute('SELECT pg_notify(%s, %s)', (_close_ch(session.sid), 'iam_revoked'))
                session.unlink()
        jobs = self.env['saas.job'].sudo().search([
            ('iam_project_id', 'in', projects.ids), ('state', '=', 'pending'), ('iam_actor_id', '!=', False),
        ])
        for job in jobs:
            instance = self.env['saas.instance'].sudo().browse(job.iam_instance_id)
            if not self._allowed(instance, job.iam_permission, job.iam_actor_id, job.iam_environment):
                job._iam_cancel()
