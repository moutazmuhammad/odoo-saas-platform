"""Unit tests for the tenant pod self-check (C6.11), run with the standard
library only: python3 -m unittest scripts/tests/test_selfcheck.py"""
import http.server
import importlib.util
import io
import os
import sys
import tempfile
import threading
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = (Path(__file__).resolve().parents[2] / 'compute' / 'operator' / 'internal'
          / 'resources' / 'platformaddons' / 'saas_selfcheck.py')


def _load():
    spec = importlib.util.spec_from_file_location('saas_selfcheck', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


selfcheck = _load()


class FakePgError(Exception):
    pass


class FakeDb:
    """psycopg2 stand-in: answers the self-check's queries from attributes."""

    def __init__(self, in_recovery=False, read_only='off', initialized=True,
                 base_state='installed', connect_error=None, write_error=None):
        self.__dict__.update(locals())
        self.queries, self.commits, self.closed = [], 0, False

    def module(self):
        db = self
        fake = types.ModuleType('psycopg2')
        fake.Error = FakePgError

        def connect(**params):
            db.params = params
            if db.connect_error:
                raise FakePgError(db.connect_error)
            return db
        fake.connect = connect
        return fake

    # connection
    def cursor(self):
        return self

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True

    # cursor
    def execute(self, sql):
        self.queries.append(sql)
        if 'pg_is_in_recovery' in sql:
            self._row = (self.in_recovery, self.read_only)
        elif 'txid_current' in sql:
            if self.write_error:
                raise FakePgError(self.write_error)
            self._row = (1,)
        elif 'to_regclass' in sql:
            self._row = (self.initialized,)
        elif 'ir_module_module' in sql:
            self._row = (self.base_state,) if self.base_state else None

    def fetchone(self):
        return self._row


class TestStorage(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: os.path.isdir(self.dir) and os.rmdir(self.dir))

    def test_mounted_writable_volume_passes_and_leaves_nothing(self):
        with mock.patch.object(selfcheck.os.path, 'ismount', return_value=True):
            selfcheck.check_storage(self.dir)
        self.assertEqual(os.listdir(self.dir), [])

    def test_not_a_mount_fails(self):
        with self.assertRaises(selfcheck.Failed) as cm:
            selfcheck.check_storage(self.dir)
        self.assertEqual(cm.exception.reason, 'storage')
        self.assertIn('not a mounted volume', cm.exception.detail)

    def test_unwritable_volume_fails(self):
        with mock.patch.object(selfcheck.os.path, 'ismount', return_value=True), \
                mock.patch.object(selfcheck.os, 'open', side_effect=OSError(30, 'Read-only file system')):
            with self.assertRaises(selfcheck.Failed) as cm:
                selfcheck.check_storage(self.dir)
        self.assertEqual(cm.exception.reason, 'storage')
        self.assertIn('Read-only file system', cm.exception.detail)

    def test_full_volume_fails(self):
        with mock.patch.object(selfcheck.os.path, 'ismount', return_value=True), \
                mock.patch.object(selfcheck.os, 'fsync', side_effect=OSError(28, 'No space left on device')):
            with self.assertRaises(selfcheck.Failed) as cm:
                selfcheck.check_storage(self.dir)
        self.assertIn('No space left on device', cm.exception.detail)
        self.assertEqual(os.listdir(self.dir), [], "the probe file is removed")


class TestDatabase(unittest.TestCase):

    def setUp(self):
        fd, self.conf = tempfile.mkstemp()
        os.write(fd, b'[options]\ndb_host = pg\ndb_port = 5432\ndb_user = u\n'
                     b'db_password = p\ndb_name = tenant\n')
        os.close(fd)
        self.addCleanup(os.unlink, self.conf)

    def _run(self, db):
        with mock.patch.dict(sys.modules, {'psycopg2': db.module()}):
            selfcheck.check_database(self.conf)

    def _fails(self, db, text):
        with self.assertRaises(selfcheck.Failed) as cm:
            self._run(db)
        self.assertEqual(cm.exception.reason, 'database')
        self.assertIn(text, cm.exception.detail)
        self.assertTrue(db.closed or db.connect_error)

    def test_healthy_database_commits_a_write(self):
        db = FakeDb()
        self._run(db)
        self.assertEqual(db.commits, 1)
        self.assertEqual(db.params['dbname'], 'tenant')
        self.assertEqual(db.params['host'], 'pg')
        self.assertTrue(db.closed)

    def test_unreachable_database_fails(self):
        self._fails(FakeDb(connect_error='could not connect to server\nmore'),
                    'cannot connect: could not connect to server')

    def test_recovery_fails(self):
        self._fails(FakeDb(in_recovery=True), 'in recovery')

    def test_refused_write_fails(self):
        self._fails(FakeDb(write_error='could not extend file: No space left on device'),
                    'No space left on device')

    def test_platform_read_only_lock_passes_without_writing(self):
        db = FakeDb(read_only='on')
        self._run(db)
        self.assertEqual(db.commits, 0)
        self.assertFalse(any('txid_current' in q for q in db.queries))

    def test_uninitialized_database_fails(self):
        self._fails(FakeDb(initialized=False), 'not initialized')

    def test_base_not_installed_fails(self):
        self._fails(FakeDb(base_state='to install'), "module 'base' is not installed")
        self._fails(FakeDb(base_state=None), "module 'base' is not installed")

    def test_unreadable_config_fails(self):
        with mock.patch.dict(sys.modules, {'psycopg2': FakeDb().module()}):
            with self.assertRaises(selfcheck.Failed) as cm:
                selfcheck.check_database('/nonexistent/odoo.conf')
        self.assertIn('cannot read', cm.exception.detail)


class _Handler(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(self.status if self.path == '/web/login' else 404)
        self.end_headers()

    def log_message(self, *args):
        pass


class TestHttp(unittest.TestCase):

    def _serve(self, status):
        handler = type('H', (_Handler,), {'status': status})
        server = http.server.HTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_login_page_answers(self):
        selfcheck.check_http(self._serve(200))
        selfcheck.check_http(self._serve(303))

    def test_server_error_fails(self):
        with self.assertRaises(selfcheck.Failed) as cm:
            selfcheck.check_http(self._serve(500))
        self.assertEqual(cm.exception.reason, 'app')
        self.assertIn('HTTP 500', cm.exception.detail)

    def test_nothing_listening_fails(self):
        port = self._serve(200)
        with mock.patch.object(selfcheck.http.client.HTTPConnection, 'request',
                               side_effect=ConnectionRefusedError(111, 'Connection refused')):
            with self.assertRaises(selfcheck.Failed) as cm:
                selfcheck.check_http(port)
        self.assertIn('not answering', cm.exception.detail)


class TestMain(unittest.TestCase):

    def test_failure_prints_one_line_and_exits_1(self):
        out = io.StringIO()
        with mock.patch.object(selfcheck, 'check_storage',
                               side_effect=selfcheck.Failed('storage', 'x is not a mounted volume')), \
                redirect_stdout(out):
            self.assertEqual(selfcheck.main(['--web']), 1)
        self.assertEqual(out.getvalue(), 'SELFCHECK storage: x is not a mounted volume\n')

    def test_web_flag_adds_the_http_check(self):
        with mock.patch.object(selfcheck, 'check_storage'), \
                mock.patch.object(selfcheck, 'check_database'), \
                mock.patch.object(selfcheck, 'check_http') as http_check:
            self.assertEqual(selfcheck.main([]), 0)
            http_check.assert_not_called()
            self.assertEqual(selfcheck.main(['--web']), 0)
            http_check.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
