from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestSaasPaymentModels(TransactionCase):
    """saas_payment.py: the provider-agnostic billing abstraction (A1). Only
    safe references are ever persisted (never card data) — see the module's
    module-level docstring. Covers SaasPaymentProviderConfig's single-default
    constraint and SaasPaymentMethod's ORM helpers directly; no real
    provider/gateway needed for those."""

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].sudo().create({'name': 'Pay Cust'})

    def _provider(self, **kw):
        vals = {'name': 'Test Provider', 'state': 'test'}
        vals.update(kw)
        return self.env['payment.provider'].sudo().create(vals)

    def _token(self, partner, provider, **kw):
        vals = {
            'provider_id': provider.id,
            'payment_method_id': self.env.ref('payment.payment_method_unknown').id,
            'partner_id': partner.id,
            'provider_ref': 'tok-%s-%s' % (partner.id, provider.id),
        }
        vals.update(kw)
        return self.env['payment.token'].sudo().create(vals)

    def _method(self, partner, provider=None, token=None, **kw):
        provider = provider or self._provider()
        if token is None:
            token = self._token(partner, provider)
        vals = {
            'partner_id': partner.id, 'provider_id': provider.id,
            'token_id': token.id if token else False,
        }
        vals.update(kw)
        return self.env['saas.payment.method'].sudo().create(vals)

    # ---------------- SaasPaymentProviderConfig ----------------
    def test_single_active_default_enforced(self):
        provider = self._provider()
        self.env['saas.payment.provider.config'].sudo().create({
            'name': 'Default A', 'provider_id': provider.id, 'is_default': True,
        })
        with self.assertRaises(ValidationError):
            self.env['saas.payment.provider.config'].sudo().create({
                'name': 'Default B', 'provider_id': provider.id, 'is_default': True,
            })

    def test_inactive_second_default_is_allowed(self):
        provider = self._provider()
        self.env['saas.payment.provider.config'].sudo().create({
            'name': 'Default A', 'provider_id': provider.id, 'is_default': True,
        })
        second = self.env['saas.payment.provider.config'].sudo().create({
            'name': 'Default B', 'provider_id': provider.id, 'is_default': True,
            'active': False,
        })
        self.assertFalse(second.active)

    # ---------------- SaasPaymentMethod ----------------
    def test_for_partner_excludes_inactive_method_and_token(self):
        provider = self._provider()
        active = self._method(self.partner, provider=provider)
        inactive_method = self._method(self.partner, provider=provider, active=False)
        token2 = self._token(self.partner, provider)
        token2.active = False
        inactive_token = self._method(self.partner, provider=provider, token=token2)
        found = self.env['saas.payment.method'].for_partner(self.partner)
        self.assertEqual(found, active)
        self.assertNotIn(inactive_method, found)
        self.assertNotIn(inactive_token, found)

    def test_for_partner_uses_commercial_partner(self):
        provider = self._provider()
        child = self.env['res.partner'].sudo().create(
            {'name': 'Contact', 'parent_id': self.partner.id})
        method = self._method(self.partner, provider=provider)
        found = self.env['saas.payment.method'].for_partner(child)
        self.assertEqual(found, method)

    def test_for_partner_empty_without_partner(self):
        found = self.env['saas.payment.method'].for_partner(self.env['res.partner'])
        self.assertFalse(found)

    def testdefault_for_partner_falls_back_to_most_recent(self):
        # No method flagged is_default: _for_partner's order
        # ('is_default desc, id desc') makes the highest-id (most recently
        # created) method the fallback.
        provider = self._provider()
        self._method(self.partner, provider=provider)
        m2 = self._method(self.partner, provider=provider)
        self.assertEqual(
            self.env['saas.payment.method'].default_for_partner(self.partner), m2)

    def testdefault_for_partner_prefers_flagged_default(self):
        provider = self._provider()
        self._method(self.partner, provider=provider)
        m2 = self._method(self.partner, provider=provider, is_default=True)
        self.assertEqual(
            self.env['saas.payment.method'].default_for_partner(self.partner), m2)

    def test_make_default_clears_others(self):
        provider = self._provider()
        m1 = self._method(self.partner, provider=provider, is_default=True)
        m2 = self._method(self.partner, provider=provider)
        m2._make_default()
        self.assertFalse(m1.is_default)
        self.assertTrue(m2.is_default)

    def test_action_remove_archives_method_and_token(self):
        provider = self._provider()
        method = self._method(self.partner, provider=provider)
        token = method.token_id
        method.action_remove()
        self.assertFalse(method.active)
        self.assertFalse(token.active)


