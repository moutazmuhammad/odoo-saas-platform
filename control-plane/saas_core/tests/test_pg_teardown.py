from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPgTeardown(TransactionCase):
    """Instance teardown must drop the per-instance Odoo template DB
    (``__odoo_template_<sub>``, datistemplate=true) — clearing the template
    flag before dropdb — so cancelled instances don't leak template DBs.
    (Found during a real provision: a naive DROP DATABASE refuses templates.)"""

    def setUp(self):
        super().setUp()
        product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1) or \
            self.env['saas.product'].sudo().create(
                {'name': 'TD Hosting', 'is_hosting': True, 'is_published': True})
        plan = self.env['saas.plan'].sudo().create({
            'name': 'TD Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 5,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [product.id])]})
        domain = self.env['saas.based.domain'].sudo().search([], limit=1) or \
            self.env['saas.based.domain'].sudo().create({'name': 'td.example.com'})
        partner = self.env['res.partner'].sudo().create({'name': 'TD Cust'})
        self.server = self.env['saas.server'].sudo().create({
            'name': 'td-db', 'ip_v4': '10.1.2.3', 'is_db_server': True})
        self.instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'tdinst', 'domain_id': domain.id, 'partner_id': partner.id,
            'saas_product_id': product.id, 'plan_id': plan.id,
            'billing_period': 'monthly', 'environment': 'production',
            'region_id': False, 'state': 'running', 'is_hosting': True,
            'db_server_id': self.server.id, 'db_user': 'saas_tdinst'})

    def test_drop_postgresql_unflags_then_drops_template(self):
        cmds = []

        class FakeSSH:
            def __enter__(self_): return self_
            def __exit__(self_, *a): return False
            def execute(self_, cmd, timeout=None):
                cmds.append(cmd)
                # The owned-DB enumeration returns a normal DB and the
                # per-instance template (datistemplate = 't').
                if 'SELECT datname, datistemplate' in cmd:
                    return (0, 'tdinst_main|f\n__odoo_template_tdinst|t\n', '')
                return (0, '1', '')

        with patch.object(type(self.server), '_get_ssh_connection',
                          return_value=FakeSSH()):
            self.instance._drop_postgresql()

        joined = '\n'.join(cmds)
        # The template flag must be cleared...
        flag_idx = next((i for i, c in enumerate(cmds)
                         if 'datistemplate=false' in c
                         and '__odoo_template_tdinst' in c), -1)
        # ...and the template DB dropped...
        drop_idx = next((i for i, c in enumerate(cmds)
                         if 'dropdb' in c and '__odoo_template_tdinst' in c), -1)
        self.assertGreaterEqual(flag_idx, 0, "template flag never cleared:\n%s" % joined)
        self.assertGreaterEqual(drop_idx, 0, "template DB never dropped:\n%s" % joined)
        self.assertLess(flag_idx, drop_idx,
                        "must clear datistemplate BEFORE dropdb")
        # ...the normal DB dropped too, and the role dropped last.
        self.assertTrue(any('dropdb' in c and 'tdinst_main' in c for c in cmds))
        self.assertTrue(any('dropuser' in c for c in cmds), "role must be dropped")
        self.assertGreater(
            next(i for i, c in enumerate(cmds) if 'dropuser' in c), drop_idx,
            "role must be dropped AFTER its databases")
