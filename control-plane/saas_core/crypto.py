"""Application-level encryption for secrets stored in the control-plane DB
(SEC-002).

The encryption key lives OUTSIDE the database — in the Odoo config file
(`[options] saas_secret_key = <fernet key>`) or the ``SAAS_SECRET_KEY``
environment variable — so a database-only compromise (leaked dump, SQL
injection, read replica, restored backup) no longer exposes tenant DB/admin
passwords or Git tokens. It is NOT a defence against full host compromise
(an attacker with both the DB and the key file); that is the accepted
trade-off of app-level encryption without a managed KMS.

Design goals that keep this safe to roll out on a live fleet:

* **Opt-in.** With no key configured, every function is a passthrough, so
  deploying the code changes nothing until a key is set.
* **Legacy-safe.** Values without the ``enc:v1:`` prefix are treated as
  plaintext and returned as-is, so existing rows keep working; they become
  encrypted the next time they are written (or via the re-encrypt sweep).
* **Idempotent.** Encrypting an already-encrypted value is a no-op.

Generate a key once with:  ``python -c "from cryptography.fernet import
Fernet; print(Fernet.generate_key().decode())"`` and put it in odoo.conf.
"""
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

from odoo.tools import config

_logger = logging.getLogger(__name__)

_PREFIX = 'enc:v1:'


def _get_key():
    """Return the configured Fernet key (str) or None if unset."""
    key = config.get('saas_secret_key') or os.environ.get('SAAS_SECRET_KEY')
    return key.strip() if key else None


def _fernet():
    key = _get_key()
    if not key:
        return None
    try:
        return Fernet(key.encode('ascii') if isinstance(key, str) else key)
    except Exception:
        _logger.error(
            "saas_secret_key is set but is not a valid Fernet key — secrets "
            "will NOT be encrypted. Generate one with "
            "Fernet.generate_key().")
        return None


def is_enabled():
    """True when a usable key is configured (encryption active)."""
    return _fernet() is not None


def is_encrypted(value):
    return isinstance(value, str) and value.startswith(_PREFIX)


def encrypt(value):
    """Encrypt *value* for storage. Passthrough for empty values, for
    already-encrypted values, and when no key is configured."""
    if not value or not isinstance(value, str):
        return value
    if value.startswith(_PREFIX):
        return value
    f = _fernet()
    if not f:
        return value
    token = f.encrypt(value.encode('utf-8')).decode('ascii')
    return _PREFIX + token


def decrypt(value):
    """Decrypt a stored value. Legacy plaintext (no prefix) is returned
    unchanged, so this is safe on rows written before encryption was on."""
    if not is_encrypted(value):
        return value
    f = _fernet()
    if not f:
        _logger.error(
            "Encountered an encrypted secret but no/invalid saas_secret_key "
            "is configured — cannot decrypt.")
        return value
    try:
        return f.decrypt(value[len(_PREFIX):].encode('ascii')).decode('utf-8')
    except InvalidToken:
        _logger.error(
            "Failed to decrypt a secret (wrong saas_secret_key?).")
        return value
