import hashlib
import json
from datetime import timedelta
from unittest.mock import patch, Mock
from odoo import fields
from odoo.tests.common import TransactionCase, HttpCase, tagged
from odoo.exceptions import AccessError, ValidationError
from ..roles import ROLE_DEFINITIONS


class IamFixture:
    def setUp(self):
        super().setUp()
        self.owner = self._make_user('iamowner@example.com')
        self.team = self._make_user('iamteam@example.com')
        self.other = self._make_user('iamother@example.com')
        self.stranger = self._make_user('iamstranger@example.com')
        domain = self.env['saas.based.domain'].sudo().create({'name': 'iam.example.com'})
        product = self.env['saas.product'].sudo().create({'name': 'IAM Hosting', 'is_hosting': True})
        plan = self.env['saas.plan'].sudo().create({'name': 'IAM Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5, 'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 10})
        def project(sub, user):
            return self.env['saas.instance'].sudo().create({'subdomain': sub, 'domain_id': domain.id,
                'partner_id': user.partner_id.id, 'saas_product_id': product.id, 'plan_id': plan.id,
                'environment': 'production', 'is_hosting': True, 'state': 'running', 'region_id': False})
        self.project = project('iamone', self.owner)
        self.project2 = project('iamtwo', self.owner)
        self.foreign = project('iamforeign', self.other)
        self.stage = self.project.copy({'subdomain': 'iamstage', 'environment': 'staging', 'parent_id': self.project.id, 'state': 'running'})
        self.dev = self.project.copy({'subdomain': 'iamdev', 'environment': 'development', 'parent_id': self.project.id, 'state': 'running'})
        self.iam = self.env['saas.iam']

    def _make_user(self, login):
        return self.env['res.users'].sudo().with_context(no_reset_password=True).create({
            'name': login.split('@')[0], 'login': login, 'email': login, 'password': 'iam-test-pass',
            'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])],
        })

    def _grant(self, role, env='all', project=None, user=None, group=None):
        return self.env['saas.iam.grant'].sudo().create({
            'project_id': (project or self.project).id, 'role': role, 'environment': env,
            'user_id': False if group else (user or self.team).id, 'group_id': group.id if group else False,
        })