@tagged('post_install', '-at_install')
class TestSaasPaymentGatewayRouting(TransactionCase):
    """SaasPaymentGateway._provider_for_partner: country-routing precedence
    (explicit country config row > default config row > provider's own
    available_country_ids > any enabled provider)."""

    def setUp(self):
        super().setUp()
        self.gateway = self.env['saas.payment.gateway']
        self.country_a = self.env.ref('base.us')
        self.country_b = self.env.ref('base.de')
        self.partner = self.env['res.partner'].sudo().create(
            {'name': 'Route Cust', 'country_id': self.country_a.id})

    def _provider(self, **kw):
        vals = {'name': 'Route Provider', 'state': 'test'}
        vals.update(kw)
        return self.env['payment.provider'].sudo().create(vals)

    def test_explicit_country_routing_wins(self):
        matched = self._provider(name='Matched')
        other_default = self._provider(name='Other Default')
        self.env['saas.payment.provider.config'].sudo().create({
            'name': 'US Routing', 'provider_id': matched.id,
            'country_ids': [(6, 0, [self.country_a.id])],
        })
        self.env['saas.payment.provider.config'].sudo().create({
            'name': 'Fallback', 'provider_id': other_default.id, 'is_default': True,
        })
        self.assertEqual(
            self.gateway._provider_for_partner(self.partner), matched)

    def test_default_routing_used_when_no_country_match(self):
        default_provider = self._provider(name='Default Provider')
        self.env['saas.payment.provider.config'].sudo().create({
            'name': 'Fallback', 'provider_id': default_provider.id, 'is_default': True,
        })
        # partner's country (US) isn't in any explicit routing row.
        self.assertEqual(
            self.gateway._provider_for_partner(self.partner), default_provider)

    def test_provider_available_countries_used_without_config_rows(self):
        restricted = self._provider(
            name='Restricted', available_country_ids=[(6, 0, [self.country_b.id])])
        matches_country = self._provider(
            name='Matches', available_country_ids=[(6, 0, [self.country_a.id])])
        self.assertEqual(
            self.gateway._provider_for_partner(self.partner), matches_country)
        self.assertNotEqual(
            self.gateway._provider_for_partner(self.partner), restricted)

    def test_any_enabled_provider_as_last_resort(self):
        only = self._provider(name='Only Enabled')
        self.assertEqual(self.gateway._provider_for_partner(self.partner), only)

    def test_disabled_config_provider_is_skipped(self):
        disabled = self._provider(name='Disabled', state='disabled')
        fallback = self._provider(name='Fallback Enabled')
        self.env['saas.payment.provider.config'].sudo().create({
            'name': 'US Routing (disabled provider)', 'provider_id': disabled.id,
            'country_ids': [(6, 0, [self.country_a.id])],
        })
        self.assertEqual(
            self.gateway._provider_for_partner(self.partner), fallback)


