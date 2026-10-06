"""Drop the retired replica-tier table once every module has upgraded.

Odoo removes the model but leaves its table behind; nothing reads it now.
"""


def migrate(cr, version):
    cr.execute('DROP TABLE IF EXISTS saas_compute_tier CASCADE')
