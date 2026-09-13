# -*- coding: utf-8 -*-
"""Lightweight, DB-backed rate limiter for abuse-prone public endpoints.

A fixed-window counter keyed by ``scope:key:window``. The single hot path
(``_hit``) is an atomic ``INSERT ... ON CONFLICT DO UPDATE`` so it is correct
across Odoo's multiple worker processes (no shared in-memory state) and never
races: every concurrent request increments the same row under a row lock.

Used to blunt brute-force / enumeration / OTP-guessing on the public auth and
provisioning endpoints (login, registration, OTP verify, subdomain check,
admin-password reset). It is deliberately coarse — protection, not metering.
"""
import logging
import time

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class SaasRateLimit(models.Model):
    _name = 'saas.rate.limit'
    _description = 'SaaS Rate-limit Counter'

    bucket = fields.Char(required=True, index=True)
    scope = fields.Char(required=True, index=True)
    hits = fields.Integer(default=0)
    window_start = fields.Datetime(required=True)
    expires_at = fields.Datetime(required=True, index=True)

    _sql_constraints = [
        ('bucket_uniq', 'unique(bucket)',
         'A rate-limit bucket is unique per window.'),
    ]

    @api.model
    def _hit(self, scope, key, limit, window_seconds):
        """Register one request for ``(scope, key)`` in the current fixed
        window and report whether the caller is still under ``limit``.

        Returns ``(allowed: bool, retry_after: int)``. ``retry_after`` is the
        number of seconds until the current window rolls over (only meaningful
        when ``allowed`` is False). Atomic and worker-safe.
        """
        if not key:
            key = '-'
        now = int(time.time())
        window_id = now // window_seconds
        window_start_epoch = window_id * window_seconds
        reset_epoch = window_start_epoch + window_seconds
        bucket = '%s:%s:%d' % (scope, key, window_id)
        window_start = fields.Datetime.to_datetime(
            time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(window_start_epoch)))
        expires_at = fields.Datetime.to_datetime(
            time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(reset_epoch)))
        # Atomic upsert: the first request for a bucket inserts hits=1, every
        # subsequent one increments under PostgreSQL's row lock. RETURNING gives
        # us the post-increment count with no read-modify-write race.
        self.env.cr.execute(
            """
            INSERT INTO saas_rate_limit
                (bucket, scope, hits, window_start, expires_at,
                 create_uid, create_date, write_uid, write_date)
            VALUES (%s, %s, 1, %s, %s, %s, (now() at time zone 'UTC'),
                    %s, (now() at time zone 'UTC'))
            ON CONFLICT (bucket) DO UPDATE
                SET hits = saas_rate_limit.hits + 1,
                    write_date = (now() at time zone 'UTC')
            RETURNING hits
            """,
            (bucket, scope, window_start, expires_at,
             self.env.uid, self.env.uid),
        )
        hits = self.env.cr.fetchone()[0]
        allowed = hits <= limit
        if not allowed:
            _logger.warning(
                "Rate limit hit: scope=%s key=%s (%d/%d in %ds window)",
                scope, key, hits, limit, window_seconds)
        return allowed, max(0, reset_epoch - now)

    @api.autovacuum
    def _gc_expired_buckets(self):
        """Drop rolled-over windows so the table stays tiny."""
        self.search([('expires_at', '<', fields.Datetime.now())]).unlink()
