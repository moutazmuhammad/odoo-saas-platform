"""Kubeconfig field should be like SSH private key (18.0.50.0.0).

``saas.region.kubeconfig`` (a raw ``EncryptedChar`` pasted directly onto
the region) is replaced by ``saas.region.kubeconfig_id``, a Many2one to a
new dedicated ``saas.kubeconfig`` model — the same upload-only,
encrypted-at-rest, dedicated-record shape ``saas.ssh.key.pair`` already
uses for SSH credentials.

This is a POST-migrate (not pre_init_hook): unlike Step A's addon-move
scenario, ``saas_core`` already exists installed with real data in it —
this is an upgrade of an existing module, not a first install adopting
another addon's rows.

By the time this runs, the new code (with the ``kubeconfig`` field
already removed from ``saas.region``) is what's active, so the old value
can't be read through the ORM any more (the field doesn't exist on the
model) — read the raw column directly and decrypt/re-encrypt with the
same ``saas_core.crypto`` helpers the ``EncryptedChar`` field type uses
internally, replicating exactly what a fresh file-upload through the new
UI would have produced (base64 of the YAML, then Fernet-encrypted).
"""
import base64
import logging

from odoo.addons.saas_core import crypto

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        SELECT id, name, kubeconfig FROM saas_region
        WHERE kubeconfig IS NOT NULL AND kubeconfig != ''
    """)
    rows = cr.fetchall()
    if not rows:
        _logger.info(
            "saas_core 18.0.50.0.0: no region had a kubeconfig set — "
            "nothing to migrate.")
        return

    for region_id, region_name, raw in rows:
        plaintext_yaml = crypto.decrypt(raw)
        if not plaintext_yaml:
            continue
        b64 = base64.b64encode(plaintext_yaml.encode('utf-8')).decode('ascii')
        stored = crypto.encrypt(b64)
        cr.execute("""
            INSERT INTO saas_kubeconfig
                (name, kubeconfig_enc, create_date, write_date, create_uid, write_uid)
            VALUES (%s, %s, now(), now(), 1, 1)
            RETURNING id
        """, ('Kubeconfig — %s' % (region_name or region_id), stored))
        kubeconfig_id = cr.fetchone()[0]
        cr.execute(
            "UPDATE saas_region SET kubeconfig_id = %s WHERE id = %s",
            (kubeconfig_id, region_id))
        _logger.info(
            "saas_core 18.0.50.0.0: migrated region '%s' (id=%s)'s "
            "kubeconfig into new saas.kubeconfig id=%s.",
            region_name, region_id, kubeconfig_id)
