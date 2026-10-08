"""Customer email alerts (service down / back online, high CPU, high storage).

Hooks into what already runs — the 20-second availability observer and the
10-minute usage refresh — and sends the customer an email on a real
condition, never on a single blip:

* **Service down**: the observation has been ``unavailable``/``unreachable``
  for ``saas_master.alert_down_seconds`` (default 60 s = three failed
  checks) while the server is supposed to be running. One email per
  outage, and a "back online" email when it recovers.
* **High CPU**: ``cpu_usage_pct`` at or above ``saas_master.alert_cpu_pct``
  (default 90) for ``saas_master.alert_cpu_minutes`` (default 10). At most
  one email every ``saas_master.alert_repeat_hours`` (default 24).
* **High storage**: ``storage_usage_pct`` at or above
  ``saas_master.alert_storage_pct`` (default 90). At most one email every
  ``saas_master.alert_repeat_hours``.

Every send is best effort: a mail failure is logged and never breaks the
observer or the usage cron.
"""
import datetime
import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)

_DOWN_STATES = ('unavailable', 'unreachable')


class SaasInstanceAlerts(models.Model):
    _inherit = 'saas.instance'

    alert_down_since = fields.Datetime(
        copy=False, help="First failed availability check of the current outage.")
    alert_down_notified = fields.Boolean(
        default=False, copy=False, help="The service-down email was sent for the current outage.")
    alert_cpu_since = fields.Datetime(
        copy=False, help="CPU has been above the alert threshold since this time.")
    alert_cpu_last_sent = fields.Datetime(copy=False)
    alert_storage_last_sent = fields.Datetime(copy=False)

    # ------------------------------------------------------------ settings
    def _alert_param(self, key, default):
        raw = self.env['ir.config_parameter'].sudo().get_param('saas_master.%s' % key, '')
        try:
            return float(raw) if raw not in (None, '') else float(default)
        except (TypeError, ValueError):
            return float(default)

    def _alert_repeat_delta(self):
        return datetime.timedelta(hours=self._alert_param('alert_repeat_hours', 24))

    def _alert_send(self, template_xmlid):
        """Email the customer; never raise into the caller."""
        self.ensure_one()
        try:
            self._send_notification(template_xmlid)
            return True
        except Exception:
            _logger.exception("Could not send %s for %s", template_xmlid, self.subdomain)
            return False

    # ------------------------------------------------------------ availability
    def _track_availability_alert(self, previous_state, new_state, lifecycle):
        """Called after every stored availability observation.

        Only a running server can be "down": stopped, suspended or
        provisioning servers are expected to be unreachable. ``unknown``
        and ``starting`` observations are neither up nor down and leave
        the current outage untouched."""
        self.ensure_one()
        now = fields.Datetime.now()
        if lifecycle != 'running':
            if self.alert_down_since or self.alert_down_notified:
                self.write({'alert_down_since': False, 'alert_down_notified': False})
            return
        if new_state in _DOWN_STATES:
            since = self.alert_down_since or now
            if not self.alert_down_since:
                self.write({'alert_down_since': now})
            grace = datetime.timedelta(seconds=self._alert_param('alert_down_seconds', 60))
            if not self.alert_down_notified and now - since >= grace:
                if self._alert_send('saas_core.mail_template_saas_service_down'):
                    self.write({'alert_down_notified': True})
                    self._append_log("Alert: service down email sent (down since %s)." % since)
            return
        if new_state == 'online':
            if self.alert_down_notified:
                self._alert_send('saas_core.mail_template_saas_service_restored')
                self._append_log("Alert: service restored email sent.")
            if self.alert_down_since or self.alert_down_notified:
                self.write({'alert_down_since': False, 'alert_down_notified': False})

    # ------------------------------------------------------------ usage
    def _track_usage_alerts(self):
        """Called after every usage refresh of a running server."""
        self.ensure_one()
        now = fields.Datetime.now()
        repeat = self._alert_repeat_delta()

        cpu_pct = self._alert_param('alert_cpu_pct', 90)
        cpu_minutes = self._alert_param('alert_cpu_minutes', 10)
        if (self.cpu_usage_pct or 0.0) >= cpu_pct:
            since = self.alert_cpu_since or now
            if not self.alert_cpu_since:
                self.write({'alert_cpu_since': now})
            sustained = now - since >= datetime.timedelta(minutes=cpu_minutes)
            recently = self.alert_cpu_last_sent and now - self.alert_cpu_last_sent < repeat
            if sustained and not recently:
                if self._alert_send('saas_core.mail_template_saas_high_cpu'):
                    self.write({'alert_cpu_last_sent': now})
                    self._append_log("Alert: high CPU email sent (%.0f%%)." % self.cpu_usage_pct)
        elif self.alert_cpu_since:
            self.write({'alert_cpu_since': False})

        storage_pct = self._alert_param('alert_storage_pct', 90)
        if (self.storage_usage_pct or 0.0) >= storage_pct:
            recently = self.alert_storage_last_sent and now - self.alert_storage_last_sent < repeat
            if not recently:
                if self._alert_send('saas_core.mail_template_saas_high_storage'):
                    self.write({'alert_storage_last_sent': now})
                    self._append_log(
                        "Alert: high storage email sent (%.0f%%)." % self.storage_usage_pct)
