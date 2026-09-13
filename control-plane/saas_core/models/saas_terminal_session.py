from odoo import fields, models


class SaasTerminalSession(models.Model):
    """Metadata for an in-flight SSH terminal session.

    The actual paramiko channel lives only in the worker process that
    opened it (``owner_pid``). Other workers use this table to discover
    the owner and forward keystrokes / output via PostgreSQL
    ``LISTEN``/``NOTIFY`` (see ``controllers/ssh_terminal.py``). Without
    this, ``--workers > 0`` deployments lose any input RPC that lands
    on a non-owner worker.
    """
    _name = 'saas.terminal.session'
    _description = 'SSH Terminal Session (runtime state)'
    _rec_name = 'sid'

    sid = fields.Char(required=True, index=True)
    uid = fields.Integer(required=True)
    server_model = fields.Char(required=True)
    server_id = fields.Integer(required=True)
    server_name = fields.Char()
    owner_pid = fields.Integer(required=True)
    last_activity = fields.Datetime(required=True)
    closed = fields.Boolean(default=False)

    _sql_constraints = [
        ('sid_unique', 'unique(sid)', 'Session id must be unique.'),
    ]
