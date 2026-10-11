from odoo.tests.common import tagged

from .test_portal_security import _PortalTestBase


@tagged('post_install', '-at_install')
class TestDashboardSummary(_PortalTestBase):
    """The roll-up behind the customer's home dashboard (/saas/api/v1/dashboard)."""

    def _summary(self, login, password):
        self.authenticate(login, password)
        result = self._json_call('/saas/api/v1/dashboard')
        self.assertTrue(result and result.get('ok'), result)
        return result['data']['summary']

    def test_owner_sees_health_builds_backups_and_renewal(self):
        today = self.env.cr.now().date()
        self.instance.write({'daily_backup_enabled': True})
        if 'next_invoice_date' in self.instance._fields:
            self.instance.next_invoice_date = today
        self.env['saas.build'].sudo().create({
            'instance_id': self.instance.id, 'branch': 'main',
            'commit_sha': 'abcdef1234567', 'state': 'failed'})
        backup = self.env['saas.instance.backup'].sudo().create({
            'name': 'nightly', 'instance_id': self.instance.id,
            'partner_id': self.owner.partner_id.id, 'state': 'done'})
        self.env.cr.execute(
            "UPDATE saas_instance_backup SET create_date = now() at time zone 'UTC' - interval '2 days'"
            " WHERE id = %s", [backup.id])
        backup.invalidate_recordset(['create_date'])

        summary = self._summary('portalowner@example.com', 'ownerpass123')

        env = next(e for e in summary['environments'] if e['id'] == self.instance.id)
        self.assertEqual(env['project_id'], self.instance.id)
        self.assertEqual(env['environment'], 'production')
        # No recent runtime observation in a test database.
        self.assertEqual(env['status'], 'unknown')
        self.assertEqual([(b['state'], b['branch'], b['commit']) for b in summary['builds']],
                         [('failed', 'main', 'abcdef12')])
        self.assertTrue(summary['builds'][0]['started_at'].endswith('Z'))
        [bk] = [b for b in summary['backups'] if b['env_id'] == self.instance.id]
        self.assertEqual(bk['status'], 'late')
        self.assertTrue(bk['last_backup_at'])
        if 'next_invoice_date' in self.instance._fields:
            self.assertIn({'project_id': self.instance.id, 'project_name': self.instance.name,
                           'date': str(today)}, summary['renewals'])

    def test_backups_disabled_and_recent(self):
        self.instance.write({'daily_backup_enabled': False})
        summary = self._summary('portalowner@example.com', 'ownerpass123')
        [bk] = [b for b in summary['backups'] if b['env_id'] == self.instance.id]
        self.assertEqual(bk['status'], 'none')

        self.instance.write({'daily_backup_enabled': True})
        self.env['saas.instance.backup'].sudo().create({
            'name': 'fresh', 'instance_id': self.instance.id,
            'partner_id': self.owner.partner_id.id, 'state': 'done'})
        summary = self._summary('portalowner@example.com', 'ownerpass123')
        [bk] = [b for b in summary['backups'] if b['env_id'] == self.instance.id]
        self.assertEqual(bk['status'], 'ok')

    def test_cancelled_projects_are_left_out(self):
        self.instance.state = 'cancelled'
        summary = self._summary('portalowner@example.com', 'ownerpass123')
        self.assertFalse([e for e in summary['environments'] if e['id'] == self.instance.id])
        self.assertFalse([b for b in summary['backups'] if b['env_id'] == self.instance.id])

    def test_other_customers_see_nothing_of_this_project(self):
        self.env['saas.build'].sudo().create({'instance_id': self.instance.id, 'state': 'success'})
        summary = self._summary('portalintruder@example.com', 'intruderpass123')
        for section in ('environments', 'builds', 'backups', 'renewals'):
            self.assertFalse([r for r in summary[section] if r['project_id'] == self.instance.id], section)
