from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class SaasAddon(models.Model):
    """Configurable paid add-on (e.g. Daily Snapshots).

    Add-ons are summed into a quote by the pricing engine
    (`saas.pricing.engine._addons_total`). New add-ons are created as
    records here — no code change. An add-on whose price already lives in
    Settings (Daily Snapshots → `snapshot_price_per_gb`) can point at
    that config key via ``price_config_param`` so there is a single source
    of truth and the existing billing flow is undisturbed.
    """
    _name = 'saas.addon'
    _description = 'SaaS Paid Add-on'
    _order = 'sequence, name'

    sequence = fields.Integer(default=10)
    name = fields.Char(required=True)
    code = fields.Char(
        required=True,
        help='Stable technical code used by the pricing engine / checkout '
             '(e.g. "daily_snapshots"). Do not change once in use.',
    )
    active = fields.Boolean(default=True)
    applies_to = fields.Selection(
        [('hosting', 'Hosting only'),
         ('services', 'Services only'),
         ('both', 'Both')],
        string='Applies To', default='both', required=True,
    )
    monthly_price = fields.Float(
        string='Monthly Price', default=0.0,
        help='Flat monthly price (used in "flat" mode). Ignored when '
             '"Price Config Param" is set.',
    )
    price_config_param = fields.Char(
        string='Price Config Param',
        help='Optional: read the FLAT price from this ir.config_parameter '
             'key instead of the stored monthly price — keeps a single '
             'source of truth for add-ons whose price already lives in '
             'Settings (e.g. saas_master.snapshot_price_per_gb). '
             'Used as the flat amount / base fee.',
    )
    # P4: storage-aware pricing. One formula covers all three modes:
    #   flat    -> base only (today's behaviour)
    #   storage -> per-block only (base 0)
    #   hybrid  -> base + per-block
    price_mode = fields.Selection(
        [('flat', 'Flat'),
         ('storage', 'Per storage'),
         ('hybrid', 'Hybrid (base + per storage)')],
        string='Price Mode', default='flat', required=True,
        help='Flat: fixed monthly fee. Per storage: scales with the '
             "instance's storage. Hybrid: a base fee plus a per-storage "
             'amount. Backups especially should scale — a 2 TB instance '
             'costs far more to back up than a 20 GB one.',
    )
    price_per_block = fields.Float(
        string='Price per Block', default=0.0,
        help='Monthly price added per storage block (see Block Size). '
             'Used in "storage" and "hybrid" modes.',
    )
    block_gb = fields.Integer(
        string='Block Size (GB)', default=10,
        help='Storage is billed in whole blocks of this size, so the '
             'customer sees "+price per N GB" instead of noisy per-GB '
             'decimals. Must be > 0.',
    )
    description = fields.Text()

    _sql_constraints = [
        ('code_uniq', 'unique(code)', 'Add-on code must be unique.'),
    ]

    @api.constrains('price_mode', 'block_gb')
    def _check_block_gb(self):
        for rec in self:
            if rec.price_mode in ('storage', 'hybrid') and rec.block_gb <= 0:
                raise ValidationError(_(
                    "Add-on '%s': block size (GB) must be greater than 0 "
                    "for storage-based pricing."
                ) % rec.name)

    def _flat_amount(self):
        """The fixed part of the price (config param wins over the stored
        monthly price). This is the whole price in 'flat' mode and the base
        fee in 'hybrid'."""
        self.ensure_one()
        if self.price_config_param:
            try:
                return float(self.env['ir.config_parameter'].sudo().get_param(
                    self.price_config_param, '0') or 0)
            except (TypeError, ValueError):
                return 0.0
        return self.monthly_price or 0.0

    def effective_monthly_price(self, storage_gb=0):
        """Monthly price of this add-on for an instance with ``storage_gb``
        of storage. Flat mode ignores storage (back-compat: callers that
        don't pass storage get the flat price). Storage/hybrid bill in
        whole blocks: base + ceil(storage/block) × per_block."""
        self.ensure_one()
        mode = self.price_mode or 'flat'
        if mode == 'flat':
            return self._flat_amount()
        import math
        block = self.block_gb if self.block_gb and self.block_gb > 0 else 1
        blocks = math.ceil(max(storage_gb or 0, 0) / block)
        base = self._flat_amount() if mode == 'hybrid' else 0.0
        return base + blocks * (self.price_per_block or 0.0)

    @api.model
    def _sum_prices(self, kind, codes, storage_gb=0):
        """Total effective monthly price of active add-ons in ``codes``
        that apply to ``kind``, for an instance with ``storage_gb`` of
        storage (only storage/hybrid add-ons use it)."""
        if not codes:
            return 0.0
        addons = self.sudo().search([
            ('code', 'in', list(codes)),
            ('active', '=', True),
        ])
        return sum(
            a.effective_monthly_price(storage_gb=storage_gb)
            for a in addons
            if a.applies_to in (kind, 'both')
        )
