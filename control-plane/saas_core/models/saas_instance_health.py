import datetime
import logging
import threading
import time

import psycopg2

from odoo import api, fields, models

from ..drivers.runtime_health import observe_runtime

_logger = logging.getLogger(__name__)

_RUNTIME_STATE_SELECTION = [
    ('online', 'Online'), ('starting', 'Starting'), ('stopping', 'Stopping'),
    ('unavailable', 'Unavailable'), ('unreachable', 'Unreachable'),
    ('unknown', 'Unknown'), ('stopped', 'Stopped'), ('suspended', 'Suspended'),
]
_REACHABILITY_SELECTION = [
    ('reachable', 'Reachable'), ('unreachable', 'Unreachable'), ('unknown', 'Not checked'),
]
# Observation field on saas.instance -> column on saas.instance.runtime.
_RUNTIME_FIELDS = {
    'runtime_state': 'state', 'runtime_reason': 'reason', 'runtime_message': 'message',
    'runtime_workload': 'workload', 'runtime_reachability': 'reachability',
    'runtime_checked_at': 'checked_at', 'runtime_lifecycle': 'lifecycle',
}


class SaasInstanceRuntime(models.Model):
    """Latest availability observation of one tenant.

    Kept out of saas_instance on purpose: status polls and the sweep cron
    write here, so they never wait on (or conflict with) a job holding the
    tenant row for a whole provisioning/restore transaction."""
    _name = 'saas.instance.runtime'
    _description = 'Tenant Availability Observation'
    _log_access = False

    instance_id = fields.Many2one('saas.instance', required=True, ondelete='cascade', index=True)
    state = fields.Selection(_RUNTIME_STATE_SELECTION, default='unknown')
    reason = fields.Char()
    message = fields.Char()
    workload = fields.Char()
    reachability = fields.Selection(_REACHABILITY_SELECTION, default='unknown')
    checked_at = fields.Datetime(index=True)
    lifecycle = fields.Char()

    _sql_constraints = [
        ('instance_uniq', 'unique(instance_id)', 'One availability observation per tenant.'),
    ]


