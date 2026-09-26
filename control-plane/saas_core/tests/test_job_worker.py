from unittest.mock import patch

import psycopg2

import odoo
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestDedicatedJobWorker(TransactionCase):
    """The dedicated ``saas-jobs`` worker: while one holds the worker lock,
    requests/crons only enqueue; otherwise the in-process executors run jobs
    as before."""

    def setUp(self):
        super().setUp()
        self.Job = self.env['saas.job']
        self.partner = self.env['res.partner'].sudo().create({'name': 'Worker Target'})
        p = patch.object(type(self.partner), '_job_ok',
                         lambda rec, *a: {'ran': True}, create=True)
        p.start()
        self.addCleanup(p.stop)
        for op in ('commit', 'rollback'):
            p = patch.object(self.env.cr, op)
            p.start()
            self.addCleanup(p.stop)

    def _hold_worker_lock(self):
        """Simulate a live worker process: another connection holding the
        lock shared, exactly as saas_jobs.run_worker does."""
        conn = psycopg2.connect(
            **odoo.sql_db.connection_info_for(self.env.cr.dbname)[1])
        conn.autocommit = True
        conn.cursor().execute(
            "SELECT pg_advisory_lock_shared(%s)", (self.Job._WORKER_LOCK_KEY,))
        self.addCleanup(conn.close)

    def test_no_worker_by_default(self):
        self.assertFalse(self.Job._dedicated_worker_alive())
        # The probe must not leave the lock held.
        self.assertFalse(self.Job._dedicated_worker_alive())

    def test_worker_detected_while_it_holds_the_lock(self):
        self._hold_worker_lock()
        self.assertTrue(self.Job._dedicated_worker_alive())

    def test_with_worker_enqueue_spawns_no_thread(self):
        self._hold_worker_lock()
        with patch('odoo.addons.saas_core.models.saas_job.threading.Thread') as m_thread:
            job = self.Job.enqueue(self.partner, '_job_ok')
        m_thread.assert_not_called()
        self.assertEqual(job.state, 'pending')

    def test_with_worker_cron_leaves_jobs_to_it(self):
        self._hold_worker_lock()
        with patch.object(type(self.Job), '_spawn_worker', lambda s: None):
            job = self.Job.enqueue(self.partner, '_job_ok')
        self.Job._cron_run_jobs()
        self.assertEqual(job.state, 'pending')

    def test_without_worker_cron_still_runs_jobs(self):
        with patch.object(type(self.Job), '_spawn_worker', lambda s: None):
            job = self.Job.enqueue(self.partner, '_job_ok')
        self.Job._cron_run_jobs()
        self.assertEqual(job.state, 'done')

    def test_worker_step_runs_one_due_job(self):
        with patch.object(type(self.Job), '_spawn_worker', lambda s: None):
            job = self.Job.enqueue(self.partner, '_job_ok', priority=0)
        self.assertTrue(self.Job._worker_run_next())
        self.assertEqual(job.state, 'done')

    def test_worker_command_is_registered(self):
        from odoo.cli.command import commands
        self.assertIn('saas-jobs', commands)

    def test_outcome_written_only_after_heartbeat_stopped(self):
        """A heartbeat committed during the job must not be able to race the
        final status UPDATE of the job row (serialization failure)."""
        beat = {'stopped': False}
        seen = []

        def fake_loop(job_self, job_id, dbname, stop):
            stop.wait()
            beat['stopped'] = True

        Job = type(self.Job)
        real_write = Job.write

        def spy_write(recs, vals):
            if vals.get('state') == 'done':
                seen.append(beat['stopped'])
            return real_write(recs, vals)

        with patch.object(Job, '_spawn_worker', lambda s: None):
            job = self.Job.enqueue(self.partner, '_job_ok', priority=0)
        with patch.object(Job, '_heartbeat_loop', fake_loop), \
                patch.object(Job, 'write', spy_write):
            self.Job._worker_run_next()
        self.assertEqual(job.state, 'done')
        self.assertEqual(seen, [True])
