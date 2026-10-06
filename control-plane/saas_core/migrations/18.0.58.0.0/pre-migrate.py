"""Retire selectable replica tiers before the single-pod views are loaded.

Stop all job workers before upgrading core, billing and website together.
Invoice/order records remain intact; any outstanding tier charges require
billing review because the removed feature can no longer be activated.
"""
import json
import logging
from lxml import etree

_logger = logging.getLogger(__name__)
_FIELDS = {'compute_tier_id', 'pending_compute_tier_id', 'compute_tier_pending_invoice_id'}


def without_tier_fields(arch):
    root = etree.fromstring(arch.encode(), parser=etree.XMLParser(resolve_entities=False))
    for node in list(root.iter()):
        if node.getparent() is None:
            continue
        if ((node.tag == 'xpath' and 'compute_tier' in node.get('expr', ''))
                or (node.tag == 'field' and node.get('name') in _FIELDS)
                or (node.tag == 'label' and node.get('for') in _FIELDS)):
            node.getparent().remove(node)
    return etree.tostring(root, encoding='unicode')


def migrate(cr, version):
    cr.execute("""UPDATE saas_job SET state='cancelled',
        finished_at=NOW(), error='Replica tiers removed; scale through resource limits.'
        WHERE model='saas.instance' AND method='_do_scale_compute_tier'
        AND state IN ('pending', 'running')""")
    cr.execute("""SELECT id, arch_db FROM ir_ui_view
        WHERE model='saas.instance' AND arch_db::text LIKE '%compute_tier%'""")
    for view_id, translations in cr.fetchall():
        updated = {lang: without_tier_fields(arch) for lang, arch in translations.items()}
        cr.execute('UPDATE ir_ui_view SET arch_db=%s::jsonb WHERE id=%s',
                   (json.dumps(updated), view_id))
    # The removed tier model's views include billing inherits. Remove these
    # together, before Odoo validates base views against the new registry.
    cr.execute("""DELETE FROM ir_ui_view WHERE model='saas.compute.tier'
        OR id IN (SELECT res_id FROM ir_model_data WHERE module='saas_website'
            AND name='portal_compute_tier_checkout' AND model='ir.ui.view')
        RETURNING id""")
    view_ids = [row[0] for row in cr.fetchall()]
    if view_ids:
        cr.execute("DELETE FROM ir_model_data WHERE model='ir.ui.view' AND res_id=ANY(%s)", (view_ids,))
    cr.execute("SELECT to_regclass('public.account_move')")
    if cr.fetchone()[0]:
        cr.execute("""SELECT id FROM account_move WHERE invoice_origin LIKE 'SAAS:COMPUTE-TIER:%'
            AND state != 'cancel' AND payment_state NOT IN ('paid','in_payment','reversed')""")
        pending = [row[0] for row in cr.fetchall()]
        if pending:
            _logger.warning('Removed replica tiers: review outstanding invoice IDs %s; no future activation or renewal charge will be generated.', pending)
