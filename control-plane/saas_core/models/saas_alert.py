import logging
import os

import requests as http_requests

from odoo import api, models

_logger = logging.getLogger(__name__)

_LEVELS = ('info', 'warning', 'error', 'critical')


class SaasAlert(models.AbstractModel):
    """Operational alerting (SEC-009) — provider-agnostic and opt-in.

    Every alert is always written to the server log at the right level. If an
    alert webhook is configured (``ir.config_parameter`` ``saas_master.alert_webhook``
    or the ``SAAS_ALERT_WEBHOOK`` env var — a Slack/Discord/generic incoming
    webhook), a compact JSON payload is also POSTed. With no webhook set this is
    a pure logger, so it changes nothing until an operator opts in. Best-effort:
    a webhook failure is logged but never propagates into the alerted operation.
    """
    _name = 'saas.alert'
    _description = 'SaaS Operational Alerting'

    @api.model
    def _alert_webhook(self):
        param = self.env['ir.config_parameter'].sudo().get_param(
            'saas_master.alert_webhook')
        return (param or os.environ.get('SAAS_ALERT_WEBHOOK') or '').strip()

    @api.model
    def _notify(self, event, message, *, level='error', detail=None):
        """Emit an operational alert. Returns True if a webhook was POSTed."""
        if level not in _LEVELS:
            level = 'error'
        text = '[SAAS][%s] %s: %s' % (level.upper(), event, message)
        getattr(_logger, level)(text + (' | %s' % detail if detail else ''))
        url = self._alert_webhook()
        if not url:
            return False
        try:
            http_requests.post(
                url,
                json={'text': text, 'event': event, 'level': level,
                      'message': message, 'detail': detail},
                timeout=5,
            )
            return True
        except Exception:
            _logger.warning(
                "Alert webhook POST failed for event %r", event, exc_info=True)
            return False
