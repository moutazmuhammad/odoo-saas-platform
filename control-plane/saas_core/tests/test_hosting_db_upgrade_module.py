"""Characterization tests for hosting_db_upgrade_module — a customer-
facing recovery tool (stop -> one-shot `odoo -u <module>` -> bring the
container back up regardless of outcome) with raw `docker compose`
calls and, before this file, zero test coverage. Written before routing
those calls through ComputeDriver, per the same "characterize first"
discipline test_redeploy_blue_green.py established for the
blue/green cluster."""
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


def _fake_ssh(responses=None):
    """Command-substring -> (rc, out, err); default (0, '', '')."""
    responses = dict(responses or {})
    calls = []

    class FakeSSH:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, cmd, timeout=None):
            calls.append(cmd)
            for needle, resp in responses.items():
                if needle in cmd:
                    return resp
            return (0, '', '')

    return FakeSSH(), calls


@tagged('post_install', '-at_install')
class TestHostingDbUpgradeModule(TransactionCase):
    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or self.env['saas.product'].sudo().create(
            {'name': 'UM Hosting', 'is_hosting': True, 'is_published': True})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'UM Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        self.domain = self.env['saas.based.domain'].sudo().create({'name': 'um.example.com'})
        self.partner = self.env['res.partner'].sudo().create({'name': 'UM Cust'})
        self.server = self.env['saas.server'].sudo().create(
            {'name': 'um-srv', 'docker_base_path': '/home/odoo'})
        self.instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'umtest', 'domain_id': self.domain.id, 'partner_id': self.partner.id,
            'saas_product_id': self.product.id, 'plan_id': self.plan.id,
            'docker_server_id': self.server.id, 'is_hosting': True,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running'})
        self.db_full_name = self.instance._hosting_db_full_name('mydb')

    def _run(self, fake_ssh, module='sale'):
        db_full_name = self.db_full_name  # avoid shadowing by the lambdas' own `rec`
        with patch.object(type(self.server), '_get_ssh_connection',
                          lambda rec: fake_ssh), \
                patch.object(type(self.instance), 'hosting_db_list',
                             lambda rec: [{'name': db_full_name}]):
            return self.instance.hosting_db_upgrade_module('mydb', module)

    def test_happy_path_stops_upgrades_and_restarts(self):
        fake_ssh, calls = _fake_ssh()
        out = self._run(fake_ssh)
        self.assertIn('docker compose stop odoo', calls[0])
        self.assertIn('docker compose run --rm -T odoo', calls[1])
        self.assertIn('-u sale', calls[1])
        self.assertIn('-d %s' % self.db_full_name, calls[1])
        self.assertIn('docker compose up -d', calls[2])
        self.assertIn('exit 0', out)

    def test_rejects_invalid_module_name(self):
        fake_ssh, _calls = _fake_ssh()
        with self.assertRaises(UserError):
            self._run(fake_ssh, module='not valid!')

    def test_rejects_unknown_database(self):
        fake_ssh, _calls = _fake_ssh()
        with patch.object(type(self.server), '_get_ssh_connection',
                          lambda self: fake_ssh), \
                patch.object(type(self.instance), 'hosting_db_list',
                             lambda self: [{'name': 'someone_else_db'}]):
            with self.assertRaises(UserError):
                self.instance.hosting_db_upgrade_module('mydb', 'sale')

    def test_upgrade_failure_still_brings_container_back_up(self):
        fake_ssh, calls = _fake_ssh({
            'docker compose run --rm -T odoo': (1, 'traceback here', ''),
        })
        with self.assertRaises(UserError) as cm:
            self._run(fake_ssh)
        # The container-restart command still ran despite the failure.
        up_calls = [c for c in calls if 'docker compose up -d' in c]
        self.assertEqual(len(up_calls), 1,
                         "must always try to bring the container back up")
        self.assertTrue(hasattr(cm.exception, '_saas_upgrade_output'))
        self.assertIn('traceback here', cm.exception._saas_upgrade_output)

    def test_restart_failure_after_successful_upgrade_raises_distinct_error(self):
        fake_ssh, calls = _fake_ssh({
            'docker compose up -d': (1, '', 'daemon unreachable'),
        })
        with self.assertRaises(UserError) as cm:
            self._run(fake_ssh)
        self.assertIn("didn't come back up automatically", str(cm.exception))

    def test_both_upgrade_and_restart_fail_reports_upgrade_error_first(self):
        fake_ssh, calls = _fake_ssh({
            'docker compose run --rm -T odoo': (1, 'boom', ''),
            'docker compose up -d': (1, '', 'also broken'),
        })
        with self.assertRaises(UserError) as cm:
            self._run(fake_ssh)
        self.assertIn("didn't complete successfully", str(cm.exception))
