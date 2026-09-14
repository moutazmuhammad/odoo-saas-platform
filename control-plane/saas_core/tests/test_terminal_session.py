from psycopg2 import IntegrityError

from odoo import fields
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger


@tagged('post_install', '-at_install')
class TestSaasTerminalSession(TransactionCase):
    """saas.terminal.session is pure runtime metadata (no methods): which
    worker process owns an in-flight SSH terminal session, so other workers
    can forward keystrokes via PostgreSQL LISTEN/NOTIFY instead of losing the
    RPC (see controllers/ssh_terminal.py). Coverage here is necessarily thin
    — there is no business logic beyond the fields and the sid uniqueness
    constraint — but it's the regression baseline the plan wants in place
    before the SSH transport is migrated to Kubernetes pods/exec (Phase
    D.4.3)."""

    def _create(self, **overrides):
        vals = {
            'sid': 'sess-1', 'uid': 2, 'server_model': 'saas.instance',
            'server_id': 1, 'server_name': 'demo-inst', 'owner_pid': 12345,
            'last_activity': fields.Datetime.now(),
        }
        vals.update(overrides)
        return self.env['saas.terminal.session'].sudo().create(vals)

    def test_create_with_required_fields(self):
        rec = self._create()
        self.assertEqual(rec.sid, 'sess-1')
        self.assertEqual(rec.owner_pid, 12345)
        self.assertFalse(rec.closed, "closed must default to False")

    def test_sid_must_be_unique(self):
        self._create(sid='dup-sid')
        with mute_logger('odoo.sql_db'):
            with self.assertRaises(IntegrityError):
                with self.env.cr.savepoint():
                    self._create(sid='dup-sid', server_id=2)

    def test_different_sids_coexist(self):
        a = self._create(sid='sid-a')
        b = self._create(sid='sid-b')
        self.assertNotEqual(a.id, b.id)

    def test_closed_can_be_set_true(self):
        rec = self._create(closed=True)
        self.assertTrue(rec.closed)
