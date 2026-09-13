from odoo import api, fields, models


class SaasBuild(models.Model):
    """A build/deploy event for an environment — the Odoo.sh-style History
    timeline. One row per push (via Git webhook), initial deploy, or manual
    re-deploy/merge, with the commit it shipped and whether it succeeded."""
    _name = 'saas.build'
    _description = 'SaaS Environment Build'
    _order = 'id desc'

    instance_id = fields.Many2one(
        'saas.instance', string='Environment', required=True,
        ondelete='cascade', index=True)
    repo_id = fields.Many2one(
        'saas.instance.repo', string='Repository', ondelete='set null')
    branch = fields.Char(string='Branch')
    commit_sha = fields.Char(string='Commit')
    commit_short = fields.Char(string='Short Commit', compute='_compute_short')
    commit_message = fields.Text(string='Commit Message')
    author = fields.Char(string='Author')
    source = fields.Selection([
        ('push', 'Git push'),
        ('initial', 'Initial deployment'),
        ('redeploy', 'Manual re-deploy'),
        ('merge', 'Branch merge'),
    ], string='Trigger', default='push', required=True)
    state = fields.Selection([
        ('running', 'Building'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ], string='Status', default='running', required=True, index=True)
    date_start = fields.Datetime(string='Started', default=fields.Datetime.now)
    date_done = fields.Datetime(string='Finished')
    log = fields.Text(string='Log')
    # Phase 2.2: the immutable image this build produced (registry image by SHA).
    # Deploy/rollback reference image_digest (content-addressed, immutable).
    image_ref = fields.Char(
        string='Image', help='Tenant image tag pushed to the registry, '
        'e.g. 127.0.0.1:5000/tenant-<sub>:<sha>.')
    image_digest = fields.Char(
        string='Image Digest', help='Content-addressed image digest '
        '(registry@sha256:…) — the immutable reference deploy/rollback pin to.')

    @api.depends('commit_sha')
    def _compute_short(self):
        for rec in self:
            rec.commit_short = (rec.commit_sha or '')[:8]

    def _mark(self, state, log=False):
        """Terminate a running build with the given state (and optional log)."""
        self.ensure_one()
        vals = {'state': state, 'date_done': fields.Datetime.now()}
        if log:
            vals['log'] = (log or '')[:8000]
        self.write(vals)
        return self
