from odoo.tests.common import tagged

from .test_portal_security import _PortalTestBase


@tagged('post_install', '-at_install')
class TestReactivatePage(_PortalTestBase):
    """The page behind the SPA's Reactivate button on a cancelled project."""

    def test_owner_gets_the_plan_builder_for_a_cancelled_project(self):
        self.instance.state = 'cancelled'
        self.authenticate('portalowner@example.com', 'ownerpass123')
        res = self.url_open('/my/instances/%s/reactivate' % self.instance.id,
                            allow_redirects=False)
        self.assertEqual(res.status_code, 200)
        self.assertIn('do-reactivate', res.text)

    def test_running_project_is_sent_back(self):
        self.authenticate('portalowner@example.com', 'ownerpass123')
        res = self.url_open('/my/instances/%s/reactivate' % self.instance.id,
                            allow_redirects=False)
        self.assertIn(res.status_code, (301, 302, 303))
        self.assertNotIn('reactivate', res.headers['Location'])

    def test_other_customers_cannot_open_it(self):
        self.instance.state = 'cancelled'
        self.authenticate('portalintruder@example.com', 'intruderpass123')
        res = self.url_open('/my/instances/%s/reactivate' % self.instance.id,
                            allow_redirects=False)
        self.assertIn(res.status_code, (301, 302, 303))
        self.assertTrue(res.headers['Location'].endswith('/my/instances'))

    def test_detail_api_points_the_button_at_it(self):
        self.instance.state = 'cancelled'
        self.authenticate('portalowner@example.com', 'ownerpass123')
        result = self._json_call('/saas/api/v1/instances/%s' % self.instance.id)
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['data']['is_cancelled'])
        self.assertEqual(result['data']['reactivate_url'],
                         '/my/instances/%s/reactivate' % self.instance.id)
