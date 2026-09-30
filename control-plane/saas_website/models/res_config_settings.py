from odoo import fields, models

OTP_TEST_MODE_PARAM = 'saas_website.otp_test_mode'


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    saas_otp_test_mode = fields.Boolean(
        string='Show Sign-up Code On Screen',
        config_parameter=OTP_TEST_MODE_PARAM,
        help='Testing only, while no SMS provider is set up: the sign-up '
             'page shows the verification code instead of relying on SMS. '
             'Anyone can then register with any phone number. Turn it off '
             'before going live.',
    )
