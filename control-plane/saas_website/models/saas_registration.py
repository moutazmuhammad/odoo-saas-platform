import hmac
import logging
import random
import string
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.addons.saas_core.fields import EncryptedChar

_logger = logging.getLogger(__name__)

OTP_EXPIRY_MINUTES = 10
OTP_LENGTH = 6


class SaasRegistrationOtp(models.Model):
    _name = 'saas.registration.otp'
    _description = 'SaaS Registration OTP'

    identifier = fields.Char(
        required=True, index=True,
        help='Email address or phone number the code was sent to.',
    )
    channel = fields.Selection(
        [('email', 'Email'), ('phone', 'Phone')],
        required=True,
    )
    # SEC-013: the code is encrypted at rest (reuses the SEC-002 key), so a
    # read of the control-plane DB cannot reveal live verification codes when a
    # saas_secret_key is configured. Reads decrypt transparently, so the email
    # template (object.code) and _verify keep working unchanged.
    code = EncryptedChar(required=True)
    expires_at = fields.Datetime(required=True)
    verified = fields.Boolean(default=False)

    # ---- Generate & Send ----

    @api.model
    def _generate_code(self):
        return ''.join(random.choices(string.digits, k=OTP_LENGTH))

    @api.model
    def _generate_and_send_email(self, email):
        """Generate OTP, send via email. Returns the record."""
        self.search([
            ('identifier', '=', email),
            ('channel', '=', 'email'),
        ]).unlink()

        code = self._generate_code()
        expires_at = fields.Datetime.now() + timedelta(minutes=OTP_EXPIRY_MINUTES)
        record = self.create({
            'identifier': email,
            'channel': 'email',
            'code': code,
            'expires_at': expires_at,
        })

        template = self.env.ref(
            'saas_website.mail_template_registration_otp',
            raise_if_not_found=False,
        )
        if template:
            template.send_mail(record.id, force_send=True)
        else:
            _logger.warning("OTP mail template not found — code for %s: %s", email, code)

        _logger.info("Email OTP sent to %s (expires %s)", email, expires_at)
        return record

    @api.model
    def _generate_and_send_phone(self, phone):
        """Generate OTP, send via SMS. Returns the record."""
        self.search([
            ('identifier', '=', phone),
            ('channel', '=', 'phone'),
        ]).unlink()

        code = self._generate_code()
        expires_at = fields.Datetime.now() + timedelta(minutes=OTP_EXPIRY_MINUTES)
        record = self.create({
            'identifier': phone,
            'channel': 'phone',
            'code': code,
            'expires_at': expires_at,
        })

        # Try Odoo SMS module if installed
        sms_sent = False
        if 'sms.api' in self.env:
            try:
                self.env['sms.api']._send_sms_batch([{
                    'res_id': record.id,
                    'number': phone,
                    'content': 'Your verification code is: %s' % code,
                }])
                sms_sent = True
            except Exception:
                _logger.exception("SMS send failed for %s", phone)

        if not sms_sent:
            _logger.warning(
                "SMS not sent (no provider) — phone OTP for %s: %s", phone, code,
            )

        _logger.info("Phone OTP generated for %s (expires %s)", phone, expires_at)
        return record

    # ---- Verify ----

    @api.model
    def _verify(self, identifier, code, channel):
        """Verify *code* for *identifier* on *channel*. Returns True on success.

        The stored code is encrypted (non-deterministic), so we can't match it
        in SQL — fetch the single live record (``_generate_*`` keeps only one
        per identifier+channel) and compare in constant time."""
        record = self.search([
            ('identifier', '=', identifier),
            ('channel', '=', channel),
            ('verified', '=', False),
            ('expires_at', '>=', fields.Datetime.now()),
        ], limit=1)
        if not record:
            return False
        if not hmac.compare_digest(record.code or '', code or ''):
            return False
        record.verified = True
        return True

    @api.model
    def _cleanup(self, identifier):
        """Remove all OTP records for this identifier."""
        self.search([('identifier', '=', identifier)]).unlink()

    # ---- Housekeeping ----

    @api.autovacuum
    def _gc_expired_otps(self):
        """Remove expired OTP records (called by Odoo's auto-vacuum)."""
        self.search([
            ('expires_at', '<', fields.Datetime.now()),
        ]).unlink()
