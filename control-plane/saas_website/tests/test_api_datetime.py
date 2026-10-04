from datetime import datetime, timedelta, timezone

from odoo.tests.common import TransactionCase, tagged

from ..controllers.api import _utc_datetime


@tagged('post_install', '-at_install')
class TestApiDatetime(TransactionCase):
    def test_odoo_naive_datetime_is_explicit_utc(self):
        self.assertEqual(_utc_datetime(datetime(2026, 10, 4, 12, 30)),
                         '2026-10-04T12:30:00Z')
        self.assertEqual(_utc_datetime('2026-10-04 12:30:00'),
                         '2026-10-04T12:30:00Z')

    def test_aware_datetime_is_normalized_to_utc(self):
        self.assertEqual(_utc_datetime(datetime(2026, 10, 4, 15, 30,
                         tzinfo=timezone(timedelta(hours=3)))),
                         '2026-10-04T12:30:00Z')

    def test_missing_datetime(self):
        self.assertEqual(_utc_datetime(False), '')
        self.assertEqual(_utc_datetime(None), '')
