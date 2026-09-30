import base64
import os
from unittest.mock import patch

from cryptography.fernet import Fernet

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestClusterKubeconfig(TransactionCase):
    """The kubeconfig lives on the cluster (saas.server): an uploaded
    kubeconfig is encrypted at rest, the cleartext upload inbox is cleared,
    and reads return the byte-identical YAML so KubernetesDriver keeps
    working. A region can hold several clusters."""

    _SAMPLE = (
        "apiVersion: v1\nkind: Config\nclusters:\n"
        "- name: microk8s\n  cluster:\n    server: https://127.0.0.1:16443\n"
        "users:\n- name: admin\n  user:\n    token: FAKE-TOKEN-BYTES\n"
    )

    def setUp(self):
        super().setUp()
        self.upload = base64.b64encode(self._SAMPLE.encode('utf-8')).decode()
        self.region = self.env['saas.region'].sudo().create({
            'name': 'Multi Region', 'code': 'multi-cluster-test'})

    def _cluster(self, name, **vals):
        return self.env['saas.server'].sudo().create(dict(
            {'name': name, 'compute_driver': 'kubernetes',
             'region_id': self.region.id}, **vals))

    def _enc_col(self, server):
        self.env.flush_all()
        self.env.cr.execute(
            "SELECT kubeconfig_enc FROM saas_server WHERE id = %s", (server.id,))
        return self.env.cr.fetchone()[0]

    def test_uploaded_kubeconfig_encrypted_inbox_cleared_roundtrip(self):
        os.environ['SAAS_SECRET_KEY'] = Fernet.generate_key().decode()
        self.addCleanup(os.environ.pop, 'SAAS_SECRET_KEY', None)
        cluster = self._cluster('enc-cluster', kubeconfig_file=self.upload)

        self.assertFalse(cluster.kubeconfig_file,
                         "the cleartext upload inbox must be cleared on save")
        stored = self._enc_col(cluster)
        self.assertTrue(stored.startswith('enc:v1:'),
                        "the kubeconfig must be encrypted at rest")
        self.assertNotIn('FAKE-TOKEN-BYTES', stored)
        self.assertEqual(cluster._kubeconfig_yaml(), self._SAMPLE)
        self.assertTrue(cluster.kubeconfig_loaded)

    def test_plaintext_when_no_key_configured(self):
        os.environ.pop('SAAS_SECRET_KEY', None)
        cluster = self._cluster('plain-cluster')
        cluster.kubeconfig_file = self.upload
        self.assertFalse(cluster.kubeconfig_file)
        self.assertEqual(self._enc_col(cluster), self.upload,
                         "with no key configured, storage is unchanged (back-compat)")
        self.assertEqual(cluster._kubeconfig_yaml(), self._SAMPLE)

    def test_region_capacity_needs_a_connected_cluster(self):
        cluster = self._cluster('unconnected', health_state='ok')
        self.assertFalse(self.region.has_capacity(),
                         "a cluster without a kubeconfig is not capacity")
        cluster.kubeconfig_file = self.upload
        self.assertTrue(self.region.has_capacity())
        cluster.health_state = 'unreachable'
        self.assertFalse(self.region.has_capacity())

    def test_region_with_two_clusters(self):
        a = self._cluster('cluster-a', kubeconfig_file=self.upload, health_state='ok')
        b = self._cluster('cluster-b', kubeconfig_file=self.upload, health_state='ok')
        self.assertEqual(self.region.server_ids, a | b)
        a.health_state = 'unreachable'
        self.assertTrue(self.region.has_capacity(), "cluster-b still serves the region")

    def test_allocation_pinned_to_domain_cluster(self):
        a = self._cluster('pin-a', kubeconfig_file=self.upload, health_state='ok')
        b = self._cluster('pin-b', kubeconfig_file=self.upload, health_state='ok')
        Server = self.env['saas.server']
        with patch.object(type(Server), '_probe_reachable', return_value=(True, '')):
            self.assertEqual(
                Server._allocate_docker_server(region=self.region, cluster=b), b)
            self.assertEqual(
                Server._allocate_docker_server(region=self.region, cluster=a), a)
