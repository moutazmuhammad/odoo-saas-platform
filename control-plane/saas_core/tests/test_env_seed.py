from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestEnvironmentSeed(TransactionCase):
    """A new Staging/Development server starts as a copy of Production,
    replacing the empty ``<sub>_main`` the operator creates on every
    hosting server — never a database the customer already has."""

    def setUp(self):
        super().setUp()
        env = self.env
        product = env['saas.product'].sudo().search([('is_hosting', '=', True)], limit=1) \
            or env['saas.product'].sudo().create({'name': 'Seed Hosting', 'is_hosting': True, 'is_published': True})
        plan = env['saas.plan'].sudo().create({
            'name': 'Seed Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 10,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 20.0, 'yearly_price': 192.0,
            'currency_id': env.company.currency_id.id, 'saas_product_ids': [(6, 0, [product.id])]})
        partner = env['res.partner'].sudo().create({'name': 'Seed Cust'})
        domain = env['saas.based.domain'].sudo().search([], limit=1) \
            or env['saas.based.domain'].sudo().create({'name': 'seed.example.com'})
        common = {'domain_id': domain.id, 'partner_id': partner.id, 'saas_product_id': product.id,
                  'plan_id': plan.id, 'billing_period': 'monthly', 'state': 'running'}
        self.prod = env['saas.instance'].sudo().create(dict(common, subdomain='seedprod'))
        self.child = env['saas.instance'].sudo().create(dict(
            common, subdomain='seedstg', environment='staging', parent_id=self.prod.id))
        self.copies = []
        Inst = type(self.child)
        lists = {self.prod.id: [{'name': 'seedprod_main'}]}
        for attr, value in (
                ('hosting_db_list', lambda rec: lists.get(rec.id, self.child_dbs)),
                ('hosting_db_copy_from', lambda rec, source, names, overwrite_names=None:
                    self.copies.append((source.id, names, overwrite_names)))):
            p = patch.object(Inst, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def test_replaces_the_operator_initial_database(self):
        self.child_dbs = [{'name': 'seedstg_main'}]
        self.assertTrue(self.child._hosting_seed_environment_database())
        self.assertEqual(self.copies, [(self.prod.id, ['seedprod_main'], ['seedstg_main'])])

    def test_copies_when_the_server_has_no_database(self):
        self.child_dbs = []
        self.assertTrue(self.child._hosting_seed_environment_database())
        self.assertEqual(self.copies, [(self.prod.id, ['seedprod_main'], None)])

    def test_never_overwrites_customer_databases(self):
        self.child_dbs = [{'name': 'seedstg_main'}, {'name': 'seedstg_work'}]
        self.assertFalse(self.child._hosting_seed_environment_database())
        self.assertEqual(self.copies, [])
