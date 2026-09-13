import base64
import os

from cryptography.fernet import Fernet

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestSshKeyEncryption(TransactionCase):
    """SEC-002 (SSH key): an uploaded private key is encrypted at rest, the
    cleartext upload inbox is cleared, and reads return the byte-identical key
    so SSHConnection (already proven to connect to a real host with the
    plaintext key) keeps working."""

    def _enc_col(self, rec):
        self.env.flush_all()
        self.env.cr.execute(
            "SELECT private_key_enc FROM saas_ssh_key_pair WHERE id = %s",
            (rec.id,))
        return self.env.cr.fetchone()[0]

    def test_uploaded_key_encrypted_inbox_cleared_roundtrip(self):
        os.environ['SAAS_SECRET_KEY'] = Fernet.generate_key().decode()
        self.addCleanup(os.environ.pop, 'SAAS_SECRET_KEY', None)
        pem = (b'-----BEGIN OPENSSH PRIVATE KEY-----\n'
               b'b3BlbnNzaC1rZXktdjEAAAAA-FAKE-KEY-BYTES\n'
               b'-----END OPENSSH PRIVATE KEY-----\n')
        upload = base64.b64encode(pem).decode()
        kp = self.env['saas.ssh.key.pair'].sudo().create({
            'name': 'enc-key', 'type': 'ed25519', 'private_key_file': upload})

        # Cleartext upload inbox is cleared; nothing plaintext persists.
        self.assertFalse(kp.private_key_file,
                         "the cleartext upload inbox must be cleared on save")
        # The stored column holds ciphertext, not the key.
        stored = self._enc_col(kp)
        self.assertTrue(stored.startswith('enc:v1:'),
                        "the SSH key must be encrypted at rest")
        self.assertNotIn('FAKE-KEY-BYTES', stored)
        # Byte-identical round-trip → SSHConnection gets the same key bytes.
        self.assertEqual(kp._private_key_b64(), upload)
        self.assertEqual(base64.b64decode(kp._private_key_b64()), pem)
        self.assertTrue(kp.key_loaded)

    def test_plaintext_when_no_key_configured(self):
        os.environ.pop('SAAS_SECRET_KEY', None)
        upload = base64.b64encode(b'legacy-plain-key').decode()
        kp = self.env['saas.ssh.key.pair'].sudo().create({
            'name': 'plain-key', 'private_key_file': upload})
        self.assertFalse(kp.private_key_file)
        self.assertEqual(self._enc_col(kp), upload,
                         "with no key configured, storage is unchanged (back-compat)")
        self.assertEqual(kp._private_key_b64(), upload)
        self.assertTrue(kp.key_loaded)
