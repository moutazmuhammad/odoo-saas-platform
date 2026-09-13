import logging

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)

# Default friendly text shown to customers when a database operation
# fails — chosen so it works for any of create/duplicate/drop/upgrade.
# The raw exception is always logged for operators via _logger.
_GENERIC_OP_FAILURE = (
    "Something didn't work on our side while running this database "
    "operation. Please dismiss this message and try again — if it keeps "
    "happening, contact support."
)


def _sanitize_op_error(exc, operation):
    """Translate a raw exception into a short customer-friendly sentence.

    We never echo SSH timeouts, psql errors, container names, paths or
    XML-RPC fault strings directly to the customer — those are useful
    only to the operator, who gets the full traceback through the
    standard ``_logger.exception`` path.

    The operation kind (``create`` / ``duplicate`` / ``drop`` /
    ``upgrade``) is used to phrase the message; nothing else from the
    exception is surfaced.
    """
    label = {
        'create': 'create the database',
        'duplicate': 'duplicate the database',
        'drop': 'delete the database',
        'upgrade': 'upgrade the module',
        'restore': 'restore the database',
    }.get(operation, 'run that operation')
    msg = str(exc) if exc else ''
    low = msg.lower()
    # A small handful of patterns mapped to actionable copy. Everything
    # else falls back to the generic message — better one safe sentence
    # than leaking internals to chase precision.
    if 'timed out' in low or 'timeout' in low:
        return (
            "Your instance didn't respond in time while we tried to "
            "%s. Please try again in a moment." % label
        )
    if 'already exists' in low and operation in ('create', 'duplicate'):
        return (
            "A database with that name already exists. Please pick a "
            "different name."
        )
    if 'permission denied' in low or 'access denied' in low:
        return (
            "We didn't have permission to %s. Please contact support."
            % label
        )
    if 'no space' in low or 'disk full' in low:
        return (
            "Your instance is out of storage. Please upgrade your "
            "plan or delete unused databases first."
        )
    return _GENERIC_OP_FAILURE


