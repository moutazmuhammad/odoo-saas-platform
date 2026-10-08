from datetime import date, timedelta
from unittest.mock import Mock, patch

import requests

from odoo.exceptions import UserError, ValidationError
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
    def test_env_child_registers_its_own_push_webhook(self):
        """Each Staging/Development repo row needs its OWN webhook registered
        on the Git provider — otherwise only the Production hook exists and
        pushes to a Staging/Development branch never reach their servers."""
        from unittest.mock import patch
        prod = self._mk_prod('wp')
        prod.write({'staging_slots': 1})
        self.env['saas.instance.repo'].sudo().create({
            'instance_id': prod.id, 'repo_url': 'https://github.com/x/y.git',
            'branch': 'main', 'github_token': 'tok',
            'webhook_enabled': True, 'state': 'cloned',
        })
        register_calls = []
        with patch.object(
                type(self.env['saas.instance.repo']),
                '_register_webhook_with_retry',
                lambda self: register_calls.append(self) or True), \
                patch.object(
                type(self.env['saas.instance.repo']),
                '_create_branch_on_provider', lambda *a, **kw: True):
            child = prod._create_env_child('staging', name='stg-wp')
        self.assertEqual(len(register_calls), 1)
        self.assertEqual(register_calls[0].instance_id, child)
        self.assertEqual(register_calls[0].branch, 'stg-wp')

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

    def test_upgraded_child_is_billed_at_its_own_size(self):
        """A resized Staging server's slot renews at that server's own price;
        an empty slot stays at the lowest-spec price."""
        prod = self._mk_prod('pbig', due=date(2026, 2, 1), last=date(2026, 1, 1))
        prod.write({'staging_slots': 2})
        child = self._mk_child(prod, 'staging', sub='pbig-stg')
        big = self.env['saas.plan'].sudo().create({
            'name': 'Hosting (6W / 80GB)', 'is_custom': True, 'workers': 6,
            'storage_limit': 80, 'cpu_limit': 3.0, 'ram_limit': '3g',
            'price': 84.0, 'yearly_price': 806.4,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        child.plan_id = big
        lines = prod._environment_order_lines('monthly')
        self.assertEqual(len(lines), 2)
        base = prod._env_server_price('monthly')
        own = child._env_child_price('monthly')
        self.assertGreater(own, base)
        self.assertAlmostEqual(own, child._env_size_price(6, 80, 'monthly'), 2)
        self.assertAlmostEqual(lines[0][2]['price_unit'], own, 2)
        self.assertIn('pbig-stg', lines[0][2]['name'])
        self.assertAlmostEqual(lines[1][2]['price_unit'], base, 2)

    def test_child_on_env_plan_costs_the_lowest_spec_price(self):
        prod = self._mk_prod('plow', due=date(2026, 2, 1), last=date(2026, 1, 1))
        child = self._mk_child(prod, 'development', sub='plow-dev')
        child.plan_id = prod._get_env_plan()
        self.assertAlmostEqual(child._env_child_price('monthly'),
                               prod._env_server_price('monthly'), 2)

    def test_env_plan_change_quote_prorates_on_the_production_cycle(self):
        today = date.today()
        prod = self._mk_prod('pquote', due=today + timedelta(days=10),
                             last=today - timedelta(days=20))
        child = self._mk_child(prod, 'staging', sub='pquote-stg')
        child.plan_id = prod._get_env_plan()
        q = child._env_plan_change_quote(6, 80)
        self.assertGreater(q['difference'], 0)
        self.assertAlmostEqual(q['charge_now'], round(q['difference'] * 10 / 30, 2), 2)
        # Same size: nothing to pay.
        same = child._env_plan_change_quote(child.plan_id.workers, child.plan_id.storage_limit)
        self.assertEqual(same['charge_now'], 0.0)

    def test_env_plan_upgrade_invoices_the_difference_and_applies_on_payment(self):
        today = date.today()
        prod = self._mk_prod('pup', due=today + timedelta(days=15),
                             last=today - timedelta(days=15))
        child = self._mk_child(prod, 'staging', sub='pup-stg')
        child.plan_id = prod._get_env_plan()
        big = self.env['saas.plan'].sudo().create({
            'name': 'Hosting (6W / 80GB) up', 'is_custom': True, 'workers': 6,
            'storage_limit': 80, 'cpu_limit': 3.0, 'ram_limit': '3g',
            'price': 84.0, 'yearly_price': 806.4,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        resized = []
        with patch.object(type(child), '_update_container_resources',
                          lambda self: resized.append(self.id)), \
                patch.object(type(prod), '_auto_renew_method', lambda self: False):
            result = child.action_change_environment_plan(big.id)
            self.assertFalse(result['applied'])
            self.assertIn('/checkout', result['checkout_url'])
            self.assertEqual(child.pending_plan_id, big)
            self.assertEqual(child.plan_id, prod._get_env_plan(), "not applied before payment")
            invoice = child.pending_change_invoice_id
            self.assertAlmostEqual(invoice.amount_total, result['charge'], 2)
            # Optional origin: leaving it unpaid never suspends the server.
            self.assertTrue(child._is_optional_invoice(invoice))
            # Payment: the account.move hook calls _apply_pending_plan_change.
            child._apply_pending_plan_change()
        self.assertEqual(child.plan_id, big)
        self.assertFalse(child.pending_plan_id)
        self.assertFalse(child.next_invoice_date, "children never get their own cycle")
        self.assertEqual(resized, [child.id])
        # A second request while one is pending is refused.
        child.pending_plan_id = big
        with self.assertRaisesRegex(UserError, 'awaiting payment'):
            child.action_change_environment_plan(big.id)

    def test_env_plan_downgrade_applies_now_but_never_shrinks_storage(self):
        prod = self._mk_prod('pdown', due=date(2026, 2, 1), last=date(2026, 1, 1))
        child = self._mk_child(prod, 'development', sub='pdown-dev')
        Plan = self.env['saas.plan'].sudo()
        big = Plan.create({
            'name': 'Hosting (6W / 80GB) dn', 'is_custom': True, 'workers': 6,
            'storage_limit': 80, 'cpu_limit': 3.0, 'ram_limit': '3g',
            'price': 84.0, 'yearly_price': 806.4,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        smaller = Plan.create({
            'name': 'Hosting (3W / 80GB) dn', 'is_custom': True, 'workers': 3,
            'storage_limit': 80, 'cpu_limit': 1.5, 'ram_limit': '2g',
            'price': 54.0, 'yearly_price': 518.4,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])]})
        child.plan_id = big
        with patch.object(type(child), '_update_container_resources', lambda self: None):
            with self.assertRaisesRegex(UserError, 'Storage cannot be reduced'):
                child.action_change_environment_plan(prod._get_env_plan().id)
            result = child.action_change_environment_plan(smaller.id)
        self.assertTrue(result['applied'])
        self.assertEqual(child.plan_id, smaller)
        self.assertFalse(child.pending_change_invoice_id)

    def test_production_cannot_use_the_env_resize(self):
        prod = self._mk_prod('pnot')
        with self.assertRaises(UserError):
            prod.action_change_environment_plan(self.plan.id)

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
    def test_env_create_without_repo_then_link_branch(self):
        """A repo is optional at creation; once the project gets one, the
        server can be linked to a branch (existing or created from main)."""
        prod = self._mk_prod('pgate', due=date.today() + timedelta(days=20),
                             last=date.today() - timedelta(days=10))
        prod.write({'dev_slots': 1})
        with patch.object(type(prod), '_activate_pending_environment', return_value=True):
            res = prod.action_create_environment('development', name='feature-x')
        child = self.env['saas.instance'].sudo().browse(res['child_id'])
        self.assertFalse(child.repo_ids)
        # No project repo yet → linking refuses with a clear message.
        with self.assertRaises(Exception):
            child.action_link_environment_branch()
        self.env['saas.instance.repo'].sudo().create({
            'instance_id': prod.id, 'repo_url': 'https://github.com/o/r.git',
            'branch': 'main',
        })
        Repo = type(self.env['saas.instance.repo'])
        with patch.object(Repo, '_list_remote_branches', return_value=['main', 'qa']), \
                patch.object(Repo, '_create_branch_on_provider', return_value=True) as mk, \
                patch.object(type(child), 'action_redeploy', return_value=True):
            with self.assertRaises(Exception):
                child.action_link_environment_branch(branch='nope')
            child.action_link_environment_branch(branch='qa')
            self.assertEqual(child.repo_ids.branch, 'qa')
            mk.assert_not_called()
            child.action_link_environment_branch(create=True)
            mk.assert_called_once_with(child.subdomain, 'main')
            self.assertEqual(child.repo_ids.branch, child.subdomain)
            self.assertEqual(child._env_branch(), child.subdomain)

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

    def test_database_copy_rejects_different_projects(self):
        prod = self._mk_prod('copyproj')
        child = self._mk_child(prod)
        other = self._mk_prod('otherproj')
        with self.assertRaisesRegex(UserError, 'same project'):
            child.hosting_db_copy_from(other, ['otherproj_main'])

    def test_database_copy_rejects_malformed_names(self):
        prod = self._mk_prod('copyproj')
        child = self._mk_child(prod)
        with self.assertRaisesRegex(UserError, 'valid database names'):
            child.hosting_db_copy_from(prod, [{}])


    def _branch_delete_repo(self):
        prod = self._mk_prod('branch-delete-root')
        child = self._mk_child(prod)
        return self.env['saas.instance.repo'].sudo().create({
            'instance_id': child.id, 'repo_url': 'https://github.com/test/project.git',
            'branch': 'stage',
        })

    def _delete_branch_responses(self, ref_status, repo_status=200):
        response = Mock(status_code=422)
        response.raise_for_status.side_effect = requests.HTTPError('422')
        response.json.return_value = {'message': 'Reference does not exist'}
        return response, [Mock(status_code=ref_status), Mock(status_code=repo_status)]

    def test_delete_missing_github_branch_422_succeeds(self):
        repo = self._branch_delete_repo()
        response, reads = self._delete_branch_responses(404)
        with patch.object(type(repo), '_branch_provider_context', return_value=(
                'github', 'https://api.github.com', 'test', 'project', 'test-token')), \
                patch('odoo.addons.saas_core.models.saas_instance_repo.http_requests.delete', return_value=response), \
                patch('odoo.addons.saas_core.models.saas_instance_repo.http_requests.get', side_effect=reads) as get:
            self.assertTrue(repo._delete_branch_on_provider('stage'))
            self.assertEqual(get.call_count, 2)

    def test_delete_github_branch_422_does_not_hide_existing_or_inaccessible(self):
        repo = self._branch_delete_repo()
        for reference_status, repository_status in [(200, 200), (404, 403), (404, 404)]:
            response, reads = self._delete_branch_responses(reference_status, repository_status)
            with patch.object(type(repo), '_branch_provider_context', return_value=(
                    'github', 'https://api.github.com', 'test', 'project', 'test-token')), \
                    patch('odoo.addons.saas_core.models.saas_instance_repo.http_requests.delete', return_value=response), \
                    patch('odoo.addons.saas_core.models.saas_instance_repo.http_requests.get', side_effect=reads):
                with self.assertRaisesRegex(UserError, 'uncheck'):
                    repo._delete_branch_on_provider('stage')