class SaasInstanceHealth(models.Model):
    _inherit = 'saas.instance'

    runtime_state = fields.Selection(_RUNTIME_STATE_SELECTION, compute='_compute_runtime_observation')
    runtime_display_state = fields.Selection(
        selection=_RUNTIME_STATE_SELECTION,
        compute='_compute_runtime_display_state', string='Availability',
    )
    runtime_reason = fields.Char(compute='_compute_runtime_observation')
    runtime_message = fields.Char(compute='_compute_runtime_observation')
    runtime_workload = fields.Char(compute='_compute_runtime_observation')
    runtime_reachability = fields.Selection(_REACHABILITY_SELECTION, compute='_compute_runtime_observation')
    runtime_checked_at = fields.Datetime(compute='_compute_runtime_observation')
    runtime_lifecycle = fields.Char(compute='_compute_runtime_observation')

    _RUNTIME_STATES = ('provisioning', 'running', 'failed', 'stopped', 'suspended')

    def _compute_runtime_observation(self):
        observations = self.env['saas.instance.runtime'].sudo().search([('instance_id', 'in', self.ids)])
        by_instance = {o.instance_id.id: o for o in observations}
        for instance in self:
            observation = by_instance.get(instance.id)
            for name, column in _RUNTIME_FIELDS.items():
                instance[name] = observation[column] if observation else False
            if not observation:
                instance.runtime_state = 'unknown'
                instance.runtime_reachability = 'unknown'

    @api.depends('state', 'runtime_state', 'runtime_checked_at', 'runtime_lifecycle')
    def _compute_runtime_display_state(self):
        for instance in self:
            instance.runtime_display_state = instance._runtime_status_dict()['runtime_state'] or 'unknown'

    def _refresh_runtime_health(self):
        """Observe only; never restart, resume, or change subscription state."""
        self.ensure_one()
        if self.state not in self._RUNTIME_STATES:
            return
        now = fields.Datetime.now()
        if (self.runtime_checked_at and self.runtime_lifecycle == self.state
                and now - self.runtime_checked_at < datetime.timedelta(seconds=20)):
            return
        if not self.docker_server_id:
            observation = dict(runtime_state='unknown', runtime_reason='cluster_unconfigured',
                               runtime_message='No compute cluster is configured.', runtime_workload='unknown',
                               runtime_reachable=None)
        else:
            try:
                observation = observe_runtime(self._compute_driver(), self._compute_handle(), self.url, self.state)
            except Exception:
                observation = dict(runtime_state='unknown', runtime_reason='cluster_unreachable',
                                   runtime_message='Cannot check the tenant workload: cluster access failed.',
                                   runtime_workload='unknown', runtime_reachable=None)
        reachable = observation.pop('runtime_reachable')
        observation.update(runtime_reachability=('reachable' if reachable is True else
                           'unreachable' if reachable is False else 'unknown'),
                           runtime_checked_at=fields.Datetime.now(), runtime_lifecycle=self.state)
        self._store_runtime_observation(observation)

    def _store_runtime_observation(self, observation):
        values = {_RUNTIME_FIELDS[name]: value for name, value in observation.items()}
        Runtime = self.env['saas.instance.runtime'].sudo()
        stored = False
        previous_state = None
        try:
            with self.env.cr.savepoint():
                record = Runtime.search([('instance_id', '=', self.id)], limit=1)
                if record:
                    # Never queue behind a concurrent check of the same tenant.
                    self.env.cr.execute(
                        'SELECT id FROM saas_instance_runtime WHERE id = %s FOR UPDATE NOWAIT', [record.id])
                    previous_state = record.state
                    record.write(values)
                else:
                    Runtime.create(dict(values, instance_id=self.id))
                stored = True
        except (psycopg2.OperationalError, psycopg2.IntegrityError) as e:
            # A concurrent check is recording this tenant; its result stands.
            _logger.debug('[runtime-health] skipped storing observation for %s: %s', self.id, e)
        self.invalidate_recordset(list(_RUNTIME_FIELDS) + ['runtime_display_state'])
        if stored:
            # Customer alerting rides on the observation we just recorded.
            try:
                self._track_availability_alert(
                    previous_state, values.get('state'), values.get('lifecycle'))
            except Exception:
                _logger.exception('[runtime-health] availability alert failed for %s', self.id)

    def _runtime_status_dict(self):
        self.ensure_one()
        applicable = self.state in self._RUNTIME_STATES
        stale = (not self.runtime_checked_at or self.runtime_lifecycle != self.state
                 or fields.Datetime.now() - self.runtime_checked_at > datetime.timedelta(seconds=120))
        return {
            'runtime_state': ('unknown' if stale else self.runtime_state) if applicable else None,
            'runtime_reason': 'observation_stale' if stale else self.runtime_reason or '',
            'runtime_message': ('Availability has not been checked recently.' if stale
                                else self.runtime_message or ''),
            'runtime_workload': 'unknown' if stale else self.runtime_workload or 'unknown',
            'runtime_reachability': 'unknown' if stale else self.runtime_reachability or 'unknown',
            'runtime_checked_at': (fields.Datetime.to_string(self.runtime_checked_at).replace(' ', 'T') + 'Z'
                                   if self.runtime_checked_at else None),
            'runtime_stale': stale,
        }

    def _get_status_dict(self):
        self._refresh_runtime_health()
        return {**super()._get_status_dict(), **self._runtime_status_dict()}

    # One check can take ~25s (two cluster calls plus the HTTP probe), so no
    # new check starts after 30s: a sweep ends well inside its 60s interval.
    _RUNTIME_SWEEP_START_BUDGET = 30

    @api.model
    def _cron_observe_runtime_health(self):
        """Bound each sweep; status polling also refreshes visible tenants.
        Never-checked tenants go first, then the oldest observations."""
        cutoff = fields.Datetime.now() - datetime.timedelta(seconds=20)
        self.flush_model(['state'])
        self.env['saas.instance.runtime'].flush_model(['instance_id', 'checked_at'])
        self.env.cr.execute("""
            SELECT i.id FROM saas_instance i
            LEFT JOIN saas_instance_runtime r ON r.instance_id = i.id
            WHERE i.state IN %s AND (r.checked_at IS NULL OR r.checked_at < %s)
            ORDER BY r.checked_at ASC NULLS FIRST, i.id ASC
            LIMIT 50
        """, [tuple(self._RUNTIME_STATES), cutoff])
        instances = self.browse([row[0] for row in self.env.cr.fetchall()])
        deadline = time.monotonic() + self._RUNTIME_SWEEP_START_BUDGET
        for instance in instances:
            if time.monotonic() >= deadline:
                break
            instance._refresh_runtime_health()
            # Commit per tenant: a failure or conflict loses one observation,
            # not the sweep, and no lock is held across remote calls.
            if not getattr(threading.current_thread(), 'testing', False):
                self.env.cr.commit()
