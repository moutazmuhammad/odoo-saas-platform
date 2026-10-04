import datetime
import json

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
        ('rollback', 'Rollback'),
    ], string='Trigger', default='push', required=True)
    # Where a running build is in the pipeline (see saas_instance_build.py).
    stage = fields.Selection([
        ('queued', 'Queued'),
        ('building', 'Building image'),
        ('deploying', 'Upgrading modules & rolling out'),
        ('done', 'Done'),
    ], string='Stage', default='queued')
    job_name = fields.Char(string='Build Job', help='Kubernetes build Job name.')
    addons_paths = fields.Text(
        string='Addons Paths', help='JSON list of the addons paths baked into '
        'this build\'s image (needed to redeploy it on rollback).')
    module_versions = fields.Text(
        string='Module Versions', help='JSON of module manifest versions and '
        'source fingerprints. The next build upgrades only changed modules.')
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
        vals = {'state': state, 'date_done': fields.Datetime.now(), 'stage': 'done'}
        if log:
            vals['log'] = (log or '')[:8000]
        self.write(vals)
        return self


    @api.model
    def _history_retention_policy(self):
        params = self.env['ir.config_parameter'].sudo()
        def positive(key, default):
            try:
                value = int(params.get_param(key, default))
            except (TypeError, ValueError):
                return default
            return value if value > 0 else default
        return (positive('saas_master.build_history_days', 30),
                positive('saas_master.build_history_limit', 50))

    @api.model
    def _cron_cleanup_history(self):
        """Bound completed deployment records and logs per environment.

        Keep active builds, builds referenced by pending workers, the two
        latest successful image deployments (current plus rollback), and the
        latest module fingerprint baseline. Image registry data is untouched.
        """
        days, limit = self._history_retention_policy()
        cutoff = fields.Datetime.now() - datetime.timedelta(days=days)
        Build = self.sudo()
        instances = self.env['saas.instance'].sudo().search([('build_ids', '!=', False)])
        removed = 0
        for instance in instances:
            domain = [('instance_id', '=', instance.id), ('state', 'in', ['success', 'failed'])]
            keep = Build.search(domain, order='id desc', limit=limit)
            # Preserve both the current successful image and its predecessor.
            protected = Build.search([
                ('instance_id', '=', instance.id), ('state', '=', 'success'),
                ('image_digest', '!=', False)], order='id desc', limit=1)
            if protected:
                protected |= Build.search([
                    ('instance_id', '=', instance.id), ('state', '=', 'success'),
                    ('image_digest', '!=', False),
                    ('image_digest', '!=', protected.image_digest)], order='id desc', limit=1)
            protected |= Build.search([
                ('instance_id', '=', instance.id), ('state', '=', 'success'),
                ('module_versions', '!=', False)], order='id desc', limit=1)
            jobs = self.env['saas.job'].sudo().search([
                ('model', '=', 'saas.instance'), ('res_id', '=', instance.id),
                ('state', 'in', ['pending', 'running']),
                ('method', 'in', ['_job_start_build', '_job_poll_build', '_job_poll_rollout'])])
            for job in jobs:
                try:
                    args = json.loads(job.args_json or '[]')
                    if args and isinstance(args[0], int):
                        protected |= Build.browse(args[0]).exists()
                except (ValueError, TypeError, KeyError):
                    continue
            stale = Build.search(domain + [
                ('id', 'not in', protected.ids), '|',
                ('date_done', '<', cutoff),
                '&', ('date_done', '=', False), ('date_start', '<', cutoff)])
            excess = Build.search(domain + [('id', 'not in', (keep | protected).ids)])
            (stale | excess).unlink()
            removed += len(stale | excess)
        return removed
