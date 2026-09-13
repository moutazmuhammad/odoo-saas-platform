import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class SaasAuditLog(models.Model):
    """Append-only internal audit trail (SEC-010).

    Records who did what, to which target, and the outcome — for sensitive
    platform actions (instance lifecycle, destructive DB ops, secret access).
    Write-once: rows can be created and read but never updated or deleted, so
    the trail is tamper-evident even for managers. Use :meth:`_saas_audit`
    (best-effort, never raises into the caller) to record an event.
    """
    _name = 'saas.audit.log'
    _description = 'SaaS Audit Log'
    _order = 'id desc'
    _log_access = False  # we capture actor/time ourselves; rows are immutable

    actor_id = fields.Many2one('res.users', string='Actor', readonly=True, index=True)
    actor_login = fields.Char(string='Actor Login', readonly=True,
                              help='Login captured at event time (survives user rename/delete).')
    action = fields.Char(string='Action', required=True, readonly=True, index=True)
    model = fields.Char(string='Target Model', readonly=True)
    res_id = fields.Integer(string='Target ID', readonly=True)
    res_name = fields.Char(string='Target', readonly=True)
    result = fields.Selection(
        [('ok', 'Success'), ('error', 'Failure')],
        string='Result', default='ok', required=True, readonly=True, index=True)
    detail = fields.Text(string='Detail', readonly=True)
    timestamp = fields.Datetime(
        string='Timestamp', required=True, readonly=True, index=True,
        default=fields.Datetime.now)

    def write(self, vals):
        raise UserError(_("Audit log entries are immutable and cannot be modified."))

    def unlink(self):
        raise UserError(_("Audit log entries are immutable and cannot be deleted."))

    @api.model
    def _saas_audit(self, action, *, result='ok', model=None, res_id=None,
                    res_name=None, detail=None):
        """Record one audit event. Best-effort: failures here are logged but
        never propagate, so auditing can't break the audited operation."""
        try:
            user = self.env.user
            self.sudo().create({
                'actor_id': user.id,
                'actor_login': user.login,
                'action': action,
                'model': model,
                'res_id': res_id,
                'res_name': res_name,
                'result': result,
                'detail': (detail or '')[:4000] or False,
            })
        except Exception:
            _logger.exception("Failed to write audit log for action %r", action)
