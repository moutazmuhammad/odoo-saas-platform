import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..models.saas_instance import ORIGIN_DATA_RESTORATION

_logger = logging.getLogger(__name__)


class SaasRestoreRetainedWizard(models.TransientModel):
    _name = 'saas.restore.retained.wizard'
    _description = 'Restore Retained Backup to Instance'

    source_instance_id = fields.Many2one(
        'saas.instance',
        string='Source Instance',
        required=True,
        readonly=True,
        help='Instance that holds the retained snapshot. '
             'Can be cancelled or already reactivated.',
    )
    retained_snapshot_id = fields.Many2one(
        related='source_instance_id.retained_snapshot_id',
        string='Retained Snapshot',
        readonly=True,
    )
    partner_id = fields.Many2one(
        related='source_instance_id.partner_id',
        string='Customer',
        readonly=True,
    )
    target_instance_id = fields.Many2one(
        'saas.instance',
        string='Target Instance',
        required=True,
        domain="[('partner_id', '=', partner_id), "
               "('state', '=', 'running')]",
        help='The running instance the snapshot will be restored onto '
             '(its database and files are replaced). Must belong to the '
             'same customer. Can be the same instance after reactivation.',
    )
    restoration_fee = fields.Float(
        string='Restoration Fee',
        help='Computed default: months the snapshot was retained after '
             'deletion × snapshot size rounded up to the next whole GB '
             '× the per-GB monthly rate from SaaS settings. An invoice '
             'will be created and sent; the restore happens '
             'automatically when it is paid. Set to 0 for free restore.',
    )
    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if self.env.context.get('active_model') == 'saas.instance':
            instance = self.env['saas.instance'].browse(
                self.env.context.get('active_id')
            )
            if instance.exists():
                res['source_instance_id'] = instance.id
                # Computed retained-storage fee: months retained ×
                # ceil(snapshot GB) × per-GB monthly rate.
                res['restoration_fee'] = instance._get_retained_snapshot_fee()
        return res

    def _validate(self):
        """Common validation for both actions."""
        self.ensure_one()
        source = self.source_instance_id
        target = self.target_instance_id

        if not source.retained_snapshot():
            raise UserError(_(
                "No retained snapshot found for instance '%s'."
            ) % source.name)

        if target.state != 'running':
            raise UserError(_(
                "Target instance must be running (current: %s)."
            ) % target.state)

        if target.partner_id != source.partner_id:
            raise UserError(_(
                "Target instance must belong to the same customer."
            ))

    def action_send_invoice_and_schedule(self):
        """Create and send the restoration invoice. The restore will
        happen automatically when the client pays.
        """
        self._validate()
        source = self.source_instance_id
        target = self.target_instance_id

        if not self.restoration_fee or self.restoration_fee <= 0:
            raise UserError(_(
                "Please set a restoration fee, or use 'Restore Now (Free)' instead."
            ))

        # Create the invoice
        invoice = self._create_restoration_invoice(source, target)

        # Store the pending restoration on the target instance
        target.write({
            'restoration_invoice_id': invoice.id,
            'restoration_backup_id': source.retained_snapshot().id,
        })

        target._append_log(
            "Restoration invoice %s created (%.2f). "
            "Data will be restored automatically when the client pays."
            % (invoice.name, self.restoration_fee)
        )
        target.message_post(body=_(
            "Restoration invoice <b>%s</b> sent to client. "
            "Data from <b>%s</b> will be restored automatically after payment."
        ) % (invoice.name, source.subdomain or source.name))

        _logger.info(
            "Restoration invoice %s created for %s → %s (%.2f)",
            invoice.name, source.subdomain, target.subdomain,
            self.restoration_fee,
        )

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'saas.instance',
            'res_id': target.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_restore_now_free(self):
        """Queue an immediate, free restore of the retained snapshot onto
        the target (the durable job queue runs it; the target shows
        'provisioning' until it's done)."""
        self._validate()
        source = self.source_instance_id
        target = self.target_instance_id
        target.queue_full_instance_restore(source.retained_snapshot())
        target.message_post(body=_(
            "Retained snapshot restore queued by %s (no charge)."
        ) % self.env.user.name)
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'saas.instance',
            'res_id': target.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _create_restoration_invoice(self, source, target):
        """Create and post an invoice for the data restoration service."""
        self.ensure_one()
        product = target._get_billing_product()
        partner = target.partner_id

        order = self.env['sale.order'].create({
            'partner_id': partner.id,
            'origin': ORIGIN_DATA_RESTORATION % (
                target.name or target.subdomain
            ),
            'order_line': [(0, 0, {
                'product_id': product.id,
                'name': _(
                    'Data restoration — backup from %s restored to %s'
                ) % (
                    source.name or source.subdomain,
                    target.name or target.subdomain,
                ),
                'product_uom_qty': 1,
                'price_unit': self.restoration_fee,
            })],
        })
        order.action_confirm()
        invoice = order._create_invoices()
        invoice.action_post()

        _logger.info(
            "Data restoration invoice %s created for %s (%.2f)",
            invoice.name, partner.name, self.restoration_fee,
        )
        return invoice
