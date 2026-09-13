import datetime
import logging
from dateutil.relativedelta import relativedelta
from urllib.parse import quote as url_quote

import werkzeug.utils

from odoo import fields, http, _
from odoo.exceptions import AccessError, MissingError, UserError
from odoo.http import request

from odoo.addons.portal.controllers.portal import CustomerPortal
from odoo.addons.portal.controllers.portal import pager as portal_pager
from odoo.addons.payment.controllers.portal import PaymentPortal
from odoo.addons.saas_core.models.saas_instance_backup import DEFAULT_MAX_BACKUPS

_logger = logging.getLogger(__name__)


class SaasPortal(CustomerPortal):

    # The native Odoo portal account page is off-brand vs. the SPA. Send
    # customers to our themed Settings page instead (same /my/account route,
    # inherited via empty @http.route()).
    @http.route()
    def account(self, redirect=None, **post):
        return request.redirect('/my/settings')

    # Only show paid orders to customers in the portal.
    # Unpaid/cancelled orders are internal artifacts from plan changes.
    def _prepare_orders_domain(self, partner):
        domain = super()._prepare_orders_domain(partner)
        # Only include orders where at least one invoice is paid
        domain.append(('invoice_ids.payment_state', 'in', ('paid', 'in_payment')))
        return domain

    # States visible to clients in the portal
    _PORTAL_VISIBLE_STATES = (
        'draft', 'pending_payment', 'paid', 'pending_provision',
        'provisioning', 'running', 'stopped', 'suspended', 'failed',
        'cancelled', 'cancelled_by_client',
    )

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if 'instance_count' in counters:
            partner = request.env.user.partner_id
            values['instance_count'] = request.env['saas.instance'].sudo().search_count([
                ('partner_id', '=', partner.id),
                ('state', 'in', self._PORTAL_VISIBLE_STATES),
            ])
        return values

    # ==================== Instance List ====================


    # ==================== Instance Detail ====================


    # ==================== Hosting: Database Management ====================
    # These routes proxy to the instance's own /jsonrpc using the stored
    # admin master password (see saas.instance.hosting_db_*). The
    # customer never sees the master password.

    def _hosting_instance(self, instance_id, access_token=None):
        """Return an authorized sudo() recordset for a hosting instance.

        Raises a redirect-like response or AccessError when the request
        is invalid. Callers handle ``AccessError`` / ``MissingError``.
        """
        instance = self._document_check_access(
            'saas.instance', instance_id, access_token=access_token,
        )
        if not instance.is_hosting:
            raise AccessError(_(
                "Database management is only available for hosting instances."
            ))
        return instance


    @http.route(
        '/my/instances/<int:instance_id>/databases/create',
        type='http', auth='user', website=True,
        methods=['POST'], csrf=True,
    )
    def portal_instance_db_create(self, instance_id, access_token=None,
                                  **post):
        try:
            instance = self._hosting_instance(instance_id, access_token)
        except (AccessError, MissingError):
            return request.redirect('/my/instances')

        try:
            op = instance.hosting_db_create_async(
                name=post.get('name', ''),
                login=post.get('login', ''),
                password=post.get('password', ''),
                lang=post.get('lang') or 'en_US',
                country_code=post.get('country_code') or None,
            )
            notice = _(
                "Creating database '%s'… this takes about a minute. "
                "Refresh the page to see its progress."
            ) % op.db_name
            return request.redirect(
                '/my/instances/%d/databases?notice=%s'
                % (instance_id, url_quote(notice))
            )
        except UserError as e:
            return request.redirect(
                '/my/instances/%d/databases?error=%s'
                % (instance_id, url_quote(str(e)))
            )

    @http.route(
        '/my/instances/<int:instance_id>/databases/duplicate',
        type='http', auth='user', website=True,
        methods=['POST'], csrf=True,
    )
    def portal_instance_db_duplicate(self, instance_id, access_token=None,
                                     **post):
        try:
            instance = self._hosting_instance(instance_id, access_token)
        except (AccessError, MissingError):
            return request.redirect('/my/instances')

        try:
            op = instance.hosting_db_duplicate_async(
                source=post.get('source', ''),
                new_name=post.get('new_name', ''),
            )
            notice = _(
                "Duplicating to '%s'… refresh to see progress."
            ) % op.db_name
            return request.redirect(
                '/my/instances/%d/databases?notice=%s'
                % (instance_id, url_quote(notice))
            )
        except UserError as e:
            return request.redirect(
                '/my/instances/%d/databases?error=%s'
                % (instance_id, url_quote(str(e)))
            )

    @http.route(
        '/my/instances/<int:instance_id>/databases/op/<int:op_id>/dismiss',
        type='http', auth='user', website=True,
        methods=['POST'], csrf=True,
    )
    def portal_instance_db_op_dismiss(self, instance_id, op_id,
                                      access_token=None, **post):
        """Remove a DB-op tracking row from view.

        Works for any state — including ``running``, where it means
        "I don't care anymore, get this banner off my screen". The
        underlying background thread (if still alive) will keep
        running and eventually try to write to a now-deleted row;
        ``unlink()`` cascades cleanly so that's a no-op.

        Doesn't touch the database itself: if the create succeeded
        between the customer clicking Cancel and the row being
        deleted, the new DB just appears on next refresh. If it
        failed, no harm done.
        """
        try:
            instance = self._hosting_instance(instance_id, access_token)
        except (AccessError, MissingError):
            return request.redirect('/my/instances')
        op = request.env['saas.instance.db.operation'].sudo().browse(op_id)
        redirect = '/my/instances/%d/databases' % instance_id
        if not op.exists() or op.instance_id != instance:
            return request.redirect(redirect)
        op.unlink()
        return request.redirect(redirect)

    @http.route(
        '/my/instances/<int:instance_id>/databases/drop',
        type='http', auth='user', website=True,
        methods=['POST'], csrf=True,
    )
    def portal_instance_db_drop(self, instance_id, access_token=None,
                                **post):
        try:
            instance = self._hosting_instance(instance_id, access_token)
        except (AccessError, MissingError):
            return request.redirect('/my/instances')
        # Defensive: require the customer to retype the DB name to confirm.
        # Without this an errant click could nuke a production DB.
        name = (post.get('name') or '').strip()
        confirm = (post.get('confirm') or '').strip()
        if not name or name != confirm:
            return request.redirect(
                '/my/instances/%d/databases?error=%s'
                % (instance_id, url_quote(_(
                    "Type the database name exactly to confirm deletion."
                )))
            )
        try:
            op = instance.hosting_db_drop_async(name=name)
            notice = _("Deleting database '%s'…") % op.db_name
            return request.redirect(
                '/my/instances/%d/databases?notice=%s'
                % (instance_id, url_quote(notice))
            )
        except UserError as e:
            return request.redirect(
                '/my/instances/%d/databases?error=%s'
                % (instance_id, url_quote(str(e)))
            )

    @http.route(
        '/my/instances/<int:instance_id>/databases/upgrade-module',
        type='http', auth='user', website=True,
        methods=['POST'], csrf=True,
    )
    def portal_instance_db_upgrade_module(
        self, instance_id, access_token=None, **post,
    ):
        """Queue an ``odoo -u <module>`` recovery upgrade.

        Surfaces via the running-ops banner like create/duplicate/drop;
        when finished, the op record carries the captured stdout/stderr
        on ``output_log`` so the customer can read what happened.
        """
        try:
            instance = self._hosting_instance(instance_id, access_token)
        except (AccessError, MissingError):
            return request.redirect('/my/instances')
        name = (post.get('name') or '').strip()
        module = (post.get('module') or '').strip().lower()
        if not name or not module:
            return request.redirect(
                '/my/instances/%d/databases?error=%s'
                % (instance_id, url_quote(_(
                    "Both database and module are required."
                )))
            )
        try:
            op = instance.hosting_db_upgrade_module_async(
                name=name, module=module,
            )
        except UserError as e:
            return request.redirect(
                '/my/instances/%d/databases?error=%s'
                % (instance_id, url_quote(str(e)))
            )
        notice = _(
            "Repairing '%(module)s' on '%(db)s'… your instance will "
            "come back up automatically. This page auto-refreshes."
        ) % {'module': op.module_name, 'db': op.db_name}
        return request.redirect(
            '/my/instances/%d/databases?notice=%s'
            % (instance_id, url_quote(notice))
        )

    @http.route(
        '/my/instances/<int:instance_id>/databases/reset-admin-password',
        type='http', auth='user', website=True,
        methods=['POST'], csrf=True,
    )
    def portal_instance_db_reset_admin_password(
        self, instance_id, access_token=None, **post,
    ):
        """Reset the admin password on one of this instance's databases.

        Customer self-service for "I forgot my admin password". The
        actual work happens inside the customer's container via
        ``hosting_db_reset_admin_password``; we just validate the form
        and surface success / error as a portal banner.
        """
        try:
            instance = self._hosting_instance(instance_id, access_token)
        except (AccessError, MissingError):
            return request.redirect('/my/instances')
        name = (post.get('name') or '').strip()
        new_password = post.get('new_password') or ''
        confirm = post.get('confirm_password') or ''
        err_redirect = '/my/instances/%d/databases?error=%s'
        if not name:
            return request.redirect(err_redirect % (
                instance_id, url_quote(_("Pick a database to reset.")),
            ))
        if not new_password or new_password != confirm:
            return request.redirect(err_redirect % (
                instance_id, url_quote(_(
                    "New password and confirmation don't match."
                )),
            ))
        try:
            login = instance.hosting_db_reset_admin_password(
                name=name, new_password=new_password,
            )
        except UserError as e:
            return request.redirect(err_redirect % (
                instance_id, url_quote(str(e)),
            ))
        notice = _(
            "Admin password reset for '%(db)s'. Sign in as "
            "'%(login)s' with your new password."
        ) % {'db': name, 'login': login}
        return request.redirect(
            '/my/instances/%d/databases?notice=%s'
            % (instance_id, url_quote(notice))
        )

    # ==================== Deployment Status Polling ====================

    @http.route(
        '/my/instances/<int:instance_id>/status',
        type='json', auth='user', website=True,
    )
    def portal_instance_status(self, instance_id, access_token=None, **kw):
        """JSON endpoint for polling deployment status."""
        try:
            instance_sudo = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            return {'error': 'Access denied'}
        return instance_sudo._get_status_dict()

    # ==================== Portal Self-Service Actions ====================

    @http.route(
        '/my/instances/<int:instance_id>/restart',
        type='json', auth='user', website=True,
    )
    def portal_instance_restart(self, instance_id, access_token=None, **kw):
        """Restart an instance from the portal."""
        try:
            instance_sudo = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            return {'error': 'Access denied'}
        try:
            instance_sudo.action_portal_restart()
            return {'success': True, 'message': _('Instance restart initiated.')}
        except UserError as e:
            return {'error': str(e)}

    @http.route(
        '/my/instances/<int:instance_id>/stop',
        type='json', auth='user', website=True,
    )
    def portal_instance_stop(self, instance_id, access_token=None, **kw):
        """Stop an instance from the portal."""
        try:
            instance_sudo = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            return {'error': 'Access denied'}
        try:
            instance_sudo.action_portal_stop()
            return {'success': True, 'message': _('Instance stop initiated.')}
        except UserError as e:
            return {'error': str(e)}

    @http.route(
        '/my/instances/<int:instance_id>/start',
        type='json', auth='user', website=True,
    )
    def portal_instance_start(self, instance_id, access_token=None, **kw):
        """Start a stopped instance from the portal."""
        try:
            instance_sudo = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            return {'error': 'Access denied'}
        try:
            instance_sudo.action_portal_start()
            return {'success': True, 'message': _('Instance start initiated.')}
        except UserError as e:
            return {'error': str(e)}

    # ==================== Plan Upgrade (Trial → Paid) ====================

    @http.route(
        '/my/instances/<int:instance_id>/upgrade',
        type='http', auth='user', website=True,
    )
    def portal_instance_upgrade(self, instance_id, access_token=None, **kw):
        """Show custom plan builder for upgrading a trial instance."""
        try:
            instance_sudo = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            return request.redirect('/my/instances')

        if not instance_sudo.is_trial or instance_sudo.state not in ('running', 'stopped'):
            return request.redirect('/my/instances/%s' % instance_id)

        from odoo.addons.saas_website.controllers.main import SaasWebsite
        if instance_sudo.is_hosting:
            custom_config = SaasWebsite._get_hosting_plan_config(self)
        else:
            custom_config = SaasWebsite._get_custom_plan_config(self)

        values = self._prepare_portal_layout_values()
        values.update({
            'instance': instance_sudo,
            'custom_config': custom_config,
            'page_name': 'saas_instance_upgrade',
            'error': kw.get('error'),
            'product_id': instance_sudo.saas_product_id.id if instance_sudo.saas_product_id else 0,
        })
        return request.render('saas_website.portal_instance_upgrade', values)

    @http.route(
        '/my/instances/<int:instance_id>/subscribe',
        type='http', auth='user', website=True, methods=['POST'],
    )
    def portal_instance_subscribe(self, instance_id, access_token=None, **kw):
        """Process custom plan subscription from trial."""
        try:
            instance_sudo = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            return request.redirect('/my/instances')

        workers = int(kw.get('workers', 0))
        storage = int(kw.get('storage', 0))
        billing_period = kw.get('billing_period', 'monthly')
        if billing_period not in ('monthly', 'yearly'):
            billing_period = 'monthly'

        if not workers or not storage:
            return request.redirect(
                '/my/instances/%s/upgrade?error=%s' % (
                    instance_id, url_quote('Please configure your plan.'))
            )

        # Create the custom plan (use hosting config for hosting instances)
        from odoo.addons.saas_website.controllers.main import SaasWebsite
        if instance_sudo.is_hosting:
            config = SaasWebsite._get_hosting_plan_config(self)
        else:
            config = SaasWebsite._get_custom_plan_config(self)
        workers = max(config['min_workers'], min(workers, config['max_workers']))
        storage = max(config['min_storage'], min(storage, config['max_storage']))

        if instance_sudo.is_hosting:
            plan = SaasWebsite._get_or_create_hosting_plan(
                self, instance_sudo.saas_product_id, workers, storage, config,
                region=instance_sudo.region_id,
            )
        else:
            plan = SaasWebsite._get_or_create_custom_plan(
                self, instance_sudo.saas_product_id, workers, storage, config,
                region=instance_sudo.region_id,
            )

        try:
            result = instance_sudo.action_subscribe_from_trial(
                plan.id, billing_period=billing_period,
            )
        except UserError as e:
            return request.redirect(
                '/my/instances/%s/upgrade?error=%s' % (instance_id, url_quote(str(e)))
            )

        # If result is an invoice, redirect to checkout page
        if hasattr(result, 'get_portal_url') and result.amount_total > 0:
            return request.redirect('/my/instances/%s/checkout' % instance_id)

        # Zero-amount or boolean True — go back to instance detail
        return request.redirect('/my/instances/%s' % instance_id)

    # ==================== Checkout Page ====================

    @http.route(
        '/my/instances/<int:instance_id>/checkout',
        type='http', auth='user', website=True,
    )
    def portal_checkout(self, instance_id, access_token=None, **kw):
        """Show a checkout page with order summary and embedded payment form."""
        try:
            instance_sudo = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            return request.redirect('/my/instances')

        # Find the unpaid invoice — check restoration invoice first,
        # then the regular sale order invoices.
        invoice = None

        # Restoration invoice takes priority
        if (instance_sudo.restoration_invoice_id
                and instance_sudo.restoration_invoice_id.state == 'posted'
                and instance_sudo.restoration_invoice_id.payment_state not in ('paid', 'in_payment')
                and instance_sudo.restoration_invoice_id.amount_residual > 0):
            invoice = instance_sudo.restoration_invoice_id

        # Regular sale order invoices
        if not invoice and instance_sudo.sale_order_id and instance_sudo.sale_order_id.invoice_ids:
            for inv in instance_sudo.sale_order_id.invoice_ids.sorted('create_date', reverse=True):
                if (inv.state == 'posted'
                        and inv.payment_state not in ('paid', 'in_payment')
                        and inv.amount_residual > 0):
                    invoice = inv
                    break

        # Mid-cycle add-on purchases (env slot reservation, storage block) hang
        # their prorated invoice on a dedicated field, NOT the main sale order —
        # so the generic checkout must look there too, otherwise the page finds
        # nothing and bounces back (the "Reserve doesn't work" symptom).
        if not invoice:
            for pend in (
                instance_sudo.slot_reservation_pending_invoice_id,
                instance_sudo.storage_block_pending_invoice_id,
            ):
                if (pend and pend.state == 'posted'
                        and pend.payment_state not in ('paid', 'in_payment')
                        and pend.amount_residual > 0):
                    invoice = pend
                    break

        if not invoice:
            # No unpaid invoice — nothing to pay, go to instance page
            return request.redirect('/my/instances/%s' % instance_id)

        # Get the plan being purchased (pending or current)
        target_plan = instance_sudo.pending_plan_id or instance_sudo.plan_id

        # Prepare payment form values (same approach as account_payment)
        partner_sudo = request.env.user.partner_id
        invoice_company = invoice.company_id or request.env.company
        landing_route = '/my/instances/%s?payment=success' % instance_id

        availability_report = {}
        providers_sudo = request.env['payment.provider'].sudo()._get_compatible_providers(
            invoice_company.id,
            partner_sudo.id,
            invoice.amount_residual,
            currency_id=invoice.currency_id.id,
            report=availability_report,
        )
        payment_methods_sudo = request.env['payment.method'].sudo()._get_compatible_payment_methods(
            providers_sudo.ids,
            partner_sudo.id,
            currency_id=invoice.currency_id.id,
            report=availability_report,
        )
        # We do NOT retain customer card data: never offer saved cards to
        # reuse, and (below) never show the "Save my card" checkbox.
        tokens_sudo = request.env['payment.token'].sudo().browse()

        # Get the invoice's portal access token
        invoice_access_token = invoice._portal_ensure_token()

        # Compute proration details for display
        proration_credit = 0.0
        proration_remaining_days = 0
        original_plan_name = ''
        target_full_price = target_plan._get_price_for_period(
            instance_sudo.pending_billing_period or instance_sudo.billing_period or 'monthly'
        ) if target_plan else 0
        if instance_sudo.plan_id and instance_sudo.pending_plan_id:
            # This is an upgrade — calculate what was credited
            old_period = instance_sudo.billing_period or 'monthly'
            old_price = instance_sudo.plan_id._get_price_for_period(old_period)
            original_plan_name = instance_sudo.plan_id.name
            # Use the authoritative backend proration so the displayed quote
            # matches the invoice the customer is actually charged
            # (BILL-V2-001 — no undocumented "-2").
            proration_credit, proration_remaining_days, _td = \
                instance_sudo._proration_credit(old_price)

        values = self._prepare_portal_layout_values()
        values.update({
            'instance': instance_sudo,
            'invoice': invoice,
            'target_plan': target_plan,
            'page_name': 'saas_checkout',
            'proration_credit': proration_credit,
            'proration_remaining_days': proration_remaining_days,
            'original_plan_name': original_plan_name,
            'target_full_price': target_full_price,
            # Payment form context (required by payment.form template)
            'amount': invoice.amount_residual,
            'currency': invoice.currency_id,
            'partner_id': partner_sudo.id,
            'providers_sudo': providers_sudo,
            'payment_methods_sudo': payment_methods_sudo,
            'tokens_sudo': tokens_sudo,
            'availability_report': availability_report,
            'transaction_route': '/invoice/transaction/%d' % invoice.id,
            'landing_route': landing_route,
            'access_token': invoice_access_token,
            # False for EVERY provider -> no "Save my card" checkbox.
            # payment.method_form indexes this dict for any tokenization-
            # capable provider, so an empty mapping KeyErrors as soon as
            # one is enabled — each compatible provider needs an entry.
            'show_tokenize_input_mapping': {p.id: False for p in providers_sudo},
            'company_mismatch': not PaymentPortal._can_partner_pay_in_company(
                partner_sudo, invoice_company
            ),
            'expected_company': invoice_company,
            'invoice_id': invoice.id,
            'support_email': request.env['ir.config_parameter'].sudo().get_param(
                'saas_master.support_email', ''
            ),
        })
        return request.render('saas_website.portal_checkout', values)

    # ==================== Change Plan (Paid → Paid) ====================

    @http.route(
        '/my/instances/<int:instance_id>/change-plan',
        type='http', auth='user', website=True,
    )
    def portal_instance_change_plan(self, instance_id, access_token=None, **kw):
        """Show custom plan builder for changing a paid instance's plan."""
        try:
            instance_sudo = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            return request.redirect('/my/instances')

        if instance_sudo.is_trial or instance_sudo.state not in ('running', 'stopped', 'suspended'):
            return request.redirect('/my/instances/%s' % instance_id)

        from odoo.addons.saas_website.controllers.main import SaasWebsite
        if instance_sudo.is_hosting:
            custom_config = SaasWebsite._get_hosting_plan_config(self)
        else:
            custom_config = SaasWebsite._get_custom_plan_config(self)

        current_plan = instance_sudo.plan_id
        current_workers = current_plan.workers if current_plan else 2
        current_storage = int(current_plan.storage_limit) if current_plan else 5

        # Proration info for display — via the authoritative backend helper so
        # the quote matches the invoice (BILL-V2-001, no "-2").
        old_period = instance_sudo.billing_period or 'monthly'
        old_price = current_plan._get_price_for_period(old_period) if current_plan else 0
        remaining_value, remaining_days, _td = (
            instance_sudo._proration_credit(old_price) if current_plan
            else (0.0, 0, 0))

        values = self._prepare_portal_layout_values()
        values.update({
            'instance': instance_sudo,
            'current_plan': current_plan,
            'current_workers': current_workers,
            'current_storage': current_storage,
            'custom_config': custom_config,
            'remaining_value': remaining_value,
            'remaining_days': remaining_days,
            'old_price': old_price,
            'old_period': old_period,
            'page_name': 'saas_instance_change_plan',
            'error': kw.get('error'),
        })
        return request.render('saas_website.portal_instance_change_plan', values)

    @http.route(
        '/my/instances/<int:instance_id>/do-change-plan',
        type='http', auth='user', website=True, methods=['POST'],
    )
    def portal_instance_do_change_plan(self, instance_id, access_token=None, **kw):
        """Process a custom plan change.

        Rules:
        - Storage cannot be reduced (hard block).
        - Worker reduction is scheduled for next billing cycle (downgrade).
        - Worker increase / storage increase is an immediate upgrade.
        """
        try:
            instance_sudo = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            return request.redirect('/my/instances')

        new_workers = int(kw.get('workers', 0))
        new_storage = int(kw.get('storage', 0))
        billing_period = kw.get('billing_period', instance_sudo.billing_period or 'monthly')
        if billing_period not in ('monthly', 'yearly'):
            billing_period = 'monthly'

        current_plan = instance_sudo.plan_id
        current_workers = current_plan.workers if current_plan else 0
        current_storage = int(current_plan.storage_limit) if current_plan else 0

        err_redirect = '/my/instances/%s/change-plan?error=%s'

        # Block storage reduction
        if new_storage < current_storage:
            return request.redirect(err_redirect % (
                instance_id,
                url_quote(_("Storage cannot be reduced. Current storage: %d GB.") % current_storage),
            ))

        if not new_workers or not new_storage:
            return request.redirect(err_redirect % (
                instance_id, url_quote(_("Please configure your plan.")),
            ))

        # Clamp to config limits
        from odoo.addons.saas_website.controllers.main import SaasWebsite
        if instance_sudo.is_hosting:
            config = SaasWebsite._get_hosting_plan_config(self)
        else:
            config = SaasWebsite._get_custom_plan_config(self)
        new_workers = max(config['min_workers'], min(new_workers, config['max_workers']))
        new_storage = max(current_storage, min(new_storage, config['max_storage']))

        if (new_workers == current_workers
                and new_storage == current_storage
                and billing_period == (instance_sudo.billing_period or 'monthly')):
            return request.redirect(err_redirect % (
                instance_id, url_quote(_("No changes selected.")),
            ))

        # Find or create the new plan
        if instance_sudo.is_hosting:
            new_plan = SaasWebsite._get_or_create_hosting_plan(
                self, instance_sudo.saas_product_id,
                new_workers, new_storage, config,
                region=instance_sudo.region_id,
            )
        else:
            new_plan = SaasWebsite._get_or_create_custom_plan(
                self, instance_sudo.saas_product_id,
                new_workers, new_storage, config,
                region=instance_sudo.region_id,
            )

        try:
            if new_workers < current_workers:
                # Worker reduction: always schedule for next billing cycle
                result = instance_sudo._request_downgrade(new_plan, billing_period)
            else:
                # Workers same or increased (storage can only increase):
                # use normal plan change flow
                result = instance_sudo.action_request_plan_change(
                    new_plan.id, billing_period=billing_period,
                )
        except UserError as e:
            return request.redirect(err_redirect % (
                instance_id, url_quote(str(e)),
            ))

        # Upgrade: redirect to checkout
        if hasattr(result, 'get_portal_url') and result.amount_total > 0:
            return request.redirect('/my/instances/%s/checkout' % instance_id)

        # Downgrade scheduled or zero-charge upgrade — back to detail
        return request.redirect('/my/instances/%s' % instance_id)

    # ==================== Cancel Scheduled Downgrade ====================

    @http.route(
        '/my/instances/<int:instance_id>/cancel-upgrade',
        type='http', auth='user', website=True, methods=['POST'],
    )
    def portal_cancel_upgrade(self, instance_id, access_token=None, **kw):
        """Cancel a pending plan upgrade and its unpaid invoice."""
        try:
            instance_sudo = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            return request.redirect('/my/instances')
        if instance_sudo.pending_plan_id:
            instance_sudo._cancel_pending_upgrade()
        return request.redirect('/my/instances/%s' % instance_id)

    @http.route(
        '/my/instances/<int:instance_id>/cancel-downgrade',
        type='http', auth='user', website=True, methods=['POST'],
    )
    def portal_cancel_downgrade(self, instance_id, access_token=None, **kw):
        """Cancel a scheduled plan downgrade."""
        try:
            instance_sudo = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            return request.redirect('/my/instances')
        instance_sudo.action_cancel_scheduled_downgrade()
        return request.redirect('/my/instances/%s' % instance_id)

    # ==================== Cancel Unpaid Invoice ====================

    @http.route(
        '/my/instances/<int:instance_id>/cancel-invoice/<int:invoice_id>',
        type='http', auth='user', website=True, methods=['POST'],
    )
    def portal_cancel_invoice(self, instance_id, invoice_id, access_token=None, **kw):
        """Cancel an unpaid invoice and clean up the pending upgrade."""
        try:
            instance_sudo = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            return request.redirect('/my/instances')

        # Only allow cancelling unpaid invoices belonging to this instance
        if not instance_sudo.sale_order_id:
            return request.redirect('/my/instances/%s' % instance_id)
        invoice = instance_sudo.sale_order_id.invoice_ids.filtered(
            lambda inv: inv.id == invoice_id
                        and inv.state == 'posted'
                        and inv.payment_state not in ('paid', 'in_payment')
        )
        if not invoice:
            return request.redirect('/my/instances/%s' % instance_id)

        # Block cancellation of mandatory invoices.
        # Only optional invoices (plan upgrades, subscriptions) can be
        # cancelled by the client. Initial subscriptions, renewals and
        # restorations are mandatory — the dunning system enforces payment.
        # invoice_origin is the SO name (e.g. S00123), so we trace back
        # to the sale order's origin field.
        #
        # We match the canonical untranslated tokens written by saas_core
        # (SAAS:INITIAL:, SAAS:RENEWAL:, SAAS:RESTORATION:) AND keep the
        # legacy translated prefixes for backwards compatibility with
        # historical SOs created before the token migration.
        so_origins = invoice.line_ids.sale_line_ids.order_id.mapped('origin')
        non_cancellable_prefixes = (
            'SAAS:INITIAL:',
            'SAAS:RENEWAL:',
            'SAAS:RESTORATION:',
            # Legacy translated prefixes (any locale starting with these
            # English forms; non-English locales will simply not match
            # here, which is the same behaviour as before the token fix).
            'Renewal:',
            'Data restoration:',
        )
        if any(
            o and any(o.startswith(prefix) for prefix in non_cancellable_prefixes)
            for o in so_origins
        ):
            return request.redirect('/my/instances/%s' % instance_id)

        # Cancel the invoice
        invoice.button_cancel()

        # Clear pending upgrade if this was the related invoice
        if instance_sudo.pending_plan_id:
            instance_sudo._append_log("Pending upgrade cancelled by client.")
            from markupsafe import Markup
            instance_sudo.message_post(body=Markup(
                "<b>Client cancelled plan upgrade payment</b><br/>"
                "Was upgrading to: <b>%s</b> (%s)<br/>"
                "Invoice: %s — %s %s<br/>"
                "Current plan: %s"
            ) % (
                instance_sudo.pending_plan_id.name,
                instance_sudo.pending_billing_period or instance_sudo.billing_period or 'monthly',
                invoice.name,
                invoice.currency_id.symbol if invoice.currency_id else '',
                '%.2f' % invoice.amount_total,
                instance_sudo.plan_id.name if instance_sudo.plan_id else 'N/A',
            ))
            # Send notification before clearing pending plan so template can access it
            instance_sudo._send_notification(
                'saas_core.mail_template_saas_payment_cancelled',
            )
            instance_sudo.write({
                'pending_plan_id': False,
                'pending_billing_period': False,
            })
            return request.redirect('/my/instances/%s' % instance_id)

        # If instance was never deployed (pending_payment or draft),
        # cancel it entirely to free up the subdomain
        if instance_sudo.state in ('pending_payment', 'draft'):
            subdomain = instance_sudo.name or instance_sudo.subdomain
            plan_name = instance_sudo.plan_id.name if instance_sudo.plan_id else 'N/A'
            partner_name = instance_sudo.partner_id.name
            previous_state = dict(
                instance_sudo._fields['state'].selection
            ).get(instance_sudo.state, instance_sudo.state)

            reason = (
                "Client cancelled before payment.\n"
                "Previous state: %s\n"
                "Plan: %s\n"
                "Invoice: %s\n"
                "Subdomain: %s"
            ) % (previous_state, plan_name, invoice.name, subdomain)

            instance_sudo.write({
                'state': 'cancelled_by_client',
                'cancellation_reason': reason,
            })
            instance_sudo._append_log(
                "Order cancelled by client. Subdomain released."
            )
            # Log for support follow-up (visible in backend chatter)
            from markupsafe import Markup
            instance_sudo.message_post(
                body=Markup(
                    "<b>Order cancelled by client</b><br/>"
                    "Client <b>%s</b> cancelled before payment.<br/>"
                    "Instance: %s<br/>"
                    "Plan: %s<br/>"
                    "Invoice: %s (cancelled)<br/>"
                    "This client may need a follow-up."
                ) % (partner_name, subdomain, plan_name, invoice.name),
                message_type='notification',
                subtype_xmlid='mail.mt_note',
            )
            # Notify support team to follow up with the client
            instance_sudo._send_notification(
                'saas_core.mail_template_saas_payment_cancelled',
            )
            return request.redirect('/my/instances')

        return request.redirect('/my/instances/%s' % instance_id)

    # ==================== Daily backup toggle ====================

    @http.route(
        '/my/instances/<int:instance_id>/daily-backup/toggle',
        type='http', auth='user', website=True,
        methods=['POST'], csrf=True,
    )
    def portal_toggle_daily_backup(self, instance_id, access_token=None,
                                   **post):
        """Enable / disable daily backups for a hosting instance.

        Enable path requires payment — we create an unpaid invoice and
        redirect the customer to a custom checkout page. Only once the
        invoice transitions to paid does ``daily_backup_enabled`` flip
        to True (see ``account.move._saas_check_instance_payment``).

        Disable path is a direct flag flip. No refund logic in this
        version — the customer keeps backups until the current
        billing period ends (we just stop charging on next renewal).
        """
        try:
            instance = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            return request.redirect('/my/instances')

        # Snapshots are available to hosting AND managed-services instances
        # (the one app-level add-on a service gets); only trials are excluded.
        if instance.is_trial:
            return request.redirect(
                '/my/instances/%d/backups?error=%s'
                % (instance_id, url_quote(_(
                    "Daily backups are not available on trial plans."
                )))
            )

        want_enabled = (post.get('enable') == '1')

        if not want_enabled:
            # Self-service disable is intentionally not available — the
            # add-on is irreversible from the portal so customers don't
            # accidentally lose ongoing snapshot protection. Operators
            # can still flip the flag from the backend if required.
            # We do, however, allow Cancel on a still-pending (unpaid)
            # activation invoice — that's not really a "disable", just
            # backing out of an unfinished purchase.
            pending = instance.sudo().daily_backup_pending_invoice_id
            if (
                not instance.daily_backup_enabled
                and pending
                and pending.state == 'posted'
                and pending.payment_state not in ('paid', 'in_payment')
            ):
                try:
                    pending.button_cancel()
                except Exception:
                    _logger.exception(
                        "Failed to cancel pending backup invoice %s",
                        pending.name,
                    )
                instance.sudo().daily_backup_pending_invoice_id = False
                return request.redirect(
                    '/my/instances/%d/backups?notice=%s'
                    % (instance_id, url_quote(_(
                        "Pending payment cancelled. You can re-enable "
                        "daily backups any time."
                    )))
                )
            return request.redirect(
                '/my/instances/%d/backups?error=%s'
                % (instance_id, url_quote(_(
                    "Daily backups can't be disabled from the portal. "
                    "Contact support if you need to make a change."
                )))
            )

        # Enable path — already on, or a pending invoice exists, send
        # the customer to the right place.
        if instance.daily_backup_enabled:
            return request.redirect(
                '/my/instances/%d/backups?notice=%s'
                % (instance_id, url_quote(_(
                    "Daily backups are already enabled."
                )))
            )
        try:
            instance.sudo().action_purchase_daily_backup()
        except UserError as e:
            return request.redirect(
                '/my/instances/%d/backups?error=%s'
                % (instance_id, url_quote(str(e)))
            )
        return request.redirect(
            '/my/instances/%d/daily-backup/checkout' % instance_id
        )

    @http.route(
        '/my/instances/<int:instance_id>/daily-backup/checkout',
        type='http', auth='user', website=True,
    )
    def portal_daily_backup_checkout(self, instance_id,
                                     access_token=None, **kw):
        """Custom checkout for the daily-backup add-on.

        Renders the unpaid invoice and an embedded Odoo payment widget
        (same payment.form macro the main instance checkout uses), so
        the customer can pay in-place without bouncing through the
        generic /my/invoices portal. On successful payment the
        ``account.move`` write hook flips ``daily_backup_enabled``
        for the linked instance.
        """
        try:
            instance = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            return request.redirect('/my/instances')

        invoice = instance.daily_backup_pending_invoice_id
        if not invoice or invoice.payment_state in ('paid', 'in_payment'):
            # Nothing to pay — back to backups page (the payment hook
            # will have flipped the flag if this was a successful
            # transition).
            return request.redirect(
                '/my/instances/%d/backups?notice=%s'
                % (instance_id, url_quote(_(
                    "No pending payment. If you just paid, daily "
                    "backups should now be enabled."
                )))
            )

        partner_sudo = request.env.user.partner_id.sudo()
        invoice_company = invoice.company_id or request.env.company
        landing_route = (
            '/my/instances/%d/backups?notice=%s'
            % (instance_id, url_quote(_(
                "Payment received. Daily backups are now active."
            )))
        )

        availability_report = {}
        providers_sudo = request.env['payment.provider'].sudo()._get_compatible_providers(
            invoice_company.id,
            partner_sudo.id,
            invoice.amount_residual,
            currency_id=invoice.currency_id.id,
            report=availability_report,
        )
        payment_methods_sudo = request.env['payment.method'].sudo()._get_compatible_payment_methods(
            providers_sudo.ids,
            partner_sudo.id,
            currency_id=invoice.currency_id.id,
            report=availability_report,
        )
        # We do NOT retain customer card data — no saved cards offered.
        tokens_sudo = request.env['payment.token'].sudo().browse()
        invoice_access_token = invoice._portal_ensure_token()

        # No proration on activation: snapshot subscription is a flat
        # monthly commitment (the customer can't disable from the
        # portal, so we charge the full month whether they enable on
        # the 1st or the 28th). Renewal cron then bills monthly,
        # exactly one month after the activation payment.
        monthly_price = instance.sudo()._get_daily_backup_price()

        values = self._prepare_portal_layout_values()
        values.update({
            'instance': instance,
            'invoice': invoice,
            'monthly_price': monthly_price,
            'period': 'monthly',
            'page_name': 'saas_daily_backup_checkout',
            # Same shape as portal_checkout uses, so the same
            # ``payment.form`` macro renders.
            'payment_action_id': request.env.ref('payment.action_payment_provider').id,
            'providers_sudo': providers_sudo,
            'payment_methods_sudo': payment_methods_sudo,
            'tokens_sudo': tokens_sudo,
            # False for EVERY provider -> no "Save my card" checkbox.
            # payment.method_form indexes this dict for any tokenization-
            # capable provider, so an empty mapping KeyErrors as soon as
            # one is enabled — each compatible provider needs an entry.
            'show_tokenize_input_mapping': {p.id: False for p in providers_sudo},
            'amount': invoice.amount_residual,
            'currency': invoice.currency_id,
            'partner_id': partner_sudo.id,
            'access_token': invoice_access_token,
            'transaction_route': '/invoice/transaction/%d' % invoice.id,
            'landing_route': landing_route,
            'company_mismatch': not PaymentPortal._can_partner_pay_in_company(
                partner_sudo, invoice_company,
            ),
            'expected_company': invoice_company,
            'invoice_id': invoice.id,
            'transaction_type': 'online_direct',
            'availability_report': availability_report,
        })
        return request.render(
            'saas_website.portal_daily_backup_checkout', values,
        )

    # ==================== Backups: list + download ====================

    # If a ``running`` backup record is older than this, treat it as a
    # zombie — most likely the worker crashed before flipping it to
    # ``failed``. We don't want a dead row to lock the slot forever.
    # Five minutes is comfortably above a typical small/medium DB dump
    # and small enough that a stuck record gets released on the next
    # page refresh, not 15+ minutes later.
    _ONDEMAND_RUNNING_FUSE = datetime.timedelta(minutes=5)

    def _active_ondemand_backup(self, instance):
        """Return the in-flight on-demand backup for ``instance``, if any.

        "Active" = either still actively running, or completed and not
        yet expired. Only one such backup is allowed per instance at a
        time — gates both ``Backup Now`` in the template and
        ``/backups/ondemand`` in the controller so a refresh-click
        can't race. Stale ``running`` rows (worker died before writing
        ``failed``) are flipped here so the slot frees up.
        """
        now = fields.Datetime.now()
        cutoff = now - self._ONDEMAND_RUNNING_FUSE

        candidates = instance.backup_ids.filtered(lambda b: b.ephemeral)

        # Reap zombies first — anything in ``running`` past the fuse.
        zombies = candidates.filtered(
            lambda b: b.state == 'running' and b.create_date and b.create_date < cutoff
        )
        if zombies:
            zombies.sudo().write({
                'state': 'failed',
                'error_message': _(
                    'Backup process appears to have died before completing — '
                    'no status update for over %d minutes.'
                ) % (self._ONDEMAND_RUNNING_FUSE.total_seconds() / 60),
            })

        return candidates.filtered(lambda b: (
            b.state == 'running'
            or (b.state == 'done' and (not b.expires_at or b.expires_at > now))
        ))[:1]


    @http.route(
        '/my/instances/<int:instance_id>/backups/ondemand',
        type='http', auth='user', website=True,
        methods=['POST'], csrf=True,
    )
    def portal_backup_ondemand(self, instance_id, access_token=None, **post):
        """Kick off an on-demand backup of one database.

        Hosting-only by spec. Creates a ``saas.instance.backup`` row
        with ``ephemeral=True`` and dispatches the upload to a thread;
        the cleanup cron reaps the bucket object after 1 hour.
        """
        try:
            instance = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            return request.redirect('/my/instances')

        # The form may indicate where to land after the redirect.
        # We host the Backup Now button on the Databases page now, so
        # bouncing the customer back to /backups is the wrong default.
        # Whitelist the two known destinations so this can't be
        # turned into an open-redirect.
        return_to = (post.get('return_to') or '').strip()
        if return_to == 'backups':
            redirect = '/my/instances/%d/backups' % instance_id
        else:
            redirect = '/my/instances/%d/databases' % instance_id

        if not instance.is_hosting:
            return request.redirect('%s?error=%s' % (
                redirect, url_quote(_(
                    "On-demand backups are available for hosting instances only."
                )),
            ))
        if instance.state != 'running':
            return request.redirect('%s?error=%s' % (
                redirect, url_quote(_(
                    "Instance must be running to create a backup."
                )),
            ))

        db_name = (post.get('db_name') or '').strip()
        if not db_name:
            return request.redirect('%s?error=%s' % (
                redirect, url_quote(_("Pick a database to back up.")),
            ))

        # Ensure the DB exists on the instance — protects against a
        # crafted POST trying to dump an unrelated catalog DB.
        try:
            existing = {r['name'] for r in instance.hosting_db_list()}
        except UserError as e:
            return request.redirect('%s?error=%s' % (
                redirect, url_quote(str(e)),
            ))
        if db_name not in existing:
            return request.redirect('%s?error=%s' % (
                redirect, url_quote(_(
                    "Database '%s' does not exist on this instance."
                ) % db_name),
            ))

        # Only one on-demand backup may exist at a time per instance.
        #
        # - If one is **running**: reject the new request so two
        #   parallel dumps can't fight for the SSH connection,
        #   memory, and bucket slot. The disabled button on the
        #   server-rendered page is the primary guard; this is the
        #   belt-and-braces against a stale form submission or a
        #   double-click that beat the disable-all JS.
        # - If one is **done** (ready to download): the customer is
        #   explicitly asking for a fresh dump, so delete the bucket
        #   object first and proceed with the new one.
        active = self._active_ondemand_backup(instance)
        if active and active.state == 'running':
            return request.redirect('%s?error=%s' % (
                redirect, url_quote(_(
                    "A backup of '%s' is still in progress. Wait for it "
                    "to finish before starting another."
                ) % (active.db_name or '')),
            ))
        if active and active.state == 'done':
            if active.bucket_path:
                try:
                    active._delete_from_bucket()
                except Exception:
                    _logger.warning(
                        "Failed to delete bucket object for replaced "
                        "on-demand backup %s — proceeding anyway",
                        active.bucket_path,
                    )
            active.sudo().write({
                'state': 'failed',
                'error_message': _(
                    'Replaced by a newer on-demand backup request.'
                ),
                'expires_at': fields.Datetime.now(),
            })
            # Commit so the singleton check downstream (and any
            # concurrent page render) sees the slot as free.
            try:
                request.env.cr.commit()
            except Exception:
                pass

        Backup = request.env['saas.instance.backup'].sudo()
        now_str = fields.Datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        backup = Backup.create({
            'instance_id': instance.id,
            'db_name': db_name,
            'name': 'ondemand_%s_%s' % (db_name, now_str),
            'state': 'running',
            'ephemeral': True,
        })

        from odoo.addons.saas_core.utils import run_in_background
        run_in_background(
            backup, '_run_portal_backup',
            thread_name='saas_ondemand_%s_%s' % (instance.subdomain, db_name),
        )

        return request.redirect('%s?notice=%s' % (
            redirect, url_quote(_(
                "Backup of '%s' started. Refresh in a minute — a download "
                "link valid for 24 hours will appear once ready."
            ) % db_name),
        ))

    @http.route(
        '/my/instances/<int:instance_id>/backups/<int:backup_id>/discard',
        type='http', auth='user', website=True,
        methods=['POST'], csrf=True,
    )
    def portal_backup_discard(self, instance_id, backup_id,
                              access_token=None, **kw):
        """Release the on-demand backup slot.

        - On a ``done`` ephemeral: shrinks ``expires_at`` to now so the
          next 5-min cron deletes the bucket object and the row.
        - On a ``running`` ephemeral: flips it to ``failed`` immediately
          and commits, freeing the singleton slot right away. The
          background thread, if still alive, may still complete and try
          to write — that write loses the race; any uploaded bucket
          object gets reaped on the next cleanup pass.

        Where to redirect: respect a ``return_to=databases`` form field
        so a cancel from the per-DB page lands back there instead of
        the backups page.
        """
        try:
            instance = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            return request.redirect('/my/instances')

        backup = instance.backup_ids.filtered(
            lambda b: b.id == backup_id and b.ephemeral
        )
        return_to = (kw.get('return_to') or '').strip()
        if return_to == 'databases':
            redirect = '/my/instances/%d/databases' % instance_id
        else:
            redirect = '/my/instances/%d/backups' % instance_id
        if not backup:
            return request.redirect('%s?error=%s' % (
                redirect, url_quote(_("Backup not found.")),
            ))
        if backup.state == 'running':
            backup.sudo().write({
                'state': 'failed',
                'error_message': _(
                    'Cancelled by customer while still in progress.'
                ),
                'expires_at': fields.Datetime.now(),
            })
            # Persist so a still-alive background thread doesn't roll
            # this back on its own re-raise.
            try:
                request.env.cr.commit()
            except Exception:
                pass
            return request.redirect('%s?notice=%s' % (
                redirect, url_quote(_("Backup cancelled.")),
            ))
        backup.sudo().expires_at = fields.Datetime.now()
        return request.redirect('%s?notice=%s' % (
            redirect, url_quote(_(
                "Backup discarded. The slot will open up within "
                "a few minutes once cleanup runs."
            )),
        ))

    @http.route(
        '/my/instances/<int:instance_id>/backups/<int:backup_id>/download',
        type='http', auth='user', website=True,
    )
    def portal_backup_download(self, instance_id, backup_id,
                               access_token=None, **kw):
        try:
            instance = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            return request.redirect('/my/instances')

        backup = instance.backup_ids.filtered(
            lambda b: b.id == backup_id and b.state == 'done'
        )
        # On-demand backups live on the Databases page; daily snapshots
        # (when they come back) on the Backups page. Bounce errors to
        # whichever was the obvious source so the customer doesn't
        # land on the wrong screen.
        err_redirect = (
            '/my/instances/%d/databases'
            if backup and backup.ephemeral
            else '/my/instances/%d/backups'
        ) % instance_id
        if not backup:
            err_redirect = '/my/instances/%d/databases' % instance_id
            return request.redirect('%s?error=%s' % (
                err_redirect, url_quote(_("Backup not available.")),
            ))

        try:
            backup._refresh_download_url()
        except Exception:
            _logger.exception("Could not refresh download URL for backup %s",
                              backup.id)
        if not backup.download_url:
            return request.redirect('%s?error=%s' % (
                err_redirect, url_quote(_(
                    "We couldn't generate the download link right now. "
                    "Please try again in a moment, or contact support."
                )),
            ))

        # NOTE: on-demand backups keep their full 8-hour window even
        # after the customer downloads — protects them if the download
        # was interrupted or the zip got corrupted. If they want the
        # slot freed earlier, they can hit the Discard button.
        return werkzeug.utils.redirect(backup.download_url)

    # ==================== Restore Backup ====================

    @http.route(
        '/my/instances/<int:instance_id>/backup/<int:backup_id>/restore',
        type='http', auth='user', website=True, methods=['POST'],
    )
    def portal_restore_backup(self, instance_id, backup_id, access_token=None, **kw):
        """Restore a backup to the instance from the portal."""
        try:
            instance_sudo = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            return request.redirect('/my/instances')

        # Submitter can ask to return to the backups page (the typical
        # caller now) instead of the instance detail. Whitelist the
        # values so this can't be turned into an open redirect.
        return_to = kw.get('return_to')
        if return_to == 'backups':
            redirect_path = '/my/instances/%s/backups' % instance_id
        else:
            redirect_path = '/my/instances/%s' % instance_id

        if instance_sudo.state not in ('running', 'stopped'):
            return request.redirect('%s?error=%s' % (
                redirect_path,
                url_quote(_("Instance must be Running or Stopped to restore.")),
            ))

        backup = instance_sudo.backup_ids.filtered(
            lambda b: b.id == backup_id and b.state == 'done'
        )
        if not backup:
            return request.redirect('%s?error=%s' % (
                redirect_path, url_quote(_("Backup not available.")),
            ))

        # Defensive name retype — without this an errant click could
        # nuke a production DB. Only enforced from the new backups page;
        # the legacy form on the detail page submits without it.
        if return_to == 'backups':
            confirm = (kw.get('confirm') or '').strip()
            expected = backup.db_name or instance_sudo.subdomain
            if confirm != expected:
                return request.redirect('%s?error=%s' % (
                    redirect_path,
                    url_quote(_(
                        "Type the database name exactly to confirm restore."
                    )),
                ))

        try:
            if backup.is_full_instance:
                instance_sudo.action_restore_full_instance(backup.id)
            else:
                instance_sudo.action_restore_backup(backup.id)
        except Exception:
            _logger.exception(
                "Backup restore failed for instance %s", instance_sudo.name
            )
            # Reset state if it was changed before the error
            if instance_sudo.state == 'provisioning':
                instance_sudo.state = 'running'
            return request.redirect('%s?error=%s' % (
                redirect_path,
                url_quote(_(
                    "Restore could not be started. Please try again or "
                    "contact support if the problem continues."
                )),
            ))

        return request.redirect('%s?notice=%s' % (
            redirect_path,
            url_quote(_("Restore started. Refresh in a moment.")),
        ))

    # ==================== Log Stream Proxy (Hosting) ====================

    @http.route(
        '/my/instances/<int:instance_id>/logs/stream',
        type='http', auth='user', csrf=False,
    )
    def portal_instance_log_stream(self, instance_id, tail='100', access_token=None, **kw):
        """Portal-safe SSE proxy for live container logs.

        Validates ownership, then extracts SSH connection details
        BEFORE the streaming generator starts, so the ORM cursor
        is not held open during the long-lived SSE connection.
        """
        try:
            instance_sudo = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            from werkzeug.exceptions import Forbidden
            raise Forbidden()

        if instance_sudo.state != 'running' or not instance_sudo.docker_server_id:
            from werkzeug.exceptions import NotFound
            raise NotFound()

        # Extract everything we need BEFORE the generator runs
        # (the generator runs after the ORM transaction closes)
        server = instance_sudo.docker_server_id.sudo()
        container_name = 'odoo_%s' % instance_sudo.subdomain

        # Get SSH connection details while cursor is still open
        ssh_conn = server._get_ssh_connection()

        import json as _json
        import select as _select
        import shlex as _shlex

        try:
            tail_int = int(tail)
        except (ValueError, TypeError):
            tail_int = 100

        safe_name = _shlex.quote(container_name)

        def generate():
            try:
                ssh_conn._connect()
                transport = ssh_conn._client.get_transport()
                channel = transport.open_session()
                channel.exec_command(
                    'docker logs -f --tail %d %s 2>&1' % (tail_int, safe_name)
                )
                channel.settimeout(300)

                yield b'retry: 1000\n\n'

                buf = b''
                while not channel.exit_status_ready():
                    ready, _, _ = _select.select([channel], [], [], 1.0)
                    if ready:
                        chunk = channel.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                        while b'\n' in buf:
                            line, buf = buf.split(b'\n', 1)
                            text = line.decode('utf-8', errors='replace')
                            yield ('data: %s\n\n' % _json.dumps(text)).encode('utf-8')

                while channel.recv_ready():
                    chunk = channel.recv(4096)
                    buf += chunk
                if buf:
                    text = buf.decode('utf-8', errors='replace')
                    yield ('data: %s\n\n' % _json.dumps(text)).encode('utf-8')

                yield b'event: done\ndata: stream ended\n\n'

            except Exception:
                # The raw exception string can contain SSH endpoints,
                # internal hostnames, container IDs or paramiko trace
                # fragments — none of that belongs in a browser. Send
                # a single generic line and rely on the operator log
                # for the real cause.
                _logger.exception("Log streaming error for %s", container_name)
                yield ('event: error\ndata: %s\n\n' % _json.dumps(
                    "We lost the connection to the log stream. "
                    "Please refresh the page to reconnect."
                )).encode('utf-8')
            finally:
                ssh_conn._disconnect()

        from odoo.http import Response
        return Response(
            generate(),
            content_type='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
                'Connection': 'keep-alive',
            },
            direct_passthrough=True,
        )

    # ==================== Update Repository (Hosting) ====================

    @http.route(
        '/my/instances/<int:instance_id>/update-repo',
        type='http', auth='user', website=True, methods=['POST'],
    )
    def portal_update_repo(self, instance_id, access_token=None, **kw):
        """Update or create repository for a hosting instance and redeploy."""
        try:
            instance_sudo = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            return request.redirect('/my/instances')

        if not instance_sudo.is_hosting or instance_sudo.state != 'running':
            return request.redirect('/my/instances/%s' % instance_id)

        repo_url = (kw.get('repo_url') or '').strip()
        repo_branch = (kw.get('repo_branch') or 'main').strip()
        git_token = (kw.get('git_token') or '').strip()

        Repo = request.env['saas.instance.repo'].sudo()
        existing_repo = instance_sudo.repo_ids[:1]

        if repo_url:
            if existing_repo:
                # Update existing repo
                vals = {
                    'repo_url': repo_url,
                    'branch': repo_branch,
                }
                if git_token:
                    vals['github_token'] = git_token
                    vals['webhook_enabled'] = True
                existing_repo.write(vals)
            else:
                # Create new repo
                Repo.create({
                    'instance_id': instance_sudo.id,
                    'repo_url': repo_url,
                    'branch': repo_branch,
                    'github_token': git_token or False,
                    'webhook_enabled': bool(git_token),
                })
            # Redeploy to clone/pull the repo
            try:
                instance_sudo.action_redeploy()
                instance_sudo._append_log("Repository updated by client: %s (%s)" % (repo_url, repo_branch))
            except Exception:
                _logger.exception("Failed to redeploy instance %s after repo update", instance_sudo.name)
        elif existing_repo:
            # Repo URL cleared — remove the repo
            existing_repo.unlink()
            try:
                instance_sudo.action_restart()
                instance_sudo._append_log("Repository removed by client.")
            except Exception:
                _logger.exception("Failed to restart instance %s after repo removal", instance_sudo.name)

        return request.redirect('/my/instances/%s' % instance_id)

    @http.route(
        '/my/instances/<int:instance_id>/remove-repo',
        type='http', auth='user', website=True, methods=['POST'],
    )
    def portal_remove_repo(self, instance_id, access_token=None, **kw):
        """Remove the custom repository and restart the instance."""
        try:
            instance_sudo = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            return request.redirect('/my/instances')

        if not instance_sudo.is_hosting or instance_sudo.state != 'running':
            return request.redirect('/my/instances/%s' % instance_id)

        existing_repo = instance_sudo.repo_ids[:1]
        if existing_repo:
            # Unregister webhook before removing
            if existing_repo.webhook_provider_id:
                try:
                    existing_repo._unregister_webhook_from_provider()
                except Exception:
                    pass
            existing_repo.unlink()
            try:
                instance_sudo.action_redeploy()
                instance_sudo._append_log("Repository removed by client.")
            except Exception:
                _logger.exception(
                    "Failed to redeploy instance %s after repo removal",
                    instance_sudo.name,
                )

        return request.redirect('/my/instances/%s' % instance_id)

    # ==================== Pull Repository ====================

    @http.route(
        '/my/instances/<int:instance_id>/pull-repo',
        type='http', auth='user', website=True, methods=['POST'],
    )
    def portal_pull_repo(self, instance_id, access_token=None, **kw):
        """Pull latest code from the repository and restart the instance."""
        try:
            instance_sudo = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            return request.redirect('/my/instances')

        if instance_sudo.state != 'running':
            return request.redirect('/my/instances/%s' % instance_id)

        repo = instance_sudo.repo_ids.filtered(lambda r: r.state == 'cloned')[:1]
        if not repo:
            return request.redirect('/my/instances/%s' % instance_id)

        try:
            from odoo.addons.saas_core.utils import run_in_background
            instance_sudo._append_log(
                "Pull & restart requested by client for %s..." % repo.name
            )
            run_in_background(
                repo, '_do_webhook_pull_and_restart',
                error_method='_on_repo_background_error',
                thread_name='saas_portal_pull_%s' % repo.id,
            )
        except Exception:
            _logger.exception(
                "Failed to pull repo for instance %s",
                instance_sudo.name,
            )

        return request.redirect('/my/instances/%s' % instance_id)

    # ==================== Data Restore Request ====================

    @http.route(
        '/my/instances/<int:instance_id>/request-restore',
        type='json', auth='user', website=True,
    )
    def portal_request_restore(self, instance_id, note='', **kw):
        """Client requests data restoration — sends email to support."""
        try:
            instance_sudo = self._document_check_access(
                'saas.instance', instance_id,
            )
        except (AccessError, MissingError):
            return {'error': _('Access denied.')}

        if not instance_sudo.retained_backup_path:
            return {'error': _('No backup available for restore.')}

        partner = request.env.user.partner_id
        support_email = request.env['ir.config_parameter'].sudo().get_param(
            'saas_master.support_email', ''
        )
        if not support_email:
            return {'error': _('Support email is not configured. Please contact us directly.')}

        # Send email to support
        subject = _('Data Restore Request — %s') % instance_sudo.subdomain
        body = _(
            "Data restore request from client:\n\n"
            "Client: %s (%s)\n"
            "Instance: %s\n"
            "Subdomain: %s.%s\n"
            "Plan: %s (%s)\n"
            "Retained Backup: %s\n"
            "Client Note: %s\n\n"
            "Action: Open the instance in the backend and use "
            "'Restore to Instance & Invoice' to create the restoration "
            "invoice and schedule the restore."
        ) % (
            partner.name,
            partner.email or 'no email',
            instance_sudo.name or instance_sudo.subdomain,
            instance_sudo.subdomain,
            instance_sudo.domain_id.name if instance_sudo.domain_id else '',
            instance_sudo.plan_id.name if instance_sudo.plan_id else 'N/A',
            instance_sudo.billing_period or 'N/A',
            instance_sudo.retained_backup_path,
            note or '(none)',
        )

        try:
            mail = request.env['mail.mail'].sudo().create({
                'subject': subject,
                'body_html': '<pre>%s</pre>' % body.replace('\n', '<br/>'),
                'email_from': partner.email or support_email,
                'email_to': support_email,
                # Don't auto-delete — if the send fails we want the
                # row to stay so the customer's report can be traced.
                'auto_delete': False,
            })
            mail.send()
            # ``mail.send()`` swallows transient failures by leaving
            # state='exception' on the record. Check explicitly so we
            # don't tell the customer "request sent" when the email
            # actually never reached support.
            if mail.state == 'exception':
                _logger.warning(
                    "Restore-request email for instance %s ended in "
                    "state=exception: %s",
                    instance_sudo.subdomain,
                    (mail.failure_reason or '')[:300],
                )
                return {'error': _(
                    "We couldn't deliver your request to support just "
                    "now. Please email us directly at %s and we'll "
                    "get back to you."
                ) % support_email}
        except Exception:
            _logger.exception("Failed to send restore request email")
            return {'error': _(
                "We couldn't deliver your request right now. Please "
                "email us directly at %s and we'll get back to you."
            ) % support_email}

        instance_sudo._append_log(
            "Client requested data restore via portal. Note: %s" % (note or '(none)')
        )
        instance_sudo.message_post(body=_(
            "Client requested data restore. Note: %s"
        ) % (note or '(none)'))

        return {'success': True, 'message': _('Your request has been sent to support.')}

    @http.route(
        '/my/instances/<int:instance_id>/dismiss-restore-banner',
        type='json', auth='user', website=True,
    )
    def portal_dismiss_restore_banner(self, instance_id, **kw):
        """Dismiss the data restore suggestion banner."""
        try:
            instance_sudo = self._document_check_access(
                'saas.instance', instance_id,
            )
        except (AccessError, MissingError):
            return {'error': _('Access denied.')}
        instance_sudo.restore_banner_dismissed = True
        return {'success': True}

    @http.route(
        '/my/instances/<int:instance_id>/decline-restore',
        type='json', auth='user', website=True,
    )
    def portal_decline_restore(self, instance_id, **kw):
        """Client declines data restoration — cancel the invoice and clear."""
        try:
            instance_sudo = self._document_check_access(
                'saas.instance', instance_id,
            )
        except (AccessError, MissingError):
            return {'error': _('Access denied.')}

        invoice = instance_sudo.restoration_invoice_id
        if invoice and invoice.state == 'posted' and invoice.payment_state not in ('paid', 'in_payment'):
            # Cancel the invoice
            invoice.button_cancel()

        instance_sudo.write({
            'restoration_invoice_id': False,
            'restore_banner_dismissed': True,
            'retained_backup_path': False,
        })
        instance_sudo._append_log("Client declined data restoration.")
        return {'success': True, 'message': _('Data restoration declined.')}

    # ==================== List Installed Packages (Hosting) ====================

    @http.route(
        '/my/instances/<int:instance_id>/installed-packages',
        type='json', auth='user',
    )
    def portal_installed_packages(self, instance_id, access_token=None, **kw):
        """Fetch list of installed pip packages from the running container."""
        try:
            instance_sudo = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            return {'error': 'Access denied'}

        if not instance_sudo.is_hosting or instance_sudo.state != 'running':
            return {'error': 'Instance not running'}

        try:
            server = instance_sudo.docker_server_id.sudo()
            container = 'odoo_%s' % instance_sudo.subdomain
            with server._get_ssh_connection() as ssh:
                # List from both default site-packages and custom target path
                exit_code, stdout, stderr = ssh.execute(
                    'docker exec %s bash -c "'
                    'pip3 list --path=/var/lib/odoo/pip_packages --format=columns 2>/dev/null; '
                    'pip3 list --format=columns 2>/dev/null'
                    '" 2>/dev/null' % container
                )
                result = stdout
            # Parse pip list output into structured data (deduplicated)
            packages = []
            seen = set()
            if result:
                for line in result.strip().splitlines():
                    line = line.strip()
                    if not line or line.startswith('Package') or line.startswith('-'):
                        continue
                    parts = line.split()
                    if not parts:
                        continue
                    name = parts[0]
                    if name.lower() in seen:
                        continue
                    seen.add(name.lower())
                    version = parts[1] if len(parts) >= 2 else ''
                    packages.append({'name': name, 'version': version})
            packages.sort(key=lambda p: p['name'].lower())
            return {'packages': packages, 'count': len(packages)}
        except Exception:
            _logger.exception(
                "Failed to list packages for instance %s",
                instance_sudo.name,
            )
            return {'error': _(
                "We couldn't list the installed packages right now. "
                "Please try again in a moment."
            )}

    # ==================== Update Packages (Hosting) ====================

    @http.route(
        '/my/instances/<int:instance_id>/update-packages',
        type='http', auth='user', website=True, methods=['POST'],
    )
    def portal_update_packages(self, instance_id, access_token=None, **kw):
        """Update pip packages for a hosting instance and restart to apply."""
        try:
            instance_sudo = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            return request.redirect('/my/instances')

        if not instance_sudo.is_hosting or instance_sudo.state != 'running':
            return request.redirect('/my/instances/%s' % instance_id)

        raw_packages = (kw.get('pip_packages') or '').strip()
        # Deduplicate packages
        seen = set()
        unique_pkgs = []
        for p in raw_packages.splitlines():
            p = p.strip()
            if not p or p.startswith('#'):
                continue
            key = p.lower().split('=')[0].split('<')[0].split('>')[0].split('!')[0].split('[')[0].strip()
            if key not in seen:
                seen.add(key)
                unique_pkgs.append(p)
        new_packages = '\n'.join(unique_pkgs) if unique_pkgs else ''
        old_packages = (instance_sudo.pip_packages or '').strip()

        if new_packages != old_packages:
            install_result = 'success'
            install_output = ''
            # Writing pip_packages triggers _sync_packages_from_text in
            # saas_core, which validates each line against PEP 508 and
            # raises UserError on bad input (e.g. "--index-url=…",
            # "git+https://…", local paths). Catch it here so the customer
            # gets the styled error banner instead of Odoo's bare error
            # page.
            try:
                instance_sudo.pip_packages = new_packages or False
            except UserError as e:
                _logger.info(
                    "Rejected pip_packages update for instance %s: %s",
                    instance_sudo.name, e,
                )
                return request.redirect(
                    '/my/instances/%s?pkg_result=invalid' % instance_id
                )
            try:
                with instance_sudo.docker_server_id.sudo()._get_ssh_connection() as ssh:
                    # Update requirements.txt and docker-compose on disk (for persistence)
                    instance_sudo._render_and_write_configs(ssh)

                    # Install packages live into the running container
                    # Use /var/lib/odoo/pip_packages — already persisted via ./data/odoo volume
                    container = 'odoo_%s' % instance_sudo.subdomain
                    if unique_pkgs:
                        install_cmd = (
                            'docker exec %s bash -c "'
                            'mkdir -p /var/lib/odoo/pip_packages && '
                            'pip3 install --target=/var/lib/odoo/pip_packages '
                            '--upgrade --no-warn-script-location %s'
                            '" 2>&1'
                        ) % (container, ' '.join(unique_pkgs))
                        exit_code, stdout, stderr = ssh.execute(install_cmd)
                        install_output = stdout or stderr or ''
                        instance_sudo._append_log(
                            "Packages installed live (exit=%s): %s\n%s"
                            % (exit_code, ' '.join(unique_pkgs), install_output)
                        )
                        if exit_code != 0:
                            install_result = 'partial'

                    # Restart Odoo to pick up new packages (graceful)
                    instance_sudo.action_restart()
                    instance_sudo._append_log("Packages updated by client: %s" % (new_packages or '(cleared)'))
            except Exception as e:
                _logger.exception("Failed to update packages for instance %s", instance_sudo.name)
                install_result = 'error'
                install_output = str(e)

            return request.redirect(
                '/my/instances/%s?pkg_result=%s' % (instance_id, install_result)
            )

        return request.redirect('/my/instances/%s' % instance_id)

    # ==================== Refresh Usage ====================

    @http.route(
        '/my/instances/<int:instance_id>/refresh-usage',
        type='json', auth='user',
    )
    def portal_refresh_usage(self, instance_id, access_token=None, **kw):
        """Refresh resource usage data and return updated values as JSON."""
        try:
            instance_sudo = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            return {'error': 'Access denied'}

        if instance_sudo.state != 'running':
            return {'error': 'Instance not running'}

        try:
            instance_sudo.action_refresh_usage()
        except Exception:
            # Don't leak SSH/docker exception text to the browser —
            # log the raw cause and return a friendly line.
            _logger.exception(
                "Usage refresh failed for instance %s",
                instance_sudo.subdomain,
            )
            return {'error': _(
                "We couldn't fetch live usage right now. Please try "
                "again in a moment."
            )}

        return {
            'cpu_usage': instance_sudo.cpu_usage or '0%',
            'cpu_pct': instance_sudo.cpu_usage_pct or 0,
            'ram_percent': instance_sudo.ram_percent or '0%',
            'ram_pct': instance_sudo.ram_usage_pct or 0,
            'total_storage': instance_sudo.total_storage or '—',
            'storage_pct': instance_sudo.storage_usage_pct or 0,
            'storage_limit': int(instance_sudo.plan_id.storage_limit) if instance_sudo.plan_id else 0,
        }

    # ==================== Live Logs (Hosting) ====================


    # ==================== Create Backup ====================

    @http.route(
        '/my/instances/<int:instance_id>/create-backup',
        type='json', auth='user', website=True,
    )
    def portal_create_backup(self, instance_id, **kw):
        """Create a new backup from the portal (JSON, no page refresh)."""
        try:
            instance_sudo = self._document_check_access(
                'saas.instance', instance_id,
            )
        except (AccessError, MissingError):
            return {'error': _('Access denied.')}
        if instance_sudo.state != 'running':
            return {'error': _('Instance must be running.')}
        if instance_sudo.plan_id and instance_sudo.plan_id.is_trial_plan:
            return {'error': _('Backups are not available on trial plans.')}

        running = instance_sudo.backup_ids.filtered(lambda b: b.state == 'running')
        if running:
            return {'error': _('A backup is already in progress.')}

        # Auto-rotate: delete oldest if at the fixed retention limit.
        # Trials are already rejected above, so every instance here keeps
        # the last DEFAULT_MAX_BACKUPS copies.
        done_backups = instance_sudo.backup_ids.filtered(
            lambda b: b.state == 'done'
        ).sorted('create_date')
        while len(done_backups) >= DEFAULT_MAX_BACKUPS:
            oldest = done_backups[0]
            instance_sudo._append_log(
                "Auto-removing oldest backup '%s' (limit: %d)."
                % (oldest.name, DEFAULT_MAX_BACKUPS)
            )
            oldest._delete_from_bucket()
            oldest.unlink()
            done_backups -= oldest

        # Create the backup record NOW (state=running) so polling detects it
        Backup = request.env['saas.instance.backup'].sudo()
        now_str = fields.Datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        backup = Backup.create({
            'instance_id': instance_sudo.id,
            'name': 'backup_%s' % now_str,
            'state': 'running',
        })

        from odoo.addons.saas_core.utils import run_in_background
        run_in_background(
            backup, '_run_portal_backup',
            thread_name='saas_backup_%s' % instance_sudo.subdomain,
        )

        return {'success': True, 'message': _('Backup started.')}

    # ==================== Delete Backup ====================

    @http.route(
        '/my/instances/<int:instance_id>/backup/<int:backup_id>/delete',
        type='http', auth='user', website=True, methods=['POST'],
    )
    def portal_delete_backup(self, instance_id, backup_id, access_token=None, **kw):
        """Backup deletion is not available from the portal."""
        return request.redirect('/my/instances/%s' % instance_id)

    # ==================== Backup Download Regeneration ====================

    # NOTE: the legacy ``/backup/<id>/download`` route used to live
    # here as a no-op stub redirecting to the instance page. It was
    # named ``portal_backup_download`` — the same method name as the
    # real download route a few hundred lines up, which Python class
    # body evaluation silently overwrote. That meant every click on
    # the Download button went through the stub and did nothing.
    # Removed entirely so the working ``portal_backup_download``
    # (route ``/backups/<id>/download``) is the only definition.

    # ==================== Reactivate Cancelled Instance ====================

    @http.route(
        '/my/instances/<int:instance_id>/reactivate',
        type='http', auth='user', website=True,
    )
    def portal_instance_reactivate(self, instance_id, access_token=None, **kw):
        """Show dynamic plan builder to reactivate a cancelled instance.

        Uses the same workers/storage slider UI as the change-plan page.
        """
        try:
            instance_sudo = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            return request.redirect('/my/instances')

        if instance_sudo.state not in ('cancelled', 'cancelled_by_client'):
            return request.redirect('/my/instances/%s' % instance_id)

        product = instance_sudo.saas_product_id
        if not product or not product.is_published:
            return request.redirect('/services')

        from odoo.addons.saas_website.controllers.main import SaasWebsite
        if instance_sudo.is_hosting:
            custom_config = SaasWebsite._get_hosting_plan_config(self)
        else:
            custom_config = SaasWebsite._get_custom_plan_config(self)

        # Minimum resources = what the instance had before cancellation
        old_plan = instance_sudo.plan_id
        min_workers = old_plan.workers if old_plan else custom_config['min_workers']
        min_storage = int(old_plan.storage_limit) if old_plan else custom_config['min_storage']

        values = self._prepare_portal_layout_values()
        values.update({
            'instance': instance_sudo,
            'current_workers': min_workers,
            'current_storage': min_storage,
            'custom_config': custom_config,
            'page_name': 'saas_instance_reactivate',
            'error': kw.get('error'),
        })
        return request.render('saas_website.portal_instance_reactivate', values)

    @http.route(
        '/my/instances/<int:instance_id>/do-reactivate',
        type='http', auth='user', website=True, methods=['POST'],
    )
    def portal_instance_do_reactivate(self, instance_id, access_token=None, **kw):
        """Process reactivation: build a plan from workers/storage, reset
        the cancelled instance, and run the billing flow."""
        try:
            instance_sudo = self._document_check_access(
                'saas.instance', instance_id, access_token=access_token,
            )
        except (AccessError, MissingError):
            return request.redirect('/my/instances')

        new_workers = int(kw.get('workers', 0))
        new_storage = int(kw.get('storage', 0))
        billing_period = kw.get('billing_period', 'monthly')
        if billing_period not in ('monthly', 'yearly'):
            billing_period = 'monthly'

        err_redirect = '/my/instances/%s/reactivate?error=%%s' % instance_id

        if not new_workers or not new_storage:
            return request.redirect(
                err_redirect % url_quote('Please configure your plan.')
            )

        from odoo.addons.saas_website.controllers.main import SaasWebsite
        if instance_sudo.is_hosting:
            config = SaasWebsite._get_hosting_plan_config(self)
        else:
            config = SaasWebsite._get_custom_plan_config(self)
        # Enforce minimum = previous plan resources
        old_plan = instance_sudo.plan_id
        min_workers = old_plan.workers if old_plan else config['min_workers']
        min_storage = int(old_plan.storage_limit) if old_plan else config['min_storage']
        new_workers = max(min_workers, min(new_workers, config['max_workers']))
        new_storage = max(min_storage, min(new_storage, config['max_storage']))

        # Find or create the plan
        product = instance_sudo.saas_product_id
        if instance_sudo.is_hosting:
            new_plan = SaasWebsite._get_or_create_hosting_plan(
                self, product, new_workers, new_storage, config,
                region=instance_sudo.region_id,
            )
        else:
            new_plan = SaasWebsite._get_or_create_custom_plan(
                self, product, new_workers, new_storage, config,
                region=instance_sudo.region_id,
            )

        try:
            instance_sudo.action_reactivate(new_plan.id, billing_period)
        except UserError as e:
            return request.redirect(
                err_redirect % url_quote(str(e))
            )

        # Redirect to checkout if there's an unpaid invoice, else to detail
        if (instance_sudo.sale_order_id
                and instance_sudo.sale_order_id.invoice_ids.filtered(
                    lambda i: i.state == 'posted'
                    and i.payment_state not in ('paid', 'in_payment')
                    and i.amount_residual > 0
                )):
            return request.redirect(
                '/my/instances/%s/checkout' % instance_id
            )
        return request.redirect('/my/instances/%s' % instance_id)

    # ==================== Instance Folders ====================

    @http.route(
        '/my/instances/folder/create',
        type='json', auth='user', website=True,
    )
    def portal_folder_create(self, name, parent_id=False, **kw):
        """Create a new instance folder, optionally nested under parent_id."""
        name = (name or '').strip()
        if not name:
            return {'error': _('Folder name is required.')}
        partner = request.env.user.partner_id
        vals = {
            'name': name,
            'partner_id': partner.id,
        }
        if parent_id:
            parent = request.env['saas.instance.folder'].sudo().search([
                ('id', '=', int(parent_id)),
                ('partner_id', '=', partner.id),
            ], limit=1)
            if parent:
                vals['parent_id'] = parent.id
        folder = request.env['saas.instance.folder'].sudo().create(vals)
        return {'success': True, 'folder_id': folder.id, 'name': folder.name}

    @http.route(
        '/my/instances/folder/<int:folder_id>/rename',
        type='json', auth='user', website=True,
    )
    def portal_folder_rename(self, folder_id, name, **kw):
        """Rename an instance folder."""
        name = (name or '').strip()
        if not name:
            return {'error': _('Folder name is required.')}
        partner = request.env.user.partner_id
        folder = request.env['saas.instance.folder'].sudo().search([
            ('id', '=', folder_id),
            ('partner_id', '=', partner.id),
        ], limit=1)
        if not folder:
            return {'error': _('Folder not found.')}
        folder.name = name
        return {'success': True, 'folder_id': folder.id, 'name': folder.name}

    @http.route(
        '/my/instances/folder/<int:folder_id>/delete',
        type='json', auth='user', website=True,
    )
    def portal_folder_delete(self, folder_id, **kw):
        """Delete a folder and all subfolders. Instances are moved to unfiled."""
        partner = request.env.user.partner_id
        folder = request.env['saas.instance.folder'].sudo().search([
            ('id', '=', folder_id),
            ('partner_id', '=', partner.id),
        ], limit=1)
        if not folder:
            return {'error': _('Folder not found.')}
        if folder.child_ids:
            return {'error': _('Cannot delete a folder that has subfolders. Delete or move the subfolders first.')}
        # Move instances in this folder to unfiled
        folder.instance_ids.write({'folder_id': False})
        folder.unlink()
        return {'success': True}

    @http.route(
        '/my/instances/move',
        type='json', auth='user', website=True,
    )
    def portal_instance_move_to_folder(self, instance_ids, folder_id=False, **kw):
        """Move instances to a folder (or unfiled if folder_id is False)."""
        partner = request.env.user.partner_id
        Instance = request.env['saas.instance'].sudo()
        instances = Instance.search([
            ('id', 'in', instance_ids),
            ('partner_id', '=', partner.id),
        ])
        if not instances:
            return {'error': _('No instances found.')}
        if folder_id:
            folder = request.env['saas.instance.folder'].sudo().search([
                ('id', '=', folder_id),
                ('partner_id', '=', partner.id),
            ], limit=1)
            if not folder:
                return {'error': _('Folder not found.')}
        instances.write({'folder_id': folder_id or False})
        return {'success': True}

    # ==================== Billing & Auto-renew ====================

    @http.route(
        '/my/instances/<int:instance_id>/billing/auto-renew',
        type='http', auth='user', website=True, methods=['POST'],
        csrf=True,
    )
    def portal_instance_auto_renew_toggle(self, instance_id, kind=None,
                                          enabled=None, **kw):
        """Toggle subscription / snapshot auto-renew for an instance.

        Called by the form-switch JS on the instance page. Returns a
        small JSON {ok: bool} payload so the JS can revert the checkbox
        if the server rejected the change.
        """
        import json as _json
        try:
            instance = self._document_check_access(
                'saas.instance', instance_id,
            )
        except Exception:
            return request.make_response(
                _json.dumps({'ok': False, 'error': 'forbidden'}),
                headers=[('Content-Type', 'application/json')],
            )
        if kind not in ('subscription', 'snapshot'):
            return request.make_response(
                _json.dumps({'ok': False, 'error': 'bad-kind'}),
                headers=[('Content-Type', 'application/json')],
            )
        flag = 'auto_renew_subscription' if kind == 'subscription' \
            else 'auto_renew_daily_backup'
        # Don't allow flipping ON without a saved card — toggle is
        # disabled in the UI but JS clients could bypass that.
        new_val = str(enabled or '') == '1'
        if new_val and not instance.payment_token_id:
            return request.make_response(
                _json.dumps({'ok': False, 'error': 'no-card'}),
                headers=[('Content-Type', 'application/json')],
            )
        instance.sudo().write({flag: new_val})
        return request.make_response(
            _json.dumps({'ok': True}),
            headers=[('Content-Type', 'application/json')],
        )

    @http.route(
        '/my/instances/<int:instance_id>/billing/remove-card',
        type='http', auth='user', website=True, methods=['POST'],
        csrf=True,
    )
    def portal_instance_remove_card(self, instance_id, **kw):
        """Remove the saved card from the instance.

        Also disables both auto-renew flags — there's no card to
        charge anymore, so leaving them on would be misleading and
        the next renewal would just fall back to manual payment
        anyway. The token itself is left untouched in the payment
        module (the customer may use it for other instances).
        """
        instance = self._document_check_access('saas.instance', instance_id)
        instance.sudo().write({
            'payment_token_id': False,
            'auto_renew_subscription': False,
            'auto_renew_daily_backup': False,
        })
        return request.redirect(
            '/my/instances/%s?msg=%s' % (
                instance_id,
                url_quote(_('Saved card removed. Auto-renew is now off.')),
            )
        )
