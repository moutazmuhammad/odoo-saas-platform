from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestContainerHardening(TransactionCase):
    """SEC-003: tenant containers run non-root AND ship with capability/
    privilege-escalation hardening in the generated compose file."""

    def test_compose_drops_caps_and_forbids_new_privileges(self):
        Inst = self.env['saas.instance']
        rendered = Inst._render_template('docker-compose.yml.jinja', {
            'subdomain': 'hardx', 'xmlrpc_port': 8069, 'longpolling_port': 8072,
            'odoo_image': 'odoo-light', 'odoo_version': '18.0',
            'network_name': 'net_hardx'})
        # Normalise to compare structure without a YAML dependency.
        lines = [ln.strip() for ln in rendered.splitlines()]
        self.assertIn('cap_drop:', lines,
                      "compose must declare cap_drop")
        self.assertIn('- ALL', lines,
                      "tenant container must drop all Linux capabilities")
        self.assertIn('security_opt:', lines)
        self.assertIn('- "no-new-privileges:true"', lines,
                      "tenant container must forbid privilege escalation")
        # cap_drop's "- ALL" must belong to the cap_drop block (immediately
        # follows it), not some unrelated list.
        self.assertEqual(lines[lines.index('cap_drop:') + 1], '- ALL')
