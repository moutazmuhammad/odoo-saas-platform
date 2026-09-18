from odoo import fields, models


class SaasInstance(models.Model):
    """Billing-side fields whose comodel lives in this addon.

    These two fields were declared directly on saas.instance in
    saas_core until saas.support.plan / saas.payment.method moved here —
    a Many2one's comodel must belong to an already-loaded module at
    _auto_init time (saas_billing depends on saas_core, so saas_core
    loads first), so the field declarations had to move too. Every
    method that reads/writes them (in saas_core/models/saas_instance.py)
    is untouched: Odoo merges _inherit contributions into one class, so
    self.support_plan_id / self.saas_payment_method_id keep working
    from core's own code exactly as before.
    """
    _inherit = 'saas.instance'

    support_plan_id = fields.Many2one(
        'saas.support.plan',
        string='Support Plan',
        ondelete='restrict',
        default=lambda self: self.env['saas.support.plan']._get_default(),
        help='Paid support tier for this instance (P3). A flat monthly fee '
             'billed alongside the plan; not scaled by region. Defaults to '
             'the free best-effort tier; the customer can pick a higher one '
             'at create / upgrade.',
    )

    # Saved payment method chosen for auto-renew (A1). Provider-agnostic
    # wrapper that stores ONLY safe references (provider id + external
    # customer / token refs); the legacy ``payment_token_id`` is kept in
    # sync for the existing charge path.
    saas_payment_method_id = fields.Many2one(
        'saas.payment.method', string='Auto-renew Payment Method',
        copy=False, ondelete='set null',
    )
