import json
import logging
import re
import time

from werkzeug.exceptions import Forbidden, NotFound

from odoo import http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)

STREAM_TIMEOUT = 300  # 5 minutes max
# Cap the partial-line buffer: a line without a newline could otherwise
# grow without bound.
MAX_BUFFER_BYTES = 1 * 1024 * 1024
# Strip ANSI escape sequences before forwarding — a tenant can put any
# bytes into their own logs and we don't want them rendered into the
# log viewer.
_ANSI_RE = re.compile(rb'\x1b\[[0-9;?]*[a-zA-Z]')

# Managers may stream logs of instances that aren't running.
ADMIN_LOG_GROUP = 'saas_core.group_saas_manager'


def _sse(text):
    return ('data: %s\n\n' % json.dumps(text)).encode('utf-8')


def log_stream_response(resp, label):
    """Server-sent events relaying a pod log stream opened with
    ``KubernetesDriver.open_log_stream``: one ``data:`` event per line,
    ``event: done`` when the stream ends, a generic ``event: error`` (the
    real cause goes to the server log — it can name internal hosts) on
    failure. Stops after STREAM_TIMEOUT; the client reconnects."""

    def generate():
        deadline = time.monotonic() + STREAM_TIMEOUT
        try:
            yield b'retry: 1000\n\n'
            buf = b''
            for chunk in resp.stream(4096, decode_content=True):
                buf += chunk
                if len(buf) > MAX_BUFFER_BYTES:
                    buf = buf[-MAX_BUFFER_BYTES:]
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    yield _sse(_ANSI_RE.sub(b'', line).decode('utf-8', 'replace'))
                if time.monotonic() > deadline:
                    break
            if buf:
                yield _sse(_ANSI_RE.sub(b'', buf).decode('utf-8', 'replace'))
            yield b'event: done\ndata: stream ended\n\n'
        except Exception:
            _logger.exception("Log streaming error for %s", label)
            yield ('event: error\ndata: %s\n\n' % json.dumps(
                "We lost the connection to the log stream. "
                "Please refresh the page to reconnect.")).encode('utf-8')
        finally:
            resp.close()
            resp.release_conn()

    return Response(
        generate(),
        content_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
        direct_passthrough=True,
    )


class ContainerLogsController(http.Controller):

    @http.route(
        '/saas/instance/<int:instance_id>/logs/stream',
        type='http',
        auth='user',
    )
    def stream_instance_logs(self, instance_id, tail='100', **kwargs):
        # Any user with read access to the instance may stream its logs;
        # record rules on saas.instance scope visibility.
        instance = request.env['saas.instance'].browse(instance_id)
        if not instance.exists():
            raise NotFound()
        instance.check_access('read')
        # Only a running instance has a live pod to stream. A stopped or
        # suspended (or not-yet-deployed) instance must not expose logs to
        # the customer. (Authoritative gate — the SPA also hides the Logs
        # control.)
        if instance.state != 'running' and not request.env.user.has_group(ADMIN_LOG_GROUP):
            raise Forbidden("Logs are only available while the instance is running.")
        instance = instance.sudo()
        if not instance.docker_server_id:
            raise NotFound()
        try:
            tail_int = max(0, min(int(tail), 10000))
        except (ValueError, TypeError):
            tail_int = 100
        # Open the stream while the ORM cursor is still open (the driver
        # reads the region kubeconfig); the generator runs after the
        # request transaction ends.
        try:
            resp = instance._compute_driver().open_log_stream(
                instance._compute_handle(), tail=tail_int)
        except Exception:
            _logger.exception("Opening log stream failed for %s", instance.subdomain)
            resp = None
        if resp is None:
            raise NotFound()
        return log_stream_response(resp, instance.subdomain)
