import os

from cryptography.fernet import Fernet

from odoo.tests.common import TransactionCase, tagged
from odoo.addons.saas_core import crypto


@tagged('post_install', '-at_install')
class TestSecretCrypto(TransactionCase):
    """SEC-002: the crypto helper is a safe, opt-in, legacy-tolerant layer."""

    def _set_key(self):
        key = Fernet.generate_key().decode()
        os.environ['SAAS_SECRET_KEY'] = key
        self.addCleanup(os.environ.pop, 'SAAS_SECRET_KEY', None)
        return key

    def _clear_key(self):
        os.environ.pop('SAAS_SECRET_KEY', None)

    def test_passthrough_without_key(self):
        self._clear_key()
        self.assertFalse(crypto.is_enabled())
        self.assertEqual(crypto.encrypt('hunter2'), 'hunter2',
                         "no key -> store plaintext (deploy is a no-op)")
        self.assertEqual(crypto.decrypt('hunter2'), 'hunter2')

    def test_roundtrip_with_key(self):
        self._set_key()
        self.assertTrue(crypto.is_enabled())
        token = crypto.encrypt('hunter2')
        self.assertTrue(token.startswith('enc:v1:'))
        self.assertNotIn('hunter2', token)
        self.assertEqual(crypto.decrypt(token), 'hunter2')

    def test_encrypt_is_idempotent(self):
        self._set_key()
        once = crypto.encrypt('s3cret')
        twice = crypto.encrypt(once)
        self.assertEqual(once, twice, "already-encrypted values are not re-wrapped")

    def test_legacy_plaintext_decrypts_to_itself(self):
        self._set_key()
        # A row written before encryption was enabled has no prefix.
        self.assertEqual(crypto.decrypt('legacy-plaintext'), 'legacy-plaintext')

    def test_empty_values_passthrough(self):
        self._set_key()
        for v in ('', False, None):
            self.assertEqual(crypto.encrypt(v), v)


@tagged('post_install', '-at_install')
class TestEncryptedFieldAtRest(TransactionCase):
    """SEC-002: an EncryptedChar stores ciphertext in the column but reads
    back plaintext transparently."""

    def setUp(self):
        super().setUp()
        product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or \
            self.env['saas.product'].sudo().create(
                {'name': 'ENC Hosting', 'is_hosting': True, 'is_published': True})
        plan = self.env['saas.plan'].sudo().create({
            'name': 'ENC Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [product.id])]})
        domain = self.env['saas.based.domain'].sudo().search([], limit=1) or \
            self.env['saas.based.domain'].sudo().create({'name': 'enc.example.com'})
        partner = self.env['res.partner'].sudo().create({'name': 'ENC Cust'})
        self.product, self.plan, self.domain, self.partner = product, plan, domain, partner

    def _instance(self, sub):
        return self.env['saas.instance'].sudo().create({
            'subdomain': sub, 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': False, 'state': 'running',
            'is_hosting': True})

    def _raw_db_password(self, rec):
        self.env.flush_all()
        self.env.cr.execute(
            "SELECT db_password FROM saas_instance WHERE id = %s", (rec.id,))
        return self.env.cr.fetchone()[0]

    def test_column_holds_ciphertext_read_returns_plaintext(self):
        os.environ['SAAS_SECRET_KEY'] = Fernet.generate_key().decode()
        self.addCleanup(os.environ.pop, 'SAAS_SECRET_KEY', None)
        rec = self._instance('encsecret')
        rec.db_password = 'topsecret-db-pw'
        stored = self._raw_db_password(rec)
        self.assertTrue(stored.startswith('enc:v1:'),
                        "the column must hold ciphertext, not the password")
        self.assertNotIn('topsecret-db-pw', stored)
        rec.invalidate_recordset(['db_password'])
        self.assertEqual(rec.db_password, 'topsecret-db-pw',
                         "reads must transparently decrypt")

    def test_plaintext_in_column_when_no_key(self):
        os.environ.pop('SAAS_SECRET_KEY', None)
        rec = self._instance('encplain')
        rec.db_password = 'plain-db-pw'
        stored = self._raw_db_password(rec)
        self.assertEqual(stored, 'plain-db-pw',
                         "with no key configured, storage is unchanged (plaintext)")
