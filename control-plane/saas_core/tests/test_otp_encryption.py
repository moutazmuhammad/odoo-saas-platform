import os
from datetime import timedelta

from cryptography.fernet import Fernet

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestOtpEncryption(TransactionCase):
    """SEC-013: registration/reset OTPs are encrypted at rest (no plaintext
    codes in the DB) and verified with a constant-time compare."""

    def _otp(self, code, identifier='sec13@example.com', channel='email'):
        return self.env['saas.registration.otp'].sudo().create({
            'identifier': identifier, 'channel': channel, 'code': code,
            'expires_at': fields.Datetime.now() + timedelta(minutes=10)})

    def _raw_code(self, rec):
        self.env.flush_all()
        self.env.cr.execute(
            "SELECT code FROM saas_registration_otp WHERE id = %s", (rec.id,))
        return self.env.cr.fetchone()[0]

    def test_code_encrypted_at_rest_and_verify_flow(self):
        os.environ['SAAS_SECRET_KEY'] = Fernet.generate_key().decode()
        self.addCleanup(os.environ.pop, 'SAAS_SECRET_KEY', None)
        OTP = self.env['saas.registration.otp'].sudo()
        rec = self._otp('123456')
        stored = self._raw_code(rec)
        self.assertTrue(stored.startswith('enc:v1:'),
                        "the OTP must be encrypted at rest, not plaintext")
        self.assertNotIn('123456', stored)
        # Wrong code rejected; correct code accepted; then consumed.
        self.assertFalse(OTP._verify('sec13@example.com', '000000', 'email'))
        self.assertTrue(OTP._verify('sec13@example.com', '123456', 'email'))
        self.assertFalse(OTP._verify('sec13@example.com', '123456', 'email'),
                         "a verified code can't be reused")

    def test_plaintext_and_verify_without_key(self):
        os.environ.pop('SAAS_SECRET_KEY', None)
        OTP = self.env['saas.registration.otp'].sudo()
        rec = self._otp('777777', identifier='nokey@example.com')
        self.assertEqual(self._raw_code(rec), '777777',
                         "with no key, storage is unchanged (back-compat)")
        self.assertTrue(OTP._verify('nokey@example.com', '777777', 'email'))
