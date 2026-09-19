import base64

from odoo import api, fields, models

from ..fields import EncryptedChar


class SaasKubeconfig(models.Model):
    """A Kubernetes cluster credential (kubeconfig) — same shape as
    ``saas.ssh.key.pair`` for SSH, deliberately: both are "the secret that
    lets the control plane reach a compute target," so they get the same
    upload-only, encrypted-at-rest, dedicated-record treatment rather
    than a raw text field pasted directly onto ``saas.region``.
    """
    _name = 'saas.kubeconfig'
    _description = 'Kubernetes Cluster Credentials (Kubeconfig)'
    _order = 'name'

    name = fields.Char(
        string='Name',
        required=True,
        help='Descriptive label for this cluster credential '
             '(e.g. "EU Production Cluster").',
    )
    # Upload INBOX only: a kubeconfig dropped here is moved into the
    # encrypted column (``kubeconfig_enc``) and this field is cleared on
    # save, so no cleartext kubeconfig is ever persisted in the DB /
    # attachments — mirrors saas.ssh.key.pair.private_key_file exactly.
    kubeconfig_file = fields.Binary(
        string='Upload Kubeconfig',
        groups='saas_core.group_saas_manager',
        help='Upload a kubeconfig YAML file here to set or replace it. It '
             'is encrypted at rest and this upload box is cleared on '
             'save. Restricted to SaaS Managers.',
    )
    kubeconfig_file_name = fields.Char(
        string='Upload Filename',
        help='Filename detected during upload (internal use).',
    )
    # Encrypted-at-rest store for the kubeconfig (base64 of the YAML) —
    # same convention as saas.ssh.key.pair.private_key_enc: reads decrypt
    # transparently.
    kubeconfig_enc = EncryptedChar(
        string='Encrypted Kubeconfig',
        groups='saas_core.group_saas_manager',
        copy=False,
    )
    kubeconfig_loaded = fields.Boolean(
        string='Kubeconfig Loaded',
        compute='_compute_kubeconfig_loaded',
        help='A kubeconfig is stored (encrypted) for this credential.',
    )

    @api.depends('kubeconfig_enc', 'kubeconfig_file')
    def _compute_kubeconfig_loaded(self):
        for rec in self:
            rec.kubeconfig_loaded = bool(rec.kubeconfig_enc or rec.kubeconfig_file)

    @staticmethod
    def _b64_to_str(value):
        if isinstance(value, bytes):
            return value.decode('ascii')
        return value

    def _kubeconfig_yaml(self):
        """Return the actual kubeconfig YAML text (decrypted AND
        base64-decoded — the stored value is the base64 a file upload
        always arrives as, same as saas.ssh.key.pair._private_key_b64()),
        or False."""
        self.ensure_one()
        raw_b64 = self.kubeconfig_enc or self.kubeconfig_file or False
        if not raw_b64:
            return False
        raw_b64 = self._b64_to_str(raw_b64)
        return base64.b64decode(raw_b64).decode('utf-8')

    def _capture_uploaded_kubeconfig(self, vals):
        """Move an uploaded kubeconfig out of the cleartext inbox into the
        encrypted column. Returns a (possibly new) vals dict."""
        if vals.get('kubeconfig_file'):
            vals = dict(vals)
            vals['kubeconfig_enc'] = self._b64_to_str(vals['kubeconfig_file'])
            vals['kubeconfig_file'] = False
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = [self._capture_uploaded_kubeconfig(v) for v in vals_list]
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('kubeconfig_file'):
            vals = self._capture_uploaded_kubeconfig(vals)
        return super().write(vals)
