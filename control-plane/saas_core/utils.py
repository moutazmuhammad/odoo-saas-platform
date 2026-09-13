import base64
import hashlib
import io
import logging
import os
import random
import socket
import threading
import time

import paramiko

_logger = logging.getLogger(__name__)

# ---- SSH connect resilience (ARCH-010) ----
# Transient network blips shouldn't fail a whole provisioning op, and a dead
# host shouldn't tie up workers retry-after-retry. We retry only the CONNECT
# (idempotent) with bounded backoff+jitter, and quarantine a host with a
# simple per-process circuit breaker after repeated connect failures.
_SSH_CONNECT_ATTEMPTS = 3
_SSH_CONNECT_BACKOFF = 1.0          # base seconds: 1s, 2s (+jitter)
_SSH_CB_THRESHOLD = 3               # consecutive connect failures to open
_SSH_CB_COOLDOWN = 60               # seconds a host stays quarantined
_ssh_circuit = {}                   # host -> {'fails': int, 'open_until': monotonic}
_ssh_circuit_lock = threading.Lock()


def _ssh_circuit_check(host):
    """Raise fast if *host* is currently quarantined (open circuit)."""
    with _ssh_circuit_lock:
        st = _ssh_circuit.get(host)
        if st and st['open_until'] > time.monotonic():
            raise paramiko.SSHException(
                "SSH circuit open for %s: host quarantined after repeated "
                "connect failures; will retry after cooldown." % host
            )


def _ssh_circuit_record(host, ok):
    """Record a connect outcome; open the circuit after enough failures."""
    with _ssh_circuit_lock:
        if ok:
            _ssh_circuit.pop(host, None)
            return
        st = _ssh_circuit.setdefault(host, {'fails': 0, 'open_until': 0.0})
        st['fails'] += 1
        if st['fails'] >= _SSH_CB_THRESHOLD:
            st['open_until'] = time.monotonic() + _SSH_CB_COOLDOWN
            _logger.warning(
                "SSH circuit OPENED for %s after %d consecutive connect "
                "failures; quarantined for %ds.",
                host, st['fails'], _SSH_CB_COOLDOWN,
            )


def _host_key_sha256(key):
    """Return the SSH host key fingerprint as `SHA256:<base64>` (no padding).

    Matches the format printed by `ssh-keygen -l -f` and shown by OpenSSH
    on first connect.
    """
    digest = hashlib.sha256(key.asbytes()).digest()
    b64 = base64.b64encode(digest).decode('ascii').rstrip('=')
    return 'SHA256:' + b64


class _PinnedHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """Refuse the connection unless the host key matches the pinned fingerprint.

    Replaces paramiko's default `AutoAddPolicy()` which silently trusts
    any host on first connect (every SSH call was MITM-able).
    """

    def __init__(self, expected_fingerprint):
        self.expected = expected_fingerprint.strip()
        if not self.expected.startswith('SHA256:'):
            self.expected = 'SHA256:' + self.expected.lstrip(':')

    def missing_host_key(self, client, hostname, key):
        actual = _host_key_sha256(key)
        if actual != self.expected:
            raise paramiko.SSHException(
                "Host key fingerprint mismatch for %s: expected %s, got %s"
                % (hostname, self.expected, actual)
            )


