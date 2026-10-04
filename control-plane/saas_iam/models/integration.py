"""Defense in depth for model actions and durable user-requested jobs."""
import json
import logging
from odoo import api, fields, models, SUPERUSER_ID
from odoo.exceptions import AccessError

# Public methods can also be called through Odoo RPC. Check before any sudo or side effect.
INSTANCE_METHOD_PERMISSIONS = {
    'action_portal_start': 'instance.operate', 'action_portal_stop': 'instance.operate',
    'action_portal_restart': 'instance.operate', 'action_start': 'instance.operate',
    'action_stop': 'instance.operate', 'action_restart': 'instance.operate',
    'action_redeploy': 'deploy', 'action_deploy': 'deploy', 'action_build_and_deploy': 'deploy', 'action_rollback': 'deploy', 'action_refresh_usage': 'project.view',
    'action_delete_instance': 'project.delete', 'action_cancel': 'billing.manage',
    'hosting_db_list': 'db.view', 'hosting_sql_query': 'sql.execute',
    'hosting_database_manager_url': 'database.manager',
    'hosting_db_create': 'db.create', 'hosting_db_create_async': 'db.create',
    'hosting_db_duplicate': 'db.create', 'hosting_db_duplicate_async': 'db.create',
    'hosting_db_drop': 'db.delete', 'hosting_db_drop_async': 'db.delete',
    'hosting_db_backup': 'backup.create', 'action_create_backup': 'backup.create',
    'action_restore_backup': 'db.restore', 'action_restore_full_instance': 'db.restore',
    'hosting_db_restore_prepare_upload': 'db.restore', 'hosting_db_restore_from_upload': 'db.restore',
    'hosting_db_reset_admin_password': 'db.password',
    'hosting_db_upgrade_module': 'db.upgrade', 'hosting_db_upgrade_module_async': 'db.upgrade',
    'hosting_db_upgrade_modules': 'db.upgrade', 'hosting_db_upgrade_modules_async': 'db.upgrade',
    'action_delete_environment': 'environment.delete',
    'action_open_terminal': 'terminal.open', 'action_view_logs': 'logs.view',
    'action_suspend': 'billing.manage', 'action_draft': 'billing.manage',
    'action_client_cancel_invoice': 'billing.manage', 'action_purchase_daily_backup': 'billing.manage',
    'action_change_compute_tier': 'billing.manage', 'action_purchase_storage_block': 'billing.manage',
    'action_release_storage_block': 'billing.manage', 'action_reserve_environment_slots': 'billing.manage',
    'action_release_environment_slots': 'billing.manage', 'action_confirm_and_bill': 'billing.manage',
    'action_subscribe_from_trial': 'billing.manage', 'action_request_plan_change': 'billing.manage',
    'action_cancel_scheduled_downgrade': 'billing.manage', 'action_reactivate': 'billing.manage',
}


def _guard_method(name, permission):
    def guarded(self, *args, **kwargs):
        iam = self.env['saas.iam']
        required = 'environment.delete' if name == 'action_cancel' and self and all(i.environment != 'production' for i in self) else permission
        for instance in self:
            iam._require(instance, required)
            # Copying a database also exports its data, so creator access alone is insufficient.
            if 'duplicate' in name:
                iam._require(instance, 'backup.download')
        actor = self.env.context.get('saas_iam_actor') if self.env.uid == SUPERUSER_ID else self.env.uid
        context = dict(self.env.context, saas_iam_actor=actor or self.env.uid, saas_iam_permission=required,
                       saas_iam_instance=self[:1].id, saas_iam_environment=self[:1].environment)
        checked = self.with_context(context)
        return getattr(super(IamInstance, checked), name)(*args, **kwargs)
    guarded.__name__ = name
    return guarded


