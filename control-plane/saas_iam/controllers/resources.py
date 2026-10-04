import time
from odoo import api, http
from odoo.http import request
from odoo.modules.registry import Registry
from odoo.exceptions import AccessError
from werkzeug.exceptions import Forbidden
from odoo.addons.saas_website.controllers.spa import SaasPortalSpa, spa_shell
from odoo.addons.saas_core.controllers.ssh_terminal import SshTerminalController
from odoo.addons.saas_core.controllers.container_logs import ContainerLogsController


class IamPortal(SaasPortalSpa):
    def _document_check_access(self, model_name, document_id, access_token=None):
        if model_name != 'saas.instance':
            return super()._document_check_access(model_name, document_id, access_token)
        instance = request.env['saas.instance'].sudo().browse(int(document_id)).exists()
        if not instance:
            raise AccessError('Instance not found.')
        path = request.httprequest.path
        # Legacy form routes remain live alongside the SPA. Fail closed for unclassified billing/settings actions.
        permissions = {
            'status': 'project.view', 'refresh-usage': 'project.view', 'installed-packages': 'project.view',
            'restart': 'instance.operate', 'stop': 'instance.operate', 'start': 'instance.operate',
            'update-repo': 'project.configure', 'remove-repo': 'project.configure', 'pull-repo': 'deploy',
            'download': 'backup.download', 'restore': 'db.restore', 'restore-backup': 'db.restore',
            'create-backup': 'backup.create', 'backup/create': 'backup.create',
        }
        permission = permissions.get(path.rsplit('/', 1)[-1], 'billing.manage')
        if '/databases/' in path:
            permission = {'create': 'db.create', 'duplicate': 'db.create', 'drop': 'db.delete',
                          'upgrade-module': 'db.upgrade', 'reset-admin-password': 'db.password',
                          'dismiss': 'db.view'}.get(path.rsplit('/', 1)[-1], 'database.manager')
        return request.env['saas.iam']._require(instance, permission)

    @http.route(['/my/access', '/my/access/accept', '/my/verify-profile', '/my/change-password', '/my/instances/<int:instance_id>/access'], type='http', auth='user', website=True)
    def project_access_page(self, instance_id=None, **kwargs):
        return spa_shell()


class IamTerminal(SshTerminalController):
    def _authorize_instance_shell(self, instance_id, access_token=None):
        instance = request.env['saas.instance'].sudo().browse(int(instance_id)).exists()
        try:
            request.env['saas.iam']._require(instance, 'terminal.open')
        except AccessError as exc:
            raise Forbidden(str(exc)) from exc
        if not instance.is_hosting or instance.state != 'running' or not instance.docker_server_id:
            raise Forbidden('The hosting instance must be running to open a terminal.')
        return instance

    def _get_owned_session(self, session_id):
        session = super()._get_owned_session(session_id)
        if session.server_model == 'saas.instance':
            instance = request.env['saas.instance'].sudo().browse(session.server_id).exists()
            try:
                request.env['saas.iam']._require(instance, 'terminal.open')
            except AccessError as exc:
                raise Forbidden('Terminal access was revoked.') from exc
        return session


class IamLogs(ContainerLogsController):
    @http.route()
    def stream_instance_logs(self, instance_id, tail='100', **kwargs):
        instance = request.env['saas.instance'].sudo().browse(int(instance_id)).exists()
        try:
            request.env['saas.iam']._require(instance, 'logs.view')
        except AccessError as exc:
            raise Forbidden(str(exc)) from exc
        # The core controller also checks native record rules. Run its already-authorized read as superuser.
        dbname, uid = request.env.cr.dbname, request.env.uid
        old_env = request.env
        try:
            request.update_env(su=True)
            response = super().stream_instance_logs(instance_id, tail, **kwargs)
        finally:
            request.update_env(user=old_env.uid, su=old_env.su)
        stream = response.response
        def checked_stream():
            checked_at = 0
            try:
                for chunk in stream:
                    if time.monotonic() - checked_at >= 5:
                        with Registry(dbname).cursor() as cr:
                            env = api.Environment(cr, uid, {})
                            current = env['saas.instance'].sudo().browse(instance_id).exists()
                            if not env['saas.iam']._allowed(current, 'logs.view'):
                                yield b'event: done\ndata: access revoked\n\n'
                                return
                        checked_at = time.monotonic()
                    yield chunk
            finally:
                if hasattr(stream, 'close'):
                    stream.close()
        response.response = checked_stream()
        return response
