"""Heal config params that exist by key but lost their ir.model.data xmlid.

When a config parameter declared in data/ir_config_parameter.xml exists
in the DB WITHOUT its ``saas_core.config_param_*`` xmlid, the ``noupdate``
data loader can't find the xmlid, tries to CREATE the row, and hits
``ir_config_parameter_key_uniq`` — the upgrade crashes (observed twice on
odoo.odex.sa with saas_master.max_instances_per_user).

The orphaning can recur at ANY time, not just once: deleting a parameter
from Settings > Technical > Parameters removes its xmlid with it, and the
next Settings save (``set_param``) recreates the row bare. A one-shot
version-pinned migration (this script's first life, at 18.0.26.0.0) only
heals DBs upgrading *across* that version — which is why the crash came
back. Living in ``migrations/0.0.0/`` it runs as a ``pre`` script on
EVERY version change, just before data/ir_config_parameter.xml loads.

Healing = backfill the missing xmlid (never delete the row), so the
operator's configured value is preserved and the loader skips creation.
A dangling xmlid (pointing at a deleted row while a live row holds the
key) is re-pointed at the live row for the same reason.

Only the params actually declared in data/ir_config_parameter.xml are
healed — creating an xmlid for anything else would make end-of-upgrade
orphan cleanup delete that param along with its value. KEEP THE LIST
BELOW IN SYNC WITH THE DATA FILE.
"""
import logging

_logger = logging.getLogger(__name__)

# Suffixes after "saas_master." — xmlid is "config_param_<suffix>".
# Mirrors data/ir_config_parameter.xml exactly.
_PARAM_SUFFIXES = [
    'default_instance_starting_port', 'trial_days', 'max_instances_per_user',
    'worker_price', 'storage_price_per_gb',
    'custom_plan_min_workers', 'custom_plan_max_workers',
    'custom_plan_min_storage', 'custom_plan_max_storage',
    'custom_plan_cpu_per_worker', 'custom_plan_ram_per_worker',
    'custom_plan_users_per_worker_min', 'custom_plan_users_per_worker_max',
    'custom_plan_yearly_discount_pct',
    'hosting_worker_price', 'hosting_storage_price_per_gb',
    'hosting_min_workers', 'hosting_max_workers',
    'hosting_min_storage', 'hosting_max_storage',
    'hosting_cpu_per_worker', 'hosting_ram_per_worker',
    'hosting_yearly_discount_pct',
]


def migrate(cr, version):
    healed = repointed = 0
    for suffix in _PARAM_SUFFIXES:
        key = 'saas_master.' + suffix
        name = 'config_param_' + suffix
        cr.execute(
            "SELECT id FROM ir_config_parameter WHERE key = %s", (key,))
        row = cr.fetchone()
        if not row:
            continue  # param not set on this DB → loader will create it cleanly
        param_id = row[0]
        cr.execute(
            "SELECT id, res_id FROM ir_model_data "
            "WHERE module = 'saas_core' AND name = %s",
            (name,))
        imd = cr.fetchone()
        if imd:
            if imd[1] != param_id:
                # Dangling xmlid (its row was deleted; the key now lives
                # on a different row) → re-point it at the live row.
                cr.execute(
                    "UPDATE ir_model_data SET res_id = %s, write_date = now() "
                    "WHERE id = %s",
                    (param_id, imd[0]))
                repointed += 1
            continue
        cr.execute(
            """INSERT INTO ir_model_data
                   (module, name, model, res_id, noupdate, create_date, write_date)
               VALUES ('saas_core', %s, 'ir.config_parameter', %s, true, now(), now())""",
            (name, param_id))
        healed += 1
    if healed or repointed:
        _logger.info(
            "Config-param xmlid heal: %s backfilled, %s re-pointed",
            healed, repointed,
        )
