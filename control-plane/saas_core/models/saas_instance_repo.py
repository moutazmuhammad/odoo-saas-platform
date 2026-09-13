import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import shlex
import socket
from urllib.parse import urlparse, urlunparse

import requests as http_requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..utils import run_in_background
from ..fields import EncryptedChar

_logger = logging.getLogger(__name__)


def _extract_git_host(url):
    """Return the hostname from a clone URL, supporting both
    ``https://host/owner/repo`` and SCP-style ``git@host:owner/repo``."""
    if not url:
        return ''
    url = url.strip()
    if url.startswith('git@'):
        # git@host:owner/repo(.git)
        rest = url[len('git@'):]
        return rest.split(':', 1)[0].split('/', 1)[0].strip()
    return (urlparse(url).hostname or '').strip()


def assert_safe_git_url(url):
    """Reject user-supplied Git URLs that point at the platform's own
    infrastructure (SSRF). The server clones from / calls the API of these
    hosts, so a URL resolving to loopback, link-local (incl. the cloud
    metadata endpoint 169.254.169.254), private, or otherwise non-public
    space must never be accepted.

    Raises ``UserError`` when the host is missing or resolves to a
    non-public address. Validated at the moment of use (not just on input)
    so DNS-rebinding can't slip an internal address past an earlier check.
    """
    host = _extract_git_host(url)
    if not host:
        raise UserError(_("The repository URL is missing a valid host."))

    # Resolve every A/AAAA record — a hostname can map to several IPs, and a
    # single public-looking one must not mask an internal one.
    addrs = set()
    try:
        ip = ipaddress.ip_address(host)
        addrs.add(ip)
    except ValueError:
        try:
            for fam, _t, _p, _c, sockaddr in socket.getaddrinfo(
                    host, None, proto=socket.IPPROTO_TCP):
                addrs.add(ipaddress.ip_address(sockaddr[0]))
        except (socket.gaierror, UnicodeError, ValueError):
            raise UserError(_(
                "We couldn't resolve the repository host '%s'. Check the URL."
            ) % host)

    if not addrs:
        raise UserError(_(
            "We couldn't resolve the repository host '%s'. Check the URL."
        ) % host)

    for ip in addrs:
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified
                or (ip.version == 6 and ip.ipv4_mapped is not None
                    and (ip.ipv4_mapped.is_private
                         or ip.ipv4_mapped.is_loopback
                         or ip.ipv4_mapped.is_link_local))):
            raise UserError(_(
                "For security, the repository host must be a public server. "
                "'%s' points to an internal or reserved address and can't be "
                "used."
            ) % host)


