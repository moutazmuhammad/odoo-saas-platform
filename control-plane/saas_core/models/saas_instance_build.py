"""Customer Git repos on Kubernetes: build an image per change, then roll
it out with zero downtime.

    action_build_and_deploy()      one saas.build per change (push, clone,
      └─ _job_start_build            pull, repo removal, pip change, merge)
           driver.start_image_build   in-cluster build Job → registry
      └─ _job_poll_build             every _POLL_SECONDS until the Job ends
           driver.deploy_image        CR: image + addons paths + update
      └─ _job_poll_rollout           until the operator reports the update
                                      applied (or failed)

Every step is a durable saas.job that re-enqueues itself with an ``eta``
instead of blocking a worker, and is safe to re-run (a retry after a dead
worker or a DB serialization failure reuses what's already in the cluster). The operator upgrades the changed modules
against the new image before any serving pod switches to it; if that fails
the previous image simply keeps serving. A newer build supersedes an older
one still in flight.
"""
import datetime
import json
import logging
import os
import re
import subprocess

from jinja2 import Environment, FileSystemLoader

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_POLL_SECONDS = 15
_BUILD_DEADLINE_SECONDS = 1800
# Rollout = module upgrade Job (operator deadline 1h) + the rolling update.
_ROLLOUT_TIMEOUT = datetime.timedelta(minutes=75)
_TENANT_ADDONS_ROOT = '/opt/tenant-addons'

_BUILD_TEMPLATES = Environment(
    loader=FileSystemLoader(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'templates', 'build')),
    keep_trailing_newline=True,
)


