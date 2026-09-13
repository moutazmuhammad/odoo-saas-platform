# -*- coding: utf-8 -*-
"""JSON API consumed by the VELTNEX single-page frontend.

Every endpoint speaks Odoo's ``type='json'`` (JSON-RPC 2.0) protocol and
returns a uniform envelope::

    {"ok": true,  "data": <payload>}
    {"ok": false, "error": "<friendly message>", "code": "<machine code>"}

Auth is the ordinary Odoo session cookie — because the SPA is served from
the same origin (see ``spa.py``), the browser sends it automatically. We
keep ``auth='public'`` on protected endpoints and check the user ourselves
so we can return a clean ``auth_required`` envelope instead of an Odoo
redirect/exception that the SPA would have to special-case.

This controller deliberately contains NO business logic: ordering,
billing, plan changes, and payment all stay on the existing QWeb routes
(``/services/order``, ``/hosting/order``, ``/my/instances/<id>/checkout``,
…). The SPA hands off to those with a normal navigation when money is
involved.
"""
import hmac
import logging
import re

from odoo import http, fields, _
from odoo.exceptions import AccessError, MissingError, UserError, ValidationError
from odoo.http import request

from odoo.addons.saas_core.models.saas_instance import SUBDOMAIN_RE
from .main import SaasWebsite, _ACTIVE_STATES

_logger = logging.getLogger(__name__)

# States shown in the customer's PROJECTS LIST and search. An instance still
# awaiting its first payment isn't a real project yet — it's an incomplete
# order — so it's excluded here (the customer reaches it from the Dashboard
# "Needs attention" alert, where they can either complete payment or abandon
# it). Every other active/cancelled state stays visible (cancelled instances
# remain reachable for reactivation).
_LIST_STATES = tuple(s for s in _ACTIVE_STATES if s != 'pending_payment')

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
PHONE_RE = re.compile(r'^\+?[\d\s\-\(\)]{7,20}$')

# Map account.move payment/state to the four labels the SPA renders.
_INVOICE_STATUS = {
    'paid': 'paid',
    'in_payment': 'paid',
    'partial': 'open',
    'not_paid': 'open',
    'reversed': 'draft',
}

# Map backup state to the SPA's StatusBadge vocabulary.
_BACKUP_STATUS = {'done': 'available', 'running': 'in_progress', 'failed': 'failed'}


def ok(data=None):
    return {'ok': True, 'data': data if data is not None else {}}


def err(message, code='error'):
    return {'ok': False, 'error': message, 'code': code}


