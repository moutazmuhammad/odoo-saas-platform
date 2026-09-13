import logging

from odoo import fields, models, _

from ..utils import run_in_background

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = 'account.move'

    def write(self, vals):
        result = super().write(vals)
        if 'payment_state' in vals and vals['payment_state'] in ('paid', 'in_payment'):
            self._saas_check_instance_payment()
        return result

    def button_cancel(self):
        """Refund any wallet credit consumed by an invoice before cancelling
        it, so cancelling an optional/unpaid invoice (e.g. a declined
        upgrade) returns the customer's credit to their wallet (A4)."""
        self._saas_refund_wallet_credit()
        return super().button_cancel()

    def _saas_refund_wallet_credit(self):
        """Return wallet credit applied to these (unpaid) invoices. Idempotent
        — ``saas.wallet._refund_move`` only refunds what wasn't already
        refunded."""
        Wallet = self.env['saas.wallet'].sudo()
        for move in self:
            if move.move_type != 'out_invoice':
                continue
            if move.payment_state in ('paid', 'in_payment'):
                continue
            wallet = Wallet._for_partner(move.partner_id, create=False)
            if not wallet:
                continue
            # Partial payments (F5): only the still-unpaid residual is
            # refundable — the paid-down portion stays consumed.
            cap = move.amount_residual if move.payment_state == 'partial' else None
            wallet._refund_move(move, max_amount=cap)

    def _compute_payment_state(self):
        # Capture state before recomputation
        old_states = {inv.id: inv.payment_state for inv in self}
        super()._compute_payment_state()
        # Detect invoices that just became paid
        newly_paid = self.filtered(
            lambda m: m.payment_state in ('paid', 'in_payment')
            and old_states.get(m.id) not in ('paid', 'in_payment')
        )
        if newly_paid:
            newly_paid._saas_check_instance_payment()

        # Detect invoices that were paid but are now reversed/refunded
        newly_reversed = self.filtered(
            lambda m: m.payment_state in ('reversed', 'invoicing_legacy')
            and old_states.get(m.id) in ('paid', 'in_payment')
        )
        if newly_reversed:
            newly_reversed._saas_check_payment_reversal()

    def _saas_check_instance_payment(self):
        """Handle SaaS instance payments: deploy, upgrade, or restore."""
        paid_invoices = self.filtered(
            lambda m: m.payment_state in ('paid', 'in_payment')
        )
        if not paid_invoices:
            return

        # --- Handle daily-backup add-on payments ---
        # The customer clicked Enable Daily Backups, we created an
        # unpaid invoice and stored it on the instance. Now that the
        # invoice is paid, flip the feature on.
        backup_instances = self.env['saas.instance'].search([
            ('daily_backup_pending_invoice_id', 'in', paid_invoices.ids),
        ])
        for instance in backup_instances:
            _logger.info(
                "SaaS instance %s: daily-backup add-on paid (invoice %s), "
                "enabling daily backups.",
                instance.subdomain,
                instance.daily_backup_pending_invoice_id.name,
            )
            # Capture the tokenized card (if customer ticked "Save my
            # card") so the monthly renewal cron can auto-charge.
            instance._capture_payment_token_from_invoice(
                instance.daily_backup_pending_invoice_id,
            )
            # The add-on follows the PLAN's billing period: the customer just
            # paid a prorated catch-up to the plan's renewal date, so align
            # the add-on's next-invoice date to that renewal — from now on the
            # snapshot charge is merged into the plan renewal (one bill, same
            # period). Fall back to a full period from today if the instance
            # somehow has no active billing cycle.
            today = fields.Date.today()
            period = instance.billing_period or 'monthly'
            if instance.next_invoice_date and instance.next_invoice_date > today:
                next_invoice = instance.next_invoice_date
            else:
                next_invoice = today + instance.env[
                    'saas.pricing.engine'].period_delta(period)
            instance.write({
                'daily_backup_enabled': True,
                'daily_backup_pending_invoice_id': False,
                'daily_backup_last_invoice_date': today,
                'daily_backup_next_invoice_date': next_invoice,
            })
            instance._append_log(
                "Daily backups enabled — add-on payment received. "
                "Aligned to the plan renewal; next invoice: %s." % next_invoice
            )
            instance.message_post(body=_(
                "Daily Backups add-on paid. The next 03:00 UTC backup "
                "cron will create the first snapshot, and the charge will "
                "be included on your subscription renewal from now on."
            ))

        # --- Handle storage-block purchases (v47) ---
        block_instances = self.env['saas.instance'].search([
            ('storage_block_pending_invoice_id', 'in', paid_invoices.ids),
        ])
        for instance in block_instances:
            _logger.info(
                "SaaS instance %s: storage-block purchase paid (invoice %s), "
                "expanding capacity.", instance.subdomain,
                instance.storage_block_pending_invoice_id.name)
            instance._capture_payment_token_from_invoice(
                instance.storage_block_pending_invoice_id)
            instance._activate_pending_storage_blocks()

        # --- Handle Staging/Development slot RESERVATIONS (capacity buy) ---
        # The customer reserved more env slots (no server created); grant them
        # now that the prorated reservation invoice is paid.
        slot_reservers = self.env['saas.instance'].search([
            ('slot_reservation_pending_invoice_id', 'in', paid_invoices.ids),
        ])
        for prod in slot_reservers:
            _logger.info(
                "SaaS project %s: slot reservation invoice %s paid — granting "
                "slots.", prod.subdomain,
                prod.slot_reservation_pending_invoice_id.name)
            prod._capture_payment_token_from_invoice(
                prod.slot_reservation_pending_invoice_id)
            prod._activate_reserved_slots()

        # --- Handle Staging/Development environment activation (Odoo.sh) ---
        # The customer clicked "Create staging/dev server"; we created the
        # child record + a prorated activation invoice. Now that it's paid,
        # provision the server (it never gets its own billing cycle — the
        # recurring cost rides the Production renewal).
        env_children = self.env['saas.instance'].search([
            ('env_pending_invoice_id', 'in', paid_invoices.ids),
        ])
        for child in env_children:
            _logger.info(
                "SaaS env server %s: activation invoice %s paid — deploying.",
                child.subdomain, child.env_pending_invoice_id.name)
            child._activate_pending_environment()

        # --- Resume paused snapshots when a renewal is paid ---
        # A monthly add-on invoice going unpaid pauses snapshots
        # (daily_backup_suspended). When any payment lands, re-evaluate
        # currently-paused instances so they resume immediately rather
        # than waiting for the next daily cron tick.
        paused = self.env['saas.instance'].search([
            ('daily_backup_enabled', '=', True),
            ('daily_backup_suspended', '=', True),
        ])
        for instance in paused:
            instance._sync_daily_backup_suspension()

        # --- Handle restoration fee payments ---
        _logger.info(
            "Checking restoration invoices among paid: %s",
            paid_invoices.mapped('name'),
        )
        restoration_instances = self.env['saas.instance'].search([
            ('restoration_invoice_id', 'in', paid_invoices.ids),
        ])
        _logger.info(
            "Found %d instance(s) with restoration invoice: %s",
            len(restoration_instances),
            restoration_instances.mapped('subdomain'),
        )
        for instance in restoration_instances:
            _logger.info(
                "SaaS instance %s: restoration fee paid (invoice %s), triggering restore.",
                instance.subdomain,
                instance.restoration_invoice_id.name,
            )
            instance._append_log("Restoration fee paid. Starting data restore...")
            instance.message_post(body=_(
                "Restoration fee paid. Data restore triggered automatically."
            ))
            run_in_background(
                instance, '_do_paid_restore',
                error_method='_on_background_error',
                error_args=('running',),
                thread_name='saas_restore_%s' % instance.subdomain,
            )

        sale_orders = self.env['sale.order'].search([
            ('invoice_ids', 'in', paid_invoices.ids),
        ])

        # --- Handle new instance deployment (pending_payment → paid) ---
        instances = self.env['saas.instance'].search([
            ('sale_order_id', 'in', sale_orders.ids),
            ('state', '=', 'pending_payment'),
            ('pending_plan_id', '=', False),
        ]) if sale_orders else self.env['saas.instance']
        for instance in instances:
            instance.state = 'paid'
            instance._set_next_invoice_date()
            # Save the card (if customer ticked "Save my card") so
            # the recurring-billing cron can auto-renew.
            paid_for_instance = paid_invoices.filtered(
                lambda inv: inv.line_ids.sale_line_ids.order_id.id
                in instance.sale_order_id.ids
            )
            if paid_for_instance:
                instance._capture_payment_token_from_invoice(paid_for_instance[:1])
            instance._append_log("Payment received.")
            instance.message_post(
                body=_("Payment received. Deploying instance automatically."),
            )
            _logger.info(
                "SaaS instance %s: payment received, queuing auto-deploy.",
                instance.subdomain,
            )
            # Deploy in background thread — decoupled from the accounting write
            run_in_background(
                instance, '_do_deploy_after_payment',
                error_method='_on_background_error',
                error_args=('failed',),
                thread_name='saas_deploy_payment_%s' % instance.subdomain,
            )
            # Odoo.sh-style: spawn the Staging/Development servers the customer
            # chose at checkout (already billed on this initial invoice).
            if instance.environment == 'production' and (
                    instance.pending_staging_count or instance.pending_dev_count):
                try:
                    instance._spawn_pending_environments()
                except Exception:
                    _logger.exception(
                        "Failed to spawn pending environments for %s",
                        instance.subdomain)

        # --- Handle pending plan changes (trial upgrade or paid plan change) ---
        # Match by the explicit invoice link first (exact — survives
        # sale_order_id being overwritten between request and payment),
        # then by sale order for records created before that field existed.
        upgrade_instances = self.env['saas.instance'].search([
            ('pending_plan_id', '!=', False),
            '|',
            ('pending_change_invoice_id', 'in', paid_invoices.ids),
            ('sale_order_id', 'in', sale_orders.ids),
        ])
        for instance in upgrade_instances:
            # Pick the right method: trial upgrade or paid plan change
            if instance.is_trial:
                method = '_apply_pending_upgrade'
            else:
                method = '_apply_pending_plan_change'
            # Capture saved card if the upgrade payment carried a token —
            # gives customers without a previously-saved card a chance to
            # opt in here too.
            paid_for_instance = paid_invoices.filtered(
                lambda inv: inv.line_ids.sale_line_ids.order_id.id
                in instance.sale_order_id.ids
            )
            if paid_for_instance:
                instance._capture_payment_token_from_invoice(paid_for_instance[:1])
            _logger.info(
                "SaaS instance %s: payment received, applying %s.",
                instance.subdomain, method,
            )
            run_in_background(
                instance, method,
                error_method='_on_background_error',
                error_args=(),
                thread_name='saas_planchange_%s' % instance.subdomain,
            )

    def _saas_check_payment_reversal(self):
        """Suspend running SaaS instances when a payment is reversed.

        This catches refund fraud: a customer pays, gets the instance
        deployed, then reverses the payment (chargeback / credit note).
        """
        reversed_invoices = self.filtered(
            lambda m: m.payment_state in ('reversed', 'invoicing_legacy')
        )
        if not reversed_invoices:
            return

        sale_orders = self.env['sale.order'].search([
            ('invoice_ids', 'in', reversed_invoices.ids),
        ])
        if not sale_orders:
            return

        instances = self.env['saas.instance'].search([
            ('sale_order_id', 'in', sale_orders.ids),
            ('state', '=', 'running'),
        ])
        for instance in instances:
            _logger.warning(
                "SaaS instance %s: payment reversed on invoice(s) %s — suspending.",
                instance.subdomain,
                ', '.join(reversed_invoices.mapped('name')),
            )
            instance._append_log(
                "PAYMENT REVERSED — instance suspended automatically."
            )
            instance.message_post(body=_(
                "Payment reversed (refund/chargeback). Instance suspended."
            ))
            try:
                instance.action_suspend()
            except Exception:
                _logger.exception(
                    "Failed to suspend instance %s after payment reversal",
                    instance.subdomain,
                )
