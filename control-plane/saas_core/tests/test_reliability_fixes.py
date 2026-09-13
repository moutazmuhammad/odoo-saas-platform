import socket
from unittest.mock import MagicMock, patch

import paramiko

from odoo.tests.common import TransactionCase, tagged
from odoo.addons.saas_core import utils


@tagged('post_install', '-at_install')
class TestInstanceIndexes(TransactionCase):
    """PERF-007: hot filter columns are indexed so cron/portal queries don't
    seq-scan as the table grows."""

    def test_secondary_indexes_exist(self):
        expected = {
            'saas_instance_state_idx',
            'saas_instance_docker_state_idx',
            'saas_instance_partner_idx',
            'saas_instance_plan_state_idx',
        }
        self.env.cr.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'saas_instance'")
        present = {r[0] for r in self.env.cr.fetchall()}
        missing = expected - present
        self.assertFalse(missing, "missing PERF-007 indexes: %s" % missing)


@tagged('post_install', '-at_install')
class TestBackupUrlTtl(TransactionCase):
    """SEC-008: presigned backup download links are short-lived (minutes), so
    a leaked link is not a week-long data-exfiltration window."""

    def test_presigned_ttl_is_minutes_not_days(self):
        from odoo.addons.saas_core.models import saas_instance_backup as mod
        # Must be minutes, comfortably under an hour — never back to 7 days.
        self.assertLessEqual(
            mod.PRESIGNED_URL_EXPIRY, 3600,
            "presigned backup URL TTL must stay in the minutes range (SEC-008)")
        self.assertGreater(
            mod.PRESIGNED_URL_EXPIRY, 0,
            "TTL must be positive so links can still be issued")


@tagged('post_install', '-at_install')
class TestCronBatching(TransactionCase):
    """PERF-003: the sweep crons bound their search() with a per-run limit so
    a large fleet/backlog can't be pulled into one cron transaction."""

    def test_sweep_crons_pass_batch_limit(self):
        Inst = self.env['saas.instance']
        limits = []

        def spy(self2, domain, *args, **kwargs):
            limits.append(kwargs.get('limit'))
            return self2.browse()  # empty -> each cron returns early

        with patch.object(type(Inst), 'search', spy):
            Inst._cron_retry_pending_provision()
            Inst._cron_recover_stuck_provisioning()
            Inst._cron_check_storage_limits()

        self.assertEqual(len(limits), 3, "each cron should issue one bounded search")
        self.assertTrue(
            all(lim == Inst._CRON_BATCH_SIZE for lim in limits),
            "every sweep cron must pass limit=_CRON_BATCH_SIZE; got %s" % limits)


@tagged('post_install', '-at_install')
class TestSshResilience(TransactionCase):
    """ARCH-010: SSH connect retries transient failures with backoff and a
    per-host circuit breaker, but never retries permanent (auth) failures and
    never retries command execution."""

    def setUp(self):
        super().setUp()
        utils._ssh_circuit.clear()
        self.addCleanup(utils._ssh_circuit.clear)

    def _conn(self, host):
        # private_key_b64 just needs to be valid base64 — key parsing is
        # patched out in each test.
        return utils.SSHConnection(host, 22, 'root', 'YWFhYQ==', key_type='rsa')

    def test_connect_retries_transient_then_succeeds(self):
        c = self._conn('10.9.0.1')
        fake = MagicMock()
        fake.connect.side_effect = [socket.timeout('t1'), socket.timeout('t2'), None]
        with patch.object(utils.paramiko, 'SSHClient', return_value=fake), \
                patch.object(c, '_load_private_key_bytes', return_value='pkey'), \
                patch.object(utils.time, 'sleep'):
            c._connect()
        self.assertEqual(fake.connect.call_count, 3,
                         "should retry the two transient failures then succeed")
        # A clean connect must clear any circuit state for the host.
        self.assertNotIn('10.9.0.1', utils._ssh_circuit)

    def test_connect_gives_up_and_opens_circuit(self):
        with patch.object(utils, '_SSH_CB_THRESHOLD', 1):
            c = self._conn('10.9.0.2')
            fake = MagicMock()
            fake.connect.side_effect = socket.timeout('down')
            with patch.object(utils.paramiko, 'SSHClient', return_value=fake), \
                    patch.object(c, '_load_private_key_bytes', return_value='pkey'), \
                    patch.object(utils.time, 'sleep'):
                with self.assertRaises(socket.timeout):
                    c._connect()
            self.assertEqual(fake.connect.call_count, utils._SSH_CONNECT_ATTEMPTS,
                             "should exhaust the retry budget before giving up")
            # Circuit is now open: a fresh attempt fails fast without dialing.
            c2 = self._conn('10.9.0.2')
            fake2 = MagicMock()
            with patch.object(utils.paramiko, 'SSHClient', return_value=fake2), \
                    patch.object(c2, '_load_private_key_bytes', return_value='pkey'), \
                    patch.object(utils.time, 'sleep'):
                with self.assertRaises(paramiko.SSHException):
                    c2._connect()
            fake2.connect.assert_not_called()

    def test_auth_failure_is_not_retried_and_does_not_trip_breaker(self):
        c = self._conn('10.9.0.3')
        fake = MagicMock()
        fake.connect.side_effect = paramiko.AuthenticationException('bad creds')
        with patch.object(utils.paramiko, 'SSHClient', return_value=fake), \
                patch.object(c, '_load_private_key_bytes', return_value='pkey'), \
                patch.object(utils.time, 'sleep'):
            with self.assertRaises(paramiko.AuthenticationException):
                c._connect()
        self.assertEqual(fake.connect.call_count, 1,
                         "auth failures are permanent — no retry")
        self.assertNotIn('10.9.0.3', utils._ssh_circuit,
                         "a credentials error must not quarantine the host")
