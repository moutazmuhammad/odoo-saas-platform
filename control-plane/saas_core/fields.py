"""Custom ORM fields for the SaaS platform.

``EncryptedChar`` transparently encrypts its value at the database-column
boundary (SEC-002): the Python value seen by every call site stays plaintext,
but what lands in the column is a Fernet token. It needs no migration and no
column rename — it reuses the existing column, reads legacy plaintext rows
unchanged, and is a pure passthrough until a ``saas_secret_key`` is configured.
See ``saas_core/crypto.py``.
"""
from odoo import fields

from . import crypto


class EncryptedChar(fields.Char):
    """A Char whose DB column holds an encrypted token while the cached/record
    value is plaintext. Do not put a ``size=`` on it — ciphertext is longer
    than the plaintext and a column cap would corrupt it."""

    def convert_to_column(self, value, record, values=None, validate=True):
        # Runs on the way to the DB column: encrypt the (plaintext) value.
        stored = super().convert_to_column(
            value, record, values=values, validate=validate)
        if isinstance(stored, str):
            return crypto.encrypt(stored)
        return stored

    def convert_to_record(self, value, record):
        # Runs when a value leaves the cache for a record (attribute access).
        # The cache may hold an encrypted token (just fetched from the column,
        # which bypasses convert_to_cache) or plaintext (just assigned);
        # decrypt() only touches the ``enc:v1:`` prefix, so plaintext passes
        # through untouched.
        if isinstance(value, str):
            value = crypto.decrypt(value)
        return super().convert_to_record(value, record)
