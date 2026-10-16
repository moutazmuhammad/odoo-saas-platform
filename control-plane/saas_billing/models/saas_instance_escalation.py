"""C6.5: a delayed paid upgrade is owned by a named on-call user (C6.D2).

At T1 one "Paid upgrade delayed" activity is assigned to the on-call user,
due at T2. At T2 it is replaced by a "past deadline" activity and a critical
alert. Odoo emails the assignee, so nobody has to watch alerts. Both close
themselves once the upgrade is delivered or released. Each stage happens
once per change invoice: the jobs are keyed and the stage is stored on the
invoice, in the same transaction as the activity.
"""
import logging

from markupsafe import Markup, escape

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

_ACTIVITY_TYPES = {
    'target': 'saas_billing.mail_act_upgrade_delayed',
    'deadline': 'saas_billing.mail_act_upgrade_past_deadline',
}


class SaasInstance(models.Model):
    _inherit = 'saas.instance'

    @api.model
    def _upgrade_oncall_user(self):
        """``(user, named)``: the internal user named in the settings, or the
        administrator (``named`` False) when none, or no valid one, is set."""
        raw = self.env['ir.config_parameter'].sudo().get_param('saas_master.upgrade_oncall_user_id')
        Users = self.env['res.users'].sudo()
        try:
            user = Users.browse(int(raw)).exists() if raw else Users
        except (TypeError, ValueError):
            user = Users
        if user and user.active and not user.share:
            return user, True
        return self.env.ref('base.user_admin'), False

    def _upgrade_undelivered(self, invoice):
        """``invoice`` is this instance's funded upgrade, not yet delivered
        or released."""
        self.ensure_one()
        return bool(invoice and self.upgrade_invoice_id == invoice
                    and invoice.state == 'posted' and invoice.saas_funds_secured_at
                    and not invoice.saas_delivered_at and not invoice.saas_released_at)

    def _schedule_upgrade_escalation(self, invoice):
        """Queue the T1 and T2 escalation checks of a funded upgrade at their
        exact times. Idempotent: a queued or finished check is kept."""
        self.ensure_one()
        Job = self.env['saas.job'].sudo()
        for stage, at in (('target', self.upgrade_target_at), ('deadline', self.upgrade_deadline_at)):
            if at:
                Job.enqueue(self, '_do_escalate_upgrade', args=(invoice.id, stage),
                            idempotency_key='upgrade-escalate:%s:inv%s' % (stage, invoice.id),
                            lock_key='instance:%s' % self.id, idempotent=True,
                            transactional=True, eta=at)

    def _secure_change(self, invoice, kind, mode):
        super()._secure_change(invoice, kind, mode)
        if kind in ('plan', 'storage'):
            self._schedule_upgrade_escalation(invoice)

    def _do_escalate_upgrade(self, invoice_id, stage):
        """Job: at T1 (``target``) or T2 (``deadline``), give a still
        undelivered upgrade to the on-call user."""
        self.ensure_one()
        invoice = self.env['account.move'].sudo().browse(invoice_id).exists()
        if stage not in _ACTIVITY_TYPES or not self._upgrade_undelivered(invoice):
            return
        if invoice.saas_escalation == 'deadline' or invoice.saas_escalation == stage:
            return
        user, named = self._upgrade_oncall_user()
        if stage == 'deadline':
            self._close_upgrade_escalation(_("Deadline reached; escalated again."))
            summary = _("Paid upgrade not delivered by its deadline")
            due = fields.Date.context_today(self)
        else:
            summary = _("Paid upgrade delayed")
            due = fields.Date.context_today(self, self.upgrade_deadline_at)
        # Notified explicitly, so the assignee is emailed even when the job
        # runs as that same user.
        activity = self.sudo().with_context(mail_activity_quick_update=True).activity_schedule(
            _ACTIVITY_TYPES[stage], date_deadline=due, summary=summary,
            note=self._upgrade_escalation_note(invoice, stage), user_id=user.id)
        activity.action_notify()
        invoice.saas_escalation = stage
        self._append_log("Upgrade %s escalated to %s (%s)." % (invoice.name, user.login, stage))
        Alert = self.env['saas.alert']
        if not named:
            Alert._notify('oncall_missing', '%s: no on-call user set; %s assigned to %s'
                          % (self.subdomain, summary, user.login), level='critical')
        if stage == 'deadline':
            Alert._notify('upgrade_past_deadline', '%s: %s (invoice %s)'
                          % (self.subdomain, summary, invoice.name), level='critical',
                          detail=self.package_error or self.package_block_reason or None)
        else:
            Alert._notify('upgrade_delayed', '%s: %s (invoice %s), deadline %s UTC'
                          % (self.subdomain, summary, invoice.name,
                             fields.Datetime.to_string(self.upgrade_deadline_at)),
                          level='warning',
                          detail=self.package_error or self.package_block_reason or None)

    def _upgrade_capacity_need(self, invoice):
        """What the change still needs from the cluster, as text."""
        self.ensure_one()
        kind = self._change_invoice_kind(invoice)
        cluster = self.docker_server_id.name or '-'
        if kind == 'plan' and self.plan_id and self.pending_plan_id:
            current = self.plan_id._package(storage_gb=self.effective_storage_limit_gb)
            target = self.pending_plan_id._package(storage_gb=self._package_storage_gb())
            cpu, mem = self._package_change_need(current, target)
            return _("needs %(cpu).2f vCPU / %(mem).2f GiB free on cluster %(cluster)s "
                     "(package increase plus one rolling Odoo pod)",
                     cpu=cpu / 1000.0, mem=mem / 1024.0, cluster=cluster)
        if kind == 'storage':
            more = (self._package_storage_gb() or 0) - (self.effective_storage_limit_gb or 0)
            return _("needs the volume to grow by %(gb)g GB on cluster %(cluster)s",
                     gb=max(more, 0), cluster=cluster)
        return _("cluster %s") % cluster

    def _upgrade_escalation_note(self, invoice, stage):
        self.ensure_one()

        def utc(value):
            return '%s UTC' % fields.Datetime.to_string(value) if value else '-'

        kind = self._change_invoice_kind(invoice)
        rollout = dict(self._fields['package_status'].selection).get(self.package_status, '-')
        if self.package_block_reason:
            rollout += ' (%s)' % self.package_block_reason
        lines = [
            _("Customer: %(customer)s, instance %(instance)s.",
              customer=self.partner_id.display_name or '-', instance=self.subdomain),
            _("Change: %(what)s, invoice %(invoice)s, funded %(at)s. Nothing is charged until it is delivered.",
              what=self._change_label(kind) if kind else '-', invoice=invoice.name,
              at=utc(self.upgrade_authorized_at)),
            _("Target (T1): %(t1)s. Deadline (T2): %(t2)s.",
              t1=utc(self.upgrade_target_at), t2=utc(self.upgrade_deadline_at)),
            _("Rollout: %s.") % rollout,
        ]
        if self.package_error:
            lines.append(_("Last error: %s") % self.package_error[-300:])
        lines.append(_("Capacity: %s.") % self._upgrade_capacity_need(invoice))
        if stage == 'deadline':
            lines.append(_("Not delivered by the deadline. Find the cause, then close this "
                           "activity; it also closes itself if the upgrade is delivered or released."))
        else:
            lines.append(_("To do before the deadline: free capacity on the cluster, or find why "
                           "the autoscaler did not add a node. The customer sees \"taking longer "
                           "than usual\". This closes itself once the upgrade is delivered."))
        return Markup('<br/>').join(escape(line) for line in lines)

    def _close_upgrade_escalation(self, feedback):
        """Mark this instance's open upgrade escalations done."""
        self.ensure_one()
        types = self.env['mail.activity.type']
        for xmlid in _ACTIVITY_TYPES.values():
            types |= self.env.ref(xmlid, raise_if_not_found=False) or types
        activities = self.env['mail.activity'].sudo().search([
            ('res_model', '=', self._name), ('res_id', '=', self.id),
            ('activity_type_id', 'in', types.ids)])
        if activities:
            activities.action_feedback(feedback=feedback)

    def _mark_change_delivered(self, invoice):
        super()._mark_change_delivered(invoice)
        if invoice == self.upgrade_invoice_id:
            self._close_upgrade_escalation(_("Upgrade delivered."))

    def _release_change_funds(self, invoice):
        released = super()._release_change_funds(invoice)
        if released and invoice == self.upgrade_invoice_id:
            self._close_upgrade_escalation(_("Upgrade released; nothing charged."))
        return released
