"""Plain unittest (no Odoo/DB needed) for scripts/lint_csrf_routes.py's
actual rule. Run with: python3 -m unittest control-plane/scripts/test_lint_csrf_routes.py
or (from control-plane/): python3 -m unittest scripts.test_lint_csrf_routes
"""
import unittest

from lint_csrf_routes import check_source


class TestCsrfLint(unittest.TestCase):
    def test_flags_csrf_false_with_no_methods_restriction(self):
        src = (
            "from odoo import http\n"
            "class C(http.Controller):\n"
            "    @http.route('/x', type='http', auth='user', csrf=False)\n"
            "    def x(self, **kw): pass\n"
        )
        v = check_source(src)
        self.assertEqual(len(v), 1)
        self.assertIn('no methods=', v[0])

    def test_flags_csrf_false_with_a_mutating_method(self):
        src = (
            "from odoo import http\n"
            "class C(http.Controller):\n"
            "    @http.route('/x', type='http', auth='user', "
            "methods=['GET', 'POST'], csrf=False)\n"
            "    def x(self, **kw): pass\n"
        )
        v = check_source(src)
        self.assertEqual(len(v), 1)
        self.assertIn('non-GET/HEAD verb', v[0])

    def test_allows_auth_none(self):
        src = (
            "from odoo import http\n"
            "class C(http.Controller):\n"
            "    @http.route('/x', type='http', auth='none', csrf=False)\n"
            "    def x(self, **kw): pass\n"
        )
        self.assertEqual(check_source(src), [])

    def test_allows_get_only(self):
        src = (
            "from odoo import http\n"
            "class C(http.Controller):\n"
            "    @http.route('/x', type='http', auth='user', "
            "methods=['GET'], csrf=False)\n"
            "    def x(self, **kw): pass\n"
        )
        self.assertEqual(check_source(src), [])

    def test_allows_get_and_head(self):
        src = (
            "from odoo import http\n"
            "class C(http.Controller):\n"
            "    @http.route('/x', type='http', auth='user', "
            "methods=['GET', 'HEAD'], csrf=False)\n"
            "    def x(self, **kw): pass\n"
        )
        self.assertEqual(check_source(src), [])

    def test_ignores_routes_without_explicit_csrf_kwarg(self):
        """No csrf= at all means Odoo's own default (True for type='http')
        applies — not this lint's concern."""
        src = (
            "from odoo import http\n"
            "class C(http.Controller):\n"
            "    @http.route('/x', type='http', auth='user')\n"
            "    def x(self, **kw): pass\n"
        )
        self.assertEqual(check_source(src), [])

    def test_ignores_csrf_true(self):
        src = (
            "from odoo import http\n"
            "class C(http.Controller):\n"
            "    @http.route('/x', type='http', auth='user', csrf=True)\n"
            "    def x(self, **kw): pass\n"
        )
        self.assertEqual(check_source(src), [])

    def test_flags_non_literal_csrf_for_manual_review(self):
        src = (
            "from odoo import http\n"
            "class C(http.Controller):\n"
            "    @http.route('/x', type='http', auth='user', csrf=some_flag)\n"
            "    def x(self, **kw): pass\n"
        )
        v = check_source(src)
        self.assertEqual(len(v), 1)
        self.assertIn('not a literal', v[0])

    def test_ignores_non_route_decorators(self):
        src = (
            "class C:\n"
            "    @staticmethod\n"
            "    def x(): pass\n"
        )
        self.assertEqual(check_source(src), [])

    def test_syntax_error_reported_not_raised(self):
        v = check_source("def broken(:\n", filename='broken.py')
        self.assertEqual(len(v), 1)
        self.assertIn('SyntaxError', v[0])


if __name__ == '__main__':
    unittest.main()
