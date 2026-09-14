import base64

from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestHostShellSecurity(TransactionCase):
    """SEC-005: the HOST shell (raw SSH into the platform's own machines)
    must require the narrower group_saas_host_shell, not plain
    group_saas_manager — and introducing that split must not silently
    revoke access for whoever already had it via Manager alone."""

    def _server_with_key(self, name):
        pem = (b'-----BEGIN OPENSSH PRIVATE KEY-----\n'
               b'b3BlbnNzaC1rZXktdjEAAAAA-FAKE-KEY-BYTES\n'
               b'-----END OPENSSH PRIVATE KEY-----\n')
        kp = self.env['saas.ssh.key.pair'].sudo().create({
            'name': name + '-key', 'type': 'ed25519',
            'private_key_file': base64.b64encode(pem).decode()})
        return self.env['saas.server'].sudo().create({
            'name': name, 'ip_v4': '203.0.113.10', 'ssh_key_pair_id': kp.id})

    def _user_with_groups(self, login, *xmlids):
        return self.env['res.users'].sudo().create({
            'name': login, 'login': login,
            'groups_id': [(6, 0, [self.env.ref(x).id for x in xmlids])]})

    def test_manager_alone_cannot_open_host_terminal(self):
        server = self._server_with_key('sec005-mgr-only')
        manager = self._user_with_groups(
            'sec005_mgr_only', 'saas_core.group_saas_manager')
        with self.assertRaises(AccessError):
            server.with_user(manager).action_open_terminal()

    def test_host_shell_group_can_open_host_terminal(self):
        server = self._server_with_key('sec005-hostshell')
        operator = self._user_with_groups(
            'sec005_hostshell', 'saas_core.group_saas_manager',
            'saas_core.group_saas_host_shell')
        result = server.with_user(operator).action_open_terminal()
        self.assertEqual(result['tag'], 'ssh_terminal')
        self.assertEqual(result['context']['server_id'], server.id)

    def test_plain_user_cannot_open_host_terminal(self):
        server = self._server_with_key('sec005-plainuser')
        plain = self._user_with_groups(
            'sec005_plain', 'saas_core.group_saas_user')
        with self.assertRaises(AccessError):
            server.with_user(plain).action_open_terminal()

    # -------- grandfathering migration ------------------------------------
    def test_grandfather_grants_host_shell_to_existing_managers_only(self):
        manager_group = self.env.ref('saas_core.group_saas_manager')
        host_shell_group = self.env.ref('saas_core.group_saas_host_shell')
        manager = self._user_with_groups(
            'sec005_grandfather_mgr', 'saas_core.group_saas_manager')
        plain_user = self._user_with_groups(
            'sec005_grandfather_plain', 'saas_core.group_saas_user')
        self.assertNotIn(manager, host_shell_group.users)

        self.env['res.groups']._saas_grandfather_host_shell_group()

        self.assertIn(manager, host_shell_group.users)
        self.assertNotIn(plain_user, host_shell_group.users)

    def test_grandfather_is_idempotent(self):
        host_shell_group = self.env.ref('saas_core.group_saas_host_shell')
        manager = self._user_with_groups(
            'sec005_grandfather_idem', 'saas_core.group_saas_manager')
        self.env['res.groups']._saas_grandfather_host_shell_group()
        before = set(host_shell_group.users.ids)
        self.env['res.groups']._saas_grandfather_host_shell_group()
        after = set(host_shell_group.users.ids)
        self.assertEqual(before, after)
        self.assertIn(manager.id, after)

    def test_grandfather_does_not_remove_existing_host_shell_only_users(self):
        """A user with Host Shell but NOT Manager (an unusual but valid
        grant shape) must be left alone — the migration only ever adds,
        never subtracts."""
        host_shell_group = self.env.ref('saas_core.group_saas_host_shell')
        odd_user = self._user_with_groups(
            'sec005_grandfather_odd', 'saas_core.group_saas_host_shell')
        self.env['res.groups']._saas_grandfather_host_shell_group()
        self.assertIn(odd_user, host_shell_group.users)
