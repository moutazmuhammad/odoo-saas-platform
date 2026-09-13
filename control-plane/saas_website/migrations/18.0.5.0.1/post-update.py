"""One-shot cleanup: revert every database-side change that the
earlier (now-removed) Arabic-bilingual code made.

This migration is only here because previous module versions wrote
the following into the database:
  1. ``res.lang.ar_001.active`` flipped to True.
  2. Arabic added to every ``website.language_ids``.
  3. ``ir.ui.view.arch_db`` JSONB filled with ``ar_001`` translations.

The Arabic feature has been pulled. This script reverses all three
so the deployment is back to English-only with no leftover state
the customer could stumble into.

Idempotent: re-running just re-applies the same removals.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    _logger.info("[saas_website] === Arabic cleanup START ===")

    Lang = env['res.lang'].sudo().with_context(active_test=False)
    ar = Lang.search([('code', '=', 'ar_001')], limit=1)

    # 1. Unlink Arabic from every website's language_ids.
    if ar:
        Website = env['website'].sudo()
        websites = Website.search([])
        for w in websites:
            if ar in w.language_ids:
                try:
                    w.write({'language_ids': [(3, ar.id)]})  # (3, id) = unlink without delete
                    _logger.info(
                        "[saas_website] Unlinked Arabic from website %s.",
                        w.name,
                    )
                except Exception:
                    _logger.exception(
                        "[saas_website] Could not unlink Arabic from "
                        "website %s.", w.name,
                    )

    # 2. Wipe ar_001 entries from every translatable JSONB field on
    # views shipped by saas_website. We touch only saas_website
    # views so we don't disturb translations the customer or other
    # modules may legitimately have for unrelated languages.
    IrModelData = env['ir.model.data'].sudo()
    view_ids = IrModelData.search([
        ('module', '=', 'saas_website'),
        ('model', '=', 'ir.ui.view'),
    ]).mapped('res_id')
    if view_ids:
        # Direct SQL is the simplest way to drop one language key
        # from a JSONB column across many rows in one statement.
        # The field is translatable so it's stored as
        # ``{"en_US": "...", "ar_001": "..."}``.
        cr.execute(
            """
            UPDATE ir_ui_view
               SET arch_db = arch_db - 'ar_001'
             WHERE id = ANY(%s)
               AND arch_db ? 'ar_001'
            """,
            (view_ids,),
        )
        _logger.info(
            "[saas_website] Stripped ar_001 arch_db entries from "
            "%d view(s).", cr.rowcount,
        )

    # 3. Deactivate Arabic. Skipped if the language is still in use
    # somewhere we can't see (Odoo's res.lang.write blocks
    # deactivation when a website still references it; the unlink
    # above should have cleared that — if it didn't, leave the lang
    # active and surface a warning).
    if ar and ar.active:
        try:
            ar.write({'active': False})
            _logger.info(
                "[saas_website] Deactivated ar_001 (active=False).",
            )
        except Exception as e:
            _logger.warning(
                "[saas_website] Could not deactivate ar_001 — "
                "another website or record may still reference it: %s",
                e,
            )

    # 4. Bust the ormcache so the next request sees the cleaned-up
    # state without needing a server restart.
    try:
        env.registry.clear_cache()
        _logger.info("[saas_website] Registry cache cleared.")
    except Exception:
        pass

    _logger.info("[saas_website] === Arabic cleanup END ===")