def run_in_background(record, method_name, method_args=(),
                      error_method=None, error_args=(),
                      thread_name=None,
                      heartbeat_field=None, heartbeat_interval=30):
    """Run record.method_name(*args) in a background thread with its own cursor.

    On success the cursor is committed.  On failure it is rolled back and,
    if *error_method* is given, ``record.error_method(exception, *error_args)``
    is called inside a fresh cursor that is then committed.

    Commits the current transaction first so the background thread sees the
    latest DB state, then starts the thread immediately.

    ``heartbeat_field`` (optional): the name of a Datetime field on *record*
    that a watchdog thread stamps with ``now`` every ``heartbeat_interval``
    seconds while the work runs (each in its own short, committed cursor).
    If the worker thread dies (unhandled crash, dropped DB connection, worker
    recycle) the stamps stop, so a reaper can fail the record in minutes
    instead of waiting for a coarse create-time timeout. Liveness for an
    otherwise-opaque long-running op (PROV-001).
    """
    from odoo import SUPERUSER_ID
    dbname = record.env.cr.dbname
    uid = SUPERUSER_ID
    context = dict(record.env.context)
    model_name = record._name
    record_id = record.id

    def _beat_loop(db_registry, stop):
        """Stamp ``heartbeat_field`` until *stop* is set or the work ends."""
        import odoo
        from odoo import api as odoo_api, fields as odoo_fields
        # stop.wait returns True once set; False on timeout -> beat again.
        while not stop.wait(heartbeat_interval):
            try:
                with db_registry.cursor() as hb_cr:
                    hb_env = odoo_api.Environment(hb_cr, uid, context)
                    hb_env[model_name].browse(record_id).write(
                        {heartbeat_field: odoo_fields.Datetime.now()})
                    hb_cr.commit()
            except Exception:
                # A missed beat is non-fatal; the reaper's create-time net
                # still backstops. Don't let the watchdog kill the worker.
                _logger.debug("Heartbeat write failed for %s#%s",
                              model_name, record_id, exc_info=True)

    def _target():
        import odoo
        from odoo import api as odoo_api
        try:
            db_registry = odoo.modules.registry.Registry(dbname)
            stop = threading.Event()
            beater = None
            if heartbeat_field:
                beater = threading.Thread(
                    target=_beat_loop, args=(db_registry, stop),
                    name='%s_hb' % (thread_name or method_name), daemon=True)
                beater.start()
            try:
                with db_registry.cursor() as new_cr:
                    new_env = odoo_api.Environment(new_cr, uid, context)
                    rec = new_env[model_name].browse(record_id)
                    try:
                        getattr(rec, method_name)(*method_args)
                        new_cr.commit()
                    except Exception as e:
                        new_cr.rollback()
                        if error_method:
                            try:
                                with db_registry.cursor() as err_cr:
                                    err_env = odoo_api.Environment(err_cr, uid, context)
                                    err_rec = err_env[model_name].browse(record_id)
                                    getattr(err_rec, error_method)(e, *error_args)
                                    err_cr.commit()
                            except Exception:
                                _logger.exception(
                                    "Error handler failed for %s#%s",
                                    model_name, record_id,
                                )
                        _logger.exception(
                            "Background %s failed for %s#%s",
                            method_name, model_name, record_id,
                        )
            finally:
                stop.set()
                if beater:
                    beater.join(timeout=5)
        except Exception:
            _logger.exception(
                "Background thread crashed before executing %s for %s#%s",
                method_name, model_name, record_id,
            )

    name = thread_name or 'saas_bg_%s_%s' % (method_name, record_id)

    # Commit current transaction so the thread sees the latest state,
    # then start the thread immediately (no postcommit dependency).
    record.env.cr.commit()
    _logger.info(
        "Starting background thread '%s' for %s#%s",
        name, model_name, record_id,
    )
    t = threading.Thread(target=_target, name=name, daemon=True)
    t.start()

SSH_COMMAND_TIMEOUT = 120  # seconds
SSH_CONNECT_TIMEOUT = 30  # seconds


