# Manage payment methods and automatic renewal

Control automatic charges while keeping renewal invoices in view.

Reviewed: 2026-10-05

## Saved payment methods

Saved payment methods depend on the provider’s tokenization and the payment flow. The Settings page lists available methods and the default method. Removing a method disables its use for later automatic charges; it does not cancel an invoice or subscription. Check the provider’s saving option during checkout rather than assuming every payment saves a card.

## Automatic renewal

The owner can control automatic renewal separately for the subscription and daily backups where enabled. Turning auto-renew off stops automatic charging, not invoice generation. You must still settle renewal invoices or cancel the service through its cancellation workflow.

## Failed renewal payments

The default workflow sends reminders 7 and 1 days before renewal, attempts renewal payment, and retries unpaid charges 1, 3, and 5 days after the due date. Main-subscription suspension uses the platform’s grace policy; daily-backup invoices have a separate pause policy. Exact schedules and delivery depend on configured jobs and mail. Review outstanding invoices and contact support if a restriction remains after payment.

## Related guides

- [View and pay invoices](invoices.md)
- [Enable automatic daily snapshots](daily-backups.md)
- [Cancel and reactivate a project](reactivate.md)