class SaasApi(http.Controller):

    # ==================================================================
    #  Auth helpers
    # ==================================================================

    def _user(self):
        """Return the logged-in res.users, or None for the public user."""
        user = request.env.user
        return None if user._is_public() else user

    def _partner(self):
        user = self._user()
        return user.partner_id if user else None

    def _instance(self, instance_id, access_token=None, write=False):
        """Authorized sudo recordset for an instance the caller may act on.

        The ``access_token`` is a SHARE credential: it grants READ-ONLY
        visibility (status, metrics, builds) to anyone holding the link, and
        nothing more. Every state-changing or destructive operation must be
        performed by the authenticated owner (record rules) — pass
        ``write=True`` so the token is ignored and ownership is enforced.
        Raises AccessError/MissingError on failure.
        """
        Instance = request.env['saas.instance']
        instance = Instance.browse(int(instance_id))
        if not instance.exists():
            raise MissingError(_("Instance not found."))
        # Constant-time compare so the token can't be recovered by timing.
        if (not write and access_token
                and hmac.compare_digest(
                    instance.sudo().access_token or '', access_token)):
            return instance.sudo()
        # Record-rule check: will raise AccessError if not the owner.
        instance.check_access_rights('read')
        instance.check_access_rule('read')
        return instance.sudo()

    # ------------------------------------------------------------------
    #  Abuse protection (brute-force / enumeration / OTP-guessing)
    # ------------------------------------------------------------------

    def _client_ip(self):
        """Best-effort client IP, honouring the reverse proxy (nginx sets
        X-Forwarded-For). The left-most XFF entry is the original client."""
        req = request.httprequest
        xff = req.headers.get('X-Forwarded-For', '')
        if xff:
            return xff.split(',')[0].strip()
        return req.remote_addr or '-'

    def _rate_limit(self, scope, limit, window_seconds, key=None):
        """Register one hit for this client and return an ``err`` envelope if
        the limit is exceeded, else ``None``. Keyed by ``scope`` + the client
        IP (plus an optional extra ``key``, e.g. the target phone/login)."""
        ip = self._client_ip()
        full_key = '%s|%s' % (ip, key) if key else ip
        allowed, retry_after = request.env['saas.rate.limit'].sudo()._hit(
            scope, full_key, limit, window_seconds)
        if allowed:
            return None
        mins = max(1, round(retry_after / 60))
        return err(
            _("Too many attempts. Please wait about %d minute(s) and try "
              "again.") % mins,
            'rate_limited')

    # ==================================================================
    #  Session / account
    # ==================================================================

    @http.route('/saas/api/v1/me', type='json', auth='public')
    def me(self):
        partner = self._partner()
        if not partner:
            return err(_("Please sign in to continue."), 'auth_required')
        return ok(self._serialize_user(partner))

    @http.route('/saas/api/v1/auth/login', type='json', auth='public')
    def login(self, login=None, password=None, **kw):
        if not login or not password:
            return err(_("Email and password are required."), 'invalid')
        # Throttle brute-force: by IP, and (tighter) per targeted account.
        limited = (self._rate_limit('login', 15, 300)
                   or self._rate_limit('login_acct', 7, 300,
                                       key=(login or '').strip().lower()))
        if limited:
            return limited
        try:
            request.session.authenticate(
                request.db,
                {'login': login, 'password': password, 'type': 'password'},
            )
        except Exception:
            return err(
                _("Those credentials don't match an account. Please try again."),
                'invalid_credentials',
            )
        partner = self._partner()
        if not partner:
            return err(_("Sign-in failed. Please try again."), 'invalid_credentials')
        return ok(self._serialize_user(partner))

    @http.route('/saas/api/v1/auth/logout', type='json', auth='public')
    def logout(self):
        request.session.logout(keep_db=True)
        return ok({})

    # ------- Registration (phone OTP, mirrors registration.py) --------

    def _validate_registration(self, p):
        name = (p.get('name') or '').strip()
        email = (p.get('email') or '').strip()
        phone = (p.get('phone') or '').strip()
        password = p.get('password') or ''
        if not name:
            return _("Full name is required.")
        if not email or not EMAIL_RE.match(email):
            return _("A valid email address is required.")
        if not phone or not PHONE_RE.match(phone):
            return _("A valid phone number is required.")
        if not p.get('country_id'):
            return _("Please select your country.")
        if not (p.get('city') or '').strip():
            return _("City is required.")
        if len(password) < 8:
            return _("Password must be at least 8 characters.")
        # Uniqueness is enforced, but the message must NOT reveal which
        # identifier already exists (account enumeration): one generic line
        # for email (user OR partner) and one for phone.
        Users = request.env['res.users'].sudo()
        Partner = request.env['res.partner'].sudo()
        if (Users.search_count([('login', '=', email)])
                or Partner.search_count([('email', '=ilike', email)])):
            return _("We can't create an account with these details. If you "
                     "already have one, please sign in instead.")
        if Partner.search_count([('phone', '=', phone)]):
            return _("We can't create an account with these details. If you "
                     "already have one, please sign in instead.")
        return None

    @http.route('/saas/api/v1/auth/register/start', type='json', auth='public')
    def register_start(self, **p):
        if not request.env.user._is_public():
            return err(_("You're already signed in."), 'already_authenticated')
        # Throttle OTP sending: by IP and by target phone (SMS-bomb / probing).
        phone = (p.get('phone') or '').strip()
        limited = (self._rate_limit('register_start', 10, 600)
                   or self._rate_limit('otp_send', 4, 600, key=phone))
        if limited:
            return limited
        error = self._validate_registration(p)
        if error:
            return err(error, 'invalid')
        try:
            otp = request.env['saas.registration.otp'].sudo(
            )._generate_and_send_phone(phone)
        except Exception:
            _logger.exception("Failed to send registration OTP")
            return err(
                _("We couldn't send your verification code. Please try again."),
                'otp_send_failed',
            )
        # The verification code is delivered out-of-band (SMS / server log);
        # it must never be returned to the client.
        return ok({'otp_sent': True})

    @http.route('/saas/api/v1/auth/register/resend', type='json', auth='public')
    def register_resend(self, phone=None, **kw):
        phone = (phone or '').strip()
        if not phone:
            return err(_("Phone number is required."), 'invalid')
        limited = (self._rate_limit('register_resend', 10, 600)
                   or self._rate_limit('otp_send', 4, 600, key=phone))
        if limited:
            return limited
        try:
            otp = request.env['saas.registration.otp'].sudo(
            )._generate_and_send_phone(phone)
        except Exception:
            return err(_("Couldn't resend the code. Please try again."), 'otp_send_failed')
        return ok({'otp_sent': True})

    @http.route('/saas/api/v1/auth/register/verify', type='json', auth='public')
    def register_verify(self, **p):
        phone = (p.get('phone') or '').strip()
        code = (p.get('otp') or '').strip()
        if not code:
            return err(_("Please enter the verification code."), 'invalid')
        # A 6-digit code is 1M combinations — without this an attacker could
        # walk the whole space inside the 10-minute validity window.
        limited = self._rate_limit('otp_verify', 6, 600, key=phone)
        if limited:
            return limited
        OTP = request.env['saas.registration.otp'].sudo()
        if not OTP._verify(phone, code, 'phone'):
            return err(
                _("That code is invalid or expired. Please try again or resend."),
                'otp_invalid',
            )
        # Re-validate (guards against a race between start and verify).
        error = self._validate_registration(p)
        if error:
            return err(error, 'invalid')
        email = p.get('email').strip()
        password = p.get('password')
        try:
            partner_vals = {
                'name': p.get('name').strip(),
                'email': email,
                'phone': phone,
                'city': (p.get('city') or '').strip(),
            }
            if p.get('company_name'):
                partner_vals['company_name'] = p['company_name'].strip()
            if p.get('country_id'):
                partner_vals['country_id'] = int(p['country_id'])
            if p.get('street'):
                partner_vals['street'] = p['street'].strip()
            partner = request.env['res.partner'].sudo().create(partner_vals)
            new_user = request.env['res.users'].sudo().with_context(
                no_reset_password=True,
            ).create({
                'name': p.get('name').strip(),
                'login': email,
                'partner_id': partner.id,
                'groups_id': [(6, 0, [request.env.ref('base.group_portal').id])],
            })
            new_user.password = password
            OTP._cleanup(phone)
            request.env.cr.commit()
            request.session.authenticate(
                request.db,
                {'login': email, 'password': password, 'type': 'password'},
            )
        except Exception as exc:
            _logger.exception("API registration failed for %s", email)
            return err(_("Account creation failed. Please try again."), 'create_failed')
        return ok(self._serialize_user(self._partner()))

    # ------- Password reset (email OTP, in-SPA — CX-001) --------

    @http.route('/saas/api/v1/auth/reset/start', type='json', auth='public')
    def reset_start(self, email=None, **kw):
        """Begin an in-SPA password reset: email a one-time code.

        Account-enumeration safe: the response is identical whether or not
        an account exists; a code is sent only when a matching user does.
        """
        if not request.env.user._is_public():
            return err(_("You're already signed in."), 'already_authenticated')
        email = (email or '').strip()
        if not email or not EMAIL_RE.match(email):
            return err(_("Please enter a valid email address."), 'invalid')
        limited = (self._rate_limit('reset_start', 10, 600)
                   or self._rate_limit('otp_send', 4, 600, key=email))
        if limited:
            return limited
        user = request.env['res.users'].sudo().search(
            [('login', '=', email)], limit=1)
        if user:
            try:
                request.env['saas.registration.otp'].sudo(
                )._generate_and_send_email(email)
            except Exception:
                # Log for the operator but still return the neutral response
                # so the endpoint never reveals whether the address exists.
                _logger.exception("Failed to send password-reset OTP")
        return ok({'sent': True})

    @http.route('/saas/api/v1/auth/reset/verify', type='json', auth='public')
    def reset_verify(self, email=None, otp=None, password=None, **kw):
        """Complete a password reset: verify the email code, set the new
        password, and sign the customer in."""
        if not request.env.user._is_public():
            return err(_("You're already signed in."), 'already_authenticated')
        email = (email or '').strip()
        code = (otp or '').strip()
        password = password or ''
        if not email or not EMAIL_RE.match(email):
            return err(_("Please enter a valid email address."), 'invalid')
        if not code:
            return err(_("Please enter the verification code."), 'invalid')
        if len(password) < 8:
            return err(_("Password must be at least 8 characters."), 'invalid')
        # Brute-force guard on the 6-digit code, keyed by target email.
        limited = self._rate_limit('otp_verify', 6, 600, key=email)
        if limited:
            return limited
        OTP = request.env['saas.registration.otp'].sudo()
        if not OTP._verify(email, code, 'email'):
            return err(
                _("That code is invalid or expired. Please request a new one."),
                'otp_invalid',
            )
        user = request.env['res.users'].sudo().search(
            [('login', '=', email)], limit=1)
        if not user:
            # A verified code with no user shouldn't happen (codes are only
            # sent to existing users) — never create one here.
            OTP._cleanup(email)
            return err(_("We couldn't reset that account. Please contact "
                         "support."), 'reset_failed')
        try:
            user.password = password
            OTP._cleanup(email)
            request.env.cr.commit()
            request.session.authenticate(
                request.db,
                {'login': email, 'password': password, 'type': 'password'},
            )
        except Exception:
            _logger.exception("Password reset failed for %s", email)
            return err(_("We couldn't reset your password. Please try "
                         "again."), 'reset_failed')
        return ok(self._serialize_user(self._partner()))

    # ==================================================================
    #  Public catalog + pricing
    # ==================================================================

    @http.route('/saas/api/v1/meta', type='json', auth='public')
    def meta(self):
        """One-shot bootstrap data the SPA needs across pages."""
        site = SaasWebsite()
        ICP = request.env['ir.config_parameter'].sudo()
        countries = request.env['res.country'].sudo().search([], order='name')
        domains = request.env['saas.based.domain'].sudo().search([])
        versions = request.env['saas.odoo.version'].sudo().search(
            [('is_hosting_version', '=', True)], order='name desc',
        )
        # Free-trial availability (mirrors the old site). _get_trial_info
        # accounts for trial_days > 0 and whether this user already used
        # their trial; for anonymous visitors it reports availability.
        svc_trial, trial_days = site._get_trial_info()
        host_trial, _hd = site._get_trial_info(hosting=True)
        support_plans = request.env['saas.support.plan'].sudo().search(
            [('active', '=', True)], order='sequence, monthly_price, id')
        backup_unit_price = request.env[
            'saas.pricing.engine'].daily_backup_price()
        return ok({
            'hosting_config': self._public_plan_config(site._get_hosting_plan_config()),
            'custom_config': self._public_plan_config(site._get_custom_plan_config()),
            'trial': {
                'days': trial_days,
                'services_available': svc_trial,
                'hosting_available': host_trial,
            },
            'sections': {
                'services': site._section_enabled('services'),
                'hosting': site._section_enabled('hosting'),
            },
            'support_email': ICP.get_param('saas_master.support_email', ''),
            'countries': [{'id': c.id, 'name': c.name, 'code': c.code} for c in countries],
            'domains': [{'id': d.id, 'name': d.name} for d in domains],
            'hosting_versions': [
                {'id': v.id, 'name': v.name} for v in versions
            ],
            'support_plans': [
                {'code': s.code, 'name': s.name,
                 'monthly_price': s.monthly_price or 0.0,
                 'is_default': s.is_default}
                for s in support_plans
            ],
            'daily_backup_price': backup_unit_price,
        })

    @http.route('/saas/api/v1/services', type='json', auth='public')
    def services(self):
        products = request.env['saas.product'].sudo().search([
            ('is_published', '=', True),
            ('is_hosting', '=', False),
        ], order='sequence, id')
        return ok([self._serialize_product(p) for p in products])

    @http.route('/saas/api/v1/services/<int:product_id>', type='json', auth='public')
    def service_detail(self, product_id):
        product = request.env['saas.product'].sudo().browse(product_id)
        if not product.exists() or not product.is_published:
            return err(_("Service not found."), 'not_found')
        return ok(self._serialize_product(product, detail=True))

    @http.route('/saas/api/v1/hosting/calculate', type='json', auth='public')
    def hosting_calculate(self, workers=2, storage=5, billing='monthly',
                          region=None):
        site = SaasWebsite()
        config = site._get_hosting_plan_config()
        region_rec = self._resolve_region(region)
        return ok(self._price(
            config, workers, storage, billing, kind='hosting',
            region=region_rec.id or None))

    @http.route('/saas/api/v1/hosting/calculate-project', type='json',
                auth='public')
    def hosting_calculate_project(self, workers=2, storage=5, billing='monthly',
                                  region=None, staging_count=0, dev_count=0):
        """Hosting quote PLUS the cost of the chosen Staging/Development
        servers (each at the lowest-spec env price), for the purchase flow."""
        site = SaasWebsite()
        config = site._get_hosting_plan_config()
        region_rec = self._resolve_region(region)
        base = self._price(config, workers, storage, billing, kind='hosting',
                           region=region_rec.id or None)
        engine = request.env['saas.pricing.engine'].sudo()
        env_price = engine.env_server_price(
            billing=billing, region=region_rec.id or None)
        try:
            sc = max(0, int(staging_count or 0))
            dc = max(0, int(dev_count or 0))
        except (TypeError, ValueError):
            sc = dc = 0
        env_total = round(env_price * (sc + dc), 2)
        data = dict(base)
        data.update({
            'env_server_price': env_price,
            'staging_count': sc,
            'dev_count': dc,
            'env_total': env_total,
            'project_total': round((base.get('total') or 0.0) + env_total, 2),
        })
        return ok(data)

    @http.route('/saas/api/v1/hosting/order', type='json', auth='public')
    def hosting_order(self, **fields):
        """Place a hosting order straight from the SPA — no QWeb "Review"
        page. Reuses the canonical ``/hosting/order`` logic (validation,
        instance creation, billing) and returns where to send the buyer
        (the payment checkout, or the instance view for a trial)."""
        if not self._partner():
            return err(_("Please sign in to continue."), 'auth_required')
        try:
            resp = SaasWebsite().hosting_order(**fields)
        except Exception:
            _logger.exception("SPA hosting order failed")
            return err(_("We couldn't place your order. Please try again."),
                       'order_failed')
        loc = ''
        try:
            loc = resp.headers.get('Location', '') or ''
        except Exception:
            loc = ''
        # Validation failures redirect back with ``&error=<msg>`` — surface
        # the message inline instead of bouncing to the (removed) page.
        if 'error=' in loc:
            import urllib.parse as _u
            q = _u.parse_qs(_u.urlparse(loc).query)
            msg = (q.get('error') or [''])[0]
            return err((msg or '').replace('+', ' ')
                       or _("Please review your order."), 'invalid')
        return ok({'redirect_url': loc or '/my/instances'})

    @http.route('/saas/api/v1/services/calculate', type='json', auth='public')
    def services_calculate(self, workers=2, storage=5, billing='monthly',
                           region=None):
        site = SaasWebsite()
        config = site._get_custom_plan_config()
        region_rec = self._resolve_region(region)
        return ok(self._price(
            config, workers, storage, billing, kind='services',
            region=region_rec.id or None))

    @http.route('/saas/api/v1/tiers', type='json', auth='public')
    def tiers(self, kind='hosting', region=None):
        """Public named tiers for the pricing/configure cards (S8 renders
        them). Returns [] until tiers are configured (S9), so the SPA can
        fall back to the slider configurator.

        ``region`` (id/code, optional) scales the advertised tier prices by
        that region's multiplier — the SAME scaling the engine applies to
        the resource portion of a quote — so the cards, the slider and the
        checkout all show one region-consistent number. Unknown/falsy
        region -> x1.0 (behaviour-neutral)."""
        region_rec = self._resolve_region(region)
        mult = region_rec.price_multiplier if region_rec else 1.0
        plans = request.env['saas.plan'].sudo().search(
            [('is_public_tier', '=', True)], order='sequence, id',
        )
        default_currency = request.env.company.currency_id.name or 'USD'
        out = []
        for p in plans:
            p_kind = 'hosting' if any(
                prod.is_hosting for prod in p.saas_product_ids
            ) else 'services'
            if p_kind != kind:
                continue
            out.append({
                'id': p.id,
                'name': p.name,
                'workers': p.workers,
                'storage': int(p.storage_limit or 0),
                # Region-scaled to mirror the engine's resource scaling
                # (tier price >= floor, so tier*mult >= floor*mult — the
                # max() in the engine resolves to the same number).
                'monthly': round(p.price * mult, 2),
                'yearly': round(p.yearly_price * mult, 2),
                'recommended': p.is_recommended,
                'badge': p.badge or '',
                'sequence': p.sequence,
                'currency': p.currency_id.name or default_currency,
            })
        return ok(out)

    @http.route('/saas/api/v1/regions', type='json', auth='public')
    def regions(self):
        """Regions the customer can actually pick: active AND with capacity
        (a proxy + docker host + db server in-region — the co-located trio
        an instance needs). Empty regions are excluded entirely, so they
        never appear in the picker. Each carries its price multiplier."""
        Region = request.env['saas.region']
        regs = Region._available_regions()
        cheapest = Region._cheapest_available()
        recommended = Region._recommended_available()
        # Cheapest-first so the customer still sees the lowest entry price at
        # the top, but the RECOMMENDED region is the one pre-selected at
        # checkout; the cheapest is labelled "Budget".
        regs = regs.sorted(key=lambda r: (r.price_multiplier or 1.0, r.sequence, r.id))
        return ok([{
            'id': r.id,
            'code': r.code,
            'name': r.name,
            'multiplier': r.price_multiplier or 1.0,
            # The pre-selected default is now the recommended region.
            'default': bool(recommended) and r.id == recommended.id,
            'recommended': bool(recommended) and r.id == recommended.id,
            'budget': bool(cheapest) and r.id == cheapest.id,
            'available': True,
        } for r in regs])

    @http.route('/saas/api/v1/check-subdomain', type='json', auth='public')
    def check_subdomain(self, subdomain='', domain_id=0):
        # Delegate to the canonical implementation (which throttles itself).
        return ok(SaasWebsite().check_subdomain(subdomain=subdomain, domain_id=domain_id))

    def _public_plan_config(self, cfg):
        """Strip internal per-unit rates before exposing plan config to the
        browser. The client only needs limits / discount / currency — the
        actual price comes from the calculate endpoint (engine), so the
        rates never leave the server."""
        out = {
            k: v for k, v in cfg.items()
            if k not in ('worker_price', 'storage_price_per_gb')
        }
        # Normalised sizing hint for the workers slider: recommended users =
        # workers × [min..max] (light → heavy usage). Both hosting and custom
        # config carry the min/max pair tuned in Settings.
        out['users_per_worker_min'] = int(
            cfg.get('users_per_worker_min') or 6)
        out['users_per_worker_max'] = int(
            cfg.get('users_per_worker_max')
            or cfg.get('users_per_worker_min')
            or 6
        )
        return out

    def _resolve_region(self, region):
        """Resolve a region id or code to an ACTIVE region record, or an
        empty recordset. Falsy / unknown / inactive -> empty (the engine
        treats that as a x1.0 multiplier, so pricing is behaviour-neutral).
        Used so the public pricing endpoints can scale by a customer-picked
        region the same way the checkout does."""
        Region = request.env['saas.region'].sudo()
        if not region:
            return Region.browse()
        rec = Region.browse()
        try:
            rec = Region.browse(int(region))
        except (TypeError, ValueError):
            rec = Region.search([('code', '=', region)], limit=1)
        if rec and rec.exists() and rec.active:
            return rec
        return Region.browse()

    def _price(self, config, workers, storage, billing, kind='services',
               region=None):
        """Compute one customer-facing total via the single pricing
        engine (`saas.pricing.engine`). ``config`` is accepted for
        backward-compat with callers but no longer used — the engine
        reads rates itself. ``kind`` selects the rate set ('hosting' vs
        'services'); it MUST match the caller's product, otherwise the
        slider quotes a different price than the order/invoice. ``region``
        (id, optional) scales the compute+storage portion. Per-resource
        rates stay hidden."""
        quote = request.env['saas.pricing.engine'].compute(
            kind, workers, storage, billing, region=region,
        )
        # Return exactly the historical key set (the engine returns a
        # superset; keep this stable for existing SPA consumers).
        return {
            'workers': quote['workers'],
            'storage': quote['storage'],
            'billing': quote['billing'],
            'total': quote['total'],
            'monthly_equivalent': quote['monthly_equivalent'],
            'yearly_savings': quote['yearly_savings'],
            'savings_percent': quote['savings_percent'],
            'currency': quote['currency'],
            'region_factor': quote['region_factor'],
            'limits': quote['limits'],
        }

    # ==================================================================
    #  Portal: instances
    # ==================================================================

    @http.route('/saas/api/v1/dashboard', type='json', auth='public')
    def dashboard(self):
        partner = self._partner()
        if not partner:
            return err(_("Please sign in."), 'auth_required')
        Instance = request.env['saas.instance'].sudo()
        instances = Instance.search([
            ('partner_id', '=', partner.id),
            ('state', 'in', _ACTIVE_STATES),
            # Top-level only: Staging/Development servers live inside their
            # project and are reached from its Environments board.
            ('parent_id', '=', False),
        ], order='create_date desc')
        invoices = self._partner_invoices(partner)
        open_invoices = [i for i in invoices if i.payment_state not in ('paid', 'in_payment')
                         and i.state == 'posted']
        wallet_inline = self._serialize_wallet_inline(partner)
        return ok({
            'instances': [self._serialize_instance(i) for i in instances],
            'recent_invoices': [self._serialize_invoice(i) for i in invoices[:5]],
            'wallet': wallet_inline,
            'currency': request.env.company.currency_id.name or 'USD',
            'stats': {
                'instances': len(instances),
                'running': len(instances.filtered(lambda i: i.state == 'running')),
                'open_invoices': len(open_invoices),
                'outstanding': round(sum(i.amount_residual for i in open_invoices), 2),
                'wallet_balance': wallet_inline['total'],
            },
        })

    @http.route('/saas/api/v1/instances', type='json', auth='public')
    def instances(self, itype=None):
        partner = self._partner()
        if not partner:
            return err(_("Please sign in."), 'auth_required')
        domain = [
            ('partner_id', '=', partner.id),
            # Awaiting-payment orders are hidden from the list + search
            # (surfaced on the Dashboard instead). See _LIST_STATES.
            ('state', 'in', _LIST_STATES),
            # Top-level only — children are listed on their project's board.
            ('parent_id', '=', False),
        ]
        if itype == 'services':
            domain.append(('is_hosting', '=', False))
        elif itype == 'hosting':
            domain.append(('is_hosting', '=', True))
        instances = request.env['saas.instance'].sudo().search(
            domain, order='create_date desc',
        )
        return ok([self._serialize_instance(i) for i in instances])

    @http.route('/saas/api/v1/instances/<int:instance_id>', type='json', auth='public')
    def instance_detail(self, instance_id, access_token=None):
        try:
            instance = self._instance(instance_id, access_token)
        except (AccessError, MissingError):
            return err(_("Instance not found."), 'not_found')
        return ok(self._serialize_instance(instance, detail=True))

    @http.route('/saas/api/v1/instances/<int:instance_id>/status',
                type='json', auth='public')
    def instance_status(self, instance_id, access_token=None):
        try:
            instance = self._instance(instance_id, access_token)
        except (AccessError, MissingError):
            return err(_("Instance not found."), 'not_found')
        status = instance._get_status_dict()
        status['usage'] = self._usage(instance)
        return ok(status)

    @http.route('/saas/api/v1/instances/<int:instance_id>/metrics',
                type='json', auth='public')
    def instance_metrics(self, instance_id, access_token=None):
        """Cheap, real-time-ish CPU/RAM read for the dashboard.

        Returns the cached live sample (no SSH, no worker held) and marks
        the instance as "watched" so the background sampler measures it.
        Poll this every few seconds while viewing an instance."""
        try:
            instance = self._instance(instance_id, access_token)
        except (AccessError, MissingError):
            return err(_("Instance not found."), 'not_found')
        instance.sudo()._touch_metrics_watch()
        return ok({
            'cpu': round(instance.cpu_usage_pct or 0.0),
            'ram': round(instance.ram_usage_pct or 0.0),
            'at': fields.Datetime.to_string(instance.usage_last_updated)
                  if instance.usage_last_updated else '',
        })

    @http.route('/saas/api/v1/instances/<int:instance_id>/metrics/history',
                type='json', auth='public')
    def instance_metrics_history(self, instance_id, access_token=None, range=None):
        """Odoo.sh-style performance HISTORY for the customer's own instance:
        a downsampled CPU/RAM/storage time-series over the requested window
        (default 24h, up to the 14-day retention). Tenant-isolated via
        ``self._instance`` (only the authenticated owner's instance resolves)."""
        try:
            instance = self._instance(instance_id, access_token)
        except (AccessError, MissingError):
            return err(_("Instance not found."), 'not_found')
        ranges = {'1h': 1, '6h': 6, '24h': 24, '7d': 24 * 7, '14d': 24 * 14}
        hours = ranges.get(range or '24h', 24)
        return ok(instance.sudo()._get_metric_series(hours=hours))

    @http.route('/saas/api/v1/instances/<int:instance_id>/builds',
                type='json', auth='public')
    def instance_builds(self, instance_id, access_token=None, limit=30):
        """Odoo.sh-style deployment history for the customer's own instance:
        the chronological list of builds/deploys (initial / push / redeploy /
        merge), each with its commit, author, branch, status and timing."""
        try:
            instance = self._instance(instance_id, access_token)
        except (AccessError, MissingError):
            return err(_("Instance not found."), 'not_found')
        builds = request.env['saas.build'].sudo().search(
            [('instance_id', '=', instance.id)], order='id desc',
            limit=min(int(limit or 30), 100))
        out = []
        for b in builds:
            dur = None
            if b.date_start and b.date_done:
                dur = int((b.date_done - b.date_start).total_seconds())
            out.append({
                'id': b.id,
                'source': b.source,            # initial | push | redeploy | merge
                'state': b.state,              # running | success | failed
                'branch': b.branch or '',
                'commit': b.commit_short or '',
                'commit_message': b.commit_message or '',
                'author': b.author or '',
                'at': fields.Datetime.to_string(b.date_start) if b.date_start else '',
                'duration_s': dur,
                # Browser link to the commit on GitHub/GitLab (empty if unknown).
                'commit_url': (b.repo_id._web_commit_url(b.commit_sha)
                               if b.repo_id and b.commit_sha else ''),
                # The deploy log tail — so the customer can see WHY a build
                # failed, not just that it did.
                'log': (b.log or '')[-6000:] if b.state == 'failed' else '',
            })
        return ok(out)

    @http.route('/saas/api/v1/instances/<int:instance_id>/action',
                type='json', auth='public')
    def instance_action(self, instance_id, action=None, access_token=None):
        try:
            instance = self._instance(instance_id, access_token, write=True)
        except (AccessError, MissingError):
            return err(_("Instance not found."), 'not_found')
        handlers = {
            'start': instance.action_portal_start,
            'stop': instance.action_portal_stop,
            'restart': instance.action_portal_restart,
            'refresh_usage': instance.action_refresh_usage,
        }
        handler = handlers.get(action)
        if not handler:
            return err(_("Unknown action."), 'invalid')
        try:
            handler()
        except UserError as e:
            return err(str(e), 'action_failed')
        except Exception:
            _logger.exception("Instance action %s failed for %s", action, instance_id)
            return err(_("That action couldn't be completed. Please try again."), 'action_failed')
        return ok(instance._get_status_dict())

    def _require_redeployable(self, instance):
        """Repo/package changes redeploy the instance, which requires it to
        be running or stopped (action_redeploy's own rule)."""
        if instance.state not in ('running', 'stopped'):
            raise UserError(_("The instance must be running or stopped to "
                              "apply this change."))

    @http.route('/saas/api/v1/instances/<int:instance_id>/repo',
                type='json', auth='public')
    def instance_set_repo(self, instance_id, access_token=None, repo_url='',
                          repo_branch='main', git_token=None, **kw):
        """Connect / update / disconnect the instance's Git repository, then
        redeploy. Empty repo_url disconnects (unlinks) the repo. Reuses
        action_redeploy (clone pending repos, re-render config, restart)."""
        try:
            instance = self._hosting(instance_id, access_token, write=True)
        except (AccessError, MissingError):
            return err(_("Instance not found."), 'not_found')
        # The repository + token are configured ONCE per PROJECT (on the
        # Production environment); Staging/Development inherit it. Block edits
        # on a child env and point the customer to the project.
        if instance.environment != 'production' and instance.parent_id:
            return err(
                _("The repository is managed at the project level. Open the "
                  "Production environment to change it."), 'project_scoped')
        repo_url = (repo_url or '').strip()
        repo_branch = (repo_branch or 'main').strip() or 'main'
        if repo_url and not (repo_url.startswith('https://')
                             or repo_url.startswith('git@')):
            return err(_("Repository URL must start with https:// or git@."),
                       'invalid')
        # SSRF guard: block URLs that resolve to internal/reserved hosts before
        # any clone / API call is made against them.
        if repo_url:
            from odoo.addons.saas_core.models.saas_instance_repo import (
                assert_safe_git_url)
            try:
                assert_safe_git_url(repo_url)
            except UserError as e:
                return err(str(e), 'invalid')
        inst = instance.sudo()
        existing = inst.repo_ids[:1]
        if not repo_url and not existing:
            return ok(instance._get_status_dict())  # nothing to do
        # Pre-flight: confirm the repo is reachable and the branch exists BEFORE
        # writing anything or redeploying. A bad URL / missing branch / wrong
        # token must never trigger a redeploy that crash-loops the running
        # instance — reject it cleanly and leave the instance untouched.
        if repo_url:
            eff_token = (git_token if git_token is not None
                         else (existing.sudo().github_token if existing else None))
            try:
                inst._validate_repo_before_change(repo_url, repo_branch, eff_token)
            except UserError as e:
                return err(str(e), 'repo_unreachable')
        try:
            self._require_redeployable(instance)
            if repo_url:
                vals = {'repo_url': repo_url, 'branch': repo_branch}
                if git_token is not None:
                    vals['github_token'] = (git_token or '').strip() or False
                    vals['webhook_enabled'] = bool((git_token or '').strip())
                if existing:
                    if existing.repo_url != repo_url or existing.branch != repo_branch:
                        vals['state'] = 'pending'  # re-clone on source change
                    existing.write(vals)
                else:
                    vals['instance_id'] = inst.id
                    vals['state'] = 'pending'
                    vals.setdefault('webhook_enabled', False)
                    inst.env['saas.instance.repo'].create(vals)
                # Propagate the project repo (url + token) to every child
                # environment — repo/token are per PROJECT; each child keeps its
                # own branch and re-clones on its next deploy.
                if inst.environment == 'production':
                    child_vals = {'repo_url': repo_url}
                    if git_token is not None:
                        child_vals['github_token'] = (git_token or '').strip() or False
                        child_vals['webhook_enabled'] = bool((git_token or '').strip())
                    for child in inst.child_env_ids.filtered(
                            lambda c: c.state not in ('cancelled', 'cancelled_by_client')):
                        crepo = child.repo_ids[:1]
                        if crepo:
                            crepo.write({**child_vals, 'state': 'pending'})
                        else:
                            inst.env['saas.instance.repo'].create({
                                **child_vals, 'instance_id': child.id,
                                'branch': child._env_branch(), 'state': 'pending',
                            })
            else:
                existing.unlink()  # disconnect
                # Disconnecting at the project level removes it everywhere.
                if inst.environment == 'production':
                    inst.child_env_ids.mapped('repo_ids').unlink()
            instance.action_redeploy()
        except UserError as e:
            return err(str(e), 'deploy_failed')
        except Exception:
            _logger.exception("Set repo failed for %s", instance_id)
            return err(_("Couldn't apply the change. Please try again."),
                       'deploy_failed')
        return ok(instance._get_status_dict())

    @http.route('/saas/api/v1/instances/<int:instance_id>/packages',
                type='json', auth='public')
    def instance_set_packages(self, instance_id, access_token=None,
                              pip_packages='', **kw):
        """Replace the instance's Python packages (newline-separated), then
        force-install them now and surface any failure to the customer. The
        SPA sends the full list, so removing a package drops it from the
        list (uninstalled on the forced reinstall of the remaining set)."""
        try:
            instance = self._hosting(instance_id, access_token, write=True)
        except (AccessError, MissingError):
            return err(_("Instance not found."), 'not_found')
        # Installing happens via docker exec, so the container must be up.
        if instance.state != 'running':
            return err(_("Start the instance first — packages are installed "
                         "into the running server."), 'invalid_state')
        inst = instance.sudo()
        try:
            inst.pip_packages = (pip_packages or '').strip() or False
            ok_install, output = inst._deploy_pip_packages()
            if not ok_install:
                # Saved, but the install failed — show the customer why.
                return err(
                    _("Some packages failed to install:\n\n%s")
                    % (output[-1500:] or _("pip reported an error.")),
                    'pip_failed',
                )
        except UserError as e:
            return err(str(e), 'deploy_failed')
        except Exception:
            _logger.exception("Set packages failed for %s", instance_id)
            return err(_("Couldn't apply the change. Please try again."),
                       'deploy_failed')
        return ok(instance._get_status_dict())

    # ==================================================================
    #  Portal: hosting databases
    # ==================================================================

    def _hosting(self, instance_id, access_token=None, write=False):
        instance = self._instance(instance_id, access_token, write=write)
        if not instance.is_hosting:
            raise AccessError(_("Database management is only available for hosting."))
        return instance

    # States where the instance is NOT operational: the customer must not
    # be able to take/restore/enable backups & snapshots, manage databases,
    # or stream logs. ``stopped`` (paused) and ``suspended`` (e.g. unpaid)
    # are the explicit blocks; the not-yet-deployed / dead states can't run
    # these operations either, so they're blocked too. ``running`` is the
    # only fully-operational state.
    _BLOCKED_OP_STATES = (
        'draft', 'pending_payment', 'paid', 'pending_provision',
        'provisioning', 'stopped', 'failed', 'suspended',
        'cancelled', 'cancelled_by_client',
    )

    def _require_running(self, instance):
        """Raise UserError if the instance can't service backup / snapshot /
        log / database operations in its current state. Authoritative
        server-side gate — the SPA hides the controls, but a stopped or
        suspended instance must reject these calls regardless of the UI."""
        if instance.state in self._BLOCKED_OP_STATES:
            if instance.state == 'suspended':
                msg = _("This instance is suspended. Settle the outstanding "
                        "invoice to restore access to backups, snapshots and "
                        "logs.")
            elif instance.state == 'stopped':
                msg = _("This instance is stopped. Start it to access "
                        "backups, snapshots and logs.")
            else:
                msg = _("This instance isn't running yet. Backups, snapshots "
                        "and logs are available once it's running.")
            raise UserError(msg)

    @http.route('/saas/api/v1/instances/<int:instance_id>/databases',
                type='json', auth='public')
    def db_list(self, instance_id, access_token=None):
        try:
            instance = self._hosting(instance_id, access_token)
        except (AccessError, MissingError):
            return err(_("Instance not found."), 'not_found')
        if instance.state != 'running':
            return ok({'databases': [], 'ready': False, 'state': instance.state})
        try:
            dbs = instance.hosting_db_list()
        except UserError as e:
            return err(str(e), 'list_failed')
        except Exception:
            _logger.exception("DB list failed for %s", instance_id)
            return err(_("We couldn't load your databases right now."), 'list_failed')
        ops = request.env['saas.instance.db.operation'].sudo().search([
            ('instance_id', '=', instance.id),
            ('state', '=', 'running'),
        ])
        return ok({
            'databases': [{'name': d.get('name'), 'login': d.get('login', '')}
                          for d in dbs],
            'ready': True,
            # Instance host (https://<sub>.<domain>). The SPA opens a
            # specific DB at <url>/web?db=<name> — all DBs share the
            # host, so the db must be selected via the query param.
            'url': instance.url or '',
            'pending_ops': [{'db_name': o.db_name, 'operation': o.operation} for o in ops],
        })

    @http.route('/saas/api/v1/instances/<int:instance_id>/sql',
                type='json', auth='public')
    def sql_query(self, instance_id, db=None, query=None, limit=1000,
                  access_token=None, **kw):
        """Odoo.sh-style read-only SQL console. ``db`` selects which of
        the instance's databases to run against (multi-DB aware)."""
        try:
            instance = self._hosting(instance_id, access_token, write=True)
        except (AccessError, MissingError):
            return err(_("Instance not found."), 'not_found')
        try:
            self._require_running(instance)
            result = instance.hosting_sql_query(
                db or '', query or '', limit=limit)
        except UserError as e:
            return err(str(e), 'sql_failed')
        except Exception:
            _logger.exception("SQL query failed for %s", instance_id)
            return err(_("We couldn't run your query right now."), 'sql_failed')
        # A SQL-level error (syntax, permission, read-only violation) is
        # surfaced verbatim so the customer can fix their query.
        if result.get('error'):
            return err(result['error'], 'sql_error')
        return ok(result)

    @http.route('/saas/api/v1/instances/<int:instance_id>/databases/create',
                type='json', auth='public')
    def db_create(self, instance_id, name=None, login=None, password=None,
                  access_token=None, **kw):
        try:
            instance = self._hosting(instance_id, access_token, write=True)
        except (AccessError, MissingError):
            return err(_("Instance not found."), 'not_found')
        try:
            self._require_running(instance)
            op = instance.hosting_db_create_async(
                name=name or '', login=login or '',
                password=password or '', lang='en_US', country_code=None,
            )
        except UserError as e:
            return err(str(e), 'create_failed')
        return ok({'db_name': op.db_name})

    @http.route('/saas/api/v1/instances/<int:instance_id>/databases/duplicate',
                type='json', auth='public')
    def db_duplicate(self, instance_id, source=None, name=None,
                     access_token=None, **kw):
        """Duplicate an existing database into a new name. Runs async
        (same in-flight tracking as create) and returns the new DB name."""
        try:
            instance = self._hosting(instance_id, access_token, write=True)
        except (AccessError, MissingError):
            return err(_("Instance not found."), 'not_found')
        try:
            self._require_running(instance)
            op = instance.hosting_db_duplicate_async(
                source=source or '', new_name=name or '',
            )
        except UserError as e:
            return err(str(e), 'duplicate_failed')
        return ok({'db_name': op.db_name})

    @http.route('/saas/api/v1/instances/<int:instance_id>/databases/restore/upload-url',
                type='json', auth='public')
    def db_restore_upload_url(self, instance_id, name=None,
                              access_token=None, **kw):
        """Step 1 of customer restore: hand back a presigned PUT URL so
        the browser uploads the local backup straight to the bucket
        (bypassing Odoo — no timeout, large files OK)."""
        try:
            instance = self._hosting(instance_id, access_token, write=True)
        except (AccessError, MissingError):
            return err(_("Instance not found."), 'not_found')
        try:
            self._require_running(instance)
            backup, upload_url = instance.hosting_db_restore_prepare_upload(
                name=name or '',
            )
        except UserError as e:
            return err(str(e), 'restore_failed')
        except Exception:
            _logger.exception("Restore upload-url failed for %s", instance_id)
            return err(_("We couldn't start the restore. Please try again."),
                       'restore_failed')
        return ok({
            'backup_id': backup.id,
            'upload_url': upload_url,
            'db_name': backup.db_name,
        })

    @http.route('/saas/api/v1/instances/<int:instance_id>/databases/restore/start',
                type='json', auth='public')
    def db_restore_start(self, instance_id, backup_id=None,
                         access_token=None, **kw):
        """Step 2 of customer restore: after the upload finishes, verify
        it and kick off the background restore into the target database.
        The archive is validated (real, intact Odoo backup) on the host
        BEFORE the database is touched, so a bad file changes nothing."""
        try:
            instance = self._hosting(instance_id, access_token, write=True)
        except (AccessError, MissingError):
            return err(_("Instance not found."), 'not_found')
        backup = request.env['saas.instance.backup'].sudo().browse(
            int(backup_id or 0)
        )
        if not backup.exists() or backup.instance_id.id != instance.id:
            return err(_("That upload isn't available to restore."), 'invalid')
        try:
            self._require_running(instance)
            instance.hosting_db_restore_from_upload(backup.id)
        except UserError as e:
            return err(str(e), 'restore_failed')
        except Exception:
            _logger.exception("Restore start failed for %s", instance_id)
            return err(_("We couldn't start the restore. Please try again."),
                       'restore_failed')
        return ok({})

    @http.route('/saas/api/v1/instances/<int:instance_id>/databases/drop',
                type='json', auth='public')
    def db_drop(self, instance_id, name=None, access_token=None, **kw):
        try:
            instance = self._hosting(instance_id, access_token, write=True)
        except (AccessError, MissingError):
            return err(_("Instance not found."), 'not_found')
        try:
            self._require_running(instance)
            op = instance.hosting_db_drop_async(name=name or '')
        except UserError as e:
            return err(str(e), 'drop_failed')
        return ok({'db_name': op.db_name})

    @http.route('/saas/api/v1/instances/<int:instance_id>/databases/upgrade',
                type='json', auth='public')
    def db_upgrade(self, instance_id, name=None, modules=None,
                   access_token=None, **kw):
        """Upgrade one or more modules on a database with no downtime.

        Runs async (same in-flight tracking as create/duplicate) and
        returns the op id so the client can poll its result/report."""
        try:
            instance = self._hosting(instance_id, access_token, write=True)
        except (AccessError, MissingError):
            return err(_("Instance not found."), 'not_found')
        try:
            self._require_running(instance)
            op = instance.hosting_db_upgrade_modules_async(
                name=name or '', modules=modules or '',
            )
        except UserError as e:
            return err(str(e), 'upgrade_failed')
        return ok({'db_name': op.db_name, 'op_id': op.id})

    @http.route('/saas/api/v1/instances/<int:instance_id>/databases/operation/<int:op_id>',
                type='json', auth='public')
    def db_operation_status(self, instance_id, op_id, access_token=None, **kw):
        """Poll a database operation's state + captured report (used by
        the no-downtime module upgrade so the customer sees the result)."""
        try:
            instance = self._hosting(instance_id, access_token)
        except (AccessError, MissingError):
            return err(_("Instance not found."), 'not_found')
        op = request.env['saas.instance.db.operation'].sudo().browse(op_id)
        if not op.exists() or op.instance_id.id != instance.id:
            return err(_("Operation not found."), 'not_found')
        return ok({
            'id': op.id,
            'operation': op.operation,
            'db_name': op.db_name,
            'state': op.state,
            'error': op.error_message or '',
            'output': op.output_log or '',
        })

    @http.route('/saas/api/v1/instances/<int:instance_id>/daily-backup/enable',
                type='json', auth='public')
    def daily_backup_enable(self, instance_id, access_token=None, **kw):
        """Create the daily-backup add-on activation invoice and return
        the checkout URL for the customer to pay. Enabling itself happens
        once that invoice is paid (account_move hook). Available to both
        hosting and managed-services instances (snapshots are the one
        app-level add-on a service gets)."""
        try:
            instance = self._instance(instance_id, access_token, write=True)
        except (AccessError, MissingError):
            return err(_("Instance not found."), 'not_found')
        try:
            self._require_running(instance)
            instance.action_purchase_daily_backup()
        except UserError as e:
            return err(str(e), 'enable_failed')
        except Exception:
            _logger.exception("Daily-backup enable failed for %s", instance_id)
            return err(_("We couldn't start the daily-backup checkout. "
                         "Please try again."), 'enable_failed')
        return ok({
            'checkout_url': '/my/instances/%s/daily-backup/checkout' % instance.id,
        })

    @http.route('/saas/api/v1/instances/<int:instance_id>/invoice/cancel',
                type='json', auth='public')
    def invoice_cancel(self, instance_id, access_token=None, **kw):
        """Decline an optional unpaid invoice instead of paying it. Cancels
        the invoice (and, if the instance was never deployed, the instance
        too). Mandatory invoices can't be cancelled here."""
        try:
            instance = self._instance(instance_id, access_token, write=True)
        except (AccessError, MissingError):
            return err(_("Instance not found."), 'not_found')
        invoice = instance._get_cancellable_unpaid_invoice()
        if not invoice:
            return err(_("There's no cancellable invoice for this instance."),
                       'nothing_to_cancel')
        try:
            result = instance.action_client_cancel_invoice(invoice)
        except UserError as e:
            return err(str(e), 'cancel_failed')
        except Exception:
            _logger.exception("Invoice cancel failed for %s", instance_id)
            return err(_("We couldn't cancel that invoice. Please try again."),
                       'cancel_failed')
        return ok({'result': result, 'state': instance.state})

    @http.route('/saas/api/v1/instances/<int:instance_id>/databases/backup',
                type='json', auth='public')
    def db_backup(self, instance_id, name=None, format=None,
                  access_token=None, **kw):
        try:
            instance = self._hosting(instance_id, access_token, write=True)
        except (AccessError, MissingError):
            return err(_("Instance not found."), 'not_found')
        try:
            self._require_running(instance)
            backup = instance.hosting_db_backup(
                name=name or '',
                backup_format='dump' if format == 'dump' else 'zip',
            )
        except UserError as e:
            return err(str(e), 'backup_failed')
        except Exception:
            _logger.exception("DB backup failed for %s", instance_id)
            return err(_("Couldn't start the backup. Please try again."), 'backup_failed')
        # The build runs in the background; the client polls /backups for
        # this id until it's available with a download URL, then streams
        # the download straight from the bucket (one click, end to end).
        return ok({'backup_id': backup.id})

    @http.route('/saas/api/v1/instances/<int:instance_id>/databases/reset-password',
                type='json', auth='public')
    def db_reset_password(self, instance_id, name=None, new_password=None,
                          login=None, access_token=None, **kw):
        limited = self._rate_limit('db_reset_pw', 10, 600, key=str(instance_id))
        if limited:
            return limited
        try:
            instance = self._hosting(instance_id, access_token, write=True)
        except (AccessError, MissingError):
            return err(_("Instance not found."), 'not_found')
        if not new_password or len(new_password) < 6:
            return err(_("Choose a password of at least 6 characters."), 'invalid')
        try:
            self._require_running(instance)
            reset_login = instance.hosting_db_reset_admin_password(
                name=name or '', new_password=new_password,
                login=(login or '').strip() or None,
            )
        except UserError as e:
            return err(str(e), 'reset_failed')
        return ok({'login': reset_login})

    # ==================================================================
    #  Portal: backups
    # ==================================================================

    @http.route('/saas/api/v1/instances/<int:instance_id>/backups',
                type='json', auth='public')
    def backups(self, instance_id, access_token=None):
        try:
            instance = self._instance(instance_id, access_token)
        except (AccessError, MissingError):
            return err(_("Instance not found."), 'not_found')
        # Stopped / suspended / not-yet-running: no snapshot access.
        if instance.state in self._BLOCKED_OP_STATES:
            return ok({'backups': [], 'ready': False, 'state': instance.state})
        backups = instance.backup_ids.filtered(
            lambda b: b.state in ('done', 'running')
        ).sorted('create_date', reverse=True)[:30]
        # Mint a fresh, short-lived presigned link per list (SEC-008): the URL
        # TTL is minutes, so the client must always receive a current one
        # rather than a stale long-lived link.
        backups.sudo()._refresh_download_url()
        return ok({
            'backups': [self._serialize_backup(b) for b in backups],
            'ready': True,
            'state': instance.state,
        })

    @http.route('/saas/api/v1/instances/<int:instance_id>/backups/create',
                type='json', auth='public')
    def backup_create(self, instance_id, access_token=None, **kw):
        try:
            instance = self._instance(instance_id, access_token, write=True)
        except (AccessError, MissingError):
            return err(_("Instance not found."), 'not_found')
        try:
            self._require_running(instance)
            instance.action_create_backup()
        except UserError as e:
            return err(str(e), 'backup_failed')
        except Exception:
            _logger.exception("Backup create failed for %s", instance_id)
            return err(_("Couldn't start the backup. Please try again."), 'backup_failed')
        return ok({})

    @http.route('/saas/api/v1/instances/<int:instance_id>/backups/<int:backup_id>/restore',
                type='json', auth='public')
    def backup_restore(self, instance_id, backup_id, confirm=None,
                       access_token=None, **kw):
        """Restore the instance from a snapshot. Destructive — replaces
        the instance's current state with the snapshot. Requires the
        caller to retype the instance name as confirmation. Runs in the
        background; the instance flips to ``provisioning`` and the UI
        polls its status."""
        try:
            instance = self._instance(instance_id, access_token, write=True)
        except (AccessError, MissingError):
            return err(_("Instance not found."), 'not_found')
        try:
            self._require_running(instance)
        except UserError as e:
            return err(str(e), 'not_running')
        backup = instance.backup_ids.filtered(
            lambda b: b.id == backup_id and b.state == 'done'
        )
        if not backup:
            return err(_("That snapshot isn't available to restore."), 'invalid')
        expected = backup.db_name or instance.subdomain or ''
        if (confirm or '').strip() != expected:
            return err(_("Type the instance name exactly to confirm the restore."), 'confirm')
        try:
            if backup.is_full_instance:
                instance.action_restore_full_instance(backup.id)
            else:
                instance.action_restore_backup(backup.id)
        except UserError as e:
            return err(str(e), 'restore_failed')
        except Exception:
            _logger.exception("Restore failed for instance %s", instance_id)
            return err(_("The restore couldn't be started. Please try again."), 'restore_failed')
        return ok({'state': instance.state})

    # ==================================================================
    #  Portal: invoices
    # ==================================================================

    @http.route('/saas/api/v1/invoices', type='json', auth='public')
    def invoices(self):
        partner = self._partner()
        if not partner:
            return err(_("Please sign in."), 'auth_required')
        return ok([self._serialize_invoice(i) for i in self._partner_invoices(partner)])

    @http.route('/saas/api/v1/invoices/<int:invoice_id>', type='json', auth='public')
    def invoice_detail(self, invoice_id):
        partner = self._partner()
        if not partner:
            return err(_("Please sign in."), 'auth_required')
        inv = request.env['account.move'].sudo().browse(invoice_id)
        if not inv.exists() or inv.partner_id != partner:
            return err(_("Invoice not found."), 'not_found')
        return ok(self._serialize_invoice(inv, detail=True))

    def _partner_invoices(self, partner):
        return request.env['account.move'].sudo().search([
            ('partner_id', '=', partner.id),
            ('move_type', '=', 'out_invoice'),
            ('state', '!=', 'cancel'),
        ], order='invoice_date desc, create_date desc')

    # ==================================================================
    #  Billing: wallet, auto-renew, payment methods (A1 + A4)
    # ==================================================================
    @http.route('/saas/api/v1/wallet', type='json', auth='public')
    def wallet(self):
        """Two-class wallet for the signed-in customer: 'Your balance'
        (customer money, never expires) and 'Bonus credit' (may expire),
        plus the recent ledger. UX must surface the two clearly."""
        partner = self._partner()
        if not partner:
            return err(_("Please sign in."), 'auth_required')
        cur = request.env.company.currency_id.name or 'USD'
        wallet = request.env['saas.wallet'].sudo()._for_partner(
            partner, create=False)
        if not wallet:
            return ok({'balance_funded': 0.0, 'balance_bonus': 0.0,
                       'balance': 0.0, 'currency': cur,
                       'bonus_expiry': None, 'transactions': []})
        txns = wallet.transaction_ids.sorted('id', reverse=True)[:50]
        return ok({
            'balance_funded': round(wallet.balance_funded, 2),
            'balance_bonus': round(wallet.balance_bonus, 2),
            'balance': round(wallet.balance, 2),
            'currency': wallet.currency_id.name or cur,
            'bonus_expiry': self._wallet_bonus_expiry(wallet),
            'transactions': [{
                'id': t.id,
                'date': fields.Datetime.to_string(t.create_date),
                'amount': round(t.amount, 2),
                'balance_after': round(t.balance_after, 2),
                'kind': t.kind,
                'credit_class': t.credit_class or False,
                'description': t.reason or '',
            } for t in txns],
        })

    def _wallet_bonus_expiry(self, wallet):
        """Soonest expiry across live bonus lots (None if no bonus credit)."""
        if not wallet:
            return None
        today = fields.Date.today()
        dates = [lot.expiry_date for lot in wallet.lot_ids
                 if lot.credit_class == 'system_issued' and lot._is_live(today)
                 and lot.expiry_date]
        return fields.Date.to_string(min(dates)) if dates else None

    def _serialize_wallet_inline(self, partner):
        """Compact two-class wallet for instance/dashboard payloads."""
        cur = request.env.company.currency_id.name or 'USD'
        wallet = request.env['saas.wallet'].sudo()._for_partner(
            partner, create=False)
        if not wallet:
            return {'funded': 0.0, 'bonus': 0.0, 'total': 0.0,
                    'bonus_expiry': None, 'currency': cur}
        return {
            'funded': round(wallet.balance_funded, 2),
            'bonus': round(wallet.balance_bonus, 2),
            'total': round(wallet.balance, 2),
            'bonus_expiry': self._wallet_bonus_expiry(wallet),
            'currency': wallet.currency_id.name or cur,
        }

    @http.route('/saas/api/v1/instances/<int:instance_id>/storage/add',
                type='json', auth='public')
    def add_storage_block(self, instance_id, qty=1):
        """Buy ``qty`` storage blocks (capacity upgrade). Returns the
        activation invoice's checkout URL so the SPA can collect payment."""
        partner = self._partner()
        if not partner:
            return err(_("Please sign in."), 'auth_required')
        instance = request.env['saas.instance'].sudo().browse(instance_id)
        if not instance.exists() or instance.partner_id != partner:
            return err(_("Workspace not found."), 'not_found')
        try:
            invoice = instance.action_purchase_storage_block(int(qty or 1))
        except Exception as e:
            return err(str(e), 'error')
        if invoice is True or not getattr(invoice, 'id', False):
            return ok({'activated': True})
        return ok({
            'invoice_id': invoice.id,
            'checkout_url': '/my/instances/%s/checkout' % instance.id,
            'amount': round(invoice.amount_total, 2),
        })

    @http.route('/saas/api/v1/instances/<int:instance_id>/storage/release',
                type='json', auth='public')
    def release_storage_block(self, instance_id, qty=1):
        partner = self._partner()
        if not partner:
            return err(_("Please sign in."), 'auth_required')
        instance = request.env['saas.instance'].sudo().browse(instance_id)
        if not instance.exists() or instance.partner_id != partner:
            return err(_("Workspace not found."), 'not_found')
        try:
            instance.action_release_storage_block(int(qty or 1))
        except Exception as e:
            return err(str(e), 'error')
        return ok({'released': True,
                   'blocks_owned': instance.extra_storage_blocks})

    # ==================================================================
    #  Odoo.sh-style environments (Production project + Staging/Dev)
    # ==================================================================
    @http.route('/saas/api/v1/instances/<int:instance_id>/environments',
                type='json', auth='public')
    def environments(self, instance_id, access_token=None):
        """The project view: Production + its Staging/Development servers."""
        try:
            instance = self._instance(instance_id, access_token)
        except (AccessError, MissingError):
            return err(_("Instance not found."), 'not_found')
        prod = instance if instance.environment == 'production' else (
            instance.parent_id or instance)
        children = prod.child_env_ids.filtered(
            lambda c: c.state not in ('cancelled', 'cancelled_by_client')
        ).sorted('id')
        repo = prod.repo_ids[:1]
        prod_plan = prod.plan_id
        return ok({
            'project_id': prod.id,
            'project_name': prod.project_name or prod.subdomain or prod.name,
            'production': self._serialize_env_child(prod),
            'main_branch': prod.main_branch or 'main',
            'env_server_price': prod._env_server_price(),
            'billing_cycle': prod.billing_period or 'monthly',
            # Gate: env servers require a repo connected to Production.
            'has_repo': bool(repo),
            'repo_url': repo.repo_url or '' if repo else '',
            'environments': [self._serialize_env_child(c) for c in children],
            # Scaling: the Production server's current compute/storage (so the
            # workspace can offer a "Scale resources" CTA) and how many of the
            # purchased Staging/Development slots are in use (so it can offer
            # "add another" up to the entitlement, or buy an extra slot beyond).
            'production_plan': {
                'plan_name': prod_plan.name or '',
                'workers': prod_plan.workers or 0,
                'storage_gb': int(prod_plan.storage_limit or 0),
                'is_trial': prod.is_trial,
            },
            'slots': {
                'staging': {
                    'used': prod._env_used_for('staging'),
                    'total': prod._env_slots_for('staging'),
                },
                'development': {
                    'used': prod._env_used_for('development'),
                    'total': prod._env_slots_for('development'),
                },
            },
        })

    @http.route('/saas/api/v1/instances/<int:instance_id>/environments/create',
                type='json', auth='public')
    def environment_create(self, instance_id, type=None, name=None, branch=None):
        """One-click create of a Staging/Development server. Returns either
        ``auto_provisioned`` (wallet/card settled it) or a ``checkout_url``."""
        partner = self._partner()
        if not partner:
            return err(_("Please sign in."), 'auth_required')
        instance = request.env['saas.instance'].sudo().browse(instance_id)
        if not instance.exists() or instance.partner_id != partner:
            return err(_("Project not found."), 'not_found')
        prod = instance if instance.environment == 'production' \
            else instance.parent_id
        if not prod:
            return err(_("Environments are managed from the Production "
                         "server."), 'invalid')
        try:
            result = prod.action_create_environment(
                type, name=name, branch=branch)
        except (UserError, ValidationError) as e:
            return err(str(e), 'error')
        except Exception:
            _logger.exception("Env create failed for %s", instance_id)
            return err(_("Couldn't create the environment. Please try again."),
                       'error')
        return ok(result)

    def _project_anchor(self, instance_id):
        """Return the owned Production anchor for ``instance_id`` (or its
        parent), or ``(None, err_envelope)``."""
        partner = self._partner()
        if not partner:
            return None, err(_("Please sign in."), 'auth_required')
        instance = request.env['saas.instance'].sudo().browse(instance_id)
        if not instance.exists() or instance.partner_id != partner:
            return None, err(_("Project not found."), 'not_found')
        prod = instance if instance.environment == 'production' \
            else instance.parent_id
        if not prod:
            return None, err(_("Environments are managed from the Production "
                               "server."), 'invalid')
        return prod, None

    @http.route('/saas/api/v1/instances/<int:instance_id>/environments/reserve',
                type='json', auth='public')
    def environment_reserve(self, instance_id, type=None, qty=1):
        """Reserve (buy) Staging/Development slots — paid capacity, no repo and
        no server created. Returns ``auto_provisioned`` or a ``checkout_url``."""
        prod, error = self._project_anchor(instance_id)
        if error:
            return error
        try:
            result = prod.action_reserve_environment_slots(type, qty=int(qty or 1))
        except (UserError, ValidationError) as e:
            return err(str(e), 'error')
        except Exception:
            _logger.exception("Slot reserve failed for %s", instance_id)
            return err(_("Couldn't reserve the slot. Please try again."), 'error')
        return ok(result)

    @http.route('/saas/api/v1/instances/<int:instance_id>/environments/release',
                type='json', auth='public')
    def environment_release(self, instance_id, type=None, qty=1):
        """Release free (unused) Staging/Development slots, crediting the unused
        time back to the wallet and lowering the recurring charge."""
        prod, error = self._project_anchor(instance_id)
        if error:
            return error
        try:
            result = prod.action_release_environment_slots(type, qty=int(qty or 1))
        except (UserError, ValidationError) as e:
            return err(str(e), 'error')
        except Exception:
            _logger.exception("Slot release failed for %s", instance_id)
            return err(_("Couldn't release the slot. Please try again."), 'error')
        return ok(result)

    @http.route('/saas/api/v1/instances/<int:instance_id>/environments/'
                '<int:child_id>/delete', type='json', auth='public')
    def environment_delete(self, instance_id, child_id, delete_branch=False):
        """Remove a Staging/Development server; optionally delete its branch."""
        partner = self._partner()
        if not partner:
            return err(_("Please sign in."), 'auth_required')
        child = request.env['saas.instance'].sudo().browse(child_id)
        if not child.exists() or child.partner_id != partner \
                or child.environment == 'production':
            return err(_("Environment not found."), 'not_found')
        try:
            child.action_delete_environment(delete_branch=bool(delete_branch))
        except (UserError, ValidationError) as e:
            return err(str(e), 'error')
        return ok({'deleted': True})

    @http.route('/saas/api/v1/instances/<int:instance_id>/environments/merge',
                type='json', auth='public')
    def environment_merge(self, instance_id, source_id=None, target_id=None):
        """Odoo.sh-style drag-to-merge: merge the source server's branch into
        the target server's branch (and redeploy the target)."""
        partner = self._partner()
        if not partner:
            return err(_("Please sign in."), 'auth_required')
        Inst = request.env['saas.instance'].sudo()
        target = Inst.browse(int(target_id or 0))
        source = Inst.browse(int(source_id or 0))
        if not target.exists() or not source.exists() \
                or target.partner_id != partner or source.partner_id != partner:
            return err(_("Environment not found."), 'not_found')
        try:
            result = target.action_merge_environment(source.id)
        except (UserError, ValidationError) as e:
            return err(str(e), 'error')
        except Exception:
            _logger.exception("Merge failed %s->%s", source_id, target_id)
            return err(_("The merge couldn't be completed. Please try again."),
                       'error')
        return ok(result)

    # NOTE: Build/deploy History is intentionally NOT exposed to customers.
    # saas.build records are still kept for operators (visible in the Odoo
    # backend), but there is no customer-facing API or UI for them.

    @http.route('/saas/api/v1/instances/<int:instance_id>/branches',
                type='json', auth='public')
    def instance_branches(self, instance_id, access_token=None):
        """Remote branch list for the Staging branch picker."""
        try:
            instance = self._instance(instance_id, access_token)
        except (AccessError, MissingError):
            return err(_("Instance not found."), 'not_found')
        prod = instance if instance.environment == 'production' else (
            instance.parent_id or instance)
        repo = prod.repo_ids[:1]
        main_branch = prod.main_branch or 'main'
        if not repo:
            return ok({'branches': [], 'main_branch': main_branch})
        try:
            branches = repo._list_remote_branches()
        except Exception:
            branches = []
        return ok({'branches': branches, 'main_branch': main_branch})

    @http.route('/saas/api/v1/billing/payment-methods', type='json', auth='public')
    def payment_methods(self):
        partner = self._partner()
        if not partner:
            return err(_("Please sign in."), 'auth_required')
        methods = request.env['saas.payment.method'].sudo()._for_partner(partner)
        return ok([self._serialize_payment_method(m) for m in methods])

    @http.route('/saas/api/v1/billing/payment-methods/<int:method_id>/remove',
                type='json', auth='public')
    def remove_payment_method(self, method_id):
        partner = self._partner()
        if not partner:
            return err(_("Please sign in."), 'auth_required')
        method = request.env['saas.payment.method'].sudo().browse(method_id)
        if not method.exists() or method.partner_id != partner.commercial_partner_id:
            return err(_("Payment method not found."), 'not_found')
        method.action_remove()
        return ok({'removed': True})

    @http.route('/saas/api/v1/instances/<int:instance_id>/auto-renew',
                type='json', auth='public')
    def set_auto_renew(self, instance_id, subscription=None, daily_backup=None):
        """Toggle auto-renew for an instance (A1). Customers can turn the
        automatic charge on/off; invoices are still issued either way."""
        partner = self._partner()
        if not partner:
            return err(_("Please sign in."), 'auth_required')
        instance = request.env['saas.instance'].sudo().browse(instance_id)
        if not instance.exists() or instance.partner_id != partner:
            return err(_("Instance not found."), 'not_found')
        vals = {}
        if subscription is not None:
            vals['auto_renew_subscription'] = bool(subscription)
        if daily_backup is not None:
            vals['auto_renew_daily_backup'] = bool(daily_backup)
        if vals:
            instance.write(vals)
        return ok({
            'auto_renew_subscription': instance.auto_renew_subscription,
            'auto_renew_daily_backup': instance.auto_renew_daily_backup,
        })

    def _serialize_payment_method(self, method):
        """Safe, PCI-free view of a saved payment method (no PAN/CVV/expiry —
        only the provider + a masked label)."""
        if not method:
            return None
        return {
            'id': method.id,
            'provider': method.provider_code or '',
            'label': method.display_label or _('Saved method'),
            'is_default': method.is_default,
        }

    # ==================================================================
    #  Serializers
    # ==================================================================

    def _serialize_user(self, partner):
        initials = ''.join(
            w[0] for w in (partner.name or '').split()[:2]
        ).upper() or 'U'
        return {
            'id': partner.id,
            'name': partner.name,
            'email': partner.email or '',
            'company': partner.commercial_company_name or partner.company_name or partner.name,
            'initials': initials,
            'phone': partner.phone or '',
            # Internal (backend) users get a link to the Odoo backend in the
            # SPA account menu — portal users don't. `_serialize_user` is
            # always called for the current user, so env.user is correct.
            'is_internal': request.env.user.has_group('base.group_user'),
        }

    def _usage(self, instance):
        return {
            'cpu': round(instance.cpu_usage_pct or 0.0),
            'ram': round(instance.ram_usage_pct or 0.0),
            'storage': round(instance.storage_usage_pct or 0.0),
        }

    def _serialize_env_child(self, inst):
        """Compact card for an environment server (production or child).

        The Production environment is shown as "production" — the PROJECT keeps
        its own name (project_name); the production *server* is just the
        production environment, not named after the project."""
        return {
            'id': inst.id,
            'name': 'production' if inst.environment == 'production'
                    else (inst.subdomain or inst.name),
            'domain': inst.name or '',
            'url': inst.url or '',
            'version': inst.odoo_version_id.name or '',
            'environment': inst.environment,
            'environment_label': dict(
                inst._fields['environment'].selection
            ).get(inst.environment, inst.environment),
            'branch': inst._env_branch(),
            'state': inst.state,
            'state_label': dict(
                inst._fields['state'].selection
            ).get(inst.state, inst.state),
            'is_production': inst.environment == 'production',
            'pending_payment': inst.state == 'pending_payment',
            'pending_invoice_id': inst.env_pending_invoice_id.id or False,
        }

    def _serialize_instance(self, instance, detail=False):
        plan = instance.plan_id
        data = {
            'id': instance.id,
            'name': instance.subdomain or instance.name,
            'domain': instance.name or '',
            'url': instance.url or '',
            # The customer-facing region name — NEVER the internal server
            # name. Prefer the instance's own region; fall back to the
            # docker server's region (legacy instances with no region_id).
            'region': (
                instance.region_id.name
                or instance.docker_server_id.region_id.name
                or ''
            ),
            'version': instance.odoo_version_id.name or '',
            'state': instance.state,
            'state_label': dict(
                instance._fields['state'].selection
            ).get(instance.state, instance.state),
            'workers': plan.workers if plan else 0,
            'storage_gb': int(plan.storage_limit) if plan else 0,
            'billing_cycle': instance.billing_period or 'monthly',
            'created': fields.Datetime.to_string(instance.create_date) if instance.create_date else '',
            'is_hosting': instance.is_hosting,
            'is_trial': instance.is_trial,
            'usage': self._usage(instance),
            # Odoo.sh-style environments.
            'environment': instance.environment,
            'environment_label': dict(
                instance._fields['environment'].selection
            ).get(instance.environment, instance.environment),
            'branch': instance._env_branch(),
            'parent_id': instance.parent_id.id or False,
        }
        if detail:
            invoices = instance._get_all_invoices().filtered(
                lambda i: i.state != 'cancel'
            ).sorted('create_date', reverse=True)
            backups = instance.backup_ids.filtered(
                lambda b: b.state in ('done', 'running')
            ).sorted('create_date', reverse=True)[:10]
            data.update({
                'plan_name': plan.name if plan else '',
                'next_invoice_date': fields.Date.to_string(instance.next_invoice_date)
                    if instance.next_invoice_date else '',
                'daily_backup_enabled': instance.daily_backup_enabled,
                'daily_backup_suspended': instance.daily_backup_suspended,
                'daily_backup_pending': bool(instance.daily_backup_pending_invoice_id),
                'daily_backup_price': instance._get_daily_backup_price(),
                'daily_backup_next_invoice_date': fields.Date.to_string(
                    instance.daily_backup_next_invoice_date
                ) if instance.daily_backup_next_invoice_date else '',
                # Post-purchase custom code & packages (hosting only).
                'pip_packages': instance.pip_packages or '',
                'pip_install_error': instance.pip_install_error or '',
                'last_error': instance.last_error or '',
                'repo': ({
                    'url': instance.repo_ids[:1].repo_url or '',
                    'branch': instance.repo_ids[:1].branch or 'main',
                    'has_token': bool(instance.repo_ids[:1].github_token),
                    'state': instance.repo_ids[:1].state or '',
                } if (instance.is_hosting and instance.repo_ids) else {
                    'url': '', 'branch': 'main', 'has_token': False, 'state': '',
                }),
                'pending_plan': instance.pending_plan_id.name if instance.pending_plan_id else '',
                'scheduled_plan': instance.scheduled_plan_id.name if instance.scheduled_plan_id else '',
                'backups': [self._serialize_backup(b) for b in backups],
                'invoices': [self._serialize_invoice(i) for i in invoices],
                'has_unpaid_invoice': any(
                    i.state == 'posted' and i.payment_state not in ('paid', 'in_payment')
                    and i.amount_residual > 0 for i in invoices
                ),
                'checkout_url': '/my/instances/%s/checkout' % instance.id,
                # An optional unpaid invoice the customer may decline instead
                # of paying (plan upgrade / add-on). Mandatory invoices
                # (initial / renewal / restoration) are not cancellable.
                'cancellable_invoice_id': instance._get_cancellable_unpaid_invoice().id or False,
                # Auto-renew + saved payment method.
                'auto_renew_subscription': instance.auto_renew_subscription,
                'auto_renew_daily_backup': instance.auto_renew_daily_backup,
                'payment_method': self._serialize_payment_method(
                    instance._auto_renew_method()),
                # v47 — capacity ("upgrade experience") summary.
                'capacity': instance._capacity_summary(),
                # Wallet: two-class balance (customer money vs bonus).
                'wallet': self._serialize_wallet_inline(instance.partner_id),
                # Odoo.sh environments: the project's main branch, the
                # per-server price, and the child Staging/Dev servers.
                'main_branch': instance.main_branch or 'main',
                'env_server_price': instance._env_server_price(),
                'environments': [
                    self._serialize_env_child(c)
                    for c in instance.child_env_ids.sorted('id')
                    if c.state not in ('cancelled', 'cancelled_by_client')
                ] if instance.environment == 'production' else [],
            })
            # Cancelled instances: surface the retained snapshot (if any) so
            # the customer knows their data is kept and can reactivate to
            # restore it. Restoring after cancellation carries a one-time fee.
            data.update(self._reactivation_info(instance))
        return data

    def _reactivation_info(self, instance):
        """Reactivation + retained-snapshot details for a cancelled instance.

        Empty (is_cancelled False) for any non-cancelled instance. When the
        instance was cancelled we keep the most recent full-instance snapshot
        in cold storage; restoring it requires reactivating the subscription
        and pays a one-time data-restoration fee."""
        if instance.state not in ('cancelled', 'cancelled_by_client'):
            return {'is_cancelled': False}
        Backup = request.env['saas.instance.backup'].sudo()
        retained = Backup.search([
            ('instance_id', '=', instance.id),
            ('is_full_instance', '=', True),
            ('state', '=', 'done'),
        ], order='create_date desc', limit=1)
        has_retained = bool(retained) or bool(instance.retained_backup_path)
        # Computed: months retained after deletion × ceil(snapshot GB)
        # × the per-GB monthly rate.
        fee = instance._get_retained_snapshot_fee()
        return {
            'is_cancelled': True,
            'has_retained_snapshot': has_retained,
            'retained_snapshot_date': (
                fields.Datetime.to_string(retained.create_date)
                if retained and retained.create_date else ''
            ),
            'restoration_fee': round(fee, 2),
            'currency': instance.plan_id.currency_id.name
                or request.env.company.currency_id.name or 'USD',
            'reactivate_url': '/my/instances/%s/reactivate' % instance.id,
        }

    def _serialize_backup(self, b):
        return {
            'id': b.id,
            'label': b.name or _('Backup'),
            'type': 'manual' if b.ephemeral else 'automatic',
            'size_mb': round(b.size_mb or 0.0, 1),
            'created': fields.Datetime.to_string(b.create_date) if b.create_date else '',
            'status': _BACKUP_STATUS.get(b.state, 'available'),
            'download_url': b.download_url or '',
            'is_full_instance': b.is_full_instance,
            'db_name': b.db_name or '',
            'format': b.format or '',
        }

    def _serialize_invoice(self, inv, detail=False):
        if inv.payment_state in ('paid', 'in_payment'):
            status = 'paid'
        elif inv.state == 'draft':
            status = 'draft'
        elif (inv.invoice_date_due and inv.invoice_date_due < fields.Date.today()
              and inv.amount_residual > 0):
            status = 'overdue'
        else:
            status = _INVOICE_STATUS.get(inv.payment_state, 'open')
        instance = request.env['saas.instance'].sudo().search(
            [('sale_order_id', 'in', inv.line_ids.sale_line_ids.order_id.ids)], limit=1,
        )
        data = {
            'id': inv.id,
            'number': inv.name or _('Draft'),
            'status': status,
            'issued': fields.Date.to_string(inv.invoice_date) if inv.invoice_date
                else (fields.Datetime.to_string(inv.create_date) if inv.create_date else ''),
            'due': fields.Date.to_string(inv.invoice_date_due) if inv.invoice_date_due else '',
            'total': round(inv.amount_total, 2),
            'residual': round(inv.amount_residual, 2),
            'currency': inv.currency_id.name or 'USD',
            'currency_symbol': inv.currency_id.symbol or '$',
            'instance_name': instance.subdomain if instance else '',
            'portal_url': inv.get_portal_url(),
            'payable': inv.state == 'posted' and inv.payment_state not in ('paid', 'in_payment')
                       and inv.amount_residual > 0,
        }
        if detail:
            data['lines'] = [{
                'description': line.name,
                'quantity': line.quantity,
                'total': round(line.price_subtotal, 2),
            } for line in inv.invoice_line_ids.filtered(lambda l: l.display_type == False)]
            data['subtotal'] = round(inv.amount_untaxed, 2)
            data['tax'] = round(inv.amount_tax, 2)
        return data

    def _serialize_product(self, product, detail=False):
        data = {
            'id': product.id,
            'name': product.name,
            'tagline': product.subtitle or '',
            'icon': product.icon or 'fa fa-cube',
            'is_hosting': product.is_hosting,
            'image_url': '/web/image/saas.product/%s/image' % product.id if product.image else '',
        }
        if detail:
            data['description'] = product.description or ''
            data['features'] = [
                {'title': f.name, 'description': getattr(f, 'description', '') or ''}
                for f in product.feature_line_ids
            ]
            # Trial plan for this service (if configured), for the
            # "Start free trial" button. Trial plans may be flagged custom,
            # so we look across all plans here, not just the public ones.
            trial_plan = product.plan_ids.filtered(lambda p: p.is_trial_plan)[:1]
            data['trial_plan_id'] = trial_plan.id if trial_plan else 0
            data['plans'] = [{
                'id': p.id,
                'name': p.name,
                'workers': p.workers,
                'storage_gb': int(p.storage_limit),
                'is_trial': p.is_trial_plan,
            } for p in product.plan_ids.sorted('sequence') if not p.is_custom]
        else:
            data['highlights'] = [f.name for f in product.feature_line_ids][:3]
        return data
