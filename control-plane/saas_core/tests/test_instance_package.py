from psycopg2 import IntegrityError

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger


@tagged('post_install', '-at_install')
class TestSaasInstancePackage(TransactionCase):
    """saas.instance.package: the One2many side of an instance's pip
    requirements. create/write/unlink must keep saas.instance.pip_packages
    (the text field admins/customers actually edit) in sync via
    _sync_text_from_packages(), and the model enforces non-empty,
    per-instance-unique package names."""

    def setUp(self):
        super().setUp()
        self.product = self.env['saas.product'].sudo().search(
            [('is_hosting', '=', False)], limit=1) or self.env[
            'saas.product'].sudo().create({'name': 'Pkg Product'})
        self.plan = self.env['saas.plan'].sudo().create({
            'name': 'Pkg Plan', 'is_custom': True, 'workers': 2,
            'storage_limit': 10, 'cpu_limit': 1.0, 'ram_limit': '1g',
            'price': 30.0, 'yearly_price': 288.0,
            'currency_id': self.env.company.currency_id.id,
            'saas_product_ids': [(6, 0, [self.product.id])],
        })
        self.partner = self.env['res.partner'].sudo().create({'name': 'Pkg Cust'})
        self.domain = self.env['saas.based.domain'].sudo().search([], limit=1) \
            or self.env['saas.based.domain'].sudo().create(
                {'name': 'pkg.example.com'})
        self.instance = self.env['saas.instance'].sudo().create({
            'subdomain': 'pkginst', 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'billing_period': 'monthly',
            'state': 'running',
        })
        self.Package = self.env['saas.instance.package'].sudo()

    def test_create_strips_whitespace_and_syncs_text(self):
        pkg = self.Package.create({
            'instance_id': self.instance.id, 'name': '  phonenumbers  ',
        })
        self.assertEqual(pkg.name, 'phonenumbers')
        self.assertEqual(self.instance.pip_packages, 'phonenumbers')

    def test_write_strips_whitespace_and_syncs_text(self):
        pkg = self.Package.create({
            'instance_id': self.instance.id, 'name': 'openpyxl',
        })
        pkg.write({'name': '  openpyxl==3.1.0  '})
        self.assertEqual(pkg.name, 'openpyxl==3.1.0')
        self.assertEqual(self.instance.pip_packages, 'openpyxl==3.1.0')

    def test_unlink_syncs_text(self):
        pkg = self.Package.create({
            'instance_id': self.instance.id, 'name': 'requests',
        })
        self.assertEqual(self.instance.pip_packages, 'requests')
        pkg.unlink()
        self.assertFalse(self.instance.pip_packages)

    def test_multiple_packages_sync_as_lines(self):
        self.Package.create({'instance_id': self.instance.id, 'name': 'a'})
        self.Package.create({'instance_id': self.instance.id, 'name': 'b'})
        self.assertEqual(self.instance.pip_packages, 'a\nb')

    def test_empty_name_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.Package.create({'instance_id': self.instance.id, 'name': '   '})

    def test_duplicate_name_per_instance_is_rejected(self):
        self.Package.create({'instance_id': self.instance.id, 'name': 'dupe'})
        with mute_logger('odoo.sql_db'):
            with self.assertRaises(IntegrityError):
                with self.env.cr.savepoint():
                    self.Package.create({
                        'instance_id': self.instance.id, 'name': 'dupe',
                    })

    def test_same_name_allowed_on_different_instances(self):
        other = self.env['saas.instance'].sudo().create({
            'subdomain': 'pkginst2', 'domain_id': self.domain.id,
            'partner_id': self.partner.id, 'saas_product_id': self.product.id,
            'plan_id': self.plan.id, 'billing_period': 'monthly',
            'state': 'running',
        })
        self.Package.create({'instance_id': self.instance.id, 'name': 'shared'})
        # Same name on a different instance must not raise.
        self.Package.create({'instance_id': other.id, 'name': 'shared'})
        self.assertEqual(self.instance.pip_packages, 'shared')
        self.assertEqual(other.pip_packages, 'shared')
