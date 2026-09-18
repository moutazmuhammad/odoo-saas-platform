"""Plain unittest (no Odoo/DB needed) for scripts/lint_addon_boundaries.py's
actual rule. Run with:
python3 -m unittest control-plane/scripts/test_lint_addon_boundaries.py
or (from control-plane/): python3 -m unittest scripts.test_lint_addon_boundaries
"""
import unittest

from lint_addon_boundaries import check_source


class TestBoundaryLint(unittest.TestCase):
    def test_flags_cross_addon_private_call(self):
        src = (
            "class Foo:\n"
            "    def bar(self):\n"
            "        self.env['saas.wallet']._refund_move(self)\n"
        )
        v = check_source(src, addon='saas_core',
                          owners={'saas.wallet': 'saas_billing'})
        self.assertEqual(len(v), 1)
        self.assertIn('saas_billing', v[0])

    def test_allows_same_addon_private_call(self):
        src = (
            "class Foo:\n"
            "    def bar(self):\n"
            "        self.env['saas.wallet']._refund_move(self)\n"
        )
        v = check_source(src, addon='saas_billing',
                          owners={'saas.wallet': 'saas_billing'})
        self.assertEqual(v, [])

    def test_allows_public_method_call(self):
        src = (
            "class Foo:\n"
            "    def bar(self):\n"
            "        self.env['saas.wallet'].for_partner(self)\n"
        )
        v = check_source(src, addon='saas_core',
                          owners={'saas.wallet': 'saas_billing'})
        self.assertEqual(v, [])

    def test_allows_unowned_model(self):
        # e.g. a base Odoo model never registered in `owners`.
        src = (
            "class Foo:\n"
            "    def bar(self):\n"
            "        self.env['account.move']._post()\n"
        )
        v = check_source(src, addon='saas_core', owners={})
        self.assertEqual(v, [])

    def test_ignores_dunder_methods(self):
        src = (
            "class Foo:\n"
            "    def bar(self):\n"
            "        self.env['saas.wallet'].__class__(self)\n"
        )
        v = check_source(src, addon='saas_core',
                          owners={'saas.wallet': 'saas_billing'})
        self.assertEqual(v, [])

    def test_sees_through_sudo_and_with_context(self):
        src = (
            "class Foo:\n"
            "    def bar(self):\n"
            "        self.env['saas.wallet'].sudo().with_context(x=1)._refund_move(self)\n"
        )
        v = check_source(src, addon='saas_core',
                          owners={'saas.wallet': 'saas_billing'})
        self.assertEqual(len(v), 1)

    def test_ignores_non_env_attribute_calls(self):
        src = (
            "class Foo:\n"
            "    def bar(self):\n"
            "        self._some_local_helper()\n"
        )
        v = check_source(src, addon='saas_core',
                          owners={'saas.wallet': 'saas_billing'})
        self.assertEqual(v, [])


if __name__ == '__main__':
    unittest.main()