class IamInstance(models.Model):
    _inherit = 'saas.instance'

    for _method, _permission in INSTANCE_METHOD_PERMISSIONS.items():
        locals()[_method] = _guard_method(_method, _permission)
    del _method, _permission

    @api.model_create_multi
    def create(self, vals_list):
        iam = self.env['saas.iam']
        if iam._is_managed_user():
            for values in vals_list:
                parent = self.sudo().browse(values.get('parent_id', 0)).exists()
                environment = values.get('environment', 'production')
                if not parent or parent.parent_id or environment not in ('staging', 'development'):
                    iam._require_customer_owner()
                iam._require(parent, 'environment.create', environment=environment)
                if values.get('partner_id') != parent.partner_id.id:
                    raise AccessError('An environment must belong to its project customer.')
        return super().create(vals_list)

    def write(self, values):
        transferred = self.filtered(lambda i: not i.parent_id and i.partner_id.id != values['partner_id']) if 'partner_id' in values else self.browse()
        result = super().write(values)
        if transferred:
            self.env['saas.iam.grant'].sudo().search([('project_id', 'in', transferred.ids), ('active', '=', True)]).write({'active': False})
            self.env['saas.iam']._invalidate_access(transferred.sudo())
        return result

    def _get_status_dict(self):
        data = super()._get_status_dict()
        iam = self.env['saas.iam']
        if not iam._is_owner(self):
            data = {key: value for key, value in data.items() if key in {
                'id', 'state', 'state_label', 'url', 'backup_running', 'db_ops_running', 'provisioning_log',
            }}
        if not iam._allowed(self, 'logs.view'):
            data['provisioning_log'] = ''
        return data

    def action_create_environment(self, env_type, name=None, branch=None):
        self.ensure_one()
        iam = self.env['saas.iam']
        iam._require(self, 'environment.create', environment=env_type)
        # Buying an additional slot spends customer money. A team creator uses reserved capacity.
        if not iam._is_owner(self) and self._env_used_for(env_type) >= self._env_slots_for(env_type):
            raise AccessError('Ask the project owner to reserve another environment slot.')
        return super(IamInstance, self.with_context(
            saas_iam_actor=self.env.uid, saas_iam_permission='environment.create',
            saas_iam_instance=self.id, saas_iam_environment=env_type)).action_create_environment(env_type, name, branch)

    def action_merge_environment(self, source_id):
        self.ensure_one()
        source = self.env['saas.instance'].sudo().browse(source_id).exists()
        iam = self.env['saas.iam']
        iam._require(self, 'deploy')
        iam._require(source, 'project.view')
        if iam._project(source) != iam._project(self):
            raise AccessError('Environments must belong to the same project.')
        return super(IamInstance, self.with_context(
            saas_iam_actor=self.env.uid, saas_iam_permission='deploy',
            saas_iam_instance=self.id, saas_iam_environment=self.environment)).action_merge_environment(source_id)

    def hosting_db_copy_from(self, source, db_names, overwrite_names=None):
        iam = self.env['saas.iam']
        iam._require(self, 'db.restore')
        iam._require(source, 'backup.download')
        if iam._project(source) != iam._project(self):
            raise AccessError('Databases must be copied within the same project.')
        return super(IamInstance, self.with_context(
            saas_iam_actor=self.env.uid, saas_iam_permission='db.restore',
            saas_iam_instance=self.id, saas_iam_environment=self.environment)).hosting_db_copy_from(source, db_names, overwrite_names)


class IamJob(models.Model):
    _inherit = 'saas.job'
    iam_actor_id = fields.Many2one('res.users', ondelete='set null', index=True)
    iam_project_id = fields.Many2one('saas.instance', ondelete='set null', index=True)
    iam_instance_id = fields.Integer()
    iam_permission = fields.Char()
    iam_environment = fields.Char()

    @api.model
    def enqueue(self, record, method, args=(), **kwargs):
        job = super().enqueue(record, method, args, **kwargs)
        permission = record.env.context.get('saas_iam_permission')
        actor = record.env.context.get('saas_iam_actor')
        if permission and actor and (self.env.uid == SUPERUSER_ID or actor == self.env.uid):
            instance = (record if record._name == 'saas.instance' else self.env['saas.instance'].sudo().browse(record.env.context.get('saas_iam_instance'))).exists()
            if instance:
                job.sudo().write({
                    'iam_actor_id': actor, 'iam_project_id': self.env['saas.iam']._project(instance).id,
                    'iam_instance_id': instance.id, 'iam_permission': permission,
                    'iam_environment': record.env.context.get('saas_iam_environment') or instance.environment,
                })
        return job

    def _execute(self):
        self.ensure_one()
        if self.iam_permission:
            instance = self.env['saas.instance'].sudo().browse(self.iam_instance_id).exists()
            actor = self.iam_actor_id
            if not actor or not self.env['saas.iam']._allowed(instance, self.iam_permission, actor, self.iam_environment):
                self._iam_cancel()
                self.env.cr.commit()
                return
        if self.iam_permission:
            checked = self.with_context(saas_iam_actor=self.iam_actor_id.id, saas_iam_permission=self.iam_permission,
                                        saas_iam_instance=self.iam_instance_id, saas_iam_environment=self.iam_environment)
            return super(IamJob, checked)._execute()
        return super()._execute()

    def _iam_cancel(self):
        message = 'Project access was revoked before this job started.'
        self.write({'state': 'cancelled', 'finished_at': fields.Datetime.now(), 'error': message})
        if self.on_error and self.res_id:
            try:
                with self.env.cr.savepoint():
                    record = self.env[self.model].with_user(SUPERUSER_ID).browse(self.res_id).exists()
                    if record:
                        getattr(record, self.on_error)(AccessError(message), *json.loads(self.on_error_args_json or '[]'))
            except Exception:
                logging.getLogger(__name__).exception('IAM job cancellation recovery failed for job %s', self.id)