class SaasInstanceRepo(models.Model):
    _name = 'saas.instance.repo'
    _description = 'Custom Module Repository'
    _order = 'sequence, id'
    _sql_constraints = [
        ('unique_repo_per_instance',
         'UNIQUE(instance_id, repo_url)',
         'This repository is already added to this instance.'),
    ]

    instance_id = fields.Many2one(
        'saas.instance',
        string='Instance',
        required=True,
        ondelete='cascade',
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(
        string='Name',
        compute='_compute_name',
        store=True,
        help='Repository name derived from the URL.',
    )
    repo_url = fields.Char(
        string='Repository URL',
        required=True,
        help='Git clone URL (HTTPS). e.g. https://github.com/user/repo.git',
    )
    branch = fields.Char(
        string='Branch',
        default='main',
        required=True,
    )
    github_token = EncryptedChar(
        string='GitHub Token',
        help='Personal access token for private repositories. '
             'Leave empty for public repos.',
        copy=False,
        groups='base.group_system',
    )
    addons_subdir = fields.Char(
        string='Addons Subdirectory',
        help='Subdirectory inside the repo that contains addons. '
             'Leave empty if addons are at the root of the repo.',
    )
    state = fields.Selection(
        [
            ('pending', 'Pending'),
            ('cloned', 'Cloned'),
            ('error', 'Error'),
        ],
        default='pending',
        string='Status',
        readonly=True,
    )
    last_pull = fields.Datetime(string='Last Pull', readonly=True)
    error_message = fields.Text(string='Error', readonly=True)

    # Webhook auto-deploy
    webhook_enabled = fields.Boolean(
        string='Auto-Deploy',
        default=True,
        help='Automatically pull and restart when code is pushed to the tracked branch.',
    )
    webhook_registered = fields.Boolean(
        string='Webhook Active',
        compute='_compute_webhook_registered',
        help='Whether the webhook is actually registered on the Git provider.',
    )
    webhook_secret = fields.Char(
        string='Webhook Secret',
        copy=False,
        readonly=True,
        groups='saas_core.group_saas_manager',
        help='Secret token used to validate incoming webhook requests. '
             'Only managers can read it — anyone with this secret can '
             'trigger a deploy on the linked repo.',
    )
    webhook_url = fields.Char(
        string='Webhook URL',
        compute='_compute_webhook_url',
        groups='saas_core.group_saas_manager',
        help='Webhook endpoint registered on the Git provider. '
             'Embeds the secret — restricted to managers.',
    )
    webhook_provider_id = fields.Char(
        string='Provider Webhook ID',
        readonly=True,
        copy=False,
        help='ID of the webhook on the Git provider (for cleanup on disable).',
    )
    webhook_last_event = fields.Datetime(
        string='Last Webhook Event',
        readonly=True,
    )
    last_delivery_id = fields.Char(
        string='Last Delivery ID',
        readonly=True,
        copy=False,
        help='Most recent X-GitHub-Delivery / X-Gitea-Delivery header processed. '
             'Used to deduplicate provider retries (idempotency).',
    )

    def init(self):
        # Index used by the webhook controller on every push receive.
        # Without it each push sequentially scans saas_instance_repo.
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS saas_instance_repo_webhook_secret_uidx
                ON saas_instance_repo (webhook_secret)
                WHERE webhook_secret IS NOT NULL
        """)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('webhook_secret'):
                vals['webhook_secret'] = secrets.token_hex(20)
            if 'webhook_enabled' not in vals:
                vals['webhook_enabled'] = True
        return super().create(vals_list)

    @api.depends('repo_url')
    def _compute_name(self):
        for rec in self:
            if rec.repo_url:
                # Extract repo name from URL
                url = rec.repo_url.rstrip('/')
                if url.endswith('.git'):
                    url = url[:-4]
                rec.name = url.split('/')[-1] if '/' in url else url
            else:
                rec.name = ''

    def _get_repo_dir_name(self):
        """Return a safe directory name for this repo."""
        self.ensure_one()
        return self.name or 'repo_%d' % self.id

    @staticmethod
    def _strip_userinfo(url):
        """Remove any user:pass@ credentials from an HTTPS URL."""
        if not url or not url.startswith('https://'):
            return url
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            # Rebuild without userinfo
            parsed = parsed._replace(netloc=parsed.hostname + (
                ':%s' % parsed.port if parsed.port else ''))
            return urlunparse(parsed)
        return url

    def _web_commit_url(self, sha):
        """Browser URL for ``sha`` on the repo's host (GitHub/GitLab/Gitea use
        ``/commit/<sha>``). Returns '' when we can't build a clean https URL."""
        self.ensure_one()
        if not sha:
            return ''
        url = self._strip_userinfo(self.repo_url or '')
        if not url or not url.startswith('https://'):
            return ''
        if url.endswith('.git'):
            url = url[:-4]
        return '%s/commit/%s' % (url.rstrip('/'), sha)

    def _get_clone_url(self):
        """Return the clone URL, injecting token if needed for private repos."""
        self.ensure_one()
        assert_safe_git_url(self.repo_url)
        url = self._strip_userinfo(self.repo_url)
        token = self.sudo().github_token
        if token and url.startswith('https://'):
            url = 'https://x-access-token:%s@%s' % (
                token, url[len('https://'):]
            )
        return url

    @api.onchange('repo_url')
    def _onchange_repo_url(self):
        if self.repo_url:
            self.repo_url = self._strip_userinfo(self.repo_url.strip())

    @api.depends('webhook_enabled', 'webhook_provider_id')
    def _compute_webhook_registered(self):
        for rec in self:
            rec.webhook_registered = bool(
                rec.webhook_enabled and rec.webhook_provider_id
            )

    @api.depends('webhook_secret')
    def _compute_webhook_url(self):
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        for rec in self:
            if rec.webhook_secret and rec.id:
                rec.webhook_url = '%s/saas/webhook/%s' % (base_url, rec.webhook_secret)
            else:
                rec.webhook_url = False

    def _get_public_base_url(self):
        """Return web.base.url only if it's a valid public URL, else False."""
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        if not base_url:
            return False
        lower = base_url.lower()
        if 'localhost' in lower or '127.0.0.1' in lower or '0.0.0.0' in lower:
            return False
        if not lower.startswith('https://'):
            return False
        return base_url.rstrip('/')

    def _register_webhook_with_retry(self, max_retries=3):
        """Register webhook on Git provider with retries, then verify it's there."""
        self.ensure_one()
        import time

        base_url = self._get_public_base_url()
        if not base_url:
            _logger.warning(
                "Webhook: web.base.url is not a public HTTPS URL. "
                "Cannot register webhook for %s. "
                "Set web.base.url to your public domain.", self.name,
            )
            self.instance_id._append_log(
                "Auto-deploy webhook NOT registered: web.base.url is not a public HTTPS URL."
            )
            return False

        # If already registered, verify it still exists; skip if valid
        if self.webhook_provider_id:
            try:
                if self._verify_webhook_on_provider():
                    _logger.info("Webhook already registered and valid for %s", self.name)
                    return True
            except Exception:
                pass
            # Old hook is gone, clear and re-register
            self.webhook_provider_id = False

        # Retry registration
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                self._register_webhook_on_provider()
                break
            except Exception as e:
                last_error = e
                _logger.warning(
                    "Webhook register attempt %d/%d failed for %s: %s",
                    attempt, max_retries, self.name, e,
                )
                if attempt < max_retries:
                    time.sleep(2 * attempt)
        else:
            self.instance_id._append_log(
                "Auto-deploy webhook registration failed after %d attempts: %s"
                % (max_retries, last_error)
            )
            return False

        # Verify it's actually registered
        try:
            registered = self._verify_webhook_on_provider()
            if registered:
                self.instance_id._append_log(
                    "Auto-deploy webhook registered and verified for %s" % self.name
                )
                return True
            else:
                self.instance_id._append_log(
                    "Webhook sent to %s but could not be verified. "
                    "Check the repo webhook settings manually." % self._detect_provider()
                )
                return False
        except Exception as e:
            _logger.warning("Webhook verification failed for %s: %s", self.name, e)
            self.instance_id._append_log(
                "Auto-deploy webhook registered for %s (verification skipped: %s)"
                % (self.name, e)
            )
            return True

    def _verify_webhook_on_provider(self):
        """Check if our webhook URL exists on the Git provider. Returns True/False."""
        self.ensure_one()
        token = self.sudo().github_token
        if not token or not self.webhook_provider_id:
            return False

        provider = self._detect_provider()
        owner, repo_name = self._parse_owner_repo()
        if not provider or not owner or not repo_name:
            return False

        base = self._get_provider_base_url()
        hook_id = self.webhook_provider_id

        try:
            if provider == 'github':
                resp = http_requests.get(
                    '%s/repos/%s/%s/hooks/%s' % (base, owner, repo_name, hook_id),
                    headers={
                        'Authorization': 'token %s' % token,
                        'Accept': 'application/vnd.github+json',
                    },
                    timeout=15,
                )
                return resp.status_code == 200

            elif provider == 'gitlab':
                from urllib.parse import quote as url_quote
                project_path = '%s/%s' % (owner, repo_name)
                resp = http_requests.get(
                    '%s/projects/%s/hooks/%s' % (
                        base, url_quote(project_path, safe=''), hook_id,
                    ),
                    headers={'PRIVATE-TOKEN': token},
                    timeout=15,
                )
                return resp.status_code == 200

            elif provider == 'gitea':
                parsed = urlparse(self.repo_url)
                gitea_base = '%s://%s/api/v1' % (parsed.scheme or 'https', parsed.hostname)
                resp = http_requests.get(
                    '%s/repos/%s/%s/hooks/%s' % (gitea_base, owner, repo_name, hook_id),
                    headers={'Authorization': 'token %s' % token},
                    timeout=15,
                )
                return resp.status_code == 200

            elif provider == 'bitbucket':
                resp = http_requests.get(
                    '%s/repositories/%s/%s/hooks/%s' % (base, owner, repo_name, hook_id),
                    auth=(owner, token),
                    timeout=15,
                )
                return resp.status_code == 200

        except http_requests.RequestException:
            return False

        return False

    def action_enable_webhook(self):
        """Enable auto-deploy: generate secret and register webhook on Git provider."""
        for rec in self:
            base_url = rec._get_public_base_url()
            if not base_url:
                raise UserError(_(
                    "Cannot register webhook: web.base.url is '%s'.\n\n"
                    "Set it to your public HTTPS domain in:\n"
                    "Settings → Technical → System Parameters → web.base.url\n\n"
                    "Example: https://main.saas.odex.sa"
                ) % rec.env['ir.config_parameter'].sudo().get_param('web.base.url', ''))

            if not rec.webhook_secret:
                rec.webhook_secret = secrets.token_hex(20)
            rec.webhook_enabled = True
            # If repo was in error state due to old webhook failure,
            # reset to cloned since the clone itself was fine.
            if rec.state == 'error' and not rec.error_message:
                rec.state = 'cloned'
            # Use the retrying wrapper so transient provider failures
            # don't roll back the user's transaction (which would lose
            # the freshly-generated secret).
            rec._register_webhook_with_retry()

    def action_disable_webhook(self):
        """Disable auto-deploy and remove webhook from Git provider."""
        for rec in self:
            try:
                rec._unregister_webhook_from_provider()
            except Exception as e:
                _logger.warning("Auto-unregister webhook failed for %s: %s", rec.name, e)
            rec.webhook_enabled = False
            rec.webhook_provider_id = False

    def action_regenerate_webhook_secret(self):
        """Regenerate the webhook secret and re-register on provider."""
        for rec in self:
            # Remove old webhook
            try:
                rec._unregister_webhook_from_provider()
            except Exception:
                pass
            rec.webhook_secret = secrets.token_hex(20)
            if rec.webhook_enabled:
                try:
                    rec._register_webhook_on_provider()
                except Exception as e:
                    _logger.warning("Re-register webhook failed for %s: %s", rec.name, e)

    # ------------------------------------------------------------------
    # Git provider detection & API
    # ------------------------------------------------------------------

    def _detect_provider(self):
        """Detect the Git provider from the repo URL.
        Returns: 'github', 'gitlab', 'bitbucket', 'gitea', or False.
        """
        self.ensure_one()
        if not self.repo_url:
            return False
        url = self.repo_url.lower()
        if 'github.com' in url:
            return 'github'
        if 'gitlab.com' in url or 'gitlab' in url:
            return 'gitlab'
        if 'bitbucket.org' in url:
            return 'bitbucket'
        # Gitea / self-hosted: try Gitea API if token is present
        if self.sudo().github_token:
            return 'gitea'
        return False

    def _parse_owner_repo(self):
        """Extract owner/repo from the URL. Returns (owner, repo) or (False, False)."""
        self.ensure_one()
        url = self.repo_url.strip().rstrip('/')
        if url.endswith('.git'):
            url = url[:-4]
        # Handle git@host:owner/repo
        if url.startswith('git@'):
            _, path = url.split(':', 1)
            parts = path.strip('/').split('/')
        else:
            parsed = urlparse(url)
            parts = parsed.path.strip('/').split('/')
        if len(parts) >= 2:
            return parts[-2], parts[-1]
        return False, False

    def _get_provider_base_url(self):
        """Get the API base URL for the provider."""
        self.ensure_one()
        url = self.repo_url.lower()
        if 'github.com' in url:
            return 'https://api.github.com'
        if 'gitlab.com' in url:
            return 'https://gitlab.com/api/v4'
        if 'bitbucket.org' in url:
            return 'https://api.bitbucket.org/2.0'
        # Self-hosted (e.g. Gitea): the host comes straight from the customer's
        # URL and we're about to call its API — guard against SSRF first.
        assert_safe_git_url(self.repo_url)
        parsed = urlparse(self.repo_url)
        return '%s://%s' % (parsed.scheme or 'https', parsed.hostname)

    def _find_existing_webhook(self, provider, base, owner, repo, token):
        """Check if our webhook URL is already registered on the provider.

        Returns the hook ID if found, False otherwise.
        """
        webhook_url = self.webhook_url
        try:
            hooks = []
            if provider == 'github':
                resp = http_requests.get(
                    '%s/repos/%s/%s/hooks' % (base, owner, repo),
                    headers={
                        'Authorization': 'token %s' % token,
                        'Accept': 'application/vnd.github+json',
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                hooks = resp.json()
                for h in hooks:
                    url = h.get('config', {}).get('url', '')
                    if webhook_url and webhook_url in url:
                        return str(h.get('id', ''))

            elif provider == 'gitlab':
                from urllib.parse import quote as url_quote
                project_path = '%s/%s' % (owner, repo)
                resp = http_requests.get(
                    '%s/projects/%s/hooks' % (base, url_quote(project_path, safe='')),
                    headers={'PRIVATE-TOKEN': token},
                    timeout=15,
                )
                resp.raise_for_status()
                hooks = resp.json()
                for h in hooks:
                    if webhook_url and webhook_url in h.get('url', ''):
                        return str(h.get('id', ''))

            elif provider == 'gitea':
                parsed = urlparse(self.repo_url)
                gitea_base = '%s://%s/api/v1' % (
                    parsed.scheme or 'https', parsed.hostname,
                )
                resp = http_requests.get(
                    '%s/repos/%s/%s/hooks' % (gitea_base, owner, repo),
                    headers={'Authorization': 'token %s' % token},
                    timeout=15,
                )
                resp.raise_for_status()
                hooks = resp.json()
                for h in hooks:
                    url = h.get('config', {}).get('url', '')
                    if webhook_url and webhook_url in url:
                        return str(h.get('id', ''))
        except Exception:
            pass
        return False

    def _update_existing_webhook(self, provider, base, owner, repo, token,
                                  hook_id, webhook_url):
        """Update an existing webhook on the provider with the current secret."""
        try:
            if provider == 'github':
                http_requests.patch(
                    '%s/repos/%s/%s/hooks/%s' % (base, owner, repo, hook_id),
                    headers={
                        'Authorization': 'token %s' % token,
                        'Accept': 'application/vnd.github+json',
                    },
                    json={
                        'active': True,
                        'events': ['push'],
                        'config': {
                            'url': webhook_url,
                            'content_type': 'json',
                            'secret': self.webhook_secret,
                            'insecure_ssl': '0',
                        },
                    },
                    timeout=15,
                ).raise_for_status()
            elif provider == 'gitlab':
                from urllib.parse import quote as url_quote
                project_path = '%s/%s' % (owner, repo)
                http_requests.put(
                    '%s/projects/%s/hooks/%s' % (
                        base, url_quote(project_path, safe=''), hook_id,
                    ),
                    headers={'PRIVATE-TOKEN': token},
                    json={
                        'url': webhook_url,
                        'push_events': True,
                        'token': self.webhook_secret,
                        'enable_ssl_verification': True,
                    },
                    timeout=15,
                ).raise_for_status()
            elif provider == 'gitea':
                parsed = urlparse(self.repo_url)
                gitea_base = '%s://%s/api/v1' % (
                    parsed.scheme or 'https', parsed.hostname,
                )
                http_requests.patch(
                    '%s/repos/%s/%s/hooks/%s' % (gitea_base, owner, repo, hook_id),
                    headers={'Authorization': 'token %s' % token},
                    json={
                        'active': True,
                        'events': ['push'],
                        'config': {
                            'url': webhook_url,
                            'content_type': 'json',
                            'secret': self.webhook_secret,
                        },
                    },
                    timeout=15,
                ).raise_for_status()
        except Exception as e:
            _logger.warning(
                "Failed to update existing webhook %s on %s: %s",
                hook_id, provider, e,
            )

    # ------------------------------------------------------------------
    # Branch automation (Odoo.sh-style environments). Every Staging /
    # Development server is bound to a Git branch created from the
    # project's main branch; on delete the customer may opt to remove it.
    # ------------------------------------------------------------------
    def _branch_provider_context(self):
        """Resolve ``(provider, base, owner, repo, token)`` for branch ops, or
        raise a clear UserError when we can't talk to the provider."""
        self.ensure_one()
        token = self.sudo().github_token
        provider = self._detect_provider()
        owner, repo = self._parse_owner_repo()
        if not provider or not owner or not repo:
            raise UserError(_(
                "Couldn't determine the Git provider for %s. Branch automation "
                "needs a GitHub/GitLab/Gitea/Bitbucket HTTPS URL.")
                % (self.repo_url or ''))
        if not token:
            raise UserError(_(
                "An access token is required to create or delete branches on "
                "%s.") % (self.repo_url or ''))
        return provider, self._get_provider_base_url(), owner, repo, token

    def _gitea_api_base(self):
        parsed = urlparse(self.repo_url)
        return '%s://%s/api/v1' % (parsed.scheme or 'https', parsed.hostname)

    def _create_branch_on_provider(self, branch, source_branch):
        """Create ``branch`` from ``source_branch`` on the Git provider.
        Idempotent: if the branch already exists, this is a no-op."""
        self.ensure_one()
        if not branch or branch == source_branch:
            return False
        provider, base, owner, repo, token = self._branch_provider_context()
        try:
            if provider == 'github':
                ref = http_requests.get(
                    '%s/repos/%s/%s/git/ref/heads/%s' % (
                        base, owner, repo, source_branch),
                    headers={'Authorization': 'token %s' % token,
                             'Accept': 'application/vnd.github+json'},
                    timeout=15)
                ref.raise_for_status()
                sha = ref.json().get('object', {}).get('sha')
                resp = http_requests.post(
                    '%s/repos/%s/%s/git/refs' % (base, owner, repo),
                    headers={'Authorization': 'token %s' % token,
                             'Accept': 'application/vnd.github+json'},
                    json={'ref': 'refs/heads/%s' % branch, 'sha': sha},
                    timeout=15)
                if resp.status_code == 422:  # already exists
                    return True
                resp.raise_for_status()
            elif provider == 'gitlab':
                from urllib.parse import quote as url_quote
                proj = url_quote('%s/%s' % (owner, repo), safe='')
                resp = http_requests.post(
                    '%s/projects/%s/repository/branches' % (base, proj),
                    headers={'PRIVATE-TOKEN': token},
                    params={'branch': branch, 'ref': source_branch},
                    timeout=15)
                if resp.status_code == 400:  # already exists
                    return True
                resp.raise_for_status()
            elif provider == 'gitea':
                resp = http_requests.post(
                    '%s/repos/%s/%s/branches' % (
                        self._gitea_api_base(), owner, repo),
                    headers={'Authorization': 'token %s' % token},
                    json={'new_branch_name': branch,
                          'old_branch_name': source_branch},
                    timeout=15)
                if resp.status_code == 409:  # already exists
                    return True
                resp.raise_for_status()
            elif provider == 'bitbucket':
                resp = http_requests.post(
                    '%s/repositories/%s/%s/refs/branches' % (base, owner, repo),
                    headers={'Authorization': 'Bearer %s' % token},
                    json={'name': branch, 'target': {'hash': source_branch}},
                    timeout=15)
                if resp.status_code in (409, 400):  # already exists
                    return True
                resp.raise_for_status()
            else:
                raise UserError(_(
                    "Branch automation is not supported for this provider."))
        except UserError:
            raise
        except http_requests.HTTPError as e:
            raise UserError(_(
                "Failed to create branch '%s' from '%s' on %s: %s")
                % (branch, source_branch, provider, e))
        _logger.info("Created branch %s from %s on %s/%s",
                     branch, source_branch, owner, repo)
        return True

    def _merge_branch_on_provider(self, target_branch, source_branch):
        """Merge ``source_branch`` INTO ``target_branch`` on the Git provider
        (Odoo.sh-style drag-to-merge). Returns a short status string:
        'merged', 'up_to_date'. Raises UserError on conflict / unsupported
        provider so the caller can surface a clear message."""
        self.ensure_one()
        if not target_branch or not source_branch \
                or target_branch == source_branch:
            raise UserError(_("Pick two different branches to merge."))
        provider, base, owner, repo, token = self._branch_provider_context()
        if provider == 'github':
            resp = http_requests.post(
                '%s/repos/%s/%s/merges' % (base, owner, repo),
                headers={'Authorization': 'token %s' % token,
                         'Accept': 'application/vnd.github+json'},
                json={'base': target_branch, 'head': source_branch,
                      'commit_message': 'Merge %s into %s (via VELTNEX)'
                                        % (source_branch, target_branch)},
                timeout=30)
            if resp.status_code == 201:
                return 'merged'
            if resp.status_code == 204:
                return 'up_to_date'
            if resp.status_code == 409:
                raise UserError(_(
                    "Merge conflict between '%s' and '%s'. Resolve it in Git, "
                    "then try again.") % (source_branch, target_branch))
            raise UserError(_(
                "Couldn't merge '%s' into '%s' (%s): %s")
                % (source_branch, target_branch, resp.status_code,
                   (resp.text or '')[:200]))
        # Other providers: a one-call branch merge isn't uniformly available
        # (GitLab/Gitea/Bitbucket go through merge requests). Be honest rather
        # than half-do it.
        raise UserError(_(
            "Drag-to-merge is currently supported for GitHub repositories. "
            "Please merge '%s' into '%s' from your Git host.")
            % (source_branch, target_branch))

    def _delete_branch_on_provider(self, branch):
        """Delete ``branch`` on the Git provider. Tolerates an already-gone
        branch; refuses to delete the project's protected main branch."""
        self.ensure_one()
        instance = self.instance_id
        main_branch = (instance.parent_id.main_branch if instance
                       and instance.parent_id else None) or (
            instance.main_branch if instance else None)
        if not branch or (main_branch and branch == main_branch):
            return False
        provider, base, owner, repo, token = self._branch_provider_context()
        try:
            if provider == 'github':
                resp = http_requests.delete(
                    '%s/repos/%s/%s/git/refs/heads/%s' % (
                        base, owner, repo, branch),
                    headers={'Authorization': 'token %s' % token,
                             'Accept': 'application/vnd.github+json'},
                    timeout=15)
            elif provider == 'gitlab':
                from urllib.parse import quote as url_quote
                proj = url_quote('%s/%s' % (owner, repo), safe='')
                resp = http_requests.delete(
                    '%s/projects/%s/repository/branches/%s' % (
                        base, proj, url_quote(branch, safe='')),
                    headers={'PRIVATE-TOKEN': token},
                    timeout=15)
            elif provider == 'gitea':
                resp = http_requests.delete(
                    '%s/repos/%s/%s/branches/%s' % (
                        self._gitea_api_base(), owner, repo, branch),
                    headers={'Authorization': 'token %s' % token},
                    timeout=15)
            elif provider == 'bitbucket':
                resp = http_requests.delete(
                    '%s/repositories/%s/%s/refs/branches/%s' % (
                        base, owner, repo, branch),
                    headers={'Authorization': 'Bearer %s' % token},
                    timeout=15)
            else:
                return False
            if resp.status_code not in (404, 410):  # already gone is fine
                resp.raise_for_status()
        except http_requests.HTTPError as e:
            raise UserError(_(
                "Failed to delete branch '%s' on %s: %s") % (branch, provider, e))
        _logger.info("Deleted branch %s on %s/%s", branch, owner, repo)
        return True

    def _list_remote_branches(self):
        """List branch names on the remote (for the Staging branch picker).
        Best-effort: returns [] when provider/credentials don't allow it."""
        self.ensure_one()
        provider = self._detect_provider()
        owner, repo = self._parse_owner_repo()
        if not provider or not owner or not repo:
            return []
        token = self.sudo().github_token
        base = self._get_provider_base_url()
        names = []
        if provider == 'github':
            headers = {'Accept': 'application/vnd.github+json'}
            if token:
                headers['Authorization'] = 'token %s' % token
            resp = http_requests.get(
                '%s/repos/%s/%s/branches' % (base, owner, repo),
                headers=headers, params={'per_page': 100}, timeout=15)
            resp.raise_for_status()
            names = [b.get('name') for b in resp.json()]
        elif provider == 'gitlab':
            from urllib.parse import quote as url_quote
            proj = url_quote('%s/%s' % (owner, repo), safe='')
            headers = {'PRIVATE-TOKEN': token} if token else {}
            resp = http_requests.get(
                '%s/projects/%s/repository/branches' % (base, proj),
                headers=headers, params={'per_page': 100}, timeout=15)
            resp.raise_for_status()
            names = [b.get('name') for b in resp.json()]
        elif provider == 'gitea':
            headers = {'Authorization': 'token %s' % token} if token else {}
            resp = http_requests.get(
                '%s/repos/%s/%s/branches' % (
                    self._gitea_api_base(), owner, repo),
                headers=headers, timeout=15)
            resp.raise_for_status()
            names = [b.get('name') for b in resp.json()]
        elif provider == 'bitbucket':
            headers = {'Authorization': 'Bearer %s' % token} if token else {}
            resp = http_requests.get(
                '%s/repositories/%s/%s/refs/branches' % (base, owner, repo),
                headers=headers, timeout=15)
            resp.raise_for_status()
            names = [b.get('name') for b in resp.json().get('values', [])]
        return [n for n in names if n]

    def _ensure_env_branch(self):
        """For a child env repo, make sure its branch exists on the remote,
        creating it from the project's main branch when missing. No-op for
        Production (it always tracks an existing main branch)."""
        self.ensure_one()
        instance = self.instance_id
        if not instance or instance.environment == 'production':
            return
        source = (instance.parent_id.main_branch
                  if instance.parent_id else 'main') or 'main'
        self._create_branch_on_provider(self.branch, source)

    def _register_webhook_on_provider(self):
        """Register the webhook on the Git provider via API.

        If the webhook URL already exists on the provider (e.g. from a
        previous registration), reuses it instead of creating a duplicate.
        """
        self.ensure_one()
        token = self.sudo().github_token
        if not token:
            _logger.info("No token for %s, skipping auto-register", self.name)
            return
        provider = self._detect_provider()
        if not provider:
            _logger.info("Unknown provider for %s, skipping auto-register", self.name)
            return

        webhook_url = self.webhook_url
        if not webhook_url:
            return

        owner, repo = self._parse_owner_repo()
        if not owner or not repo:
            return

        base = self._get_provider_base_url()

        # Check if webhook already exists on the provider.
        # If found, update it with the current secret to ensure
        # signature verification works, then reuse the ID.
        existing_id = self._find_existing_webhook(
            provider, base, owner, repo, token,
        )
        if existing_id:
            self._update_existing_webhook(
                provider, base, owner, repo, token, existing_id, webhook_url,
            )
            self.webhook_provider_id = existing_id
            _logger.info(
                "Webhook already exists on %s for %s/%s (hook_id=%s), updated secret",
                provider, owner, repo, existing_id,
            )
            return

        hook_id = False

        try:
            if provider == 'github':
                headers = {
                    'Authorization': 'token %s' % token,
                    'Accept': 'application/vnd.github+json',
                }
                resp = http_requests.post(
                    '%s/repos/%s/%s/hooks' % (base, owner, repo),
                    headers=headers,
                    json={
                        'name': 'web',
                        'active': True,
                        'events': ['push'],
                        'config': {
                            'url': webhook_url,
                            'content_type': 'json',
                            'secret': self.webhook_secret,
                            'insecure_ssl': '0',
                        },
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                hook_id = str(resp.json().get('id', ''))

            elif provider == 'gitlab':
                headers = {'PRIVATE-TOKEN': token}
                project_path = '%s/%s' % (owner, repo)
                from urllib.parse import quote as url_quote
                resp = http_requests.post(
                    '%s/projects/%s/hooks' % (base, url_quote(project_path, safe='')),
                    headers=headers,
                    json={
                        'url': webhook_url,
                        'push_events': True,
                        'token': self.webhook_secret,
                        'enable_ssl_verification': True,
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                hook_id = str(resp.json().get('id', ''))

            elif provider == 'bitbucket':
                resp = http_requests.post(
                    '%s/repositories/%s/%s/hooks' % (base, owner, repo),
                    auth=(owner, token),
                    json={
                        'description': 'SaaS Auto-Deploy',
                        'url': webhook_url,
                        'active': True,
                        'events': ['repo:push'],
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                hook_id = str(resp.json().get('uuid', ''))

            elif provider == 'gitea':
                parsed = urlparse(self.repo_url)
                gitea_base = '%s://%s/api/v1' % (
                    parsed.scheme or 'https', parsed.hostname,
                )
                headers = {'Authorization': 'token %s' % token}
                resp = http_requests.post(
                    '%s/repos/%s/%s/hooks' % (gitea_base, owner, repo),
                    headers=headers,
                    json={
                        'type': 'gitea',
                        'active': True,
                        'events': ['push'],
                        'config': {
                            'url': webhook_url,
                            'content_type': 'json',
                            'secret': self.webhook_secret,
                        },
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                hook_id = str(resp.json().get('id', ''))

            if hook_id:
                self.webhook_provider_id = hook_id
                _logger.info(
                    "Webhook auto-registered on %s for %s/%s (hook_id=%s)",
                    provider, owner, repo, hook_id,
                )

        except http_requests.RequestException as e:
            _logger.warning(
                "Failed to auto-register webhook on %s for %s/%s: %s",
                provider, owner, repo, e,
            )
            raise UserError(
                _("Could not register webhook on %s: %s\n"
                  "You can manually add this URL in your repo settings:\n%s")
                % (provider, e, webhook_url)
            )

    def _unregister_webhook_from_provider(self):
        """Remove the webhook from the Git provider via API."""
        self.ensure_one()
        token = self.sudo().github_token
        if not token or not self.webhook_provider_id:
            return

        provider = self._detect_provider()
        owner, repo = self._parse_owner_repo()
        if not provider or not owner or not repo:
            return

        base = self._get_provider_base_url()
        hook_id = self.webhook_provider_id

        try:
            if provider == 'github':
                http_requests.delete(
                    '%s/repos/%s/%s/hooks/%s' % (base, owner, repo, hook_id),
                    headers={
                        'Authorization': 'token %s' % token,
                        'Accept': 'application/vnd.github+json',
                    },
                    timeout=15,
                )

            elif provider == 'gitlab':
                from urllib.parse import quote as url_quote
                project_path = '%s/%s' % (owner, repo)
                http_requests.delete(
                    '%s/projects/%s/hooks/%s' % (
                        base, url_quote(project_path, safe=''), hook_id,
                    ),
                    headers={'PRIVATE-TOKEN': token},
                    timeout=15,
                )

            elif provider == 'bitbucket':
                http_requests.delete(
                    '%s/repositories/%s/%s/hooks/%s' % (base, owner, repo, hook_id),
                    auth=(owner, token),
                    timeout=15,
                )

            elif provider == 'gitea':
                parsed = urlparse(self.repo_url)
                gitea_base = '%s://%s/api/v1' % (
                    parsed.scheme or 'https', parsed.hostname,
                )
                http_requests.delete(
                    '%s/repos/%s/%s/hooks/%s' % (gitea_base, owner, repo, hook_id),
                    headers={'Authorization': 'token %s' % token},
                    timeout=15,
                )

            _logger.info(
                "Webhook removed from %s for %s/%s (hook_id=%s)",
                provider, owner, repo, hook_id,
            )
        except http_requests.RequestException as e:
            _logger.warning(
                "Failed to remove webhook from %s for %s/%s: %s",
                provider, owner, repo, e,
            )

    def action_check_webhook(self):
        """Check if the webhook is correctly registered on the Git provider."""
        self.ensure_one()
        token = self.sudo().github_token
        if not token:
            raise UserError(_("No token configured. Cannot check webhook status."))

        provider = self._detect_provider()
        owner, repo_name = self._parse_owner_repo()
        if not provider or not owner or not repo_name:
            raise UserError(_("Could not detect provider or parse owner/repo from URL."))

        base = self._get_provider_base_url()
        hooks = []

        try:
            if provider == 'github':
                resp = http_requests.get(
                    '%s/repos/%s/%s/hooks' % (base, owner, repo_name),
                    headers={
                        'Authorization': 'token %s' % token,
                        'Accept': 'application/vnd.github+json',
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                hooks = resp.json()

            elif provider == 'gitlab':
                from urllib.parse import quote as url_quote
                project_path = '%s/%s' % (owner, repo_name)
                resp = http_requests.get(
                    '%s/projects/%s/hooks' % (base, url_quote(project_path, safe='')),
                    headers={'PRIVATE-TOKEN': token},
                    timeout=15,
                )
                resp.raise_for_status()
                hooks = resp.json()

            elif provider == 'gitea':
                parsed = urlparse(self.repo_url)
                gitea_base = '%s://%s/api/v1' % (parsed.scheme or 'https', parsed.hostname)
                resp = http_requests.get(
                    '%s/repos/%s/%s/hooks' % (gitea_base, owner, repo_name),
                    headers={'Authorization': 'token %s' % token},
                    timeout=15,
                )
                resp.raise_for_status()
                hooks = resp.json()

        except http_requests.RequestException as e:
            raise UserError(_("Failed to fetch webhooks from %s: %s") % (provider, e))

        # Check if our webhook URL is in the list
        webhook_url = self.webhook_url
        found = False
        details = []
        for h in hooks:
            # GitHub/Gitea: config.url, GitLab: url
            hook_url = ''
            if isinstance(h, dict):
                hook_url = h.get('config', {}).get('url', '') or h.get('url', '')
            details.append("  ID=%s  URL=%s  Active=%s" % (
                h.get('id', '?'), hook_url, h.get('active', h.get('push_events', '?')),
            ))
            if webhook_url and webhook_url in hook_url:
                found = True

        msg = "Provider: %s\nRepo: %s/%s\nOur webhook URL: %s\n\n" % (
            provider, owner, repo_name, webhook_url,
        )
        if found:
            msg += "FOUND - Webhook is registered on %s" % provider
        else:
            msg += "NOT FOUND - Webhook is NOT registered on %s\n\n" % provider
            msg += "Registered webhooks (%d):\n%s" % (len(hooks), '\n'.join(details) if details else '  (none)')
            msg += "\n\nTIP: Click 'Enable Webhook' to register it, or add the URL manually in your repo settings."

        raise UserError(msg)

    def action_test_webhook(self):
        """Send a test ping to our own webhook endpoint to verify it's reachable."""
        self.ensure_one()
        import hashlib
        webhook_url = self.webhook_url
        if not webhook_url:
            raise UserError(_("No webhook URL configured."))

        payload = json.dumps({
            'ref': 'refs/heads/%s' % self.branch,
            'commits': [{'message': 'Webhook test from SaaS platform'}],
            'repository': {'clone_url': self.repo_url},
        }).encode()
        sig = 'sha256=' + hmac.new(
            self.webhook_secret.encode(), payload, hashlib.sha256,
        ).hexdigest()

        try:
            resp = http_requests.post(
                webhook_url,
                data=payload,
                headers={
                    'Content-Type': 'application/json',
                    'X-GitHub-Event': 'ping',
                    'X-Hub-Signature-256': sig,
                },
                timeout=15,
            )
            body = resp.text[:500]
            msg = (
                "Test result:\n\n"
                "URL: %s\n"
                "Status: %s %s\n"
                "Response: %s"
            ) % (webhook_url, resp.status_code, resp.reason, body)

            if resp.status_code == 200:
                msg += "\n\nWebhook endpoint is reachable and accepted the request."
            else:
                msg += "\n\nWebhook endpoint returned an error. Check the URL and server logs."
        except http_requests.RequestException as e:
            msg = (
                "Test FAILED:\n\n"
                "URL: %s\n"
                "Error: %s\n\n"
                "The webhook endpoint is NOT reachable. Check:\n"
                "- web.base.url is set to the public HTTPS domain\n"
                "- Nginx proxies /saas/webhook/* to Odoo\n"
                "- Firewall allows incoming HTTPS"
            ) % (webhook_url, e)

        raise UserError(msg)

    @staticmethod
    def _normalize_repo_url(url):
        """Normalize a repo URL for comparison (strip protocol, auth, .git suffix)."""
        if not url:
            return ''
        url = url.strip().rstrip('/')
        # Remove .git suffix
        if url.endswith('.git'):
            url = url[:-4]
        # Remove protocol
        for prefix in ('https://', 'http://', 'git@', 'ssh://'):
            if url.startswith(prefix):
                url = url[len(prefix):]
                break
        # git@github.com:user/repo -> github.com/user/repo
        url = url.replace(':', '/', 1) if ':' in url and '/' not in url.split(':')[0] else url
        # Remove any auth info
        if '@' in url:
            url = url.split('@', 1)[1]
        return url.lower()

    def _match_webhook_repo_url(self, incoming_url):
        """Check if an incoming webhook repo URL matches this repo."""
        return self._normalize_repo_url(self.repo_url) == self._normalize_repo_url(incoming_url)

    def _match_webhook_branch(self, ref):
        """Check if a webhook ref (e.g. refs/heads/main) matches this repo's branch."""
        if not ref:
            return False
        branch = ref
        if branch.startswith('refs/heads/'):
            branch = branch[len('refs/heads/'):]
        return branch == self.branch

    @staticmethod
    def verify_signature(payload_body, secret, signature_header):
        """Verify webhook signature (HMAC-SHA256 or GitLab plain token).

        SHA-1 signatures are explicitly rejected — they are deprecated
        by every supported provider and accepting them would let an
        attacker downgrade clients that send both headers.
        """
        if not signature_header or not secret:
            return False
        if signature_header.startswith('sha1='):
            return False
        if signature_header.startswith('sha256='):
            expected = 'sha256=' + hmac.new(
                secret.encode(), payload_body, hashlib.sha256,
            ).hexdigest()
            return hmac.compare_digest(expected, signature_header)
        # GitLab sends the token directly in X-Gitlab-Token header.
        return hmac.compare_digest(secret, signature_header)

    # Lock to prevent concurrent deploys on the same instance
    _deploy_locks = {}

    def _run_webhook_deploy(self, build_id):
        """Queue job (ARCH-004 Phase 4): pull + restart for a webhook push and
        mark the build success. Raises on failure → the job's on_error
        (_on_webhook_deploy_error) marks the build failed + handles the repo."""
        self.ensure_one()
        self._do_webhook_pull_and_restart()
        build = self.env['saas.build'].browse(build_id)
        if build.exists():
            log_tail = (self.instance_id.provisioning_log or '')[-8000:]
            build._mark('success', log_tail)

    def _on_webhook_deploy_error(self, exception, build_id):
        """on_error handler for the webhook-deploy job: fail the build, then run
        the standard repo background-error handling."""
        self.ensure_one()
        build = self.env['saas.build'].browse(build_id)
        if build.exists():
            build._mark('failed', str(exception))
        self._on_repo_background_error(exception)

    def _do_webhook_pull_and_restart(self):
        """Pull repo and restart instance (runs in background thread).

        Uses a blocking lock so no push event is lost — if another
        deploy is running, this one waits then proceeds.
        """
        self.ensure_one()
        import threading

        instance_id = self.instance_id.id
        lock = self._deploy_locks.setdefault(instance_id, threading.Lock())

        # Wait for any running deploy to finish, then proceed.
        lock.acquire()
        try:
            _logger.info(
                "Webhook auto-deploy: pulling %s for instance %s",
                self.name, self.instance_id.name,
            )
            self._do_pull_repo()
            self.instance_id._update_repo_config_and_restart()
            self.webhook_last_event = fields.Datetime.now()
            # Clear error state on successful pull
            if self.state == 'error':
                self.state = 'cloned'
                self.error_message = False
            _logger.info(
                "Webhook auto-deploy: completed for %s / %s",
                self.name, self.instance_id.name,
            )
        finally:
            lock.release()

    def _get_remote_repo_path(self):
        """Return the full remote path inside the instance's addons directory."""
        self.ensure_one()
        instance = self.instance_id
        instance_path = instance._get_instance_path()
        return '%s/addons/%s' % (instance_path, self._get_repo_dir_name())

    def _detect_addons_subdir(self, ssh, repo_path):
        """Detect the addons subdirectory by scanning for __manifest__.py.

        If modules sit directly in the repo root (e.g. repo/module_a/),
        returns ``False`` (no subdir needed).  If they're nested one level
        deep (e.g. repo/addons/module_a/), returns that subdirectory
        name (e.g. ``'addons'``).
        """
        self.ensure_one()
        # Find all __manifest__.py relative to repo root
        exit_code, stdout, _ = ssh.execute(
            'find %s -name __manifest__.py -maxdepth 3 '
            '-not -path "*/.git/*" 2>/dev/null'
            % shlex.quote(repo_path)
        )
        if exit_code != 0 or not stdout.strip():
            return False

        import os
        # Collect parent directories (the module dirs) relative to repo root
        subdirs = set()
        for line in stdout.strip().splitlines():
            # line: /path/to/repo/[subdir/]module/__manifest__.py
            rel = line.replace(repo_path + '/', '', 1)
            parts = rel.split('/')
            if len(parts) == 2:
                # module/__manifest__.py — modules at repo root, no subdir
                return False
            elif len(parts) == 3:
                # subdir/module/__manifest__.py — modules in a subfolder
                subdirs.add(parts[0])

        if len(subdirs) == 1:
            return subdirs.pop()
        # Multiple subdirs or no clear pattern — don't guess
        return False

    def _get_container_addons_path(self):
        """Return the addons path inside the container for this repo."""
        self.ensure_one()
        base = '/mnt/extra-addons/%s' % self._get_repo_dir_name()
        if self.addons_subdir:
            return '%s/%s' % (base, self.addons_subdir.strip('/'))
        return base

    def _clone_repo(self):
        """Clone the repository on the remote server (no config update or restart)."""
        for rec in self:
            instance = rec.instance_id
            instance._ensure_can_ssh()
            server = instance.docker_server_id
            repo_path = rec._get_remote_repo_path()
            clone_url = rec._get_clone_url()

            # Env servers (staging/dev) may target a branch that doesn't exist
            # yet — create it from the project's main branch before cloning
            # (Odoo.sh style). Best-effort here (it is normally created at
            # env-create time); a genuinely missing branch surfaces as a clear
            # clone error below.
            if rec.instance_id.environment != 'production':
                try:
                    rec._ensure_env_branch()
                except Exception as e:
                    _logger.warning(
                        "Env branch ensure failed for %s: %s", rec.name, e)

            try:
                with server._get_ssh_connection() as ssh:
                    # Allow git on dirs owned by the container user
                    ssh.execute(
                        "git config --global --add safe.directory '*' 2>/dev/null || true"
                    )
                    ssh.execute(
                        "sudo git config --system --add safe.directory '*' 2>/dev/null || true"
                    )

                    # Create parent directory
                    parent = '/'.join(repo_path.rsplit('/', 1)[:-1])
                    ssh.execute('mkdir -p %s' % shlex.quote(parent))

                    # Remove existing repo dir if re-cloning
                    ssh.execute('rm -rf %s' % shlex.quote(repo_path))

                    # Clone
                    instance._append_log(
                        "Cloning repo %s (branch: %s)..." % (rec.repo_url, rec.branch)
                    )
                    # Force TLS cert verification at clone time (ARCH-017) so a
                    # downgraded/compromised host git config can't silently
                    # disable it and allow a MITM of customer code over https.
                    clone_cmd = (
                        'git -c http.sslVerify=true clone --branch %s '
                        '--single-branch --depth 1 %s %s 2>&1'
                    ) % (
                        shlex.quote(rec.branch),
                        shlex.quote(clone_url),
                        shlex.quote(repo_path),
                    )
                    exit_code, stdout, stderr = ssh.execute(clone_cmd, timeout=300)
                    if exit_code != 0:
                        rec.state = 'error'
                        rec.error_message = stdout + '\n' + stderr
                        raise UserError(
                            _("Failed to clone repository:\n%s\n%s")
                            % (stdout[-500:], stderr[-500:])
                        )

                    # Set permissions for container's odoo user
                    container_uid = instance._get_container_uid(ssh)
                    ssh.execute(
                        'sudo chown -R %s:%s %s && sudo chmod -R 777 %s'
                        % (container_uid, container_uid,
                           shlex.quote(repo_path), shlex.quote(repo_path))
                    )

                    # Auto-detect addons subdirectory if not set.
                    # Looks for __manifest__.py files and determines the
                    # common parent directory of all modules.
                    if not rec.addons_subdir:
                        detected = rec._detect_addons_subdir(ssh, repo_path)
                        if detected:
                            rec.addons_subdir = detected
                            instance._append_log(
                                "Auto-detected addons subdirectory: %s" % detected
                            )

                    instance._append_log("Repository cloned successfully.")
                    rec.state = 'cloned'
                    rec.last_pull = fields.Datetime.now()
                    rec.error_message = False

                    # Auto-register webhook on Git provider.
                    # Wrapped in try/except so a webhook failure never
                    # marks the repo as 'error' — the clone succeeded.
                    if rec.webhook_enabled:
                        try:
                            if rec.sudo().github_token:
                                rec._register_webhook_with_retry()
                            else:
                                instance._append_log(
                                    "Auto-deploy webhook NOT registered for %s: "
                                    "no access token provided. Add a token to "
                                    "enable automatic webhook registration."
                                    % rec.name
                                )
                        except Exception as e:
                            _logger.warning(
                                "Webhook registration failed for %s: %s",
                                rec.name, e,
                            )
                            instance._append_log(
                                "WARNING: Webhook registration failed for %s: %s"
                                % (rec.name, e)
                            )

            except UserError:
                raise
            except Exception as e:
                rec.state = 'error'
                rec.error_message = str(e)
                raise UserError(
                    _("Failed to clone repository: %s") % str(e)
                )

    def action_clone_repo(self):
        """Clone the repository, update config, and restart the instance (async)."""
        for rec in self:
            rec.instance_id._ensure_can_ssh()
            # Durable queue (ARCH-004): clone+restart is re-runnable (idempotent)
            # and serialised per instance; no secret args (token is on the row).
            self.env['saas.job']._enqueue(
                rec, '_do_clone_and_restart', channel='deploy',
                lock_key='instance:%s' % rec.instance_id.id, idempotent=True,
                on_error='_on_repo_background_error')

    def _do_clone_and_restart(self):
        """Clone repo and restart instance (runs in background thread)."""
        self._clone_repo()
        self.instance_id._update_repo_config_and_restart()

    def _on_repo_background_error(self, exception):
        """Handle background repo operation failure.

        Only sets state to 'error' if the repo was in 'pending' state
        (clone failed).  If the repo was already 'cloned' (pull/restart
        failed), keep it as 'cloned' so webhooks continue to work.
        """
        if self.state == 'pending':
            self.state = 'error'
        self.error_message = str(exception)
        self.instance_id._append_log(
            "Repo operation failed for %s: %s" % (self.name, exception)
        )

    def action_pull_repo(self):
        """Git pull the repo (async, no restart)."""
        for rec in self:
            if rec.state != 'cloned':
                raise UserError(_("Repository must be cloned first."))
            self.env['saas.job']._enqueue(
                rec, '_do_pull_repo', channel='deploy',
                lock_key='instance:%s' % rec.instance_id.id, idempotent=True,
                on_error='_on_repo_background_error')

    def _do_pull_repo(self):
        """Pull repo (runs in background thread)."""
        self.ensure_one()
        instance = self.instance_id
        instance._ensure_can_ssh()
        server = instance.docker_server_id
        repo_path = self._get_remote_repo_path()
        clone_url = self._get_clone_url()

        try:
            with server._get_ssh_connection() as ssh:
                # Set safe.directory for both current user and root to handle
                # ownership mismatches (repo may be owned by root or container UID)
                ssh.execute(
                    "git config --global --add safe.directory '*' 2>/dev/null || true"
                )
                ssh.execute(
                    "sudo git config --system --add safe.directory '*' 2>/dev/null || true"
                )
                ssh.execute(
                    'cd %s && git remote set-url origin %s'
                    % (shlex.quote(repo_path), shlex.quote(clone_url))
                )

                instance._append_log(
                    "Pulling latest changes for %s..." % self.name
                )
                pull_cmd = 'cd %s && git -c http.sslVerify=true pull origin %s 2>&1' % (
                    shlex.quote(repo_path), shlex.quote(self.branch),
                )
                exit_code, stdout, stderr = ssh.execute(pull_cmd, timeout=300)
                if exit_code != 0:
                    self.error_message = stdout + '\n' + stderr
                    raise UserError(
                        _("Git pull failed:\n%s\n%s")
                        % (stdout[-500:], stderr[-500:])
                    )

                instance._append_log("Pull completed: %s" % stdout.strip()[:200])
                self.last_pull = fields.Datetime.now()
                self.error_message = False

        except UserError:
            raise
        except Exception as e:
            self.error_message = str(e)
            raise UserError(
                _("Failed to pull repository: %s") % str(e)
            )

    def action_reset_to_cloned(self):
        """Reset repo state from error to cloned.

        Use when the repo was cloned successfully but a subsequent
        operation (like webhook registration) set the state to error.
        """
        for rec in self:
            if rec.state == 'error':
                rec.state = 'cloned'
                rec.error_message = False

    def action_remove_repo(self):
        """Remove the repo from the server, update config, and restart."""
        self.unlink()
        return True

    def unlink(self):
        """Delete repo files from server, remove webhook, remove records, and update running instances."""
        for rec in self:
            if rec.webhook_enabled and rec.webhook_provider_id:
                try:
                    rec._unregister_webhook_from_provider()
                except Exception:
                    pass
        instances_to_restart = self.env['saas.instance']
        for rec in self:
            instance = rec.instance_id
            if instance.docker_server_id and rec.state == 'cloned':
                try:
                    instance._ensure_can_ssh()
                    server = instance.docker_server_id
                    repo_path = rec._get_remote_repo_path()
                    with server._get_ssh_connection() as ssh:
                        ssh.execute('rm -rf %s' % shlex.quote(repo_path))
                    instance._append_log("Removed repo directory %s" % repo_path)
                except Exception:
                    _logger.exception("Failed to remove repo dir for %s", rec.name)
            if instance.state == 'running':
                instances_to_restart |= instance

        res = super().unlink()

        for instance in instances_to_restart:
            try:
                instance._update_repo_config_and_restart()
            except Exception:
                _logger.exception(
                    "Failed to update config after repo removal for instance %s",
                    instance.name,
                )
        return res
