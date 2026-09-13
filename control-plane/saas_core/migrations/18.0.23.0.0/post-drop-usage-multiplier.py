"""Remove the retired Resource Usage Multiplier setting.

The multiplier (saas_master.resource_usage_multiplier) scaled the live
CPU/RAM monitoring figures. It has been removed from the code and UI;
the data record lived in a noupdate block, so Odoo won't orphan-clean it
— delete the parameter and its ir.model.data anchor here.
"""


def migrate(cr, version):
    cr.execute(
        "DELETE FROM ir_config_parameter WHERE key = %s",
        ('saas_master.resource_usage_multiplier',),
    )
    cr.execute(
        "DELETE FROM ir_model_data WHERE module = %s AND name = %s",
        ('saas_core', 'config_param_resource_usage_multiplier'),
    )
