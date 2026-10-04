import logging
import re

from odoo import http, _
from odoo.http import request
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
PHONE_RE = re.compile(r'^\+?[\d\s\-\(\)]{7,20}$')


def _registration_identity_error(env, email, phone, country_id):
    """Reserve identifiers for login accounts, including disabled accounts.

    A historical CRM/billing contact without a login does not own an account
    identifier. Registration always creates a separate partner; matching an
    old contact never grants access to that contact's billing records.
    """
    duplicate = _("We can't create an account with these details. If you already have one, please sign in instead.")
    Users = env['res.users'].sudo().with_context(active_test=False)
    if Users.search_count(['|', ('login', '=ilike', email.strip()), ('partner_id.email', '=ilike', email.strip())]):
        return duplicate
    try:
        country = env['res.country'].sudo().browse(int(country_id)).exists()
    except (TypeError, ValueError):
        country = env['res.country']
    if not country:
        return _('Please select a valid country.')
    if 'saas.iam.member' in env:
        try:
            env['saas.iam.member']._check_unique_phone(phone, country=country)
        except ValidationError:
            return duplicate
    elif Users.search_count(['|', ('partner_id.phone', '=', phone), ('partner_id.mobile', '=', phone)]):
        return duplicate
    return None


class SaasRegistration(http.Controller):

    # ------------------------------------------------------------------
    #  Helpers
    # ------------------------------------------------------------------

    def _throttled(self, scope, limit, window_seconds, key=None):
        """True if this client has exceeded ``limit`` hits for ``scope`` in
        the window. Keyed by client IP (honouring the nginx proxy) + key."""
        req = request.httprequest
        xff = req.headers.get('X-Forwarded-For', '')
        ip = (xff.split(',')[0].strip() if xff else (req.remote_addr or '-'))
        full_key = '%s|%s' % (ip, key) if key else ip
        allowed, _retry = request.env['saas.rate.limit'].sudo()._hit(
            scope, full_key, limit, window_seconds)
        return not allowed

    def _registration_context(self, form_values=None, error=None,
                               otp_sent=False):
        """Build common template context for the registration page."""
        fv = form_values or {}
        product_id = int(fv.get('product_id') or 0)
        plan_id = int(fv.get('plan_id') or 0)
        product = plan = None
        if product_id:
            product = request.env['saas.product'].sudo().browse(product_id)
            if not product.exists():
                product = None
        if plan_id:
            plan = request.env['saas.plan'].sudo().browse(plan_id)
            if not plan.exists():
                plan = None

        countries = request.env['res.country'].sudo().search([], order='name')

        return {
            'product': product,
            'plan': plan,
            'is_trial': fv.get('is_trial') == '1',
            'countries': countries,
            'form_values': fv,
            'error': error,
            'otp_sent': otp_sent,
        }

    def _validate_registration_fields(self, post):
        """Validate all required registration fields. Returns error string or None."""
        name = (post.get('name') or '').strip()
        email = (post.get('email') or '').strip()
        phone = (post.get('phone') or '').strip()
        company_name = (post.get('company_name') or '').strip()
        country_id = post.get('country_id')
        city = (post.get('city') or '').strip()
        password = post.get('password') or ''
        confirm = post.get('confirm_password') or ''

        if not name:
            return _("Full name is required.")
        if not email or not EMAIL_RE.match(email):
            return _("A valid email address is required.")
        if not phone or not PHONE_RE.match(phone):
            return _("A valid phone number is required (7-20 digits).")
        # company_name is optional
        if not country_id or country_id == '':
            return _("Please select your country.")
        if not city:
            return _("City is required.")
        if len(password) < 8:
            return _("Password must be at least 8 characters.")
        if password != confirm:
            return _("Passwords do not match.")

        error = _registration_identity_error(request.env, email, phone, country_id)
        if error:
            return error

        # Validate phone matches the selected country
        if country_id:
            country = request.env['res.country'].sudo().browse(int(country_id))
            if country.exists():
                try:
                    from odoo.addons.phone_validation.tools.phone_validation import phone_format
                    phone_format(
                        phone, country.code, country.phone_code,
                        force_format='E164', raise_exception=True,
                    )
                except Exception:
                    return _(
                        "The phone number '%s' is not valid for %s. "
                        "Please enter a number that matches your country.",
                        phone, country.name,
                    )

        return None

    def _build_redirect_url(self, post):
        """Build the configure page URL to redirect to after registration."""
        # Hosting flow
        if post.get('hosting') == '1':
            params = []
            for key in ('workers', 'storage', 'billing', 'odoo_version_id',
                        'region_id', 'support_code'):
                val = post.get(key)
                if val:
                    params.append('%s=%s' % (key, val))
            if post.get('is_trial') == '1':
                params.append('is_trial=1')
            url = '/hosting/configure'
            if params:
                url += '?' + '&'.join(params)
            return url

        # Service flow
        product_id = int(post.get('product_id') or 0)
        plan_id = int(post.get('plan_id') or 0)
        is_trial = post.get('is_trial') == '1'
        if product_id and plan_id:
            url = '/services/%d/plans/%d/configure' % (product_id, plan_id)
            if is_trial:
                url += '?trial=1'
            return url
        return '/services'

    # ------------------------------------------------------------------
    #  Step 1: Show form / validate & send OTP
    # ------------------------------------------------------------------

    @http.route('/services/register', type='http', auth='public',
                website=True, methods=['GET', 'POST'],
                sitemap=False)
    def register_form(self, **post):
        # If already logged in, skip registration
        if not request.env.user._is_public():
            return request.redirect(self._build_redirect_url(post))

        if request.httprequest.method == 'GET':
            return request.render(
                'saas_website.registration_form',
                self._registration_context(form_values=post),
            )

        # POST: validate fields then send OTP
        phone = post.get('phone', '').strip()
        # Throttle OTP sending (SMS-bomb / probing): by IP and by target phone.
        if (self._throttled('register_start', 10, 600)
                or self._throttled('otp_send', 4, 600, key=phone)):
            return request.render(
                'saas_website.registration_form',
                self._registration_context(
                    form_values=post,
                    error=_("Too many attempts. Please wait a few minutes and "
                            "try again."),
                ),
            )
        error = self._validate_registration_fields(post)
        if error:
            return request.render(
                'saas_website.registration_form',
                self._registration_context(form_values=post, error=error),
            )

        # Send phone OTP
        OTP = request.env['saas.registration.otp'].sudo()
        try:
            OTP._generate_and_send_phone(phone)
        except Exception:
            _logger.exception("Failed to send OTP to %s", phone)
            return request.render(
                'saas_website.registration_form',
                self._registration_context(
                    form_values=post,
                    error=_("Failed to send verification code. "
                            "Please try again."),
                ),
            )

        ctx = self._registration_context(form_values=post, otp_sent=True)
        return request.render('saas_website.registration_form', ctx)

    # ------------------------------------------------------------------
    #  Step 2: Verify OTP, create partner + user, log in
    # ------------------------------------------------------------------

    @http.route('/services/register/verify', type='http', auth='public',
                website=True, methods=['POST'], csrf=True, sitemap=False)
    def register_verify(self, **post):
        phone = (post.get('phone') or '').strip()
        phone_otp = (post.get('phone_otp') or '').strip()

        if not phone_otp:
            return request.render(
                'saas_website.registration_form',
                self._registration_context(
                    form_values=post, otp_sent=True,
                    error=_("Please enter the verification code."),
                ),
            )

        # A 6-digit code is 1M combinations — throttle so it can't be walked
        # within the validity window.
        if self._throttled('otp_verify', 6, 600, key=phone):
            return request.render(
                'saas_website.registration_form',
                self._registration_context(
                    form_values=post, otp_sent=True,
                    error=_("Too many attempts. Please wait a few minutes and "
                            "try again."),
                ),
            )

        # Verify phone OTP
        OTP = request.env['saas.registration.otp'].sudo()
        if not OTP._verify(phone, phone_otp, 'phone'):
            return request.render(
                'saas_website.registration_form',
                self._registration_context(
                    form_values=post, otp_sent=True,
                    error=_("Invalid or expired verification code. "
                            "Please try again or resend."),
                ),
            )

        # OTP verified — create partner + user
        name = (post.get('name') or '').strip()
        email = (post.get('email') or '').strip()
        company_name = (post.get('company_name') or '').strip()
        country_id = int(post.get('country_id') or 0)
        city = (post.get('city') or '').strip()
        street = (post.get('street') or '').strip()
        job_title = (post.get('job_title') or '').strip()
        password = post.get('password', '')

        try:
            # Recheck both identifiers after OTP verification and hold the
            # phone claim lock through account creation.
            error = _registration_identity_error(request.env, email, phone, country_id)
            if error:
                return request.render(
                    'saas_website.registration_form',
                    self._registration_context(form_values=post, error=error),
                )

            # Create partner with full contact details
            partner_vals = {
                'name': name,
                'email': email,
                'phone': phone,
                'city': city,
                'function': job_title or False,
            }
            if company_name:
                partner_vals['company_name'] = company_name
            if country_id:
                partner_vals['country_id'] = country_id
            if street:
                partner_vals['street'] = street
            partner = request.env['res.partner'].sudo().create(partner_vals)

            # Create portal user linked to this partner
            new_user = request.env['res.users'].sudo().with_context(
                no_reset_password=True,
            ).create({
                'name': name,
                'login': email,
                'partner_id': partner.id,
                'groups_id': [
                    (6, 0, [request.env.ref('base.group_portal').id]),
                ],
            })

            # Set password explicitly (triggers proper hashing)
            new_user.password = password

            # Clean up used OTP records
            OTP._cleanup(phone)

            # Commit so authenticate() can see the new user
            # (it opens its own cursor)
            request.env.cr.commit()

            # Log the user in
            request.session.authenticate(request.db, {
                'login': email,
                'password': password,
                'type': 'password',
            })

            _logger.info(
                "New SaaS customer registered: %s (%s, %s)",
                name, email, company_name,
            )

        except Exception as exc:
            _logger.exception("Registration failed for %s", email)
            return request.render(
                'saas_website.registration_form',
                self._registration_context(
                    form_values=post,
                    error=_("Account creation failed: %s") % str(exc),
                ),
            )

        # Redirect to configure page
        return request.redirect(self._build_redirect_url(post))
