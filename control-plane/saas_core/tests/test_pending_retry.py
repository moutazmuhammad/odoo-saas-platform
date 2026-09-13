from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPendingRetry(TransactionCase):
    """PROV-004: queued (pending_provision) deploys retry immediately when
    capacity/health changes, instead of waiting out their back-off."""

    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or \
            self.env['saas.product'].sudo().create(
                {'name': 'PR Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'PR Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.domain = self.env['saas.based.domain'].sudo().search([], limit=1) or \
            self.env['saas.based.domain'].sudo().create({'name': 'pr.example.com'})
        self.partner = self.env['res.partner'].sudo().create({'name': 'PR Cust'})

    def _pending(self, sub, attempts=0):
        return self.env['saas.instance'].sudo().create({
            'subdomain': sub, 'domain_id': self.domain.id, 'partner_id': self.partner.id,
            'saas_product_id': self.product.id, 'plan_id': self.plan.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'is_hosting': True, 'state': 'pending_provision',
            'pending_provision_since': fields.Datetime.now(),
            'pending_provision_attempts': attempts})

    def test_flag_helper_targets_only_pending(self):
        pend = self._pending('prpend1')
        running = self.env['saas.instance'].sudo().create({
            'subdomain': 'prrun', 'domain_id': self.domain.id, 'partner_id': self.partner.id,
            'saas_product_id': self.product.id, 'plan_id': self.plan.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'is_hosting': True, 'state': 'running'})
        n = self.env['saas.instance']._saas_flag_pending_for_retry()
        self.assertGreaterEqual(n, 1)
        self.assertTrue(pend.pending_retry_now)
        self.assertFalse(running.pending_retry_now)

    def test_new_docker_host_flags_pending(self):
        pend = self._pending('prpend2')
        self.assertFalse(pend.pending_retry_now)
        self.env['saas.server'].sudo().create({
            'name': 'pr-new-host', 'ip_v4': '10.2.0.1', 'is_docker_host': True})
        self.assertTrue(pend.pending_retry_now,
                        "adding a docker host must flag queued deploys")

    def test_health_recovery_flags_pending(self):
        server = self.env['saas.server'].sudo().create({
            'name': 'pr-recover', 'ip_v4': '10.2.0.2', 'is_docker_host': True})
        server.health_state = 'unreachable'
        pend = self._pending('prpend3')
        self.assertFalse(pend.pending_retry_now)
        server._update_health(True)  # -> ok transition
        self.assertTrue(pend.pending_retry_now,
                        "a host recovering to healthy must flag queued deploys")

    def test_cron_bypasses_backoff_when_flagged(self):
        # Big back-off (attempts=10) + fresh write_date => normally skipped.
        flagged = self._pending('prflag', attempts=10)
        flagged.pending_retry_now = True
        control = self._pending('prctl', attempts=10)  # not flagged
        Inst = self.env['saas.instance']
        # No docker host exists, so action_deploy just re-queues (no real deploy);
        # the cron commits per item, which the test cursor forbids -> neutralise.
        with patch.object(self.env.cr, 'commit'), \
                patch.object(self.env.cr, 'rollback'):
            Inst._cron_retry_pending_provision()
        self.assertFalse(flagged.pending_retry_now, "flag must be consumed")
        self.assertEqual(flagged.pending_provision_attempts, 11,
                         "flagged instance must be retried despite back-off")
        self.assertEqual(control.pending_provision_attempts, 10,
                         "un-flagged in-back-off instance must be skipped")
