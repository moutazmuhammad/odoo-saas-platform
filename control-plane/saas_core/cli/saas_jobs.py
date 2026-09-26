"""``odoo-bin saas-jobs``: the dedicated durable-job worker.

Runs ``saas.job`` jobs in its own long-lived process, outside Odoo's server:
Odoo kills cron workers after ``limit_time_real_cron`` and reloads a threaded
server when any request/cron thread overruns, which orphaned deploys and
restores that legitimately take minutes. Here nothing watches the clock; a
job's own timeouts bound it.

While it runs it holds ``saas.job._WORKER_LOCK_KEY`` (shared) on a dedicated
connection, which switches requests/crons to enqueue-only; if it dies, the lock
goes with its connection and the in-process executors take over again. Several
workers may run at once (jobs are claimed with ``FOR UPDATE SKIP LOCKED``).

    odoo-bin --addons-path=<same as odoo.conf> saas-jobs -c odoo.conf -d <db>

``SAAS_JOB_THREADS`` (default 4) sets how many jobs run concurrently.
SIGTERM/SIGINT stop claiming new jobs and wait for the running ones.
"""
import logging
import os
import select
import signal
import sys
import threading

import psycopg2

import odoo
from odoo import api, SUPERUSER_ID
from odoo.cli import Command
from odoo.modules.registry import Registry
from odoo.tools import config

_logger = logging.getLogger(__name__)

# Seconds an idle thread sleeps between queue checks when no NOTIFY arrives
# (a job whose eta passed, a missed notification).
IDLE_POLL = 5


class SaasJobs(Command):
    """Run the SaaS durable job queue outside Odoo's time limits"""
    name = 'saas-jobs'

    def run(self, args):
        config.parse_config(args, setup_logging=True)
        dbname = (config['db_name'] or '').split(',')[0].strip()
        if not dbname:
            sys.exit("saas-jobs: pass the database with -d")
        threads = max(1, int(os.environ.get('SAAS_JOB_THREADS') or 4))
        run_worker(dbname, threads)


def run_worker(dbname, threads):
    Job = Registry(dbname)['saas.job']
    conn = psycopg2.connect(**odoo.sql_db.connection_info_for(dbname)[1])
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock_shared(%s)", (Job._WORKER_LOCK_KEY,))
        cur.execute("LISTEN %s" % Job._NOTIFY_CHANNEL)

    stop = threading.Event()
    wake = threading.Event()

    def _stop(signum, _frame):
        _logger.info("saas-jobs: signal %s, finishing running jobs", signum)
        stop.set()
        wake.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    workers = [threading.Thread(target=_work, args=(dbname, stop, wake),
                                name='saas-jobs-%d' % i)
               for i in range(threads)]
    for t in workers:
        t.start()
    _logger.info("saas-jobs: %d thread(s) serving database %s", threads, dbname)

    while not stop.is_set():
        if select.select([conn], [], [], IDLE_POLL) == ([], [], []):
            continue
        conn.poll()
        if conn.notifies:
            conn.notifies.clear()
            wake.set()
    for t in workers:
        t.join()
    conn.close()
    _logger.info("saas-jobs: stopped")


def _work(dbname, stop, wake):
    threading.current_thread().dbname = dbname  # Odoo's log records use it
    while not stop.is_set():
        ran = False
        try:
            registry = Registry(dbname).check_signaling()
            with registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                ran = env['saas.job']._worker_run_next()
        except Exception:
            _logger.exception("saas-jobs: worker loop error")
        if not ran:
            wake.wait(IDLE_POLL)
            wake.clear()
