from odoo import api, models


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
