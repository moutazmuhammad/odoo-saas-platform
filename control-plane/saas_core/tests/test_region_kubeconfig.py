import os

from cryptography.fernet import Fernet

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestRegionKubeconfig(TransactionCase):
    """saas.region.kubeconfig follows the same EncryptedChar convention as
    saas.ssh.key.pair.private_key_enc (SEC-002): encrypted at rest once a
    key is configured, a transparent plaintext passthrough when it isn't,
    and a byte-identical round-trip either way."""

    _SAMPLE = (
        "apiVersion: v1\nkind: Config\nclusters:\n"
        "- name: microk8s\n  cluster:\n    server: https://127.0.0.1:16443\n"
        "users:\n- name: admin\n  user:\n    token: FAKE-TOKEN-BYTES\n"
    )

    def _enc_col(self, region):
        self.env.flush_all()
        self.env.cr.execute(
            "SELECT kubeconfig FROM saas_region WHERE id = %s", (region.id,))
        return self.env.cr.fetchone()[0]

    def test_kubeconfig_encrypted_at_rest_roundtrip(self):
        os.environ['SAAS_SECRET_KEY'] = Fernet.generate_key().decode()
        self.addCleanup(os.environ.pop, 'SAAS_SECRET_KEY', None)
        region = self.env['saas.region'].sudo().create({
            'name': 'kube-region', 'code': 'kube-test',
            'kubeconfig': self._SAMPLE,
        })

        stored = self._enc_col(region)
        self.assertTrue(stored.startswith('enc:v1:'),
                        "kubeconfig must be encrypted at rest")
        self.assertNotIn('FAKE-TOKEN-BYTES', stored)
        self.assertEqual(region.kubeconfig, self._SAMPLE)

    def test_kubeconfig_plaintext_when_no_key_configured(self):
        os.environ.pop('SAAS_SECRET_KEY', None)
        region = self.env['saas.region'].sudo().create({
            'name': 'kube-region-plain', 'code': 'kube-test-plain',
            'kubeconfig': self._SAMPLE,
        })
        self.assertEqual(self._enc_col(region), self._SAMPLE,
                         "with no key configured, storage is unchanged (back-compat)")
        self.assertEqual(region.kubeconfig, self._SAMPLE)

    def test_kubeconfig_optional(self):
        region = self.env['saas.region'].sudo().create({
            'name': 'no-kube-region', 'code': 'no-kube-test',
        })
        self.assertFalse(region.kubeconfig)