@tagged('post_install', '-at_install')
class TestProjectIam(IamFixture, TransactionCase):
    def test_fixed_roles_and_owner_billing_separation(self):
        self.assertEqual(len(ROLE_DEFINITIONS), 16)
        self._grant('project_admin')
        self.assertTrue(self.iam._allowed(self.stage, 'db.delete', self.team))
        self.assertFalse(self.iam._allowed(self.project, 'billing.manage', self.team))
        self.assertFalse(self.iam._allowed(self.project, 'database.manager', self.team))
        self.assertTrue(self.iam._allowed(self.project, 'billing.manage', self.owner))

    def test_scopes_combine_without_production_escalation(self):
        self._grant('logs', 'staging')
        self._grant('deploy', 'development')
        self.assertTrue(self.iam._allowed(self.stage, 'logs.view', self.team))
        self.assertFalse(self.iam._allowed(self.stage, 'deploy', self.team))
        self.assertTrue(self.iam._allowed(self.dev, 'deploy', self.team))
        self.assertFalse(self.iam._allowed(self.project, 'deploy', self.team))
        self.assertFalse(self.iam._allowed(self.project, 'project.view', self.team))
        self.assertTrue(self.iam._allowed(self.project, 'project.discover', self.team))
        self.assertFalse(self.iam._allowed(self.foreign, 'project.discover', self.team))

    def test_viewer_cannot_access_logs_sql_terminal_databases(self):
        self._grant('viewer')
        for permission in ('logs.view', 'sql.execute', 'terminal.open', 'db.view', 'db.delete', 'backup.download', 'iam.manage'):
            self.assertFalse(self.iam._allowed(self.project, permission, self.team), permission)

    def test_direct_model_actions_require_roles_even_with_sudo(self):
        self._grant('viewer')
        instance = self.project.with_user(self.team).sudo()
        for method, args in [('action_portal_stop', ()), ('hosting_sql_query', ('x', 'SELECT 1')),
                             ('hosting_db_drop_async', ('iamone_db',)), ('hosting_database_manager_url', ())]:
            with self.assertRaises(AccessError):
                getattr(instance, method)(*args)

    def test_multi_project_assignment_and_customer_boundary(self):
        self.iam.with_user(self.owner)._validate_payload([self.project.id, self.project2.id], ['viewer', 'logs'], ['staging', 'development'])
        with self.assertRaises(ValidationError):
            self.iam.with_user(self.owner)._validate_payload([self.project.id, self.foreign.id], ['viewer'], ['all'])
        with self.assertRaises(ValidationError):
            self.iam.with_user(self.owner)._validate_payload([self.stage.id], ['viewer'], ['all'])

    def test_access_admin_delegation_ceiling(self):
        self._grant('access_admin', 'staging')
        self._grant('logs', 'staging')
        delegated = self.iam.with_user(self.team)
        delegated._validate_assignment(self.project, ['viewer', 'logs'], ['staging'])
        for role, env in [('deploy', 'staging'), ('logs', 'production'), ('logs', 'all'), ('project_admin', 'staging')]:
            with self.assertRaises(AccessError):
                delegated._validate_assignment(self.project, [role], [env])

    def test_group_membership_and_revocation(self):
        group = self.env['saas.iam.group'].sudo().create({'name': 'Developers', 'owner_id': self.owner.partner_id.id, 'user_ids': [(4, self.team.id)]})
        self._grant('deploy', 'staging', group=group)
        self.assertTrue(self.iam._allowed(self.stage, 'deploy', self.team))
        group.write({'user_ids': [(5, 0, 0)]})
        self.assertFalse(self.iam._allowed(self.stage, 'deploy', self.team))

    def test_customer_transfer_removes_old_team_access(self):
        self._grant('viewer')
        self.project.partner_id = self.other.partner_id
        self.assertFalse(self.iam._allowed(self.stage, 'project.view', self.team))
        self.assertTrue(self.iam._allowed(self.stage, 'project.view', self.other))

    def test_invitation_requires_invited_login_and_is_single_use(self):
        result = self.iam.with_user(self.owner)._invite(self.team.login, [self.project.id, self.project2.id], ['viewer'], ['staging'])
        token = result['invite_url'].split('token=')[1]
        self.assertFalse(self.iam._allowed(self.stage, 'project.view', self.team))
        with self.assertRaises(AccessError):
            self.iam.with_user(self.stranger)._accept(token)
        self.iam.with_user(self.team)._accept(token)
        self.assertTrue(self.iam._allowed(self.stage, 'project.view', self.team))
        self.assertFalse(self.iam._allowed(self.project, 'project.view', self.team))
        with self.assertRaises(AccessError):
            self.iam.with_user(self.team)._accept(token)

    def test_expired_and_revoked_invitations(self):
        result = self.iam.with_user(self.owner)._invite(self.team.login, [self.project.id], ['viewer'], ['all'])
        invite = self.env['saas.iam.invitation'].sudo().browse(result['id'])
        invite.write({'expires_at': fields.Datetime.now() - timedelta(days=1)})
        with self.assertRaises(AccessError):
            self.iam.with_user(self.team)._accept(result['invite_url'].split('token=')[1])
        invite.write({'expires_at': fields.Datetime.now() + timedelta(days=1)})
        self.iam.with_user(self.owner)._revoke(invite.grant_ids.ids)
        with self.assertRaises(AccessError):
            self.iam.with_user(self.team)._accept(result['invite_url'].split('token=')[1])

    def test_terminal_and_pending_jobs_revoked(self):
        grant = self._grant('terminal', 'staging')
        self._grant('operator', 'staging')
        session = self.env['saas.terminal.session'].sudo().create({'sid': '00000000-0000-0000-0000-000000000001', 'uid': self.team.id,
            'server_model': 'saas.instance', 'server_id': self.stage.id, 'server_name': 'test', 'owner_pid': 1, 'last_activity': fields.Datetime.now()})
        self.iam.with_user(self.owner)._revoke([grant.id])
        self.assertFalse(session.exists())
        operator = self.env['saas.iam.grant'].sudo().search([('user_id', '=', self.team.id), ('role', '=', 'operator')])
        job = self.env['saas.job'].sudo().create({'model': 'saas.instance', 'res_id': self.stage.id, 'method': '_do_restart',
            'iam_actor_id': self.team.id, 'iam_project_id': self.project.id, 'iam_instance_id': self.stage.id,
            'iam_permission': 'instance.operate', 'iam_environment': 'staging'})
        self.iam.with_user(self.owner)._revoke(operator.ids)
        self.assertEqual(job.state, 'cancelled')

    def test_native_orm_and_grant_mutation_not_exposed_to_teammates(self):
        self._grant('viewer')
        with self.assertRaises(AccessError):
            self.project.with_user(self.team).read(['name'])
        with self.assertRaises(AccessError):
            self.env['saas.iam.grant'].with_user(self.team).create({'project_id': self.project.id, 'role': 'project_admin', 'user_id': self.team.id})

    def test_allowed_restart_tags_job_and_revocation_restores_state(self):
        grant = self._grant('operator', 'staging')
        with patch.object(type(self.env['saas.job']), '_spawn_worker'):
            self.stage.with_user(self.team).sudo().action_portal_restart()
        job = self.env['saas.job'].search([('res_id', '=', self.stage.id), ('method', '=', '_do_restart')], limit=1)
        self.assertEqual(job.iam_actor_id, self.team)
        self.assertEqual(job.iam_environment, 'staging')
        self.assertEqual(self.stage.state, 'provisioning')
        self.iam.with_user(self.owner)._revoke(grant.ids)
        self.assertEqual(job.state, 'cancelled')
        self.assertEqual(self.stage.state, 'running')
        self.assertFalse(self.stage.pending_operation)

    def test_worker_nested_action_retains_original_actor(self):
        self._grant('operator', 'staging')
        worker_record = self.stage.with_user(1).with_context(saas_iam_actor=self.team.id)
        with patch.object(type(self.env['saas.job']), '_spawn_worker'):
            worker_record.action_restart()
        job = self.env['saas.job'].search([('res_id', '=', self.stage.id), ('method', '=', '_do_restart')], limit=1)
        self.assertEqual(job.iam_actor_id, self.team)

    def test_project_admin_cannot_spend_customer_money(self):
        self._grant('project_admin')
        with self.assertRaises(AccessError):
            self.project.with_user(self.team).sudo().action_reserve_environment_slots('staging', 1)

    def test_status_redacts_logs_and_billing_by_role(self):
        self.project.provisioning_log = 'private deployment log'
        self._grant('viewer')
        status = self.project.with_user(self.team).sudo()._get_status_dict()
        self.assertEqual(status['provisioning_log'], '')
        self.assertNotIn('restoration_pending', status)
        self._grant('logs')
        self.assertEqual(self.project.with_user(self.team).sudo()._get_status_dict()['provisioning_log'], 'private deployment log')

    def test_environment_deleter_can_remove_stage_without_billing_access(self):
        self._grant('environment_deleter', 'staging')
        self.stage.with_user(self.team).sudo().action_delete_environment()
        self.assertEqual(self.stage.state, 'cancelled')
        self.assertEqual(self.project.state, 'running')
        with self.assertRaises(AccessError):
            self.project.with_user(self.team).sudo().action_cancel()

    def test_scoped_creator_can_use_reserved_slot_without_production_access(self):
        self._grant('environment_creator', 'staging')
        self.project.write({'staging_slots': 2, 'is_trial': False})
        with patch.object(type(self.project), '_create_env_child', return_value=self.stage), \
             patch.object(type(self.stage), '_activate_pending_environment') as activate:
            result = self.project.with_user(self.team).sudo().action_create_environment('staging')
        self.assertEqual(result['child_id'], self.stage.id)
        activate.assert_called_once()
        self.project.staging_slots = 1
        with self.assertRaises(AccessError):
            self.project.with_user(self.team).sudo().action_create_environment('staging')


