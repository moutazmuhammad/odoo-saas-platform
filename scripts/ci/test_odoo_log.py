import unittest
from check_odoo_log import check_log


class OdooLogTests(unittest.TestCase):
    def test_success(self):
        self.assertEqual(check_log("INFO 0 failed, 0 error(s) of 42 tests when loading database 'ci'"), 42)

    def test_successful_module_cannot_hide_another_failure(self):
        with self.assertRaises(ValueError):
            check_log("0 failed, 0 error(s) of 8 tests\n"
                      "1 failed, 0 error(s) of 42 tests when loading database 'ci'")

    def test_module_errors_cannot_hide_behind_clean_final_summary(self):
        with self.assertRaises(ValueError):
            check_log("0 failed, 2 error(s) of 8 tests\n"
                      "0 failed, 0 error(s) of 42 tests when loading database 'ci'")

    def test_shutdown_without_summary(self):
        with self.assertRaises(ValueError):
            check_log('odoo.service.server: Initiating shutdown')

    def test_zero_tests_is_not_success(self):
        with self.assertRaises(ValueError):
            check_log("0 failed, 0 error(s) of 0 tests when loading database 'ci'")
