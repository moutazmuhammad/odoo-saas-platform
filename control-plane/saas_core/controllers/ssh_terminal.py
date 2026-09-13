"""SSH terminal controller — multi-worker safe.

Sessions are owned by exactly one Odoo worker (the one that opened the
paramiko channel). All other workers reach the channel via PostgreSQL
``LISTEN``/``NOTIFY``, so input POSTs and SSE GETs can be served by any
worker the load balancer happens to pick.

Channels per session (UUID stripped of dashes -> short identifier):
  * ``saas_term_in_<sid>``    — input bytes, any worker -> owner pump
  * ``saas_term_out_<sid>``   — output bytes, owner pump -> SSE worker
  * ``saas_term_close_<sid>`` — close event, fan-out to pump + SSE

Each NOTIFY payload is base64 (PG caps payloads at 8000 bytes, so we
chunk raw output at ``NOTIFY_CHUNK_BYTES`` before encoding).
"""
import base64
import json
import logging
import os
import re
import select
import shlex
import threading
import time
import uuid

import psycopg2
import psycopg2.extensions
from werkzeug.exceptions import Forbidden, NotFound

import odoo
from odoo import api, fields, http, SUPERUSER_ID
from odoo.http import request, Response

_logger = logging.getLogger(__name__)

# Pump threads owned by THIS process. Keyed by session id. Other
# workers must reach these via NOTIFY — they cannot look in this dict.
_local_sessions = {}
_local_sessions_lock = threading.Lock()
_worker_pid = os.getpid()

SESSION_IDLE_TIMEOUT = 600           # cleanup cutoff
SSE_STREAM_TIMEOUT = 300             # 5 min per SSE; browser auto-reconnects
SSH_KEEPALIVE_INTERVAL = 15
SSE_HEARTBEAT_INTERVAL = 10
MAX_CONCURRENT_SESSIONS = 32         # per worker
# base64(N) ~= 4N/3, NOTIFY payload caps at 8000 bytes — stay well under.
NOTIFY_CHUNK_BYTES = 4096
# How long /create blocks waiting for the SSH banner before returning it
# inline. Beyond this, banner is read by the pump (and may be lost if
# SSE hasn't subscribed yet — acceptable for a tiny initial slice).
INITIAL_BANNER_WAIT = 0.5

TERMINAL_GROUP = 'saas_core.group_saas_manager'
_SID_RE = re.compile(
    r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$'
)


# ─── Channel naming ──────────────────────────────────────────────────
# PG identifiers are plain (a-zA-Z0-9_); strip the dashes from the uuid
# so we don't have to double-quote the channel name everywhere.

def _sid_token(sid):
    return sid.replace('-', '')


def _in_ch(sid):
    return 'saas_term_in_' + _sid_token(sid)


def _out_ch(sid):
    return 'saas_term_out_' + _sid_token(sid)


def _close_ch(sid):
    return 'saas_term_close_' + _sid_token(sid)


def _validate_sid(sid):
    if not isinstance(sid, str) or not _SID_RE.match(sid):
        raise NotFound("Invalid session id")


# ─── Dedicated psycopg2 connections ──────────────────────────────────
# Odoo's connection pool isn't right for LISTEN: we need a connection
# that holds the subscription for the whole pump/stream lifetime and
# can be ``select()``-ed for new notifications. So we open our own.

def _open_listener_conn(dbname):
    cfg = odoo.tools.config
    args = {'dbname': dbname}
    for src, dst in (('db_host', 'host'), ('db_port', 'port'),
                     ('db_user', 'user'), ('db_password', 'password')):
        val = cfg.get(src)
        if val:
            args[dst] = val
    conn = psycopg2.connect(**args)
    conn.set_isolation_level(
        psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT,
    )
    return conn


