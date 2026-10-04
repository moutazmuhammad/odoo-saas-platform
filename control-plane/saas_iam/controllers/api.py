from odoo import http, _, fields
from odoo.http import request
from odoo.exceptions import AccessError, ValidationError, UserError
from werkzeug.exceptions import Forbidden
from odoo.addons.saas_website.controllers.api import SaasApi, ok, err
from ..roles import ENVIRONMENTS, ROLE_DEFINITIONS, permissions_for


def _response(operation):
    try:
        with request.env.cr.savepoint():
            return ok(operation())
    except (AccessError, ValidationError, UserError, ValueError, TypeError) as exc:
        return err(str(exc), 'access_denied' if isinstance(exc, AccessError) else 'invalid')


class IamApi(SaasApi):
    def _serialize_env_child(self, inst):
        data = super()._serialize_env_child(inst)
        iam = request.env['saas.iam']
        data['permissions'] = sorted(iam._permissions(inst))
        if not iam._allowed(inst, 'project.view'):
            data.update(domain='', url='', branch='', state='restricted', state_label=_('Restricted'),
                        version='', pending_invoice_id=False, pending_payment=False)
        elif not iam._is_owner(inst):
            data['pending_invoice_id'] = False
        return data

    def _serialize_instance(self, instance, detail=False):
        data = super()._serialize_instance(instance, detail)
        iam = request.env['saas.iam']
        data['permissions'] = sorted(iam._permissions(instance))
        data['is_project_owner'] = iam._is_owner(instance)
        data['can_manage_access'] = any(iam._allowed(instance, 'iam.manage', environment=e) for e in ENVIRONMENTS)
        if not iam._is_owner(instance):
            for key in ('wallet', 'invoices', 'payment_method', 'capacity', 'checkout_url',
                        'cancellable_invoice_id', 'has_unpaid_invoice', 'pending_plan', 'scheduled_plan',
                        'next_invoice_date', 'daily_backup_price', 'daily_backup_next_invoice_date',
                        'compute_tier_pending', 'env_server_price', 'auto_renew_subscription', 'auto_renew_daily_backup',
                        'reactivation_price', 'reactivation_checkout_url', 'restore_snapshot', 'restoration_fee', 'reactivate_url'):
                data.pop(key, None)
            data['invoices'] = []
            data['backups'] = data.get('backups', []) if iam._allowed(instance, 'backup.view') else []
            data['environments'] = [c for c in data.get('environments', []) if 'project.view' in c.get('permissions', [])]
            if not iam._allowed(instance, 'logs.view'):
                data['last_error'] = ''
            if not iam._allowed(instance, 'project.view'):
                for key in ('repo', 'compute_tier'):
                    data.pop(key, None)
                data['usage'] = {'cpu': 0, 'ram': 0, 'storage': 0}
                data['compute_tiers'] = []
                data.update(url='', domain='', branch='', state='restricted', state_label=_('Restricted'))
        return data

    def _serialize_backup(self, backup):
        data = super()._serialize_backup(backup)
        iam = request.env['saas.iam']
        # Give a permission-checked portal URL, never a reusable object-storage credential.
        data['download_url'] = ('/saas/api/v1/instances/%s/backups/%s/download' % (backup.instance_id.id, backup.id)
                                if iam._allowed(backup.instance_id, 'backup.download') and backup.state == 'done' else '')
        return data

    @http.route()
    def instance_builds(self, instance_id, access_token=None, limit=30):
        response = super().instance_builds(instance_id, access_token, limit)
        if response.get('ok'):
            instance = request.env['saas.instance'].sudo().browse(instance_id)
            if not request.env['saas.iam']._allowed(instance, 'logs.view'):
                for build in response['data']:
                    build['log'] = ''
        return response

    @http.route()
    def environments(self, instance_id, access_token=None):
        response = super().environments(instance_id, access_token)
        if response.get('ok'):
            data = response['data']
            project = request.env['saas.instance'].sudo().browse(data['project_id'])
            iam = request.env['saas.iam']
            data['environments'] = [e for e in data['environments'] if iam._allowed(request.env['saas.instance'].sudo().browse(e['id']), 'project.view')]
            data['can_create'] = {e: iam._allowed(project, 'environment.create', environment=e) for e in ('staging', 'development')}
            data['can_manage_access'] = any(iam._allowed(project, 'iam.manage', environment=e) for e in ENVIRONMENTS)
            data['is_project_owner'] = iam._is_owner(project)
            if not iam._is_owner(project):
                data.pop('env_server_price', None)
                data['production_plan'] = {}
        return response

    @http.route('/saas/api/v1/iam', type='json', auth='user')
    def iam_list(self):
        iam = request.env['saas.iam']
        domain = [('parent_id', '=', False)]
        if not iam._is_platform_admin(request.env.user):
            domain += ['|', ('partner_id', '=', request.env.user.partner_id.id),
                       ('id', 'in', iam._visible_project_ids())]
        accessible = request.env['saas.instance'].sudo().search(domain)
        projects = accessible
        projects = projects.filtered(lambda p: any(iam._allowed(p, 'iam.manage', environment=e) for e in ENVIRONMENTS))
        grants = request.env['saas.iam.grant'].sudo().search([('project_id', 'in', projects.ids), ('active', '=', True)])
        visible = grants.filtered(lambda g: any(iam._allowed(g.project_id, 'iam.manage', environment=e) for e in (ENVIRONMENTS if g.environment == 'all' else (g.environment,))))
        def editable(grant):
            try:
                iam._validate_assignment(grant.project_id, [grant.role], [grant.environment])
                return True
            except AccessError:
                return False
        customers = projects.mapped('partner_id')
        groups = request.env['saas.iam.group'].sudo().search([('owner_id', 'in', customers.ids), ('active', '=', True)])
        accepted = request.env['saas.iam.invitation'].sudo().search([
            ('owner_id', 'in', customers.ids), ('state', '=', 'accepted'), ('user_id', '!=', False),
        ])
        members = accepted.mapped('user_id')
        return ok({
            'empty_reason': ('no_projects' if not accessible else 'access_not_granted') if not projects else None,
            'roles': [{'code': code, 'name': label, 'permissions': sorted(perms)} for code, (label, perms) in ROLE_DEFINITIONS.items()],
            'projects': [{'id': p.id, 'name': p.project_name or p.subdomain or p.name,
                          'customer_id': p.partner_id.id, 'is_owner': iam._is_owner(p),
                          'assignable_roles': {e: [code for code in ROLE_DEFINITIONS if iam._allowed(p, 'iam.manage', environment=e) and permissions_for(code).issubset(iam._permissions(p, environment=e))] for e in ENVIRONMENTS}}
                         for p in projects],
            'grants': [{'id': g.id, 'project_id': g.project_id.id, 'role': g.role, 'environment': g.environment,
                        'user_id': g.user_id.id or None, 'group_id': g.group_id.id or None,
                        'name': g.user_id.name or g.group_id.name or g.invitation_id.email,
                        'email': g.user_id.login or g.invitation_id.email or '',
                        'pending': bool(g.invitation_id), 'expired': bool(g.invitation_id and g.invitation_id.expires_at < fields.Datetime.now()),
                        'editable': editable(g)} for g in visible],
            'groups': [{'id': g.id, 'name': g.name, 'customer_id': g.owner_id.id, 'user_ids': g.user_ids.ids} for g in groups],
            'members': [{'id': u.id, 'name': u.name, 'email': u.login,
                         'customer_ids': accepted.filtered(lambda i: i.user_id == u).mapped('owner_id').ids} for u in members],
        })

    @http.route('/saas/api/v1/iam/invite', type='json', auth='user')
    def iam_invite(self, email=None, project_ids=None, roles=None, environments=None, group_id=None):
        limited = self._rate_limit('iam_invite', 30, 3600)
        return limited or _response(lambda: request.env['saas.iam']._invite(email, project_ids, roles, environments, group_id))

    @http.route('/saas/api/v1/iam/accept', type='json', auth='user')
    def iam_accept(self, token=None):
        limited = self._rate_limit('iam_accept', 20, 600)
        return limited or _response(lambda: request.env['saas.iam']._accept(token))

    @http.route('/saas/api/v1/iam/grant', type='json', auth='user')
    def iam_grant(self, project_ids=None, roles=None, environments=None, user_id=None, group_id=None):
        def grant():
            iam = request.env['saas.iam']
            projects = iam._validate_payload(project_ids, roles, environments)
            if bool(user_id) == bool(group_id):
                raise ValidationError(_('Choose a user or a team group.'))
            if group_id:
                group = request.env['saas.iam.group'].sudo().browse(int(group_id)).exists()
                if not group or group.owner_id != projects[0].partner_id:
                    raise AccessError(_('Team group not found.'))
                subject = {'group_id': group.id}
            else:
                user = request.env['res.users'].sudo().browse(int(user_id)).exists()
                accepted = request.env['saas.iam.invitation'].sudo().search_count([
                    ('owner_id', '=', projects[0].partner_id.id), ('user_id', '=', user.id), ('state', '=', 'accepted'),
                ]) if user else 0
                if not user or not user.active or not accepted:
                    raise AccessError(_('Invite this teammate before granting access.'))
                subject = {'user_id': user.id}
            iam._assign(projects, roles, environments, **subject)
            return {'saved': True}
        return _response(grant)

    @http.route('/saas/api/v1/iam/revoke', type='json', auth='user')
    def iam_revoke(self, grant_ids=None):
        def revoke():
            request.env['saas.iam']._revoke(grant_ids)
            return {'removed': True}
        return _response(revoke)

    @http.route('/saas/api/v1/iam/groups', type='json', auth='user')
    def iam_group(self, name=None, group_id=None, user_ids=None, delete=False):
        def change():
            owner = request.env.user.partner_id
            Group = request.env['saas.iam.group'].sudo()
            group = Group.browse(int(group_id or 0)).exists()
            if group_id and (not group or group.owner_id != owner):
                raise AccessError(_('Team group not found.'))
            if delete:
                projects = request.env['saas.iam.grant'].sudo().search([('group_id', '=', group.id)]).mapped('project_id')
                group.write({'active': False})
                request.env['saas.iam']._invalidate_access(projects)
            else:
                values = {}
                if name is not None:
                    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
                        raise ValidationError(_('Enter a team group name.'))
                    values['name'] = name.strip()
                if user_ids is not None:
                    if not isinstance(user_ids, list):
                        raise ValidationError(_('Select accepted teammates.'))
                    accepted_ids = request.env['saas.iam.invitation'].sudo().search([
                        ('owner_id', '=', owner.id), ('state', '=', 'accepted'), ('user_id', '!=', False),
                    ]).mapped('user_id').ids
                    if any(int(uid) not in accepted_ids for uid in user_ids):
                        raise AccessError(_('Invite teammates before adding them to a team group.'))
                    values['user_ids'] = [(6, 0, user_ids)]
                if group:
                    group.write(values)
                    projects = request.env['saas.iam.grant'].sudo().search([('group_id', '=', group.id)]).mapped('project_id')
                    request.env['saas.iam']._invalidate_access(projects)
                else:
                    group = Group.create({'owner_id': owner.id, **values})
            request.env['saas.iam']._audit('iam.group.change', detail=group.name)
            return {'id': group.id}
        return _response(change)

    @http.route('/saas/api/v1/instances/<int:instance_id>/backups/<int:backup_id>/download', type='http', auth='user', methods=['GET'])
    def iam_backup_download(self, instance_id, backup_id):
        instance = request.env['saas.instance'].sudo().browse(instance_id).exists()
        request.env['saas.iam']._require(instance, 'backup.download')
        backup = instance.backup_ids.filtered(lambda b: b.id == backup_id and b.state == 'done')
        if not backup:
            raise Forbidden(_('Backup not available.'))
        backup._refresh_download_url()
        if not backup.download_url:
            raise Forbidden(_('Backup download is not ready.'))
        request.env['saas.iam']._audit('iam.backup.download', instance)
        return request.redirect(backup.download_url, local=False)
