import json
import logging

from odoo import http, SUPERUSER_ID, api, fields
from odoo.http import request

_logger = logging.getLogger(__name__)


class SaasWebhookController(http.Controller):
    """Generic Git webhook receiver for auto-deploy.

    Supports:
      - GitHub   (X-Hub-Signature-256, X-GitHub-Event)
      - GitLab   (X-Gitlab-Token, X-Gitlab-Event)
      - Bitbucket (X-Hub-Signature-256, X-Event-Key)
      - Gitea    (X-Hub-Signature-256, X-Gitea-Event)

    Endpoint: POST /saas/webhook/<secret>

    Hard requirements (security):
    - Method must be POST. GET requests would log the secret in Nginx
      access logs / browser history.
    - A valid HMAC signature header MUST be present and verified. The
      URL path is treated as a routing key only, NOT as authentication
      on its own (it can leak via logs/history).
    - SHA-1 signatures are rejected. Modern providers all send SHA-256.
    - Same provider delivery-id is processed at most once (idempotency).
    """

    # Per-repo cap on how often a single webhook secret can trigger the
    # pipeline, so a leaked/guessed secret can't be used to fan out forced
    # redeploys (SEC-011). Generous vs. any realistic push cadence.
    _WEBHOOK_RATE_LIMIT = 30
    _WEBHOOK_RATE_WINDOW = 60

    def _webhook_deny(self):
        """Uniform rejection for every authentication failure (unknown
        secret, missing / SHA-1 / invalid signature). Returning an identical
        response for all of them removes the 404-vs-403 oracle that let an
        attacker tell a valid webhook secret from an invalid one — the real
        reason is logged server-side for operators (SEC-011)."""
        return request.make_json_response(
            {'status': 'ignored', 'reason': 'not found'}, status=404,
        )

    @http.route(
        '/saas/webhook/<string:secret>',
        type='http',
        auth='none',
        methods=['POST'],
        csrf=False,
    )
    def handle_webhook(self, secret, **kw):
        env = api.Environment(request.cr, SUPERUSER_ID, {})
        Repo = env['saas.instance.repo']
        headers = request.httprequest.headers

        # 1. Find the repo by secret. Do this BEFORE parsing the body so
        #    we can refuse early without consuming resources.
        repo = Repo.search([
            ('webhook_secret', '=', secret),
            ('webhook_enabled', '=', True),
        ], limit=1)
        if not repo:
            _logger.warning("Webhook: no matching repo for incoming secret")
            return self._webhook_deny()

        # 1b. Rate-limit per repo (keyed only after a valid secret matched, so
        #     random probes create no state and shared provider IPs aren't
        #     throttled across tenants). Caps deploy fan-out on a known secret.
        allowed, retry_after = env['saas.rate.limit'].sudo()._hit(
            'webhook', str(repo.id),
            self._WEBHOOK_RATE_LIMIT, self._WEBHOOK_RATE_WINDOW)
        if not allowed:
            _logger.warning(
                "Webhook: rate limit hit for repo %s — backing off %ss",
                repo.name, retry_after)
            return request.make_json_response(
                {'status': 'error', 'reason': 'rate limited'},
                status=429, headers=[('Retry-After', str(retry_after))],
            )

        # 2. Read signature header. Missing signature == reject (URL secret
        #    alone is not authentication).
        signature = (
            headers.get('X-Hub-Signature-256')   # GitHub / Gitea / Bitbucket
            or headers.get('X-Gitlab-Token')      # GitLab (token compare)
        )
        # Explicitly reject SHA-1: deprecated for years.
        if not signature and headers.get('X-Hub-Signature'):
            _logger.warning(
                "Webhook: SHA-1 signature received for repo %s — rejected",
                repo.name,
            )
            return self._webhook_deny()
        if not signature:
            _logger.warning(
                "Webhook: missing signature header for repo %s — rejected",
                repo.name,
            )
            return self._webhook_deny()

        # 3. Verify signature against the raw payload BEFORE any parsing.
        payload_body = request.httprequest.get_data()
        if not Repo.verify_signature(payload_body, repo.webhook_secret, signature):
            _logger.warning(
                "Webhook: signature verification failed for repo %s", repo.name,
            )
            return self._webhook_deny()

        # 4. Idempotency: drop duplicate deliveries (provider retries,
        #    deliberate replays). The handler is otherwise unauthenticated
        #    so this is the only replay defence we have.
        delivery_id = (
            headers.get('X-GitHub-Delivery')
            or headers.get('X-Gitea-Delivery')
            or headers.get('X-Request-Id')
            or ''
        ).strip()
        if delivery_id:
            if repo.last_delivery_id == delivery_id:
                _logger.info(
                    "Webhook: duplicate delivery %s for repo %s — ignored",
                    delivery_id, repo.name,
                )
                return request.make_json_response(
                    {'status': 'ignored', 'reason': 'duplicate delivery'},
                )
            repo.write({'last_delivery_id': delivery_id})

        # 5. Sanity-check instance and repo state.
        instance = repo.instance_id
        if instance.state not in ('running', 'stopped'):
            return request.make_json_response(
                {'status': 'ignored', 'reason': 'instance not running'},
            )
        if repo.state not in ('cloned', 'error'):
            return request.make_json_response(
                {'status': 'ignored', 'reason': 'repo not cloned'},
            )

        # 6. Parse body now that signature has been verified.
        try:
            payload = json.loads(payload_body)
        except (ValueError, TypeError):
            return request.make_json_response(
                {'status': 'error', 'reason': 'invalid JSON'}, status=400,
            )

        # 7. Determine event type and filter to push events on the tracked branch.
        event_type = (
            headers.get('X-GitHub-Event')
            or headers.get('X-Gitea-Event')
            or headers.get('X-Gitlab-Event', '').replace(' ', '_').lower()
            or headers.get('X-Event-Key', '')
        )
        if not self._is_push_event(event_type, payload):
            return request.make_json_response(
                {'status': 'ignored', 'reason': 'not a push event'},
            )
        ref = self._extract_ref(payload)
        if not repo._match_webhook_branch(ref):
            return request.make_json_response(
                {'status': 'ignored', 'reason': 'branch mismatch'},
            )

        # 8. Open the build record (visible as "Building…") and enqueue the
        #    pull+restart on the durable queue (ARCH-004 Phase 4) — survives a
        #    worker crash (reaper) and records failures (alert/audit), replacing
        #    the old fire-and-forget daemon thread. The per-instance lock_key
        #    serialises concurrent pushes to the same instance.
        commit = self._extract_commit(payload)
        build_branch = ref.rsplit('/', 1)[-1] if ref else (repo.branch or '')
        _logger.info(
            "Webhook: triggering auto-deploy for %s on instance %s (ref=%s)",
            repo.name, instance.name, ref,
        )
        build = env['saas.build'].create({
            'instance_id': instance.id,
            'repo_id': repo.id,
            'branch': build_branch,
            'commit_sha': commit['sha'],
            'commit_message': commit['message'],
            'author': commit['author'],
            'source': 'push',
            'state': 'running',
        })
        env['saas.job']._enqueue(
            repo, '_run_webhook_deploy', args=(build.id,),
            channel='deploy', lock_key='instance:%s' % instance.id,
            idempotent=True,
            on_error='_on_webhook_deploy_error', on_error_args=(build.id,))

        return request.make_json_response({
            'status': 'ok',
            'message': 'Auto-deploy triggered for %s' % repo.name,
        })

    @staticmethod
    def _is_push_event(event_type, payload):
        """Detect if the webhook event is a push/commit event."""
        event_type = (event_type or '').lower()
        if event_type == 'push':
            return True
        if event_type in ('push_hook', 'tag_push_hook'):
            return True
        if event_type.startswith('repo:push'):
            return True
        # Fallback: GitHub/Gitea-shaped push payloads carry both `ref`
        # AND a `commits` list (PR events have `ref` but no `commits`).
        if 'ref' in payload and isinstance(payload.get('commits'), list):
            return True
        # Bitbucket push (no event header on some setups)
        if isinstance(payload.get('push'), dict) \
                and isinstance(payload['push'].get('changes'), list):
            return True
        return False

    @staticmethod
    def _extract_commit(payload):
        """Best-effort commit metadata across providers (GitHub/Gitea/GitLab/
        Bitbucket). Returns {sha, message, author}, all possibly empty."""
        sha = payload.get('after') or ''
        head = payload.get('head_commit') or {}
        msg = head.get('message') or ''
        author = (head.get('author') or {}).get('name') or ''
        if not sha:
            sha = head.get('id') or ''
        # GitLab carries pusher/user at top level.
        author = author or payload.get('user_name') or (
            payload.get('pusher') or {}).get('name') or ''
        # Fallback to the last entry of the commits list.
        commits = payload.get('commits')
        if (not sha or not msg) and isinstance(commits, list) and commits:
            last = commits[-1]
            sha = sha or last.get('id') or ''
            msg = msg or last.get('message') or ''
            author = author or (last.get('author') or {}).get('name') or ''
        # Bitbucket push shape.
        if not sha:
            try:
                new = payload['push']['changes'][0]['new']
                target = new.get('target', {})
                sha = target.get('hash', '')
                msg = msg or target.get('message', '')
                author = author or (target.get('author', {}) or {}).get('raw', '')
            except (KeyError, IndexError, TypeError):
                pass
        return {
            'sha': (sha or '')[:40],
            'message': (msg or '').strip()[:500],
            'author': (author or '')[:120],
        }

    @staticmethod
    def _extract_ref(payload):
        """Extract the branch ref from various provider payloads."""
        if 'ref' in payload:
            return payload['ref']
        try:
            changes = payload.get('push', {}).get('changes', [])
            if changes:
                new = changes[0].get('new', {})
                return 'refs/heads/%s' % new.get('name', '')
        except (KeyError, IndexError, TypeError):
            pass
        return ''
