"""Include the separately limited cron sidecar in stored tenant margins."""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    instances = env['saas.instance'].with_context(active_test=False).search([])
    env.add_to_compute(instances._fields['monthly_cost'], instances)
    instances._recompute_recordset()
