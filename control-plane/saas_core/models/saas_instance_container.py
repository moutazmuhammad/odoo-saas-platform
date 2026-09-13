from odoo import fields, models


class SaasInstanceContainer(models.Model):
    """Phase 5.1 SEAM (model only — no scale-out implementation yet).

    A tenant is modelled as a SET of role-tagged workloads instead of exactly one
    container, so the Business/Starter scale-out tier (N app workers + a single
    cron node + a longpolling node behind an LB) becomes ADDITIVE later — no
    rewrite of the single-container path that every tenant uses today.

    v1: this table is EMPTY for normal tenants; `saas.instance._workloads()`
    falls back to the implicit single 'app' container. Only a scale-out instance
    populates explicit rows. Do NOT build the LB / PgBouncer / replica here —
    those wait for a real large tenant (see PHASE-BREAKDOWN §5)."""
    _name = 'saas.instance.container'
    _description = 'SaaS Instance Workload (scale-out seam)'
    _order = 'instance_id, sequence, id'

    instance_id = fields.Many2one(
        'saas.instance', string='Instance', required=True,
        ondelete='cascade', index=True)
    role = fields.Selection(
        [('app', 'App (HTTP worker)'),
         ('cron', 'Cron (only one runs ir.cron)'),
         ('longpoll', 'Longpolling / websocket')],
        string='Role', required=True, default='app',
        help='Workload role. Exactly one cron node runs ir.cron; the others '
             'set --max-cron-threads=0. A longpoll node serves the bus/websocket.')
    name = fields.Char(
        string='Container Name', required=True,
        help='Container/pod name for this workload (e.g. odoo_<sub>_app2).')
    sequence = fields.Integer(default=10)
