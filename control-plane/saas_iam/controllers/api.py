from odoo import http, _, fields
from odoo.http import request
from odoo.exceptions import AccessError, ValidationError, UserError
from werkzeug.exceptions import Forbidden
from contextlib import nullcontext
from odoo.addons.saas_website.controllers.api import SaasApi, ok, err, _LIST_STATES
from ..roles import ENVIRONMENTS, ROLE_DEFINITIONS, permissions_for


def _response(operation, savepoint=True):
    try:
        with request.env.cr.savepoint() if savepoint else nullcontext():
            return ok(operation())
    except (AccessError, ValidationError, UserError, ValueError, TypeError) as exc:
        return err(str(exc), 'access_denied' if isinstance(exc, AccessError) else 'invalid')


class IamApi(SaasApi):
    def _serialize_user(self, partner):
        data = super()._serialize_user(partner)
        data['must_change_password'] = bool(request.env.user.sudo().iam_initial_password)
        data['must_verify_phone'] = request.env.user.sudo()._needs_phone_verification()
        data['is_managed_teammate'] = request.env['saas.iam']._is_managed_user()
        data['can_create_projects'] = not data['is_managed_teammate']
        return data

    def _otp_sent_payload(self, otp):
        # WhatsApp verification must prove possession, including on test sites.
        return {'otp_sent': True}

    def _phone_options(self):
        user = request.env.user.sudo()
        country = user.partner_id.country_id or user.iam_managed_owner_id.country_id or request.env.company.country_id
        return {
            'phone_country_id': country.id or None,
            'phone_countries': [{'id': c.id, 'name': c.name, 'phone_code': c.phone_code}
                                for c in request.env['res.country'].sudo().search(
                                    [('phone_code', '>', 0)], order='name')],
        }

    @http.route('/saas/api/v1/iam/phone/setup', type='json', auth='user')
    def iam_phone_setup(self):
        def setup():
            user = request.env['res.users']._phone_setup_user()
            return {**self._phone_options(), 'phone': user.partner_id.phone or ''}
        return _response(setup)

    @http.route('/saas/api/v1/iam/phone/send', type='json', auth='user')
    def iam_phone_send(self, phone=None, country_id=None):
        limited = self._rate_limit('iam_phone_send', 4, 600)
        return limited or _response(lambda: request.env['res.users']._send_phone_code(phone, country_id), savepoint=False)

    @http.route('/saas/api/v1/iam/phone/verify', type='json', auth='user')
    def iam_phone_verify(self, code=None):
        limited = self._rate_limit('iam_phone_verify', 6, 600)
        # Failed guesses must retain the attempt counter in this request.
        return limited or _response(lambda: request.env['res.users']._verify_phone_code(code), savepoint=False)

    @http.route('/saas/api/v1/iam/members', type='json', auth='user')
    def iam_member(self, name=None, email=None, phone=None, member_id=None, delete=False, reset_password=False, country_id=None):
        def change():
            Member = request.env['saas.iam.member']
            if delete or reset_password:
                if delete and reset_password:
                    raise ValidationError(_('Choose one action.'))
                member = Member.sudo().browse(int(member_id or 0)).exists()
                if not member:
                    raise ValidationError(_('Teammate profile not found.'))
                member = member.with_user(request.env.user)
                return member._delete_profile() if delete else member._reset_password()
            return Member._create_profile(name, email, phone, country_id=country_id)
        limited = self._rate_limit('iam_member', 30, 3600)
        return limited or _response(change)

    @http.route(['/saas/api/v1/iam/password/change', '/saas/api/v1/iam/verification/finish'], type='json', auth='user')
    def iam_password_change(self, password=None, **kw):
        def change():
            result = request.env['res.users']._change_initial_password(password)
            request.session.session_token = request.env.user._compute_session_token(request.session.sid)
            return result
        limited = self._rate_limit('iam_password_change', 10, 600)
        return limited or _response(change)

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
        domain = [('parent_id', '=', False), ('state', 'in', _LIST_STATES)]
        if not iam._is_platform_admin(request.env.user):
            domain += iam._project_domain()
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
        profiles = request.env['saas.iam.member'].sudo().search([('owner_id', 'in', customers.ids)])
        profiles_all = request.env['saas.iam.member'].sudo().with_context(active_test=False).search([('owner_id', 'in', customers.ids)])
        accepted = accepted.filtered(lambda i: not profiles_all.filtered(lambda m: m.owner_id == i.owner_id and m.user_id == i.user_id and not m.active))
        members = accepted.mapped('user_id') | profiles.mapped('user_id')
        return ok({
            'current_customer_id': request.env.user.partner_id.id,
            **self._phone_options(),
            'can_manage_groups': bool(projects.filtered(lambda p: p.partner_id == request.env.user.partner_id)),
            'empty_reason': ('no_projects' if not accessible and not iam._is_managed_user() else 'access_not_granted') if not projects else None,
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
            'groups': [{'id': g.id, 'name': g.name, 'customer_id': g.owner_id.id, 'user_ids': g.user_ids.ids,
                        'editable': g.owner_id == request.env.user.partner_id} for g in groups],
            'profiles': [{'id': m.id, 'user_id': m.user_id.id, 'name': m.name, 'email': m.user_id.login,
                          'phone': m.phone or '', 'customer_id': m.owner_id.id, 'ready': m._ready(),
                          'must_change_password': bool(m.user_id.sudo().iam_initial_password),
                          'must_verify_phone': m.user_id.sudo()._needs_phone_verification(),
                          'can_reset_password': m.owner_id == request.env.user.partner_id and m._can_manage_login(),
                          'editable': m.owner_id == request.env.user.partner_id} for m in profiles],
            'members': [{'id': u.id, 'name': u.name, 'email': u.login,
                         'customer_ids': (accepted.filtered(lambda i: i.user_id == u).mapped('owner_id') | profiles.filtered(lambda m: m.user_id == u).mapped('owner_id')).ids} for u in members],
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
                eligible = request.env['saas.iam.member']._eligible_ids(projects[0].partner_id)
                if not user or not user.active or user.id not in eligible:
                    raise AccessError(_('Create a teammate profile or invite this teammate before granting access.'))
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
            request.env['saas.iam']._require_customer_owner()
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
                        raise ValidationError(_('Select teammates.'))
                    accepted_ids = request.env['saas.iam.member']._eligible_ids(owner)
                    if any(int(uid) not in accepted_ids for uid in user_ids):
                        raise AccessError(_('Create teammate profiles before adding them to a team group.'))
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
