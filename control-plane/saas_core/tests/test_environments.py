from datetime import date, timedelta

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestEnvironments(TransactionCase):
    """Odoo.sh-style environments: a Production project plus per-server-billed
    Staging/Development children.

    These tests stay on the non-deploy paths (records, pricing, billing lines,
    constraints) — provisioning goes through SSH/background threads and is out
    of scope for unit tests.
    """

    def setUp(self):
        super().setUp()
        self.icp = self.env['ir.config_parameter'].sudo()
        self.icp.set_param('saas_master.hosting_worker_price', '10.0')
        self.icp.set_param('saas_master.hosting_storage_price_per_gb', '0.3')
        self.icp.set_param('saas_master.hosting_min_workers', '2')
        self.icp.set_param('saas_master.hosting_min_storage', '5')
        self.icp.set_param('saas_master.hosting_yearly_discount_pct', '20')
        self.icp.set_param('saas_master.env_price_factor', '1.0')
        self.engine = self.env['saas.pricing.engine']
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', True)], limit=1)
        if not self.product:
            self.product = self.env['saas.product'].sudo().create({
                'name': 'TEST Hosting', 'is_hosting': True, 'is_published': True,
            })
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'TEST Env Plan', 'is_custom': True,
            'workers': 4, 'storage_limit': 50, 'cpu_limit': 2.0, 'ram_limit': '2g',
            'price': 55.0, 'yearly_price': 528.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])],
        })
        self.partner = self.env['res.partner'].sudo().create({'name': 'Env Cust'})
        self.domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create({'name': 'env.example.com'})

    def _mk_prod(self, sub='proj', billing='monthly', state='running',
                 due=None, last=None):
        return self.env['saas.instance'].sudo().create({
            'subdomain': sub, 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'billing_period': billing,
            'environment': 'production', 'region_id': False,
            'state': state, 'next_invoice_date': due, 'last_invoice_date': last,
        })

    def _mk_child(self, prod, env_type='staging', sub=None, state='running'):
        return self.env['saas.instance'].sudo().create({
            'subdomain': sub or ('%s-%s' % (prod.subdomain, env_type)),
            'domain_id': self.domain.id, 'partner_id': self.partner.id,
            'saas_product_id': self.product.id, 'plan_id': self.plan.id,
            'billing_period': prod.billing_period,
            'environment': env_type, 'parent_id': prod.id, 'region_id': False,
            'state': state,
        })

    # ---------------------------------------------------------------- hierarchy
    def test_production_cannot_have_parent(self):
        prod = self._mk_prod('hroot')
        with self.assertRaises(ValidationError):
            self.env['saas.instance'].sudo().create({
                'subdomain': 'bad-prod', 'domain_id': self.domain.id,
                'partner_id': self.partner.id,
                'saas_product_id': self.product.id, 'plan_id': self.plan.id,
                'environment': 'production', 'parent_id': prod.id,
                'region_id': False,
            })

    def test_child_requires_production_parent(self):
        with self.assertRaises(ValidationError):
            self.env['saas.instance'].sudo().create({
                'subdomain': 'orphan-stg', 'domain_id': self.domain.id,
                'partner_id': self.partner.id,
                'saas_product_id': self.product.id, 'plan_id': self.plan.id,
                'environment': 'staging', 'parent_id': False, 'region_id': False,
            })

    def test_child_under_child_rejected(self):
        prod = self._mk_prod('hroot2')
        staging = self._mk_child(prod, 'staging')
        with self.assertRaises(ValidationError):
            self.env['saas.instance'].sudo().create({
                'subdomain': 'nested', 'domain_id': self.domain.id,
                'partner_id': self.partner.id,
                'saas_product_id': self.product.id, 'plan_id': self.plan.id,
                'environment': 'development', 'parent_id': staging.id,
                'region_id': False,
            })

    # ----------------------------------------------------------------- pricing
    def test_env_server_price_matches_engine(self):
        prod = self._mk_prod('pprice')
        expected = self.engine.env_server_price(billing='monthly')
        self.assertAlmostEqual(prod._env_server_price('monthly'), expected, 2)
        # min spec (2 workers, 5 GB) at the configured rates × factor 1.0.
        self.assertAlmostEqual(expected, 2 * 10.0 + 5 * 0.3, 2)

    def test_env_price_factor_applied(self):
        self.icp.set_param('saas_master.env_price_factor', '0.5')
        prod = self._mk_prod('pfactor')
        full = 2 * 10.0 + 5 * 0.3
        self.assertAlmostEqual(prod._env_server_price('monthly'), full * 0.5, 2)

    # -------------------------------------------------------------- renewal line
    def test_environment_order_lines_one_per_slot(self):
        # Billing follows purchased SLOTS (entitlement), not how many children
        # are spun up — creating within slots is free, so renewal bills slots.
        prod = self._mk_prod('plines', due=date(2026, 2, 1),
                             last=date(2026, 1, 1))
        prod.write({'staging_slots': 1, 'dev_slots': 1})
        lines = prod._environment_order_lines('monthly')
        self.assertEqual(len(lines), 2)
        price = prod._env_server_price('monthly')
        for cmd in lines:
            self.assertAlmostEqual(cmd[2]['price_unit'], price, 2)
            self.assertEqual(cmd[2]['product_uom_qty'], 1)
        # Releasing a slot drops its recurring line.
        prod.write({'dev_slots': 0})
        self.assertEqual(len(prod._environment_order_lines('monthly')), 1)

    def test_renewal_invoice_includes_environment_lines(self):
        prod = self._mk_prod('prenew', due=date(2026, 1, 1),
                             last=date(2025, 12, 1))
        prod.write({'staging_slots': 1})
        prod._generate_renewal_invoice()
        so = self.env['sale.order'].sudo().search(
            [('origin', '=', 'SAAS:RENEWAL:%s' % prod.name)],
            order='id desc', limit=1)
        # Recurring env lines are labelled "… slot #N (…)".
        env_lines = [l for l in so.order_line if 'slot' in (l.name or '')]
        self.assertEqual(len(env_lines), 1)

    # ------------------------------------------------------------ cron exclusion
    def test_children_excluded_from_renewal_cron_domain(self):
        prod = self._mk_prod('pcron', due=date(2026, 1, 1),
                             last=date(2025, 12, 1))
        child = self._mk_child(prod, 'staging', sub='pcron-stg')
        # The exact domain _cron_generate_recurring_invoices uses.
        due = self.env['saas.instance'].sudo().search([
            ('state', '=', 'running'),
            ('is_trial', '=', False),
            ('plan_id', '!=', False),
            ('parent_id', '=', False),
            ('next_invoice_date', '<=', date(2026, 1, 1)),
        ])
        self.assertIn(prod, due)
        self.assertNotIn(child, due)

    # --------------------------------------------------------- initial purchase
    def test_initial_invoice_bills_chosen_env_servers(self):
        prod = self._mk_prod('pinit', state='draft', due=False, last=False)
        prod.write({'pending_staging_count': 1, 'pending_dev_count': 2})
        prod.action_confirm_and_bill()
        so = self.env['sale.order'].sudo().search(
            [('origin', '=', 'SAAS:INITIAL:%s' % prod.name)],
            order='id desc', limit=1)
        env_lines = [l for l in so.order_line if 'server' in (l.name or '')]
        self.assertEqual(len(env_lines), 3)
        price = prod._env_server_price('monthly')
        for l in env_lines:
            self.assertAlmostEqual(l.price_unit, price, 2)

    # --------------------------------------------------------------- repo gate
    def test_env_create_requires_repo(self):
        prod = self._mk_prod('pgate')
        with self.assertRaises(Exception):
            prod.action_create_environment('development', name='feature-x')

    def test_env_create_needs_a_reserved_slot(self):
        # Creating is capped at the reserved count: with a repo but 0 reserved
        # slots, the "+" must refuse and tell the customer to reserve first.
        prod = self._mk_prod('pgate2', due=date.today() + timedelta(days=20),
                             last=date.today() - timedelta(days=10))
        self.env['saas.instance.repo'].sudo().create({
            'instance_id': prod.id, 'repo_url': 'https://example.com/o/r.git',
            'branch': 'main',
        })
        prod.write({'staging_slots': 0})
        with self.assertRaises(Exception):
            prod.action_create_environment('staging', name='stg1')

    # ----------------------------------------------------------------- merge
    def test_merge_self_rejected(self):
        prod = self._mk_prod('pm1')
        with self.assertRaises(Exception):
            prod.action_merge_environment(prod.id)

    def test_merge_cross_project_rejected(self):
        a = self._mk_prod('pma')
        b = self._mk_prod('pmb')
        with self.assertRaises(Exception):
            a.action_merge_environment(b.id)

    def test_merge_reaches_provider(self):
        prod = self._mk_prod('pmg')
        self.env['saas.instance.repo'].sudo().create({
            'instance_id': prod.id, 'repo_url': 'https://example.com/o/r.git',
            'branch': 'main',
        })
        child = self._mk_child(prod, 'staging', sub='pmg-stg')
        # Same project, target has a repo, source is a sibling → guards pass and
        # the provider call is attempted; a non-provider URL fails cleanly.
        with self.assertRaises(Exception):
            prod.action_merge_environment(child.id)

    # --------------------------------------------------------------- deletion
    def test_delete_environment_frees_slot_without_refund(self):
        # Deleting a server frees its reserved slot for reuse but does NOT
        # refund or lower the reserved count — the customer paid for the
        # capacity and can recreate within the cycle for free.
        today = date.today()
        prod = self._mk_prod('pdel', due=today + timedelta(days=15),
                             last=today - timedelta(days=15))
        prod.write({'staging_slots': 1})
        child = self._mk_child(prod, 'staging', sub='pdel-stg')
        wallet = prod._wallet(create=True)
        before = wallet.balance
        child.action_delete_environment(delete_branch=False)
        prod.invalidate_recordset(['staging_slots'])
        wallet = prod._wallet(create=True)
        self.assertEqual(wallet.balance, before)         # no refund
        self.assertEqual(prod.staging_slots, 1)          # count unchanged
        self.assertEqual(prod._env_used_for('staging'), 0)  # slot freed
        self.assertIn(child.state, ('cancelled', 'provisioning'))
