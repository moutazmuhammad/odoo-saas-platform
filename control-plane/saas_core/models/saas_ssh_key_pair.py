from odoo import api, fields, models

from ..fields import EncryptedChar


class SaasSshKeyPair(models.Model):
    _name = 'saas.ssh.key.pair'
    _description = 'SSH Key Pair'
    _order = 'name'

    name = fields.Char(
        string='Name',
        required=True,
        help='Descriptive label for this key pair (e.g. "Production EU Key").',
    )
    private_key_filename = fields.Char(
        string='Key Filename',
        default='id_rsa',
        help='Original filename of the private key (e.g. id_rsa, id_ed25519). '
             'Used when writing the key to a temporary file for SSH connections.',
    )
    type = fields.Selection(
        selection=[
            ('rsa', 'RSA'),
            ('dsa', 'DSA'),
            ('ecdsa', 'ECDSA'),
            ('ed25519', 'ED25519'),
        ],
        string='Key Type',
        default='rsa',
        help='Cryptographic algorithm of the private key. '
             'Must match the actual key file format.',
    )
    # Upload INBOX only: a key dropped here is moved into the encrypted column
    # (``private_key_enc``) and this field is cleared on save, so no cleartext
    # private key is ever persisted in the DB / attachments (SEC-002).
    private_key_file = fields.Binary(
        string='Upload Private Key',
        groups='saas_core.group_saas_manager',
        help='Upload a PEM private key here to set or replace it. It is '
             'encrypted at rest and this upload box is cleared on save. '
             'Restricted to SaaS Managers.',
    )
    private_key_file_name = fields.Char(
        string='Upload Filename',
        help='Filename detected during upload (internal use).',
    )
    # Encrypted-at-rest store for the key (base64 of the PEM). Reads decrypt
    # transparently; legacy rows that still hold a cleartext upload are read via
    # the fallback in ``_private_key_b64`` and migrated by writing them back.
    private_key_enc = EncryptedChar(
        string='Encrypted Private Key',
        groups='saas_core.group_saas_manager',
        copy=False,
    )
    key_loaded = fields.Boolean(
        string='Key Loaded',
        compute='_compute_key_loaded',
        help='A private key is stored (encrypted) for this pair.',
    )

    @api.depends('private_key_enc', 'private_key_file')
    def _compute_key_loaded(self):
        for rec in self:
            rec.key_loaded = bool(rec.private_key_enc or rec.private_key_file)

    def _private_key_b64(self):
        """Return the base64 private key for SSH (decrypted), or False.

        Prefers the encrypted store; falls back to a legacy cleartext upload so
        existing key pairs keep working until migrated."""
        self.ensure_one()
        return self.private_key_enc or self.private_key_file or False

    @staticmethod
    def _b64_to_str(value):
        if isinstance(value, bytes):
            return value.decode('ascii')
        return value

    def _capture_uploaded_key(self, vals):
        """Move an uploaded key out of the cleartext inbox into the encrypted
        column. Returns a (possibly new) vals dict."""
        if vals.get('private_key_file'):
            vals = dict(vals)
            vals['private_key_enc'] = self._b64_to_str(vals['private_key_file'])
            vals['private_key_file'] = False
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = [self._capture_uploaded_key(v) for v in vals_list]
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('private_key_file'):
            vals = self._capture_uploaded_key(vals)
        return super().write(vals)

    @api.model
    def _saas_migrate_legacy_keys(self):
        """Encrypt any legacy cleartext keys still sitting in the upload inbox.
        Idempotent; safe to run after configuring ``saas_secret_key``."""
        migrated = 0
        for rec in self.sudo().search([('private_key_file', '!=', False)]):
            rec.write({'private_key_file': rec.private_key_file})
            migrated += 1
        return migrated
