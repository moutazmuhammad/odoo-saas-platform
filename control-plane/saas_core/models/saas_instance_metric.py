from odoo import api, fields, models

# Customer-facing performance history is retained for this many days, then pruned.
METRIC_RETENTION_DAYS = 14


class SaasInstanceMetric(models.Model):
    """A time-series performance sample for one tenant instance.

    Powers the Odoo.sh-style per-customer metrics dashboard: CPU/RAM relative to
    the plan allocation + storage, sampled every few minutes and retained for
    METRIC_RETENTION_DAYS. Kept deliberately narrow (one short row per sample) so
    14 days × all tenants stays small; a daily prune cron enforces the window.

    Tenant isolation is at the API layer — the controller only ever returns rows
    for the authenticated partner's own instance."""
    _name = 'saas.instance.metric'
    _description = 'SaaS Instance Performance Sample'
    _order = 'ts'
    _log_access = False   # high-volume table — skip create/write_uid/date columns

    instance_id = fields.Many2one(
        'saas.instance', required=True, ondelete='cascade', index=True)
    ts = fields.Datetime(
        string='Sampled At', required=True, index=True,
        default=fields.Datetime.now)
    cpu_pct = fields.Float(string='CPU % of plan', digits=(6, 1))
    ram_pct = fields.Float(string='RAM % of plan', digits=(6, 1))
    storage_mb = fields.Float(string='Storage (MB)', digits=(12, 2))
    storage_pct = fields.Float(string='Storage % of plan', digits=(6, 1))

    def init(self):
        # Composite index for the hot query: one instance over a time range.
        self.env.cr.execute("""
            CREATE INDEX IF NOT EXISTS saas_instance_metric_inst_ts_idx
            ON saas_instance_metric (instance_id, ts)
        """)