class SaasInstance(models.Model):
    _inherit = 'saas.instance'

    build_ids = fields.One2many('saas.build', 'instance_id', string='Builds')

    # ------------------------------------------------------------------
    # entry point
    # ------------------------------------------------------------------
    def action_build_and_deploy(self, source='redeploy', repo=None, commit_sha=None,
                                commit_message=None, author=None, build=None):
        """Build this instance's repos into a new image and roll it out.
        Returns the ``saas.build``. Safe to call on any instance: one that
        isn't deployed yet just records the request (its first deploy picks
        the repos up)."""
        self.ensure_one()
        if self.state != 'running':
            # A deploy picks the repos up (see _do_deploy_locked_kubernetes);
            # a stopped/suspended instance has no pods to roll.
            return self.env['saas.build']
        if self._build_sources() or self._build_pip_lines():
            self._build_registry()  # fail fast, before anything is queued
        Build = self.env['saas.build'].sudo()
        if not build:
            build = Build.create({
                'instance_id': self.id,
                'repo_id': repo.id if repo else (self.repo_ids[:1].id or False),
                'branch': (repo.branch if repo else self._env_branch()) or False,
                'commit_sha': commit_sha or False,
                'commit_message': commit_message or False,
                'author': author or False,
                'source': source,
                'state': 'running',
                'stage': 'queued',
            })
        self._supersede_running_builds(build)
        self.env['saas.job'].enqueue(
            self, '_job_start_build', args=(build.id,), channel='deploy',
            lock_key='build:%s' % self.id, idempotency_key='build-start:%s' % build.id,
            idempotent=True, on_error='_on_build_job_error', on_error_args=(build.id,))
        self._append_log("Build #%d queued (%s)." % (build.id, source))
        return build

    def _validate_repo_before_change(self, repo_url, branch, github_token=None):
        """Pre-flight a repository URL + branch (with optional token) with
        ``git ls-remote`` BEFORE the repo is saved or anything is rebuilt, so
        a wrong URL, missing branch or bad token is rejected with a clear
        ``UserError`` and the running instance is left untouched."""
        self.ensure_one()
        repo_url = (repo_url or '').strip()
        branch = (branch or 'main').strip() or 'main'
        if not repo_url:
            return
        # SSRF guard: only public git hosts.
        from .saas_instance_repo import assert_safe_git_url
        assert_safe_git_url(repo_url)
        url = re.sub(r'^(https?://)[^@/]+@', r'\1', repo_url)
        token = (github_token or '').strip()
        if token and url.startswith('https://'):
            url = 'https://x-access-token:%s@%s' % (token, url[len('https://'):])
        try:
            proc = subprocess.run(
                ['git', '-c', 'http.sslVerify=true', 'ls-remote', '--heads', url, branch],
                capture_output=True, text=True, timeout=60,
                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
        except (OSError, subprocess.TimeoutExpired) as e:
            raise UserError(_(
                "Couldn't reach the repository to validate it. Please try "
                "again.\n%s") % str(e)[-300:])
        out = ((proc.stdout or '') + (proc.stderr or '')).strip()
        if token:  # never echo the token back to the client
            out = out.replace(token, '***')
        if proc.returncode != 0:
            raise UserError(_(
                "The repository could not be accessed — check the URL, the "
                "branch, and the access token (private repos need a token).\n%s"
            ) % out[-400:])
        if ('refs/heads/%s' % branch) not in out:
            raise UserError(_(
                "Branch '%s' was not found in the repository. Check the "
                "branch name.") % branch)

    def action_redeploy(self):
        """Rebuild at the current branch heads and roll out."""
        for rec in self:
            rec.action_build_and_deploy('redeploy')
        return True

    # ------------------------------------------------------------------
    # configuration
    # ------------------------------------------------------------------
    def _build_registry(self):
        """The region's registry settings; raises if the region can't build."""
        self.ensure_one()
        region = self.docker_server_id.region_id.sudo()
        if not region or not region.registry_host:
            raise UserError(_(
                "Git repositories can't be deployed in region '%s' yet: no "
                "container registry is configured for it (Region > Tenant "
                "Image Builds).") % (region.name if region else '-'))
        prefix = (region.registry_prefix or '').strip('/')
        repo_path = '/'.join(p for p in (prefix, 'tenant-%s' % self.subdomain) if p)
        return {
            'host': region.registry_host,
            'push_host': region.registry_push_host or region.registry_host,
            'repository': '%s/%s' % (region.registry_host, repo_path),
            'push_repository': '%s/%s' % (region.registry_push_host or region.registry_host, repo_path),
            'username': region.registry_username or None,
            'password': region.registry_password or None,
            'insecure': bool(region.registry_insecure),
            'builder_image': region.builder_image or 'moby/buildkit:v0.16.0-rootless',
            'git_image': region.git_image or 'alpine/git:v2.45.2',
        }

    def _base_image_parts(self):
        self.ensure_one()
        version = self.odoo_version_id
        return version.docker_image, version.docker_image_tag

    def _build_sources(self):
        """(repo record, directory name) for everything baked into the image:
        the product's own repos (a Services product ships its modules this
        way) and this instance's repos."""
        self.ensure_one()
        sources = []
        for repo in self.saas_product_id.repo_ids.sorted('sequence'):
            sources.append((repo, 'product-%s' % repo._get_repo_dir_name()))
        for repo in self.repo_ids.sorted('sequence'):
            sources.append((repo, repo._get_repo_dir_name()))
        return sources

    def _build_pip_lines(self):
        self.ensure_one()
        lines = []
        for line in (self.pip_packages or '').splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                lines.append(line)
        return lines

    # ------------------------------------------------------------------
    # pipeline steps (saas.job entry points)
    # ------------------------------------------------------------------
    def _job_start_build(self, build_id):
        self.ensure_one()
        build = self.env['saas.build'].sudo().browse(build_id)
        if not build.exists() or build.state != 'running':
            return
        driver = self._compute_driver()
        base_repo, base_tag = self._base_image_parts()
        sources = self._build_sources()
        if not sources and not self._build_pip_lines():
            # Nothing to bake (e.g. last repo removed): the plain version image.
            self._deploy_build(build, driver, repository=base_repo, tag=base_tag,
                               addons_paths=[], module_versions={}, modules=[])
            return
        reg = self._build_registry()
        tag = '%s-b%d' % (base_tag, build.id)
        spec_repos = []
        for repo, dirname in sources:
            ref = build.commit_sha if (build.commit_sha and repo == build.repo_id) else ''
            spec_repos.append({
                'url': repo._get_clone_url(),
                'ref': ref,
                'branch': repo.branch or 'main',
                'dir': dirname,
            })
        dockerfile = _BUILD_TEMPLATES.get_template('Dockerfile.jinja').render(
            base_image='%s:%s' % (base_repo, base_tag))
        job_name = ('build-%d-%s' % (build.id, self.subdomain))[:63].rstrip('-')
        driver.start_image_build(
            name=job_name, repos=spec_repos, dockerfile=dockerfile,
            requirements='\n'.join(self._build_pip_lines()),
            base_image='%s:%s' % (base_repo, base_tag),
            image_ref='%s:%s' % (reg['push_repository'], tag),
            builder_image=reg['builder_image'], git_image=reg['git_image'],
            registry_host=reg['host'], registry_push_host=reg['push_host'],
            registry_username=reg['username'], registry_password=reg['password'],
            registry_insecure=reg['insecure'], deadline_seconds=_BUILD_DEADLINE_SECONDS)
        build.write({
            'stage': 'building', 'job_name': job_name,
            'image_ref': '%s:%s' % (reg['repository'], tag),
        })
        self._append_log("Build #%d: building image %s..." % (build.id, build.image_ref))
        self._schedule_build_step('_job_poll_build', build)

    def _job_poll_build(self, build_id):
        self.ensure_one()
        build = self.env['saas.build'].sudo().browse(build_id)
        if not build.exists() or build.state != 'running' or build.stage != 'building':
            return
        driver = self._compute_driver()
        status = driver.build_status(build.job_name)
        if status['state'] == 'running':
            limit = datetime.timedelta(seconds=_BUILD_DEADLINE_SECONDS + 300)
            if fields.Datetime.now() - build.date_start > limit:
                self._fail_build(build, _("Build timed out."), driver)
            else:
                self._schedule_build_step('_job_poll_build', build)
            return
        if status['state'] == 'failed':
            self._fail_build(build, status.get('log') or _("Image build failed."), driver)
            return

        result = status['result'] or {}
        repos_by_dir = {r._get_repo_dir_name(): r for r in self.repo_ids}
        addons_paths = []
        for item in result.get('repos') or []:
            path = '%s/%s' % (_TENANT_ADDONS_ROOT, item['dir'])
            if item.get('subdir'):
                path += '/' + item['subdir'].strip('/')
            addons_paths.append(path)
            repo = repos_by_dir.get(item['dir'])
            if repo:
                repo.sudo().write({
                    'addons_subdir': item.get('subdir') or False,
                    'state': 'cloned', 'error_message': False,
                    'last_pull': fields.Datetime.now(),
                })
                if repo == build.repo_id and item.get('sha') and not build.commit_sha:
                    build.commit_sha = item['sha']
        versions = result.get('modules') or {}
        repository, _sep, tag = build.image_ref.rpartition(':')
        build.image_digest = '%s@%s' % (repository, status['digest'])
        self._deploy_build(build, driver, repository=repository, tag=tag,
                           addons_paths=addons_paths, module_versions=versions,
                           modules=self._modules_to_update(build, versions),
                           log=status.get('log'))
        try:
            driver.cleanup_build(build.job_name)
        except Exception:
            _logger.exception("Build #%d: cleanup of Job %s failed", build.id, build.job_name)

    def _deploy_build(self, build, driver, *, repository, tag, addons_paths,
                      module_versions, modules, log=None):
        base_repo, _base_tag = self._base_image_parts()
        reg = self._build_registry() if repository != base_repo else {}
        databases = []
        if self.is_hosting:
            # Upgrade the customer databases too, and make sure the pods
            # serve them (instances deployed before the filter existed).
            self._ensure_hosting_db_filter()
            if modules:
                databases = [d['name'] for d in self.hosting_db_list()]
        driver.deploy_image(
            self._compute_handle(), repository=repository, tag=tag,
            addons_paths=addons_paths, update_token='build-%d' % build.id,
            modules=modules, databases=databases, registry_host=reg.get('host'),
            registry_username=reg.get('username'), registry_password=reg.get('password'))
        vals = {
            'stage': 'deploying',
            'addons_paths': json.dumps(addons_paths),
            'module_versions': json.dumps(module_versions, sort_keys=True),
            'image_ref': build.image_ref or '%s:%s' % (repository, tag),
        }
        if log:
            vals['log'] = log[-8000:]
        build.write(vals)
        self._append_log(
            "Build #%d: image ready; upgrading %s%s and rolling out (the "
            "current version keeps serving until this succeeds)."
            % (build.id, ', '.join(modules) if modules else 'no modules',
               ' in %d customer database(s)' % len(databases) if databases else ''))
        self._schedule_build_step('_job_poll_rollout', build)

    def _job_poll_rollout(self, build_id):
        self.ensure_one()
        build = self.env['saas.build'].sudo().browse(build_id)
        if not build.exists() or build.state != 'running' or build.stage != 'deploying':
            return
        driver = self._compute_driver()
        handle = self._compute_handle()
        status = driver.update_status(handle)
        token = 'build-%d' % build.id
        if status['state'] == 'applied' and status['applied_token'] == token:
            if status['phase'] == 'Ready' and status['observed_image'] == build.image_ref:
                build._mark('success')
                self.sudo().deploy_image = build.image_ref
                self._append_log("Build #%d is live (%s)." % (build.id, build.image_ref))
                self.message_post(body=_("Build #%d deployed.") % build.id)
                return
        elif status['state'] == 'failed':
            detail = driver.update_job_log(handle) or status['message']
            self._fail_build(build, _(
                "Module upgrade failed — the previous version is still "
                "serving.\n%s") % detail)
            return
        # write_date is the hand-off to the operator (_deploy_build): polls
        # never write to the build.
        if fields.Datetime.now() - build.write_date > _ROLLOUT_TIMEOUT:
            self._fail_build(build, _("Rollout timed out (%s).") % (status['message'] or status['phase']))
            return
        self._schedule_build_step('_job_poll_rollout', build)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _schedule_build_step(self, method, build):
        self.env['saas.job'].enqueue(
            self, method, args=(build.id,), channel='deploy',
            eta=fields.Datetime.now() + datetime.timedelta(seconds=_POLL_SECONDS),
            idempotent=True, on_error='_on_build_job_error', on_error_args=(build.id,))

    def _modules_to_update(self, build, versions):
        """Modules whose manifest version changed since the last successful
        build (all of them when there is none — e.g. the first build on a
        database that already has these modules). Uninstalled ones are
        ignored by ``odoo -u``."""
        previous = self.env['saas.build'].sudo().search([
            ('instance_id', '=', self.id), ('state', '=', 'success'),
            ('id', '!=', build.id), ('module_versions', '!=', False),
        ], order='id desc', limit=1)
        if not previous:
            return sorted(versions)
        try:
            old = json.loads(previous.module_versions or '{}')
        except ValueError:
            old = {}
        return sorted(m for m, v in versions.items() if old.get(m) != v)

    def _supersede_running_builds(self, build):
        older = self.env['saas.build'].sudo().search([
            ('instance_id', '=', self.id), ('state', '=', 'running'),
            ('id', '<', build.id)])
        if not older:
            return
        driver = None
        for old in older:
            was_building = old.stage == 'building'
            old._mark('failed', _("Superseded by build #%d.") % build.id)
            if old.job_name and was_building:
                try:
                    driver = driver or self._compute_driver()
                    driver.cleanup_build(old.job_name)
                except Exception:
                    _logger.exception("Could not remove superseded build Job %s", old.job_name)

    def _fail_build(self, build, message, driver=None):
        build._mark('failed', message)
        pending_repos = self.repo_ids.filtered(lambda r: r.state == 'pending')
        if pending_repos:
            pending_repos.sudo().write({'state': 'error', 'error_message': message[:2000]})
        self._append_log("Build #%d failed: %s" % (build.id, message[-2000:]))
        self.message_post(body=_("Build #%d failed; the running version was not changed.") % build.id)
        if driver and build.job_name:
            try:
                driver.cleanup_build(build.job_name)
            except Exception:
                _logger.exception("Build #%d: cleanup failed", build.id)

    def _on_build_job_error(self, exception, build_id):
        build = self.env['saas.build'].sudo().browse(build_id)
        if build.exists() and build.state == 'running':
            self._fail_build(build, str(exception))


class SaasBuild(models.Model):
    _inherit = 'saas.build'

    def action_rollback(self):
        """Roll the environment back to this (successful) build's image. No
        module upgrade runs; the database keeps its current schema."""
        self.ensure_one()
        if self.state != 'success' or not self.image_ref:
            raise UserError(_("Only a successful build with an image can be restored."))
        instance = self.instance_id
        repository, _sep, tag = self.image_ref.rpartition(':')
        build = self.sudo().create({
            'instance_id': instance.id,
            'repo_id': self.repo_id.id or False,
            'branch': self.branch,
            'commit_sha': self.commit_sha,
            'commit_message': _("Rollback to build #%d") % self.id,
            'source': 'rollback',
            'state': 'running',
            'image_ref': self.image_ref,
            'image_digest': self.image_digest,
        })
        instance._supersede_running_builds(build)
        instance._deploy_build(
            build, instance._compute_driver(), repository=repository, tag=tag,
            addons_paths=json.loads(self.addons_paths or '[]'),
            module_versions=json.loads(self.module_versions or '{}'), modules=[])
        return build
