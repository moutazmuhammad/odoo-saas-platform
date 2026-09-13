import json
import logging
import re
import shlex
import select

from werkzeug.exceptions import Forbidden, NotFound

from odoo import http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)

STREAM_TIMEOUT = 300  # 5 minutes max
# Cap the in-process line buffer per stream. A noisy container could
# otherwise grow this without bound if the client reads slowly.
MAX_BUFFER_BYTES = 1 * 1024 * 1024
# Strip ANSI escape sequences before forwarding — a tenant can put any
# bytes into their own logs and we don't want them rendered into the
# manager's HTML log viewer.
_ANSI_RE = re.compile(rb'\x1b\[[0-9;?]*[a-zA-Z]')

# Group required for the cross-container log endpoint (which can target
# any container on any server, including other tenants').
ADMIN_LOG_GROUP = 'saas_core.group_saas_manager'


class ContainerLogsController(http.Controller):

    @http.route(
        '/saas/instance/<int:instance_id>/logs/stream',
        type='http',
        auth='user',
    )
    def stream_instance_logs(self, instance_id, tail='100', **kwargs):
        # Per-instance route: any user with read access to the instance
        # may stream logs. Record rules on saas.instance scope visibility.
        instance = request.env['saas.instance'].browse(instance_id)
        if not instance.exists():
            raise NotFound()
        instance.check_access('read')
        # Only a running instance has a live container to stream. A stopped
        # or suspended (or not-yet-deployed) instance must not expose logs
        # to the customer. Managers keep cross-tenant access via the other
        # route. (Authoritative gate — the SPA also hides the Logs control.)
        if instance.state != 'running' and not request.env.user.has_group(ADMIN_LOG_GROUP):
            raise Forbidden("Logs are only available while the instance is running.")
        return self._stream(
            instance.docker_server_id, instance._get_container_name(), tail,
        )

    @http.route(
        '/saas/container/<int:container_id>/logs/stream',
        type='http',
        auth='user',
    )
    def stream_logs(self, container_id, tail='100', **kwargs):
        # Cross-tenant route: a single container record can reference
        # any container on any server. Restrict to managers — read access
        # alone on saas.docker.container does not imply ownership.
        if not request.env.user.has_group(ADMIN_LOG_GROUP):
            raise Forbidden(
                "SaaS Manager privileges required to stream container logs."
            )
        container = request.env['saas.docker.container'].browse(container_id)
        if not container.exists():
            raise NotFound()
        container.check_access('read')
        return self._stream(container.server_id, container.name, tail)

    def _stream(self, server, container_name, tail):
        safe_name = shlex.quote(container_name)

        # Validate tail is a safe integer
        try:
            tail_int = max(0, min(int(tail), 10000))
        except (ValueError, TypeError):
            tail_int = 100

        # Obtain SSH connection while the ORM cursor is still open.
        # The generator runs after the request transaction ends,
        # so ORM access inside generate() would hit a closed cursor.
        ssh_conn = server._get_ssh_connection()

        def generate():
            try:
                ssh_conn._connect()
                transport = ssh_conn._client.get_transport()
                channel = transport.open_session()
                channel.exec_command(
                    'docker logs -f --tail %d %s 2>&1' % (tail_int, safe_name)
                )
                channel.settimeout(STREAM_TIMEOUT)

                yield b'retry: 1000\n\n'

                buf = b''
                while not channel.exit_status_ready():
                    ready, _, _ = select.select([channel], [], [], 1.0)
                    if ready:
                        chunk = channel.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                        # Bound buffer growth — drop oldest data if a
                        # noisy container outpaces the client.
                        if len(buf) > MAX_BUFFER_BYTES:
                            buf = buf[-MAX_BUFFER_BYTES:]
                        while b'\n' in buf:
                            line, buf = buf.split(b'\n', 1)
                            line = _ANSI_RE.sub(b'', line)
                            text = line.decode('utf-8', errors='replace')
                            yield ('data: %s\n\n' % json.dumps(text)).encode('utf-8')

                while channel.recv_ready():
                    chunk = channel.recv(4096)
                    buf += chunk
                if buf:
                    text = _ANSI_RE.sub(b'', buf).decode('utf-8', errors='replace')
                    yield ('data: %s\n\n' % json.dumps(text)).encode('utf-8')

                yield b'event: done\ndata: stream ended\n\n'

            except Exception as e:
                _logger.exception(
                    "Log streaming error for container %s", container_name,
                )
                yield ('event: error\ndata: %s\n\n' % json.dumps(str(e))).encode('utf-8')
            finally:
                ssh_conn._disconnect()

        return Response(
            generate(),
            content_type='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
            },
            direct_passthrough=True,
        )