class SaasInstanceDbOperation(models.Model):
    """Tracking record for an asynchronous database operation.

    Customer-initiated create / duplicate / drop on a hosting instance
    can each take 30-90 seconds (Odoo CLI init, pg_dump-style copy,
    drop-and-cleanup respectively). Doing them in-line during the HTTP
    request hits nginx's ``proxy_read_timeout`` and starves the saas
    master's worker pool — both observed in production as 502 Bad
    Gateway from the saas master and orphaned half-initialized
    databases on the customer's instance.

    This model is the queue/tracker. The portal creates one of these,
    spawns the real work in a background thread, and returns the HTTP
    response immediately. The background flips ``state`` to ``done`` or
    ``failed`` (with ``error_message``) when finished. The Databases
    page renders any pending or recently-failed ops so the customer
    sees what happened.

    Zombie reaper: any 'running' row older than ``_ZOMBIE_TIMEOUT_MIN``
    minutes is auto-failed by ``_cron_reap_stuck_db_operations`` — this
    is the safety net for cases where the background thread crashed
    silently (saas master restart mid-thread, SSH socket dropped, the
    Odoo registry got reloaded), so the customer never sees an eternal
    "Creating database..." spinner.
    """
    _name = 'saas.instance.db.operation'
    _description = 'SaaS Instance Database Operation'
    _order = 'create_date desc'

    # Any op older than this and still in 'running' is reaped. Tuned to
    # be comfortably longer than the slowest legitimate op (large DB
    # duplicate ~5–10 min, module upgrade on a big DB up to ~15 min).
    # This is the backstop for rows that never got a heartbeat.
    _ZOMBIE_TIMEOUT_MIN = 30

    # A running op whose background worker is alive stamps ``last_heartbeat``
    # every ~30s (see utils.run_in_background). If the heartbeat goes stale by
    # this many minutes the worker is presumed dead and the op is failed fast —
    # well under the 30-min net, so the customer isn't stuck on a spinner
    # (PROV-001). Comfortably larger than the beat interval to absorb a few
    # missed beats / DB hiccups.
    _HEARTBEAT_STALE_MIN = 3

    instance_id = fields.Many2one(
        'saas.instance', required=True, ondelete='cascade', index=True,
    )
    db_name = fields.Char(
        required=True, index=True,
        help='Full (prefixed) database name the operation targets.',
    )
    operation = fields.Selection(
        [
            ('create', 'Create'),
            ('duplicate', 'Duplicate'),
            ('drop', 'Drop'),
            ('upgrade', 'Upgrade Module'),
            ('restore', 'Restore'),
        ],
        required=True,
    )
    # For ``duplicate`` ops, the source DB name. ``False`` for create / drop.
    source_db = fields.Char()
    # For ``upgrade`` ops, the module(s) to upgrade. The recovery path
    # (``_run_upgrade``) stores a single name; the no-downtime path
    # (``_run_upgrade_live``) stores a space-separated list (or
    # ``all``). Each name is pre-validated against ``_UPGRADE_MODULE_RE``
    # so an arbitrary string can't smuggle shell/CLI tokens downstream.
    module_name = fields.Char()
    state = fields.Selection(
        [('running', 'Running'), ('done', 'Done'), ('failed', 'Failed')],
        default='running', required=True, index=True,
    )
    # Stamped at creation and then every ~30s by the background worker's
    # watchdog while it runs; a stale value means the worker died (PROV-001).
    last_heartbeat = fields.Datetime(default=fields.Datetime.now, index=True)
    error_message = fields.Text(readonly=True)
    output_log = fields.Text(
        readonly=True,
        help='Captured stdout/stderr of the underlying CLI invocation '
             '(currently used for ``upgrade`` ops so the customer sees '
             'why the upgrade failed — or that it succeeded — without '
             'having to dig in the docker host).',
    )

    def _mark_failed(self, exc, output=None):
        """Persist a customer-safe failure message + log the raw exception.

        Centralised so every ``_run_*`` worker handles failure the same
        way: the raw exception goes to the operator log, the customer
        sees a friendly sentence on the portal.
        """
        self.ensure_one()
        _logger.exception(
            "DB operation %s for instance %s (db=%s) failed: %s",
            self.operation, self.instance_id.id, self.db_name, exc,
        )
        vals = {
            'state': 'failed',
            'error_message': _sanitize_op_error(exc, self.operation),
        }
        if output is not None:
            vals['output_log'] = output or ''
        try:
            self.write(vals)
            self.env.cr.commit()
        except Exception:
            _logger.exception(
                "Failed to record failure on db.operation %s — the "
                "zombie reaper will clean it up.", self.id,
            )

    def _run_create(self, login, password, lang, country_code):
        """Background worker: run the sync create, update state.

        Receives the secrets (login/password) via ``method_args`` so
        they never get persisted on this tracking record. The cursor
        is committed before re-raising so a failure leaves a clean
        ``failed`` row instead of a stuck ``running`` one when the
        thread's transaction rolls back.
        """
        self.ensure_one()
        try:
            self.instance_id.hosting_db_create(
                name=self.db_name,
                login=login,
                password=password,
                lang=lang,
                country_code=country_code,
            )
            # A freshly-created DB can't have meaningful prior backups —
            # any matching backup record is from a previous incarnation
            # of the same name (e.g. drop happened before the cleanup in
            # ``_run_drop`` was deployed, or the customer dropped the DB
            # directly outside the portal). Reap so a stale "Backup
            # ready" badge doesn't follow the new database around.
            self._reap_stale_backups()
            self.write({'state': 'done'})
            try:
                self.env.cr.commit()
            except Exception:
                pass
        except Exception as e:
            self._mark_failed(e)
            raise

    def _reap_stale_backups(self):
        """Unlink ephemeral / per-DB backup records pointing at ``self.db_name``.

        The :meth:`saas.instance.backup.unlink` override drops the
        bucket object before removing the row, so this cleans both
        sides. ``is_full_instance`` restic snapshots are skipped —
        they span every DB on the instance and can't be selectively
        carved up.
        """
        self.ensure_one()
        related = self.env['saas.instance.backup'].sudo().search([
            ('instance_id', '=', self.instance_id.id),
            ('db_name', '=', self.db_name),
            ('is_full_instance', '=', False),
        ])
        if not related:
            return
        try:
            related.unlink()
        except Exception:
            _logger.exception(
                "Failed to unlink backups for DB '%s' on instance %s "
                "during op %s",
                self.db_name, self.instance_id.id, self.operation,
            )

    def _run_duplicate(self):
        self.ensure_one()
        try:
            self.instance_id.hosting_db_duplicate(
                source=self.source_db,
                new_name=self.db_name,
            )
            # Same rationale as ``_run_create`` — the target name just
            # came into existence, so any pre-existing backup for it is
            # an orphan from a previous incarnation.
            self._reap_stale_backups()
            self.write({'state': 'done'})
            try:
                self.env.cr.commit()
            except Exception:
                pass
        except Exception as e:
            self._mark_failed(e)
            raise

    def _run_upgrade(self):
        """Background worker: ``odoo -u <module> -d <db>`` on the container.

        Recovery tool for when the customer's live Odoo is broken
        (500 Internal Server Error on every page). XML-RPC into the
        live worker won't work in that state, so we bypass it: stop
        the container, run ``docker compose run --rm odoo odoo -u``
        with ``--stop-after-init`` (one-shot, no HTTP), then start the
        container back up. The full stdout/stderr is captured on the
        op record so the portal can show it.
        """
        self.ensure_one()
        try:
            output = self.instance_id.hosting_db_upgrade_module(
                name=self.db_name,
                module=self.module_name or '',
            )
            self.write({'state': 'done', 'output_log': output or ''})
            try:
                self.env.cr.commit()
            except Exception:
                pass
        except Exception as e:
            # Pull the output off the exception when our helper raised
            # a ``UserError`` carrying the captured CLI output (so the
            # portal can render it). Plain exceptions just go to
            # error_message.
            output = getattr(e, '_saas_upgrade_output', None) or ''
            self._mark_failed(e, output=output)
            raise

    def _run_upgrade_live(self):
        """Background worker: no-downtime module upgrade on the live container.

        Calls :meth:`saas.instance.hosting_db_upgrade_modules`, which
        runs Odoo's ``button_immediate_upgrade`` inside the running
        container (no stop). The captured report is stored on the op so
        the portal can show it whether the run succeeded or failed.
        """
        self.ensure_one()
        try:
            output = self.instance_id.hosting_db_upgrade_modules(
                name=self.db_name,
                modules=self.module_name or '',
            )
            self.write({'state': 'done', 'output_log': output or ''})
            try:
                self.env.cr.commit()
            except Exception:
                pass
        except Exception as e:
            output = getattr(e, '_saas_upgrade_output', None) or ''
            self._mark_failed(e, output=output)
            raise

    def _run_restore(self, backup_id):
        """Background worker: restore one database from an uploaded backup.

        Calls ``_do_restore_backup`` directly (NOT action_restore_backup),
        so the instance is never flipped to 'provisioning' — the
        container keeps running and only the target DB is replaced.
        """
        self.ensure_one()
        try:
            self.instance_id._do_restore_backup(backup_id)
            self.write({'state': 'done'})
            try:
                self.env.cr.commit()
            except Exception:
                pass
        except Exception as e:
            self._mark_failed(e)
            raise

    def _run_drop(self):
        self.ensure_one()
        try:
            self.instance_id.hosting_db_drop(name=self.db_name)
            # The PG database is gone — its on-demand zip backups are
            # now orphaned, so reap them from the bucket too.
            self._reap_stale_backups()
            self.write({'state': 'done'})
            try:
                self.env.cr.commit()
            except Exception:
                pass
        except Exception as e:
            self._mark_failed(e)
            raise

    # ------------------------------------------------------------------
    # Zombie reaper — safety net for crashed background threads
    # ------------------------------------------------------------------
    @api.model
    def _cron_reap_stuck_db_operations(self):
        """Fail every 'running' op whose worker is gone.

        Runs from cron every few minutes. The background workers
        normally flip state themselves; this is the safety net for
        when they crashed silently (saas master restart, registry
        reload, SSH socket dropped) so the customer doesn't see an
        eternal "Creating database X..." banner.

        Two cutoffs, OR'd:
        * **heartbeat stale** (``_HEARTBEAT_STALE_MIN``): a live worker
          stamps ``last_heartbeat`` every ~30s, so a stale stamp means
          the worker died — fail it fast (minutes). A legitimately long
          op keeps beating, so it's never touched however long it runs.
        * **create-time net** (``_ZOMBIE_TIMEOUT_MIN``): backstop for
          rows that somehow never beat (heartbeat thread failed to start,
          legacy rows created before this field existed).
        """
        import datetime as _dt
        now = fields.Datetime.now()
        hb_cutoff = now - _dt.timedelta(minutes=self._HEARTBEAT_STALE_MIN)
        old_cutoff = now - _dt.timedelta(minutes=self._ZOMBIE_TIMEOUT_MIN)
        zombies = self.search([
            ('state', '=', 'running'),
            '|',
                ('last_heartbeat', '<', hb_cutoff),
                '&', ('last_heartbeat', '=', False),
                     ('create_date', '<', old_cutoff),
        ])
        if not zombies:
            return
        _logger.warning(
            "Reaping %d zombie DB operation(s) older than %d min: %s",
            len(zombies), self._ZOMBIE_TIMEOUT_MIN,
            zombies.mapped('db_name'),
        )
        for op in zombies:
            try:
                op.write({
                    'state': 'failed',
                    'error_message': _(
                        "This operation didn't finish in time. "
                        "Please dismiss this message and try again."
                    ),
                })
                self.env.cr.commit()
            except Exception:
                self.env.cr.rollback()
                _logger.exception(
                    "Failed to reap zombie op %s", op.id,
                )