@tagged('post_install', '-at_install')
class TestIamApi(IamFixture, HttpCase):
    def _rpc(self, path, params=None):
        return self.url_open(path, data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params or {}}),
                             headers={'Content-Type': 'application/json'}).json().get('result')

    def test_shared_projects_list_detail_and_billing_redaction(self):
        self._grant('viewer')
        self.authenticate(self.team.login, 'iam-test-pass')
        result = self._rpc('/saas/api/v1/instances')
        self.assertIn(self.project.id, [i['id'] for i in result['data']])
        result = self._rpc('/saas/api/v1/instances/%s' % self.project.id)
        self.assertTrue(result['ok'])
        self.assertEqual(result['data']['invoices'], [])
        self.assertNotIn('wallet', result['data'])
        self.assertEqual(result['data']['permissions'], ['build.view', 'project.view'])

    def test_viewer_cannot_stop_delete_get_manager_or_run_sql(self):
        self._grant('viewer')
        self.authenticate(self.team.login, 'iam-test-pass')
        for suffix, params in [('action', {'action': 'stop'}), ('sql', {'db': 'x', 'query': 'SELECT 1'}),
                               ('database-manager', {}), ('databases/drop', {'name': 'x'})]:
            result = self._rpc('/saas/api/v1/instances/%s/%s' % (self.project.id, suffix), params)
            self.assertFalse(result['ok'], suffix)

    def test_scoped_environment_board_masks_production(self):
        self._grant('viewer', 'staging')
        self.authenticate(self.team.login, 'iam-test-pass')
        result = self._rpc('/saas/api/v1/instances/%s/environments' % self.project.id)
        self.assertTrue(result['ok'])
        self.assertEqual(result['data']['production']['state'], 'restricted')
        self.assertEqual([e['id'] for e in result['data']['environments']], [self.stage.id])
        self.assertFalse(self._rpc('/saas/api/v1/instances/%s/metrics' % self.project.id)['ok'])

    def test_owner_invitation_acceptance_and_multi_project_grants(self):
        self.authenticate(self.owner.login, 'iam-test-pass')
        catalog = self._rpc('/saas/api/v1/iam')
        self.assertTrue(catalog['ok'])
        self.assertEqual(len(catalog['data']['roles']), 16)
        result = self._rpc('/saas/api/v1/iam/invite', {'email': self.team.login,
            'project_ids': [self.project.id, self.project2.id], 'roles': ['viewer', 'logs'], 'environments': ['staging', 'development']})
        self.assertTrue(result['ok'], result)
        self.authenticate(self.team.login, 'iam-test-pass')
        accepted = self._rpc('/saas/api/v1/iam/accept', {'token': result['data']['invite_url'].split('token=')[1]})
        self.assertTrue(accepted['ok'], accepted)
        self.assertEqual(set(accepted['data']['project_ids']), {self.project.id, self.project2.id})
        self.assertTrue(self._rpc('/saas/api/v1/instances/%s/status' % self.stage.id)['ok'])
        self.assertFalse(self._rpc('/saas/api/v1/instances/%s/status' % self.project.id)['ok'])

    def test_delegated_admin_sees_accepted_customer_teammates_and_groups(self):
        result = self.iam.with_user(self.owner)._invite(self.stranger.login, [self.project.id], ['viewer'], ['staging'])
        self.iam.with_user(self.stranger)._accept(result['invite_url'].split('token=')[1])
        self._grant('access_admin', 'staging')
        group = self.env['saas.iam.group'].sudo().create({'name': 'QA', 'owner_id': self.owner.partner_id.id})
        self.authenticate(self.team.login, 'iam-test-pass')
        result = self._rpc('/saas/api/v1/iam')
        self.assertEqual([u['id'] for u in result['data']['members']], [self.stranger.id])
        self.assertEqual([g['id'] for g in result['data']['groups']], [group.id])
        self.assertEqual(result['data']['members'][0]['customer_ids'], [self.owner.partner_id.id])
        self.assertFalse(self._rpc('/saas/api/v1/iam/groups', {'group_id': group.id, 'delete': True})['ok'])

    def test_cross_customer_grant_rejected_without_partial_writes(self):
        self.authenticate(self.owner.login, 'iam-test-pass')
        result = self._rpc('/saas/api/v1/iam/invite', {'email': self.team.login,
            'project_ids': [self.project.id, self.foreign.id], 'roles': ['viewer'], 'environments': ['all']})
        self.assertFalse(result['ok'])

    def test_terminal_and_log_routes_deny_viewer_and_share_token(self):
        self._grant('viewer')
        self.project._portal_ensure_token()
        self.authenticate(self.team.login, 'iam-test-pass')
        result = self._rpc('/saas/terminal/instance/create', {'instance_id': self.project.id, 'access_token': self.project.access_token})
        self.assertFalse(result and result.get('session_id'))
        response = self.url_open('/saas/instance/%s/logs/stream' % self.project.id)
        self.assertEqual(response.status_code, 403)

    def test_backup_download_broker_checks_current_permission(self):
        backup = self.env['saas.instance.backup'].sudo().create({'name': 'IAM backup', 'instance_id': self.stage.id,
            'state': 'done', 'download_url': 'https://storage.example.com/test.zip'})
        grant = self._grant('backup_downloader', 'staging')
        self.authenticate(self.team.login, 'iam-test-pass')
        result = self._rpc('/saas/api/v1/instances/%s/backups' % self.stage.id)
        broker = '/saas/api/v1/instances/%s/backups/%s/download' % (self.stage.id, backup.id)
        self.assertEqual(result['data']['backups'][0]['download_url'], broker)
        response = self.url_open(broker, allow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['Location'], backup.download_url)
        self.iam.with_user(self.owner)._revoke(grant.ids)
        self.assertEqual(self.url_open(broker, allow_redirects=False).status_code, 403)
