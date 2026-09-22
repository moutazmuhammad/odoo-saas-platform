"""Step 5 of the billing split: saas.instance's commercial half (sale
order / invoice links, billing period, pending/scheduled plan changes,
renewal + auto-renew state, add-on activation invoices, the per-tenant
margin fields), the retained-backup restore wizard, the Profitability
views/menu/cron and the billing mail templates moved from saas_core to
this addon.

Tables and data are untouched — only ownership metadata changes. This
runs after saas_core has loaded (and no longer declares any of these)
but before ir.model.data._process_end, which would otherwise treat the
saas_core-owned xmlids as removed and unlink the records — for
ir.model.fields that means dropping the columns and their data.
"""
import logging

_logger = logging.getLogger(__name__)

_INSTANCE_FIELDS = (
    'sale_order_id', 'restoration_invoice_id', 'sale_order_count',
    'invoice_count', 'daily_backup_pending_invoice_id',
    'daily_backup_next_invoice_date', 'daily_backup_last_invoice_date',
    'pending_compute_tier_id', 'compute_tier_pending_invoice_id',
    'payment_token_id', 'auto_renew_subscription', 'auto_renew_daily_backup',
    'billing_period', 'pending_plan_id', 'pending_billing_period',
    'pending_change_invoice_id', 'scheduled_plan_id',
    'scheduled_billing_period', 'next_invoice_date', 'last_invoice_date',
    'suspension_warning_sent', 'renewal_reminder_7d_sent',
    'renewal_reminder_1d_sent', 'pending_wallet_credit',
    'pending_storage_blocks', 'storage_block_pending_invoice_id',
    'reserved_staging_pending', 'reserved_dev_pending',
    'slot_reservation_pending_invoice_id', 'env_pending_invoice_id',
    'margin_currency_id', 'monthly_cost', 'monthly_revenue',
    'monthly_margin', 'margin_pct', 'is_profitable',
)

_EXACT_NAMES = (
    'view_saas_instance_margin_list',
    'view_saas_instance_margin_pivot',
    'view_saas_instance_margin_graph',
    'action_saas_tenant_margins',
    'saas_master_menu_dashboard',
    'action_restore_retained_wizard',
    'mail_template_saas_payment_due',
    'mail_template_saas_payment_cancelled',
    'mail_template_saas_renewal_reminder',
)

_LIKE_PATTERNS = (
    # the wizard's model, fields, access rule and form view
    '%saas_restore_retained_wizard%',
    # the cron and its implicit ir.actions.server parent
    'ir_cron_saas_margin_alert%',
)


def migrate(cr, version):
    names = list(_EXACT_NAMES)
    names += ['field_saas_instance__%s' % f for f in _INSTANCE_FIELDS]
    like = list(_LIKE_PATTERNS)
    like += ['selection__saas_instance__%s__%%' % f for f in _INSTANCE_FIELDS]
    cr.execute("""
        UPDATE ir_model_data SET module = 'saas_billing'
        WHERE module = 'saas_core'
          AND (name = ANY(%s) OR name LIKE ANY(%s))
          AND NOT EXISTS (
              SELECT 1 FROM ir_model_data o
              WHERE o.module = 'saas_billing' AND o.name = ir_model_data.name)
        RETURNING name
    """, (names, like))
    moved = [r[0] for r in cr.fetchall()]
    _logger.info(
        "saas_billing 18.0.2.0.0: re-parented %d ir_model_data row(s) from "
        "saas_core: %s", len(moved), ', '.join(sorted(moved)))