def _notify(conn, channel, payload):
    """NOTIFY on an autocommit connection. ``channel`` must be a plain
    identifier (no quoting). ``payload`` must be a str — base64 the
    bytes upstream."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_notify(%s, %s)",
            (channel, payload),
        )


# ─── Owner-side pump ─────────────────────────────────────────────────
# One thread per owned session. Bridges the paramiko Channel and PG.

class _SessionPump(threading.Thread):

    def __init__(self, sid, dbname, channel, ssh_conn, server_name):
        super().__init__(
            name='saas-term-pump-%s' % sid[:8],
            daemon=True,
        )
        self.sid = sid
        self.dbname = dbname
        self.channel = channel
        self.ssh_conn = ssh_conn
        self.server_name = server_name
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        conn = None
        try:
            conn = _open_listener_conn(self.dbname)
            with conn.cursor() as cur:
                cur.execute('LISTEN "%s"' % _in_ch(self.sid))
                cur.execute('LISTEN "%s"' % _close_ch(self.sid))

            reason = self._loop(conn)
        except Exception:
            _logger.exception(
                "Terminal pump %s crashed", self.sid,
            )
            reason = 'pump error'
        finally:
            self._teardown(conn, reason or 'session ended')

    def _loop(self, conn):
        channel = self.channel
        while not self._stop.is_set():
            if channel.closed:
                return 'session ended'
            transport = channel.get_transport()
            if not transport or not transport.is_active():
                return 'connection lost'

            try:
                ready, _, _ = select.select(
                    [channel, conn], [], [], 0.5,
                )
            except (OSError, ValueError):
                return 'select failed'

            # SSH -> PG (output)
            if channel in ready:
                try:
                    while channel.recv_ready():
                        data = channel.recv(NOTIFY_CHUNK_BYTES)
                        if not data:
                            return 'session ended'
                        _notify(
                            conn,
                            _out_ch(self.sid),
                            base64.b64encode(data).decode('ascii'),
                        )
                except Exception:
                    return 'connection lost'

            # PG -> SSH (input) or close signal
            if conn in ready:
                try:
                    conn.poll()
                except Exception:
                    return 'pg poll failed'
                while conn.notifies:
                    n = conn.notifies.pop(0)
                    if n.channel == _close_ch(self.sid):
                        return n.payload or 'session closed'
                    if n.channel == _in_ch(self.sid):
                        try:
                            raw = base64.b64decode(
                                n.payload.encode('ascii'),
                            )
                            channel.sendall(raw)
                        except Exception:
                            return 'write failed'
        return 'pump stopped'

    def _teardown(self, conn, reason):
        # Tell any SSE subscribers we're done.
        if conn is not None:
            try:
                _notify(conn, _close_ch(self.sid), reason)
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

        # Close the SSH side.
        try:
            self.channel.close()
        except Exception:
            pass
        try:
            self.ssh_conn._disconnect()
        except Exception:
            pass

        with _local_sessions_lock:
            _local_sessions.pop(self.sid, None)

        # Mark closed in DB so cron / future create calls see it gone.
        # Use the same registry-cursor pattern as utils.run_in_thread.
        try:
            reg = odoo.modules.registry.Registry(self.dbname)
            with reg.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                rec = env['saas.terminal.session'].search(
                    [('sid', '=', self.sid)], limit=1,
                )
                if rec:
                    rec.write({'closed': True})
                cr.commit()
        except Exception:
            _logger.exception(
                "Terminal pump %s: failed to mark closed", self.sid,
            )

        _logger.info(
            "Terminal session %s pump exited: %s (pid %s)",
            self.sid, reason, os.getpid(),
        )


# ─── Stale session reaper ────────────────────────────────────────────

def _cleanup_stale_sessions(env):
    """Mark sessions idle past the timeout as closed.

    Cannot kill a remote worker's pump from here, but the close
    NOTIFY below wakes the pump on the owner worker, which marks
    the row closed and tears down the SSH channel.
    """
    # Use SQL for the time comparison to avoid TZ headaches.
    env.cr.execute("""
        SELECT id, sid FROM saas_terminal_session
        WHERE closed = FALSE
          AND last_activity < (NOW() AT TIME ZONE 'UTC')
                              - interval '%s seconds'
    """ % int(SESSION_IDLE_TIMEOUT))
    rows = env.cr.fetchall()
    for _id, sid in rows:
        try:
            _validate_sid(sid)
            env.cr.execute(
                "SELECT pg_notify(%s, %s)",
                (_close_ch(sid), 'idle timeout'),
            )
        except Exception:
            _logger.exception(
                "Stale-cleanup notify failed for %s", sid,
            )
    if rows:
        env.cr.execute("""
            UPDATE saas_terminal_session
               SET closed = TRUE
             WHERE id = ANY(%s)
        """, ([r[0] for r in rows],))


class SshTerminalController(http.Controller):

    @http.route(
        '/saas/terminal/create',
        type='json',
        auth='user',
        methods=['POST'],
    )
    def create_session(self, server_model, server_id, **kwargs):
        """Open an interactive SSH shell on the requested server.

        Authorization: requires ``saas_core.group_saas_manager``.
        Hiding the UI button is not enough — every endpoint enforces
        this server-side via ``has_group()``.
        """
        if not request.env.user.has_group(TERMINAL_GROUP):
            _logger.warning(
                "Terminal access denied for uid=%s",
                request.env.uid,
            )
            raise Forbidden(
                "SaaS Manager privileges required to open a terminal."
            )

        if server_model not in ('saas.server',):
            raise Forbidden("Invalid server model")

        server = request.env[server_model].browse(int(server_id))
        if not server.exists():
            raise NotFound("Server not found")
        server.check_access('read')

        _cleanup_stale_sessions(request.env)

        with _local_sessions_lock:
            if len(_local_sessions) >= MAX_CONCURRENT_SESSIONS:
                _logger.warning(
                    "Terminal session cap reached (%d) on pid %s",
                    MAX_CONCURRENT_SESSIONS, os.getpid(),
                )
                raise Forbidden(
                    "Maximum concurrent terminal sessions reached."
                )

        ssh_conn = server._get_ssh_connection()
        ssh_conn._connect()

        try:
            transport = ssh_conn._client.get_transport()
            transport.set_keepalive(SSH_KEEPALIVE_INTERVAL)
            channel = transport.open_session()
            channel.get_pty(
                term='xterm-256color', width=120, height=40,
            )
            channel.invoke_shell()
            channel.settimeout(0)
        except Exception as e:
            _logger.error(
                "Failed to open SSH shell to %s: %s",
                server.name, e,
            )
            ssh_conn._disconnect()
            raise

        sid = str(uuid.uuid4())

        # Briefly capture the banner inline so the browser has something
        # to show before SSE subscribes. Anything left in the channel
        # buffer after this is read by the pump below.
        banner = self._read_banner(channel)

        # Persist routing metadata BEFORE spawning the pump so any
        # concurrent input/output call can find the owner.
        request.env['saas.terminal.session'].sudo().create({
            'sid': sid,
            'uid': request.env.uid,
            'server_model': server_model,
            'server_id': server.id,
            'server_name': server.name,
            'owner_pid': os.getpid(),
            'last_activity': fields.Datetime.now(),
        })

        pump = _SessionPump(
            sid=sid,
            dbname=request.env.cr.dbname,
            channel=channel,
            ssh_conn=ssh_conn,
            server_name=server.name,
        )
        with _local_sessions_lock:
            _local_sessions[sid] = pump
        pump.start()

        _logger.info(
            "Terminal session %s created for %s by uid %s (pid %s)",
            sid, server.name, request.env.uid, os.getpid(),
        )

        return {
            'session_id': sid,
            'initial_output': base64.b64encode(banner).decode('ascii'),
        }

    @staticmethod
    def _read_banner(channel):
        banner = b''
        deadline = time.time() + INITIAL_BANNER_WAIT
        while time.time() < deadline and len(banner) < 8192:
            ready, _, _ = select.select([channel], [], [], 0.1)
            if not ready:
                if banner:
                    break
                continue
            try:
                chunk = channel.recv(4096)
            except Exception:
                break
            if not chunk:
                break
            banner += chunk
        return banner

    @http.route(
        '/saas/terminal/input',
        type='json',
        auth='user',
        methods=['POST'],
    )
    def send_input(self, session_id, data, **kwargs):
        _validate_sid(session_id)
        sess = self._get_session(session_id)
        if sess.closed:
            return {'status': 'closed'}

        encoded = base64.b64encode(
            data.encode('utf-8') if isinstance(data, str)
            else bytes(data),
        ).decode('ascii')
        request.env.cr.execute(
            "SELECT pg_notify(%s, %s)",
            (_in_ch(session_id), encoded),
        )
        sess.last_activity = fields.Datetime.now()
        return {'status': 'ok'}

    @http.route(
        '/saas/terminal/resize',
        type='json',
        auth='user',
        methods=['POST'],
    )
    def resize(self, session_id, cols, rows, **kwargs):
        """Resize the PTY.

        We can only resize the channel directly — and the channel
        only exists in the owner worker. If we're not the owner,
        fall back to encoding the resize as a control sequence.
        Since xterm-256color doesn't have a clean inline resize
        opcode, we just no-op in that case; the next reconnect
        will pick up the new size on /create.
        """
        _validate_sid(session_id)
        sess = self._get_session(session_id)
        if sess.closed:
            return {'status': 'closed'}

        with _local_sessions_lock:
            pump = _local_sessions.get(session_id)
        if pump is not None:
            try:
                if not pump.channel.closed:
                    pump.channel.resize_pty(
                        width=int(cols), height=int(rows),
                    )
                    sess.last_activity = fields.Datetime.now()
            except Exception:
                # Resize is best-effort — don't break the session
                pass
        return {'status': 'ok'}

    @http.route(
        '/saas/terminal/output/<string:session_id>',
        type='http',
        auth='user',
        methods=['GET'],
        csrf=False,
    )
    def stream_output(self, session_id, **kwargs):
        _validate_sid(session_id)
        sess = self._get_session(session_id)
        if sess.closed:
            return Response(
                "event: closed\ndata: session already closed\n\n",
                content_type='text/event-stream',
            )
        return self._terminal_sse_response(session_id)

    def _terminal_sse_response(self, session_id):
        """Build the live SSE Response for an already-authorized session.

        Shared by the manager terminal (``stream_output``) and the
        customer instance shell (``instance_output``) — only the ACL
        differs; the PG ``LISTEN`` relay is identical.
        """
        dbname = request.env.cr.dbname
        sid = session_id

        def generate():
            conn = None
            cur = None
            try:
                conn = _open_listener_conn(dbname)
                cur = conn.cursor()
                cur.execute('LISTEN "%s"' % _out_ch(sid))
                cur.execute('LISTEN "%s"' % _close_ch(sid))

                yield b'retry: 3000\n\n'

                start = time.time()
                last_heartbeat = time.time()

                while (time.time() - start) < SSE_STREAM_TIMEOUT:
                    try:
                        ready, _, _ = select.select(
                            [conn], [], [], 0.5,
                        )
                    except (OSError, ValueError):
                        break

                    if ready:
                        try:
                            conn.poll()
                        except Exception:
                            yield (
                                b'event: error\n'
                                b'data: "pg poll failed"\n\n'
                            )
                            break
                        while conn.notifies:
                            n = conn.notifies.pop(0)
                            if n.channel == _close_ch(sid):
                                reason = n.payload or 'session ended'
                                yield (
                                    'event: closed\ndata: %s\n\n'
                                    % reason
                                ).encode('utf-8')
                                return
                            if n.channel == _out_ch(sid):
                                yield (
                                    'data: %s\n\n'
                                    % json.dumps(n.payload)
                                ).encode('utf-8')
                                last_heartbeat = time.time()

                    now = time.time()
                    if now - last_heartbeat >= SSE_HEARTBEAT_INTERVAL:
                        yield b': heartbeat\n\n'
                        last_heartbeat = now

                yield b'event: timeout\ndata: stream timeout\n\n'
            except GeneratorExit:
                _logger.info(
                    "Client closed SSE for session %s", sid,
                )
            except Exception as e:
                _logger.exception(
                    "Terminal stream error for %s", sid,
                )
                try:
                    yield (
                        'event: error\ndata: %s\n\n'
                        % json.dumps(str(e))
                    ).encode('utf-8')
                except Exception:
                    pass
            finally:
                if cur is not None:
                    try:
                        cur.close()
                    except Exception:
                        pass
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

        return Response(
            generate(),
            content_type='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
                'Connection': 'keep-alive',
            },
            direct_passthrough=True,
        )

    @http.route(
        '/saas/terminal/close',
        type='json',
        auth='user',
        methods=['POST'],
    )
    def close_session(self, session_id, **kwargs):
        _validate_sid(session_id)
        sess = self._get_session(session_id)
        request.env.cr.execute(
            "SELECT pg_notify(%s, %s)",
            (_close_ch(session_id), 'user requested'),
        )
        sess.closed = True
        _logger.info(
            "Terminal session %s closed by uid %s",
            session_id, request.env.uid,
        )
        return {'status': 'closed'}

    def _get_session(self, session_id):
        """Return the routing record for ``session_id`` after ACL.

        Validates: caller is a SaaS manager AND owns the session.
        Raises ``NotFound``/``Forbidden`` otherwise.
        """
        if not request.env.user.has_group(TERMINAL_GROUP):
            raise Forbidden(
                "SaaS Manager privileges required for terminal access."
            )
        sess = request.env['saas.terminal.session'].sudo().search(
            [('sid', '=', session_id)], limit=1,
        )
        if not sess:
            _logger.warning(
                "Terminal session %s not found", session_id,
            )
            raise NotFound("Terminal session not found or expired.")
        if sess.uid != request.env.uid:
            raise Forbidden("Access denied to this terminal session")
        return sess

    # ==================================================================
    #  Customer instance shell (Odoo.sh-style) — opens a shell *inside*
    #  the customer's own container via ``docker exec``. Authorized by
    #  instance ownership (record rules / access token) + session
    #  ownership, NOT the manager group. The host shell is never
    #  exposed — we exec straight into the container.
    # ==================================================================
    def _get_owned_session(self, session_id):
        """Routing record for a session the *current user* owns.

        Unlike :meth:`_get_session` this does not require the manager
        group — any authenticated customer may drive a session they
        themselves opened (enforced by the ``uid`` match).
        """
        sess = request.env['saas.terminal.session'].sudo().search(
            [('sid', '=', session_id)], limit=1,
        )
        if not sess:
            raise NotFound("Terminal session not found or expired.")
        if sess.uid != request.env.uid:
            raise Forbidden("Access denied to this terminal session")
        return sess

    def _authorize_instance_shell(self, instance_id, access_token=None):
        """Return the instance (sudo) if the caller may shell into it.

        Mirrors the SPA's ``_instance`` access semantics (owner via
        record rules, or holder of the access token), then enforces the
        shell-specific preconditions: hosting-only and running.
        """
        Instance = request.env['saas.instance']
        inst = Instance.browse(int(instance_id))
        if not inst.exists():
            raise NotFound("Instance not found.")
        if not (access_token and inst.sudo().access_token == access_token):
            inst.check_access_rights('read')
            inst.check_access_rule('read')
        inst = inst.sudo()
        if not inst.is_hosting:
            raise Forbidden("Shell is only available for hosting instances.")
        if inst.state != 'running':
            raise Forbidden("The instance must be running to open a shell.")
        if not inst.docker_server_id:
            raise Forbidden("This instance isn't fully set up yet.")
        return inst

    @http.route(
        '/saas/terminal/instance/create',
        type='json',
        auth='user',
        methods=['POST'],
    )
    def create_instance_session(self, instance_id, access_token=None,
                                cols=120, rows=32, **kwargs):
        inst = self._authorize_instance_shell(instance_id, access_token)

        _cleanup_stale_sessions(request.env)
        with _local_sessions_lock:
            if len(_local_sessions) >= MAX_CONCURRENT_SESSIONS:
                raise Forbidden(
                    "Maximum concurrent terminal sessions reached."
                )

        container = inst._get_container_name()
        ssh_conn = inst.docker_server_id._get_ssh_connection()
        ssh_conn._connect()
        try:
            transport = ssh_conn._client.get_transport()
            transport.set_keepalive(SSH_KEEPALIVE_INTERVAL)
            channel = transport.open_session()
            try:
                cols_i = max(20, min(int(cols or 120), 500))
                rows_i = max(5, min(int(rows or 32), 200))
            except (TypeError, ValueError):
                cols_i, rows_i = 120, 32
            channel.get_pty(
                term='xterm-256color', width=cols_i, height=rows_i,
            )
            # Exec straight into the customer's container — the docker
            # host shell is never exposed. ``-it`` works because we
            # allocated a PTY above; the image's default user (``odoo``)
            # is honoured by ``docker exec``.
            docker_cmd = (
                'docker exec -it %s /bin/bash -l' % shlex.quote(container)
            )
            channel.exec_command(docker_cmd)
            channel.settimeout(0)
        except Exception as e:
            _logger.error(
                "Failed to open instance shell to %s: %s", container, e,
            )
            ssh_conn._disconnect()
            raise

        sid = str(uuid.uuid4())
        banner = self._read_banner(channel)

        request.env['saas.terminal.session'].sudo().create({
            'sid': sid,
            'uid': request.env.uid,
            'server_model': 'saas.instance',
            'server_id': inst.id,
            'server_name': container,
            'owner_pid': os.getpid(),
            'last_activity': fields.Datetime.now(),
        })

        pump = _SessionPump(
            sid=sid,
            dbname=request.env.cr.dbname,
            channel=channel,
            ssh_conn=ssh_conn,
            server_name=container,
        )
        with _local_sessions_lock:
            _local_sessions[sid] = pump
        pump.start()

        _logger.info(
            "Instance shell %s opened for instance %s by uid %s (pid %s)",
            sid, inst.id, request.env.uid, os.getpid(),
        )
        return {
            'session_id': sid,
            'initial_output': base64.b64encode(banner).decode('ascii'),
        }

    @http.route(
        '/saas/terminal/instance/input',
        type='json',
        auth='user',
        methods=['POST'],
    )
    def instance_input(self, session_id, data, **kwargs):
        _validate_sid(session_id)
        sess = self._get_owned_session(session_id)
        if sess.closed:
            return {'status': 'closed'}
        encoded = base64.b64encode(
            data.encode('utf-8') if isinstance(data, str) else bytes(data),
        ).decode('ascii')
        request.env.cr.execute(
            "SELECT pg_notify(%s, %s)", (_in_ch(session_id), encoded),
        )
        sess.last_activity = fields.Datetime.now()
        return {'status': 'ok'}

    @http.route(
        '/saas/terminal/instance/resize',
        type='json',
        auth='user',
        methods=['POST'],
    )
    def instance_resize(self, session_id, cols, rows, **kwargs):
        _validate_sid(session_id)
        sess = self._get_owned_session(session_id)
        if sess.closed:
            return {'status': 'closed'}
        with _local_sessions_lock:
            pump = _local_sessions.get(session_id)
        if pump is not None:
            try:
                if not pump.channel.closed:
                    pump.channel.resize_pty(
                        width=int(cols), height=int(rows),
                    )
                    sess.last_activity = fields.Datetime.now()
            except Exception:
                pass
        return {'status': 'ok'}

    @http.route(
        '/saas/terminal/instance/close',
        type='json',
        auth='user',
        methods=['POST'],
    )
    def instance_close(self, session_id, **kwargs):
        _validate_sid(session_id)
        sess = self._get_owned_session(session_id)
        request.env.cr.execute(
            "SELECT pg_notify(%s, %s)",
            (_close_ch(session_id), 'user requested'),
        )
        sess.closed = True
        _logger.info(
            "Instance shell %s closed by uid %s", session_id, request.env.uid,
        )
        return {'status': 'closed'}

    @http.route(
        '/saas/terminal/instance/output/<string:session_id>',
        type='http',
        auth='user',
        methods=['GET'],
        csrf=False,
    )
    def instance_output(self, session_id, **kwargs):
        _validate_sid(session_id)
        sess = self._get_owned_session(session_id)
        if sess.closed:
            return Response(
                "event: closed\ndata: session already closed\n\n",
                content_type='text/event-stream',
            )
        return self._terminal_sse_response(session_id)
