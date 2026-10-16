"""C6.2: an undelivered paid upgrade is released, never captured (C6.D8).

Triggers (C6.D11): automatically at T2 for any reason; before the card
authorization lapses, should T2 ever be set beyond it; when the
authorization is voided or expires outside the platform; or by staff. The
release job runs under the instance lock and is idempotent: it re-checks
delivery first (a delivered upgrade is captured instead), returns the money
(void and wallet hold, or a wallet refund when it was already taken),
withdraws only the parts of the package that are not in effect, and tells
the customer. The running service is never stopped, restarted or downsized.
"""
import json
import logging
from datetime import timedelta

from odoo import _, fields, models
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)

_AUTOMATIC = ('deadline', 'capture_deadline', 'authorization_lost')
# Released this long before the card authorization lapses (C6.D9 (a)).
_CAPTURE_MARGIN_HOURS = 1

_ODOO_KEYS = ('cpu_limit', 'cpu_request', 'mem_limit', 'mem_request', 'workers')
_DB_KEYS = ('db_cpu_limit', 'db_cpu_request', 'db_mem_limit', 'db_mem_request')


class SaasInstance(models.Model):
    _inherit = 'saas.instance'

    upgrade_withdrawn_package = fields.Text(
        copy=False, readonly=True,
        help='The package a released upgrade fell back to (C6.2): verifying it '
             'is not an "upgrade complete".')

    # ---------------------------------------------------------- triggers
    def _schedule_upgrade_release(self, invoice):
        """Queue the automatic release of a funded upgrade at T2, or before
        its authorization lapses if that comes first. Idempotent."""
        self.ensure_one()
        at, reason = self.upgrade_deadline_at, 'deadline'
        if not at:
            return
        if invoice.saas_capture_mode == 'manual' and invoice.saas_capture_deadline:
            guard = invoice.saas_capture_deadline - timedelta(hours=_CAPTURE_MARGIN_HOURS)
            if guard < at:
                at, reason = guard, 'capture_deadline'
        self._enqueue_upgrade_release(invoice, reason, eta=at)

    def _enqueue_upgrade_release(self, invoice, reason, eta=None):
        self.ensure_one()
        return self.env['saas.job'].sudo().enqueue(
            self, '_do_release_upgrade', args=(invoice.id, reason), channel='deploy',
            idempotency_key='upgrade-release:%s:inv%s' % (reason, invoice.id),
            lock_key='instance:%s' % self.id, idempotent=True, max_attempts=10,
            transactional=True, eta=eta)

    def _secure_change(self, invoice, kind, mode):
        super()._secure_change(invoice, kind, mode)
        if kind in ('plan', 'storage'):
            self._schedule_upgrade_release(invoice)

    def action_release_upgrade(self):
        """Staff: release this instance's undelivered paid upgrade now."""
        self.ensure_one()
        if not self.env.su and not self.env.user.has_group('saas_core.group_saas_manager'):
            raise AccessError(_("Only SaaS managers can release an upgrade."))
        invoice = self.upgrade_invoice_id
        if not self._upgrade_undelivered(invoice):
            raise UserError(_("There is no undelivered paid upgrade to release."))
        self._enqueue_upgrade_release(invoice, 'staff')
        self._append_log("Release of upgrade %s requested by %s." % (invoice.name, self.env.user.login))
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'type': 'info', 'message': _("The upgrade is being released; nothing will be charged.")}}

    def _authorization_lost(self, invoice):
        """A card authorization of ``invoice`` was voided, expired or failed
        outside a release (F8): stop the upgrade unless another one still
        covers it."""
        self.ensure_one()
        if self._change_invoice_kind(invoice) not in ('plan', 'storage') \
                or not self._upgrade_undelivered(invoice) or invoice.saas_capture_mode != 'manual':
            return
        if self._authorized_amount(invoice) >= invoice.amount_residual:
            return
        self._append_log("Payment authorization for %s was cancelled outside the platform." % invoice.name)
        self._enqueue_upgrade_release(invoice, 'authorization_lost')

    # ---------------------------------------------------------- the job
    def _do_release_upgrade(self, invoice_id, reason):
        """Job: release the undelivered upgrade ``invoice_id``; idempotent."""
        self.ensure_one()
        invoice = self.env['account.move'].sudo().browse(invoice_id).exists()
        if not invoice or invoice.saas_released_at or self.upgrade_invoice_id != invoice \
                or self._change_invoice_kind(invoice) not in ('plan', 'storage') \
                or not invoice.saas_funds_secured_at or invoice.state != 'posted':
            return
        if reason == 'authorization_lost' and \
                self._authorized_amount(invoice) >= invoice.amount_residual and invoice.amount_residual:
            return
        on_cluster = self.docker_server_id.compute_driver == 'kubernetes' \
            and self.state in self._RECONCILE_RUNNING + self._RECONCILE_STOPPED
        # 1. A last verification: a delivered upgrade is captured, never released (F11).
        if on_cluster and not invoice.saas_delivered_at \
                and self.package_status in ('applying', 'blocked'):
            self._verify_package()
        if invoice.saas_delivered_at:
            self._append_log("Release of %s refused: the upgrade is delivered and is "
                             "captured instead." % invoice.name)
            self._advance_change(invoice)
            return
        # 2. What the cluster already runs (raises while it is unreachable:
        # the job retries, and nothing is charged meanwhile).
        new_res = self._k8s_plan_resources() if on_cluster else {}
        new_pkg = self._package_resources()
        in_effect = self._compute_driver().package_in_effect(self._compute_handle(), new_res) \
            if new_res else None
        # 3. The money.
        refunded = self._return_change_funds(invoice)
        # 4. Withdraw what is not in effect; keep what is.
        if new_res:
            self._withdraw_package(new_res, new_pkg, in_effect)
        # 5. Tell.
        self._append_log("Upgrade %s released (%s)%s." % (
            invoice.name, reason, '; %.2f back to the wallet' % refunded if refunded else ''))
        self.message_post(body=self._release_message(reason, invoice, refunded))
        if reason in _AUTOMATIC:
            self._escalate_released_upgrade(invoice, reason)

    def _return_change_funds(self, invoice):
        """Void and return the hold; when the money was already taken (a
        provider without manual capture, or a wallet-covered invoice), give
        it back to the wallet instead. Returns the amount refunded."""
        self.ensure_one()
        # Our own void must not read as an authorization lost outside (F8).
        if self._release_change_funds(invoice.with_context(saas_releasing=True)):
            return 0.0
        return self._refund_released_change(invoice)

    def _refund_released_change(self, invoice):
        """C6.D8 fallback: the change invoice is paid but the change was not
        delivered. Its wallet part goes back to its lots and the paid part is
        credited to the wallet as customer money (C6.D6, until C15.3 refunds
        to the card). Idempotent per invoice."""
        self.ensure_one()
        kind = self._change_invoice_kind(invoice)
        wallet = self._wallet(create=True)
        reason = _('Upgrade not delivered — invoice %s') % invoice.name
        refunded = wallet._refund_move(invoice, reason=reason)
        paid = round(invoice.amount_total - invoice.amount_residual, 2)
        already = self.env['saas.wallet.transaction'].sudo().search_count([
            ('wallet_id', '=', wallet.id), ('move_id', '=', invoice.id),
            ('kind', '=', 'credit'), ('origin', '=', 'upgrade_release')])
        if paid > 0 and not already:
            wallet._credit(paid, origin='upgrade_release', reason=reason, move=invoice, instance=self)
            refunded += paid
        if kind == 'plan':
            self.write({'pending_plan_id': False, 'pending_billing_period': False,
                        'pending_change_invoice_id': False, 'pending_wallet_credit': 0.0})
        elif kind == 'storage':
            self.write({'pending_storage_blocks': 0, 'storage_block_pending_invoice_id': False})
        invoice.sudo().saas_released_at = fields.Datetime.now()
        self._close_upgrade_escalation(_("Upgrade released; refunded to the wallet."))
        return round(refunded, 2)

    def _withdraw_package(self, new_res, new_pkg, in_effect):
        """Fall back to the paid package, keeping the parts of ``new_res``
        the cluster already runs (C6.D7): they are never downsized, and are
        counted in capacity until the next package change replaces them."""
        self.ensure_one()
        in_effect = in_effect or {}
        # Forget the unapplied package: its volume size must not survive as a floor.
        self.package_desired = self.package_applied or False
        old_res = self._k8s_plan_resources()
        old_pkg = self._package_resources()
        kept = dict(self._package_retained_overlay())
        extra_cpu, extra_mem = self.package_retained_cpu_m, self.package_retained_mem_mi
        if not kept:
            extra_cpu = extra_mem = 0
        groups = (('odoo', _ODOO_KEYS, ('odoo_cpu_m', 'cron_cpu_m'), ('odoo_mem_mi', 'cron_mem_mi')),
                  ('database', _DB_KEYS, ('db_cpu_m',), ('db_mem_mi',)))
        for group, keys, cpu_keys, mem_keys in groups:
            changed = {k: new_res[k] for k in keys if new_res[k] != old_res[k]}
            if in_effect.get(group) and changed:
                kept.update(changed)
                extra_cpu += max(sum(new_pkg[k] for k in cpu_keys) - sum(old_pkg[k] for k in cpu_keys), 0)
                extra_mem += max(sum(new_pkg[k] for k in mem_keys) - sum(old_pkg[k] for k in mem_keys), 0)
        old_volume = int(self._VOLUME_SIZE_RE.match(old_res['filestore_size']).group(1))
        volume = in_effect.get('volume_gi') or 0
        if volume > old_volume:
            # A claim that was asked to grow cannot be shrunk again.
            kept.update(filestore_size='%dGi' % volume, db_storage_size='%dGi' % volume)
        if kept:
            quota = self._max_quota(old_res['quota'], new_res['quota'])
            if volume > old_volume:
                quota = self._max_quota(quota, {'requests.storage': '%dGi' % (volume + 1)})
            kept['quota'] = quota
            self.write({
                'package_retained': json.dumps({
                    'plan': self._package_plan().id, 'storage_gb': self._package_storage_gb(),
                    'res': kept}, sort_keys=True),
                'package_retained_cpu_m': extra_cpu, 'package_retained_mem_mi': extra_mem})
            self._append_log("Kept in place after the release: %s." % ', '.join(
                sorted(k for k in kept if k != 'quota')))
        self.upgrade_withdrawn_package = self._package_key(self._k8s_plan_resources())
        try:
            self._update_container_resources()
        except Exception:
            # Recorded as pending: the reconciler applies it later (C6.1).
            _logger.warning("Withdrawing the upgrade of %s failed; it is retried",
                            self.subdomain, exc_info=True)

    @staticmethod
    def _max_quota(first, second):
        """Key-wise larger of two namespace quotas."""
        from kubernetes.utils import parse_quantity
        merged = dict(first)
        for key, value in second.items():
            if key not in merged or parse_quantity(value) > parse_quantity(merged[key]):
                merged[key] = value
        return merged

    def _notify_package_live(self):
        # Falling back to the paid package after a release is not an upgrade.
        if self.upgrade_withdrawn_package and self.package_desired == self.upgrade_withdrawn_package:
            return
        return super()._notify_package_live()

    def _release_message(self, reason, invoice, refunded):
        self.ensure_one()
        money = _("%(amount)s %(currency)s is back in your wallet.",
                  amount='%.2f' % refunded, currency=invoice.currency_id.name) \
            if refunded else _("You haven't been charged.")
        if reason == 'staff':
            lead = _("Your upgrade was cancelled.")
        elif reason == 'authorization_lost':
            lead = _("Your payment authorization for the upgrade was cancelled, so the upgrade was stopped.")
        else:
            lead = _("We couldn't complete your upgrade in time.")
        tail = _("Your current plan is unchanged.")
        if reason != 'staff':
            tail += ' ' + _("You can try again.")
        return '%s %s %s' % (lead, money, tail)
