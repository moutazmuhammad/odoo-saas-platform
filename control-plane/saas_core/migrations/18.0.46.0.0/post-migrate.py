"""18.0.46.0.0 — billing overhaul (A1–A5, region default, trial promo).

Backfills only what cannot be derived automatically:

* New models (saas.wallet, saas.wallet.transaction, saas.payment.method,
  saas.payment.provider.config, saas.payment.attempt) and the new
  saas.instance / saas.region columns are created by the ORM on update —
  nothing to migrate there.

* Region default behaviour changed from "cheapest" to "recommended". To
  preserve a sensible pre-selected checkout region we mark ONE region as
  recommended when none is set yet: the existing default region, else the
  cheapest active region.

* Storage overage became blocks-only. Where a per-GB rate was configured
  but no block price was, we DON'T invent a price (operator decision) —
  we only ensure a sane block SIZE exists so block counts display.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env_ok = True
    try:
        from odoo import api, SUPERUSER_ID
        from odoo.api import Environment
        env = Environment(cr, SUPERUSER_ID, {})
    except Exception:  # pragma: no cover - defensive
        env_ok = False
    if not env_ok:
        return

    Region = env['saas.region'].sudo()
    if not Region.search_count([('is_recommended', '=', True)]):
        # Prefer the explicit default; else the cheapest available; else the
        # first active region.
        target = (
            Region.search([('active', '=', True), ('is_default', '=', True)],
                          limit=1)
            or Region._cheapest_available()
            or Region.search([('active', '=', True)],
                             order='sequence, id', limit=1)
        )
        if target:
            target.is_recommended = True
            _logger.info(
                "saas_core 46.0.0: marked region %s as recommended (checkout "
                "default).", target.name)

    ICP = env['ir.config_parameter'].sudo()
    # Ensure a storage block size exists so overage block counts display
    # even on installs that never set one (default 10 GB).
    if not ICP.get_param('saas_master.storage_block_gb'):
        ICP.set_param('saas_master.storage_block_gb', '10')
        _logger.info("saas_core 46.0.0: seeded storage_block_gb=10.")
