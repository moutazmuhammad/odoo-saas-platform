"""Remove the retired "custom price can't undercut a tier" floor.

Dead under the unified pricing model: a named tier's price is its own
linear rate minus a discount (never above the linear rate), and the
linear rate is monotonic in resources — so a custom config containing a
tier always prices at or above that tier's published price. The floor
(``_tier_floor``) could never raise a price; the always-on tier CEILING
is what keeps tiers and customs consistent.

Both parameters were settings-only (no data-file anchors).
"""


def migrate(cr, version):
    cr.execute(
        "DELETE FROM ir_config_parameter WHERE key IN %s",
        (('saas_master.custom_min_is_nearest_tier',
          'saas_master.tier_floor_buffer_pct'),),
    )