class SSHConnection:
    """Context manager for SSH connections using paramiko.

    Usage::

        with SSHConnection(host, port, user, private_key_b64, key_type) as ssh:
            exit_code, stdout, stderr = ssh.execute('ls -la')
            ssh.write_file('/remote/path/file.txt', 'file contents')
    """

    def __init__(self, host, port, user, private_key_b64, key_type='rsa',
                 timeout=SSH_COMMAND_TIMEOUT, expected_host_key=None):
        self.host = host
        self.port = port
        self.user = user
        self.private_key_b64 = private_key_b64
        self.key_type = key_type
        self.timeout = timeout
        self.expected_host_key = expected_host_key
        self._client = None

    def __enter__(self):
        self._connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._disconnect()
        return False

    def _connect(self):
        """Connect via paramiko, loading the key in-memory (no tempfile).

        Host-key policy:
        - If `expected_host_key` is set, only that fingerprint is accepted
          (MITM protection).
        - Otherwise the connection is allowed but a WARNING is logged so
          operators can spot infrastructure that hasn't been pinned yet.
          (Replaces paramiko's silent AutoAddPolicy.)
        """
        key_bytes = base64.b64decode(self.private_key_b64)
        pkey = self._load_private_key_bytes(key_bytes)

        self._client = paramiko.SSHClient()
        if self.expected_host_key:
            self._client.set_missing_host_key_policy(
                _PinnedHostKeyPolicy(self.expected_host_key)
            )
        else:
            _logger.warning(
                "SSH host key not pinned for %s (set expected_host_key_fingerprint "
                "on the saas.server record to enable MITM protection).",
                self.host,
            )
            self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._connect_with_retry(pkey)

    def _connect_with_retry(self, pkey):
        """Open the TCP/SSH connection, retrying transient failures with
        bounded backoff and honouring the per-host circuit breaker.

        Only the connect is retried — it is idempotent. Auth and host-key
        failures are permanent and raise immediately without tripping the
        breaker (the host is up; the credentials/pin are wrong).
        """
        _ssh_circuit_check(self.host)
        attempt = 0
        while True:
            attempt += 1
            try:
                self._client.connect(
                    hostname=self.host,
                    port=self.port,
                    username=self.user,
                    pkey=pkey,
                    timeout=SSH_CONNECT_TIMEOUT,
                    look_for_keys=False,
                    allow_agent=False,
                )
                _ssh_circuit_record(self.host, ok=True)
                return
            except paramiko.AuthenticationException:
                # Permanent: bad credentials, not a flaky host.
                raise
            except (socket.timeout, OSError,
                    paramiko.ssh_exception.NoValidConnectionsError) as exc:
                if attempt >= _SSH_CONNECT_ATTEMPTS:
                    _ssh_circuit_record(self.host, ok=False)
                    raise
                delay = (_SSH_CONNECT_BACKOFF * (2 ** (attempt - 1))
                         + random.uniform(0, _SSH_CONNECT_BACKOFF))
                _logger.warning(
                    "SSH connect to %s failed (attempt %d/%d): %s — retrying "
                    "in %.1fs.", self.host, attempt, _SSH_CONNECT_ATTEMPTS,
                    exc, delay,
                )
                time.sleep(delay)

    def _load_private_key_bytes(self, key_bytes):
        """Parse a private key from raw bytes (never touches the filesystem)."""
        key_classes = [
            ('rsa', paramiko.RSAKey),
            ('ed25519', paramiko.Ed25519Key),
            ('ecdsa', paramiko.ECDSAKey),
        ]
        if hasattr(paramiko, 'DSSKey'):
            key_classes.append(('dsa', paramiko.DSSKey))

        ordered = sorted(key_classes, key=lambda kv: kv[0] != self.key_type)

        errors = []
        for name, cls in ordered:
            try:
                return cls.from_private_key(io.StringIO(key_bytes.decode('utf-8')))
            except Exception as exc:
                errors.append((name, exc))

        error_details = '; '.join('%s: %s' % (n, e) for n, e in errors)
        raise paramiko.SSHException(
            "Unable to load private key (tried %s). Details: %s"
            % (', '.join(n for n, _ in errors), error_details)
        )

    def _disconnect(self):
        """Close SSH client."""
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def execute(self, command, timeout=None):
        """Execute a command over SSH.

        Returns:
            tuple: (exit_code, stdout_str, stderr_str)
        """
        _logger.debug("SSH [%s@%s:%s] executing command", self.user, self.host, self.port)
        stdin, stdout, stderr = self._client.exec_command(
            command, timeout=timeout or self.timeout,
        )
        # Read output BEFORE recv_exit_status to avoid deadlock when the
        # remote command produces large output that fills the SSH buffer.
        stdout_str = stdout.read().decode('utf-8', errors='replace')
        stderr_str = stderr.read().decode('utf-8', errors='replace')
        exit_code = stdout.channel.recv_exit_status()
        return exit_code, stdout_str, stderr_str

    def exec_command_streaming(self, command, timeout=None):
        """Start ``command`` and return paramiko ``(stdout, stderr)`` file
        objects for STREAMING reads.

        Used to pipe a large backup (``pg_dump``) straight to object
        storage without ever buffering it in memory or on disk: the
        caller reads ``stdout`` (a file-like) to EOF — typically by
        handing it to an S3/GCS streaming-upload call — then reads the
        exit code via ``stdout.channel.recv_exit_status()`` and
        ``stderr`` for diagnostics.

        ``timeout`` is the per-read socket timeout (so a wedged remote
        can't block forever); it must be comfortably larger than the
        longest gap between output chunks, NOT the total backup time.
        """
        stdin, stdout, stderr = self._client.exec_command(
            command, timeout=timeout or self.timeout,
        )
        stdin.close()
        return stdout, stderr

    def write_file(self, remote_path, content):
        """Write string content to a remote file via SFTP."""
        sftp = self._client.open_sftp()
        try:
            with sftp.file(remote_path, 'w') as f:
                f.write(content)
        finally:
            sftp.close()

    def read_file_bytes(self, remote_path):
        """Read a remote file and return its contents as bytes via SFTP."""
        sftp = self._client.open_sftp()
        try:
            with sftp.file(remote_path, 'rb') as f:
                return f.read()
        finally:
            sftp.close()