@tagged('post_install', '-at_install')
class TestSaasPaymentGatewaySaveAndCharge(TransactionCase):
    """SaasPaymentGateway.save_method_from_transaction and _charge — the two
    methods saas.instance actually calls for saved-card auto-renewal."""

    def setUp(self):
        super().setUp()
        self.gateway = self.env['saas.payment.gateway']
        self.partner = self.env['res.partner'].sudo().create({'name': 'Charge Cust'})
        self.provider = self.env['payment.provider'].sudo().create(
            {'name': 'Charge Provider', 'state': 'test'})

    def _token(self, **kw):
        vals = {
            'provider_id': self.provider.id,
            'payment_method_id': self.env.ref('payment.payment_method_unknown').id,
            'partner_id': self.partner.id,
            'provider_ref': 'tok-charge-%s' % self.partner.id,
        }
        vals.update(kw)
        return self.env['payment.token'].sudo().create(vals)

    def _tx(self, token, **kw):
        vals = {
            'provider_id': self.provider.id,
            'payment_method_id': token.payment_method_id.id,
            'partner_id': self.partner.id,
            'amount': 10.0,
            'currency_id': self.env.company.currency_id.id,
            'token_id': token.id,
            'operation': 'offline',
        }
        vals.update(kw)
        return self.env['payment.transaction'].sudo().create(vals)

    def _invoice(self, **kw):
        vals = {'move_type': 'out_invoice', 'partner_id': self.partner.id}
        vals.update(kw)
        return self.env['account.move'].sudo().create(vals)

    def _method(self, token, **kw):
        vals = {
            'partner_id': self.partner.id, 'provider_id': self.provider.id,
            'token_id': token.id,
        }
        vals.update(kw)
        return self.env['saas.payment.method'].sudo().create(vals)

    # ---------------- save_method_from_transaction ----------------
    def test_save_method_noop_without_partner_or_tx(self):
        token = self._token()
        tx = self._tx(token)
        self.assertFalse(self.gateway.save_method_from_transaction(
            self.env['res.partner'], tx))
        self.assertFalse(self.gateway.save_method_from_transaction(
            self.partner, self.env['payment.transaction']))

    def test_save_method_noop_without_token(self):
        tx = self._tx(self._token())
        tx.token_id = False
        self.assertFalse(
            self.gateway.save_method_from_transaction(self.partner, tx))

    def test_save_method_noop_with_inactive_token(self):
        token = self._token()
        tx = self._tx(token)
        token.active = False
        self.assertFalse(
            self.gateway.save_method_from_transaction(self.partner, tx))

    def test_save_method_creates_and_defaults_first(self):
        token = self._token(payment_details='Visa •••• 4242')
        tx = self._tx(token)
        method = self.gateway.save_method_from_transaction(self.partner, tx)
        self.assertTrue(method)
        self.assertEqual(method.token_id, token)
        self.assertEqual(method.display_label, 'Visa •••• 4242')
        self.assertTrue(method.is_default, "first saved method must become default")

    def test_save_method_is_idempotent_per_token(self):
        token = self._token()
        tx = self._tx(token)
        first = self.gateway.save_method_from_transaction(self.partner, tx)
        second = self.gateway.save_method_from_transaction(self.partner, tx)
        self.assertEqual(first, second)
        self.assertEqual(
            self.env['saas.payment.method'].search_count(
                [('token_id', '=', token.id)]), 1)

    def test_save_method_second_method_not_forced_default(self):
        tx1 = self._tx(self._token(provider_ref='tok-a'))
        first = self.gateway.save_method_from_transaction(self.partner, tx1)
        tx2 = self._tx(self._token(provider_ref='tok-b'))
        second = self.gateway.save_method_from_transaction(self.partner, tx2)
        self.assertTrue(first.is_default)
        self.assertFalse(second.is_default)

    # ---------------- _charge: precondition branches ----------------
    def test_charge_fails_without_token(self):
        method = self._method(self._token())
        method.token_id = False
        state, _msg = self.gateway.charge(method, self._invoice())
        self.assertEqual(state, 'failed')

    def test_charge_fails_with_inactive_token(self):
        token = self._token()
        method = self._method(token)
        token.active = False
        state, _msg = self.gateway.charge(method, self._invoice())
        self.assertEqual(state, 'failed')

    def test_charge_already_paid_invoice_short_circuits(self):
        method = self._method(self._token())
        invoice = self._invoice()
        invoice.payment_state = 'paid'
        state, _msg = self.gateway.charge(method, invoice)
        self.assertEqual(state, 'done')

    def test_charge_fails_when_provider_disabled(self):
        self.provider.state = 'disabled'
        method = self._method(self._token())
        state, _msg = self.gateway.charge(method, self._invoice())
        self.assertEqual(state, 'failed')

    # ---------------- _charge: reaches the provider ----------------
    def test_charge_success_reads_transaction_state(self):
        method = self._method(self._token())
        invoice = self._invoice()

        def _fake_send(tx_self):
            tx_self.state = 'done'

        with patch.object(
                type(self.env['payment.transaction']),
                '_send_payment_request', _fake_send):
            state, msg = self.gateway.charge(method, invoice)
        self.assertEqual(state, 'done')
        self.assertIn(invoice.name or '', msg)

    def test_charge_pending_reads_transaction_state(self):
        method = self._method(self._token())
        invoice = self._invoice()

        def _fake_send(tx_self):
            tx_self.state = 'pending'

        with patch.object(
                type(self.env['payment.transaction']),
                '_send_payment_request', _fake_send):
            state, _msg = self.gateway.charge(method, invoice)
        self.assertEqual(state, 'pending')

    def test_charge_falls_through_to_failed_on_other_states(self):
        method = self._method(self._token())
        invoice = self._invoice()

        def _fake_send(tx_self):
            tx_self.state = 'error'

        with patch.object(
                type(self.env['payment.transaction']),
                '_send_payment_request', _fake_send):
            state, _msg = self.gateway.charge(method, invoice)
        self.assertEqual(state, 'failed')

    def test_charge_exception_during_send_is_caught(self):
        method = self._method(self._token())
        invoice = self._invoice()

        def _boom(tx_self):
            raise RuntimeError('provider unreachable')

        with patch.object(
                type(self.env['payment.transaction']),
                '_send_payment_request', _boom):
            state, msg = self.gateway.charge(method, invoice)
        self.assertEqual(state, 'failed')
        self.assertIn('provider unreachable', msg)

    # ---------------- _charge: currency mismatch ----------------
    def test_charge_fails_on_currency_mismatch(self):
        method = self._method(self._token())
        foreign = self.env.ref('base.USD')
        if foreign == self.env.company.currency_id:
            foreign = self.env.ref('base.EUR')
        foreign.sudo().active = True
        journal = self.env['account.journal'].sudo().create({
            'name': 'Charge Test Bank', 'type': 'bank', 'code': 'CHGB',
            'currency_id': self.env.company.currency_id.id,
        })
        self.env['account.payment.method.line'].sudo().create({
            'payment_provider_id': self.provider.id,
            'journal_id': journal.id,
            'payment_method_id': self.env.ref(
                'account.account_payment_method_manual_in').id,
        })
        self.provider.invalidate_recordset(['journal_id'])
        invoice = self._invoice(currency_id=foreign.id)
        state, msg = self.gateway.charge(method, invoice)
        self.assertEqual(state, 'failed')
        self.assertIn('urrency', msg)
