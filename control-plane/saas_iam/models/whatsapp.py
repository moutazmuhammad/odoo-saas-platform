"""Shared Meta WhatsApp delivery for registration and teammate verification."""
import re
import secrets
from datetime import timedelta

import requests
import phonenumbers

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.addons.saas_core.fields import EncryptedChar


class WhatsAppCompany(models.Model):
    _inherit = 'res.company'

    saas_whatsapp_phone_id = fields.Char(groups='base.group_system')
    saas_whatsapp_access_token = EncryptedChar(groups='base.group_system', copy=False)
    saas_whatsapp_template = fields.Char(groups='base.group_system')
    saas_whatsapp_language = fields.Char(default='en_US', groups='base.group_system')
    saas_whatsapp_api_version = fields.Char(groups='base.group_system')


class WhatsAppSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    saas_teammate_phone_test_codes = fields.Boolean(
        string='Show teammate verification codes for testing',
        config_parameter='saas_iam.teammate_phone_test_codes',
        groups='base.group_system')

    saas_whatsapp_phone_id = fields.Char(related='company_id.saas_whatsapp_phone_id', readonly=False)
    saas_whatsapp_access_token = fields.Char(related='company_id.saas_whatsapp_access_token', readonly=False)
    saas_whatsapp_template = fields.Char(related='company_id.saas_whatsapp_template', readonly=False)
    saas_whatsapp_language = fields.Char(related='company_id.saas_whatsapp_language', readonly=False)
    saas_whatsapp_api_version = fields.Char(related='company_id.saas_whatsapp_api_version', readonly=False)


class WhatsAppVerification(models.AbstractModel):
    _name = 'saas.iam.whatsapp'
    _description = 'WhatsApp verification delivery'

    @api.model
    def _is_configured(self):
        company = self.env.company.sudo()
        return bool(re.fullmatch(r'[0-9]+', (company.saas_whatsapp_phone_id or '').strip())
            and re.fullmatch(r'v[0-9]+\.0', (company.saas_whatsapp_api_version or '').strip())
            and re.fullmatch(r'[a-z0-9_]+', (company.saas_whatsapp_template or '').strip())
            and re.fullmatch(r'[a-z]{2,3}(?:_[A-Z]{2})?', (company.saas_whatsapp_language or '').strip())
            and company.saas_whatsapp_access_token)

    @api.model
    def _test_codes_enabled(self):
        enabled = self.env['ir.config_parameter'].sudo().get_param('saas_iam.teammate_phone_test_codes', 'False')
        return str(enabled).lower() in ('true', '1') and not self._is_configured()

    @api.model
    def _recipient(self, phone):
        try:
            number = phonenumbers.parse(phone, None)
        except phonenumbers.NumberParseException as exc:
            raise ValidationError(_('Enter a valid phone number with its country code.')) from exc
        if not phonenumbers.is_valid_number(number):
            raise ValidationError(_('Enter a valid mobile phone number.'))
        return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164).lstrip('+')

    @api.model
    def _send_code(self, phone, code):
        company = self.env.company.sudo()
        phone_id = (company.saas_whatsapp_phone_id or '').strip()
        version = (company.saas_whatsapp_api_version or '').strip()
        template = (company.saas_whatsapp_template or '').strip()
        language = (company.saas_whatsapp_language or '').strip()
        token = company.saas_whatsapp_access_token
        if not self._is_configured():
            raise UserError(_('WhatsApp verification is not configured. Please contact support.'))
        recipient = self._recipient(phone)
        payload = {
            'messaging_product': 'whatsapp', 'to': recipient, 'type': 'template',
            'template': {'name': template, 'language': {'code': language}, 'components': [
                {'type': 'body', 'parameters': [{'type': 'text', 'text': code}]},
                {'type': 'button', 'sub_type': 'url', 'index': '0',
                 'parameters': [{'type': 'text', 'text': code}]},
            ]},
        }
        try:
            response = requests.post(
                'https://graph.facebook.com/%s/%s/messages' % (version, phone_id),
                headers={'Authorization': 'Bearer ' + token}, json=payload,
                timeout=(5, 15), allow_redirects=False)
            result = response.json() if response.ok else {}
        except (requests.RequestException, ValueError) as exc:
            raise UserError(_('Could not send the WhatsApp verification code. Please try again or contact support.')) from exc
        if not response.ok or not any(m.get('id') for m in result.get('messages', [])):
            raise UserError(_('Could not send the WhatsApp verification code. Please try again or contact support.'))


class WhatsAppRegistrationOtp(models.Model):
    _inherit = 'saas.registration.otp'

    @api.model
    def _generate_and_send_phone(self, phone):
        # Keep the registration identifier unchanged so its existing verify
        # endpoint uses exactly the challenge that was sent over WhatsApp.
        self.search([('identifier', '=', phone), ('channel', '=', 'phone')]).unlink()
        code = '%06d' % secrets.randbelow(1000000)
        sender = self.env['saas.iam.whatsapp']
        if sender._is_configured() or not self.env['ir.config_parameter'].sudo().get_param(
                'saas_website.otp_test_mode'):
            sender._send_code(phone, code)
        else:
            # "Show Sign-up Code On Screen" without WhatsApp: the sign-up API
            # returns this code to the page instead of delivering it.
            sender._recipient(phone)
        return self.create({'identifier': phone, 'channel': 'phone', 'code': code,
                            'expires_at': fields.Datetime.now() + timedelta(minutes=10)})
