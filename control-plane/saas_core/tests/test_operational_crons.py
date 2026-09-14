from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestOperationalCrons(TransactionCase):
    """B.3 group 2: the operational crons on saas.instance — daily-backup
    add-on pause/resume, webhook re-registration dispatch, the container-
    health back-compat shim, and the live-metrics/history-metrics sampling
    dispatch. The underlying per-instance mechanics (reconciliation,
    webhook signing, metric series/retention) are already covered
    elsewhere (test_reconcile.py, test_webhook_security.py,
    test_metrics.py) — these tests focus on the batch/dispatch layer that
    wasn't exercised anywhere before."""

    def setUp(self):
        super().setUp()
        self.Instance = self.env['saas.instance']
        self.partner = self.env['res.partner'].sudo().create(
            {'name': 'OpCron Co', 'customer_rank': 1})
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or \
            self.env['saas.product'].sudo().create(
                {'name': 'TEST OpCron Hosting', 'is_hosting': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'OpCron Plan', 'is_custom': True, 'workers': 2,
            'storage_limit': 10, 'cpu_limit': 1.0, 'ram_limit': '1g',
            'price': 30.0, 'yearly_price': 288.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])],
        })
        self.domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create(
                {'name': 'opcron.example.com'})
        for op in ('commit', 'rollback'):
            p = patch.object(self.env.cr, op)
            p.start()
            self.addCleanup(p.stop)

    def _inst(self, sub, **kw):
        vals = {
            'subdomain': sub, 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'billing_period': 'monthly',
            'state': 'running',
        }
        vals.update(kw)
        return self.env['saas.instance'].sudo().create(vals)

    # ---------------- _cron_renew_daily_backup_addons / _sync_daily_backup_suspension ----------------

    def test_sync_backup_suspension_noop_when_addon_disabled(self):
        inst = self._inst('bk1', daily_backup_enabled=False)
        with patch.object(type(inst), '_daily_backup_unpaid_invoices',
                           lambda self: (_ for _ in ()).throw(
                               AssertionError("must not even check invoices"))):
            inst._sync_daily_backup_suspension()
        self.assertFalse(inst.daily_backup_suspended)

    def test_sync_backup_suspension_pauses_past_grace(self):
        inst = self._inst('bk2', daily_backup_enabled=True)
        overdue = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'x', 'quantity': 1, 'price_unit': 10.0})],
        })
        overdue.invoice_date_due = fields.Date.today() - timedelta(days=4)
        with patch.object(type(inst), '_daily_backup_unpaid_invoices',
                           lambda self: overdue):
            inst._sync_daily_backup_suspension()
        self.assertTrue(inst.daily_backup_suspended)

    def test_sync_backup_suspension_not_yet_past_grace(self):
        inst = self._inst('bk3', daily_backup_enabled=True)
        overdue = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'x', 'quantity': 1, 'price_unit': 10.0})],
        })
        overdue.invoice_date_due = fields.Date.today() - timedelta(days=1)
        with patch.object(type(inst), '_daily_backup_unpaid_invoices',
                           lambda self: overdue):
            inst._sync_daily_backup_suspension()
        self.assertFalse(inst.daily_backup_suspended)

    def test_sync_backup_suspension_resumes_once_paid(self):
        inst = self._inst('bk4', daily_backup_enabled=True,
                           daily_backup_suspended=True)
        with patch.object(type(inst), '_daily_backup_unpaid_invoices',
                           lambda self: self.env['account.move']):
            inst._sync_daily_backup_suspension()
        self.assertFalse(inst.daily_backup_suspended)

    def test_cron_renew_daily_backup_dispatches_matching_instances_only(self):
        matching = self._inst('bkm', daily_backup_enabled=True)
        self._inst('bknomatch', daily_backup_enabled=False)
        trial_partner = self.env['res.partner'].sudo().create(
            {'name': 'OpCron Trial Co'})
        self._inst('bktrial', daily_backup_enabled=True, is_trial=True,
                    partner_id=trial_partner.id)
        calls = []
        with patch.object(type(matching), '_sync_daily_backup_suspension',
                           lambda self: calls.append(self.subdomain)):
            self.Instance._cron_renew_daily_backup_addons()
        self.assertEqual(calls, ['bkm'])

    def test_cron_renew_daily_backup_continues_past_failure(self):
        bad = self._inst('bkbad', daily_backup_enabled=True)
        good = self._inst('bkgood', daily_backup_enabled=True)
        calls = []

        def fake_sync(rec):
            calls.append(rec.subdomain)
            if rec.id == bad.id:
                raise ValueError('boom')

        with patch.object(type(bad), '_sync_daily_backup_suspension', fake_sync):
            self.Instance._cron_renew_daily_backup_addons()
        self.assertIn('bkbad', calls)
        self.assertIn('bkgood', calls)

    # ---------------- _cron_verify_webhooks ----------------

    def _repo(self, inst, **kw):
        vals = {
            'instance_id': inst.id, 'repo_url': 'https://x/%s.git' % inst.subdomain,
            'branch': 'main', 'state': 'cloned', 'webhook_enabled': True,
            'github_token': 'tok', 'webhook_provider_id': False,
        }
        vals.update(kw)
        return self.env['saas.instance.repo'].sudo().create(vals)

    def test_verify_webhooks_registers_only_matching_repos(self):
        inst = self._inst('wh1')
        self._repo(inst)  # cloned + enabled + token + not yet registered
        calls = []
        with patch.object(type(inst), '_ensure_webhooks_registered',
                           lambda self: calls.append(self.subdomain)):
            self.Instance._cron_verify_webhooks()
        self.assertEqual(calls, ['wh1'])

    def test_verify_webhooks_skips_already_registered(self):
        inst = self._inst('wh2')
        self._repo(inst, webhook_provider_id='hook-123')
        calls = []
        with patch.object(type(inst), '_ensure_webhooks_registered',
                           lambda self: calls.append(self.subdomain)):
            self.Instance._cron_verify_webhooks()
        self.assertEqual(calls, [])

    def test_verify_webhooks_skips_without_token(self):
        inst = self._inst('wh3')
        self._repo(inst, github_token=False)
        calls = []
        with patch.object(type(inst), '_ensure_webhooks_registered',
                           lambda self: calls.append(self.subdomain)):
            self.Instance._cron_verify_webhooks()
        self.assertEqual(calls, [])

    def test_verify_webhooks_skips_disabled_or_uncloned(self):
        inst = self._inst('wh4')
        self._repo(inst, webhook_enabled=False)
        inst2 = self._inst('wh5')
        self._repo(inst2, state='pending')
        calls = []
        with patch.object(type(self.Instance), '_ensure_webhooks_registered',
                           lambda self: calls.append(self.subdomain)):
            self.Instance._cron_verify_webhooks()
        self.assertEqual(calls, [])

    # ---------------- _cron_check_container_health (back-compat shim) ----------------

    def test_container_health_delegates_to_reconcile(self):
        calls = []
        with patch.object(type(self.Instance), '_cron_reconcile',
                           lambda self: calls.append(True) or 'ok'):
            result = self.Instance._cron_check_container_health()
        self.assertEqual(calls, [True])
        self.assertEqual(result, 'ok')

    # ---------------- _cron_sample_live_metrics / _cron_record_metrics ----------------

    def test_sample_live_metrics_groups_by_host_and_stops_when_unwatched(self):
        server_a = self.env['saas.server'].sudo().create({'name': 'metrics-host-a'})
        server_b = self.env['saas.server'].sudo().create({'name': 'metrics-host-b'})
        now = fields.Datetime.now()
        watched1 = self._inst('lm1', docker_server_id=server_a.id,
                               metrics_watch_until=now + timedelta(seconds=30))
        watched2 = self._inst('lm2', docker_server_id=server_a.id,
                               metrics_watch_until=now + timedelta(seconds=30))
        other_host = self._inst('lm3', docker_server_id=server_b.id,
                                 metrics_watch_until=now + timedelta(seconds=30))
        self._inst('lm4', docker_server_id=False,
                   metrics_watch_until=now + timedelta(seconds=30))
        calls = []

        def fake_sample(insts, server):
            calls.append((server.id, tuple(sorted(insts.ids))))
            # Clear the watch so the sampler's own "while watched" loop
            # exits after this single pass instead of looping for up to
            # LIVE_METRICS_SAMPLER_MAX_RUN (50s) real seconds.
            insts.write({'metrics_watch_until': now - timedelta(minutes=1)})

        with patch.object(type(self.Instance), '_sample_live_metrics_for_host',
                           fake_sample), \
                patch('odoo.addons.saas_core.models.saas_instance.time.sleep',
                      lambda s: None):
            self.Instance._cron_sample_live_metrics()

        by_host = dict(calls)
        self.assertEqual(set(by_host[server_a.id]),
                          {watched1.id, watched2.id})
        self.assertEqual(by_host[server_b.id], (other_host.id,))
        self.assertEqual(len(calls), 2, "one batched call per host, not per instance")

    def test_sample_live_metrics_noop_when_nobody_watching(self):
        calls = []
        with patch.object(type(self.Instance), '_sample_live_metrics_for_host',
                           lambda insts, server: calls.append(True)), \
                patch('odoo.addons.saas_core.models.saas_instance.time.sleep',
                      lambda s: None):
            self.Instance._cron_sample_live_metrics()
        self.assertEqual(calls, [])

    def test_record_metrics_groups_by_host_and_writes_one_row_per_instance(self):
        server = self.env['saas.server'].sudo().create({'name': 'rec-host'})
        inst1 = self._inst('rec1', docker_server_id=server.id)
        inst2 = self._inst('rec2', docker_server_id=server.id)
        self._inst('rec3', docker_server_id=False)  # excluded: no docker host
        calls = []
        with patch.object(type(self.Instance), '_sample_live_metrics_for_host',
                           lambda insts, srv: calls.append(tuple(sorted(insts.ids)))):
            n = self.Instance._cron_record_metrics()
        self.assertEqual(calls, [(inst1.id, inst2.id)])
        self.assertEqual(n, 2)
        rows = self.env['saas.instance.metric'].sudo().search(
            [('instance_id', 'in', [inst1.id, inst2.id])])
        self.assertEqual(len(rows), 2)

    def test_record_metrics_host_failure_does_not_abort_other_hosts(self):
        bad_server = self.env['saas.server'].sudo().create({'name': 'rec-bad'})
        good_server = self.env['saas.server'].sudo().create({'name': 'rec-good'})
        self._inst('recbad', docker_server_id=bad_server.id)
        good_inst = self._inst('recgood', docker_server_id=good_server.id)

        def fake_sample(insts, server):
            if server.id == bad_server.id:
                raise ValueError('boom')

        with patch.object(type(self.Instance), '_sample_live_metrics_for_host',
                           fake_sample):
            n = self.Instance._cron_record_metrics()
        self.assertEqual(n, 1)
        rows = self.env['saas.instance.metric'].sudo().search(
            [('instance_id', '=', good_inst.id)])
        self.assertEqual(len(rows), 1)
