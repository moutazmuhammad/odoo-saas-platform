"""Tenant pod self-check (C6.11), the startup probe of the Odoo web and cron
containers: a pod that cannot use its volume or its database, or whose Odoo
does not answer, never becomes ready, so a rolling update keeps the old pod
serving and a new instance is never reported delivered.

Exit 0 when every check passes. Otherwise print one line
``SELFCHECK <reason>: <detail>`` (reason: storage, database or app; the
control plane reads it from the probe event) and exit 1.

Runs on every supported Odoo image (14-20): Python 3.6+, the standard
library and psycopg2 only. Not an Odoo addon (no manifest), only shipped
with them.
"""
import configparser
import http.client
import os
import socket
import sys

CONF = '/etc/odoo/odoo.conf'
DATA_DIR = '/var/lib/odoo'
HTTP_PORT = 8069
TIMEOUT = 5


class Failed(Exception):
    def __init__(self, reason, detail):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _first_line(error):
    return (str(error).strip().splitlines() or [type(error).__name__])[0]


def check_storage(data_dir=DATA_DIR):
    """The data directory is the tenant volume (a mount, not the container's
    own filesystem) and a file there can be written, synced and read back."""
    if not os.path.ismount(data_dir):
        raise Failed('storage', '%s is not a mounted volume' % data_dir)
    path = os.path.join(data_dir, '.saas-selfcheck-%s' % socket.gethostname())
    payload = os.urandom(16)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        with open(path, 'rb') as f:
            if f.read() != payload:
                raise Failed('storage', 'a file written to %s reads back differently' % data_dir)
    except OSError as e:
        raise Failed('storage', 'cannot write to %s: %s' % (data_dir, e.strerror or e))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _db_options(conf_path):
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(conf_path) or not parser.has_section('options'):
        raise Failed('database', 'cannot read %s' % conf_path)
    opts = parser['options']
    port = opts.get('db_port') or '5432'
    return {
        'host': opts.get('db_host') or None,
        'port': int(port) if port.isdigit() else 5432,
        'user': opts.get('db_user') or None,
        'password': opts.get('db_password') or None,
        'dbname': opts.get('db_name') or 'postgres',
        'connect_timeout': TIMEOUT,
    }, bool(opts.get('db_name'))


def check_database(conf_path=CONF):
    """PostgreSQL accepts a committed write and the tenant database is
    initialized. A database the platform made read-only on purpose
    (``default_transaction_read_only``, the storage limit) only needs to
    answer: the site must stay up while it is locked."""
    import psycopg2
    params, has_db = _db_options(conf_path)
    try:
        conn = psycopg2.connect(**params)
    except psycopg2.Error as e:
        raise Failed('database', 'cannot connect: %s' % _first_line(e))
    try:
        cur = conn.cursor()
        cur.execute("SELECT pg_is_in_recovery(), current_setting('default_transaction_read_only')")
        in_recovery, read_only = cur.fetchone()
        if in_recovery:
            raise Failed('database', 'PostgreSQL is in recovery and accepts no writes')
        if read_only != 'on':
            # A transaction id makes the commit write (and flush) a WAL record.
            cur.execute('SELECT txid_current()')
            conn.commit()
        if has_db:
            cur.execute("SELECT to_regclass('ir_module_module') IS NOT NULL")
            if not cur.fetchone()[0]:
                raise Failed('database', 'database %s is not initialized' % params['dbname'])
            cur.execute("SELECT state FROM ir_module_module WHERE name = 'base'")
            row = cur.fetchone()
            if not row or row[0] != 'installed':
                raise Failed('database', "module 'base' is not installed in %s" % params['dbname'])
    except psycopg2.Error as e:
        raise Failed('database', _first_line(e))
    finally:
        conn.close()


def check_http(port=HTTP_PORT):
    """Odoo answers its login page like the HTTP probe expects (2xx/3xx)."""
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=TIMEOUT)
    try:
        conn.request('GET', '/web/login')
        status = conn.getresponse().status
    except (OSError, http.client.HTTPException) as e:
        raise Failed('app', 'Odoo is not answering on port %d: %s' % (port, _first_line(e)))
    finally:
        conn.close()
    if not 200 <= status < 400:
        raise Failed('app', '/web/login returned HTTP %d' % status)


def main(argv):
    try:
        check_storage()
        check_database()
        if '--web' in argv:
            check_http()
    except Failed as f:
        print('SELFCHECK %s: %s' % (f.reason, f.detail))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
