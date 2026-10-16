import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    @staticmethod
    def _saas_invoice_ids(commands):
        """Invoice ids linked by an ``invoice_ids`` x2many command list."""
        ids = set()
        for command in commands or ():
            if command[0] == 6:
                ids.update(command[2] or ())
            elif command[0] == 4:
                ids.add(command[1])
        return ids

    @api.model_create_multi
    def create(self, vals_list):
        """Every payment (checkout page, ``/invoice/transaction``, saved
        card) starts with a transaction: re-check the feasibility of an
        upgrade or storage purchase it pays for before any money moves (C6.7)."""
        invoice_ids = set()
        for vals in vals_list:
            invoice_ids |= self._saas_invoice_ids(vals.get('invoice_ids'))
        if invoice_ids:
            self.env['saas.instance']._gate_collection(list(invoice_ids))
        return super().create(vals_list)

    def _set_authorized(self, state_message=None, extra_allowed_states=()):
        """C6.10/C6.12: an authorization for an upgrade, a storage purchase or
        a new server's order secures its funds and starts delivery; it is
        captured once delivered. Any other authorized payment (renewals,
        add-ons, slot reservations) is captured at once, as before, because
        nothing waits for delivery."""
        txs = super()._set_authorized(state_message=state_message,
                                      extra_allowed_states=extra_allowed_states)
        Instance = self.env['saas.instance'].sudo()
        for tx in txs:
            invoices = tx.invoice_ids
            changes = Instance.search([
                '|', '|', ('pending_change_invoice_id', 'in', invoices.ids),
                ('storage_block_pending_invoice_id', 'in', invoices.ids),
                ('order_invoice_id', 'in', invoices.ids)]) if invoices else Instance
            pending = [(inst, inv) for inst in changes for inv in invoices
                       if inst._change_invoice_kind(inv)]
            if pending:
                for inst, inv in pending:
                    inst._advance_change(inv)
                continue
            try:
                tx.sudo()._send_capture_request()
            except Exception as e:  # never break the provider's notification handling
                _logger.exception("Immediate capture of transaction %s failed", tx.reference)
                self.env['saas.alert']._notify(
                    'capture_failed', 'transaction %s: immediate capture failed' % tx.reference,
                    level='error', detail=str(e)[:500])
        return txs

    def _set_canceled(self, state_message=None, extra_allowed_states=()):
        authorized = self.filtered(lambda t: t.state == 'authorized')
        txs = super()._set_canceled(state_message=state_message,
                                    extra_allowed_states=extra_allowed_states)
        (txs & authorized)._saas_authorization_lost()
        return txs

    def _set_error(self, state_message, extra_allowed_states=()):
        authorized = self.filtered(lambda t: t.state == 'authorized')
        txs = super()._set_error(state_message, extra_allowed_states=extra_allowed_states)
        (txs & authorized)._saas_authorization_lost()
        return txs

    def _saas_authorization_lost(self):
        """C6.2 (F8): an upgrade's authorization was voided, expired or
        failed outside a release: the upgrade stops, nothing is charged."""
        if not self or self.env.context.get('saas_releasing'):
            return
        Instance = self.env['saas.instance'].sudo()
        for tx in self:
            invoices = tx.invoice_ids
            if not invoices:
                continue
            for inst in Instance.search([('upgrade_invoice_id', 'in', invoices.ids)]):
                inst._authorization_lost(inst.upgrade_invoice_id)
