import base64
import os

from cryptography.fernet import Fernet

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestKubeconfigEncryption(TransactionCase):
    """saas.kubeconfig follows the same EncryptedChar/upload-inbox
    convention as saas.ssh.key.pair (SEC-002 — "Kubeconfig field should
    be like ssh private key"): an uploaded kubeconfig is encrypted at
    rest, the cleartext upload inbox is cleared, and reads return the
    byte-identical YAML so KubernetesDriver keeps working."""

    _SAMPLE = (
        "apiVersion: v1\nkind: Config\nclusters:\n"
        "- name: microk8s\n  cluster:\n    server: https://127.0.0.1:16443\n"
        "users:\n- name: admin\n  user:\n    token: FAKE-TOKEN-BYTES\n"
    )

    def _enc_col(self, kc):
        self.env.flush_all()
        self.env.cr.execute(
            "SELECT kubeconfig_enc FROM saas_kubeconfig WHERE id = %s", (kc.id,))
        return self.env.cr.fetchone()[0]

    def test_uploaded_kubeconfig_encrypted_inbox_cleared_roundtrip(self):
        os.environ['SAAS_SECRET_KEY'] = Fernet.generate_key().decode()
        self.addCleanup(os.environ.pop, 'SAAS_SECRET_KEY', None)
        upload = base64.b64encode(self._SAMPLE.encode('utf-8')).decode()
        kc = self.env['saas.kubeconfig'].sudo().create({
            'name': 'enc-kubeconfig', 'kubeconfig_file': upload})

        # Cleartext upload inbox is cleared; nothing plaintext persists.
        self.assertFalse(kc.kubeconfig_file,
                         "the cleartext upload inbox must be cleared on save")
        # The stored column holds ciphertext, not the kubeconfig.
        stored = self._enc_col(kc)
        self.assertTrue(stored.startswith('enc:v1:'),
                        "the kubeconfig must be encrypted at rest")
        self.assertNotIn('FAKE-TOKEN-BYTES', stored)
        # Byte-identical round-trip → KubernetesDriver gets the same YAML.
        self.assertEqual(kc._kubeconfig_yaml(), self._SAMPLE)
        self.assertTrue(kc.kubeconfig_loaded)

    def test_plaintext_when_no_key_configured(self):
        os.environ.pop('SAAS_SECRET_KEY', None)
        upload = base64.b64encode(self._SAMPLE.encode('utf-8')).decode()
        kc = self.env['saas.kubeconfig'].sudo().create({
            'name': 'plain-kubeconfig', 'kubeconfig_file': upload})
        self.assertFalse(kc.kubeconfig_file)
        self.assertEqual(self._enc_col(kc), upload,
                         "with no key configured, storage is unchanged (back-compat)")
        self.assertEqual(kc._kubeconfig_yaml(), self._SAMPLE)
        self.assertTrue(kc.kubeconfig_loaded)

    def test_kubeconfig_optional_on_region(self):
        region = self.env['saas.region'].sudo().create({
            'name': 'no-kube-region', 'code': 'no-kube-test',
        })
        self.assertFalse(region.kubeconfig_id)

    def test_region_links_to_kubeconfig_record(self):
        upload = base64.b64encode(self._SAMPLE.encode('utf-8')).decode()
        kc = self.env['saas.kubeconfig'].sudo().create({
            'name': 'region-link-kubeconfig', 'kubeconfig_file': upload})
        region = self.env['saas.region'].sudo().create({
            'name': 'kube-region', 'code': 'kube-test',
            'kubeconfig_id': kc.id,
        })
        self.assertEqual(region.kubeconfig_id._kubeconfig_yaml(), self._SAMPLE)
