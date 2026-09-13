"""Odoo.sh-style environments (18.0.49.0.0).

Adding the ``environment`` Selection (default ``production``), ``parent_id``
(NULL) and ``main_branch`` (default ``main``) columns is handled by Odoo's
stored-field initialisation, which applies each field's default to existing
rows. This post-migrate only refines ``main_branch``: existing hosting
instances that already have a connected repo should track that repo's branch
as their main branch (so Production maps to the real primary branch, and any
future Development branches are created from it).
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    # Belt-and-suspenders: ensure existing rows have a concrete environment /
    # main_branch even on databases where the default backfill didn't run.
    cr.execute("""
        UPDATE saas_instance
           SET environment = 'production'
         WHERE environment IS NULL
    """)
    cr.execute("""
        UPDATE saas_instance
           SET main_branch = 'main'
         WHERE main_branch IS NULL OR main_branch = ''
    """)

    # For every (production) instance that has at least one repo, adopt the
    # first repo's branch as the project's main branch.
    cr.execute("""
        UPDATE saas_instance i
           SET main_branch = r.branch
          FROM (
              SELECT DISTINCT ON (instance_id) instance_id, branch
                FROM saas_instance_repo
               WHERE branch IS NOT NULL AND branch <> ''
            ORDER BY instance_id, sequence, id
          ) r
         WHERE r.instance_id = i.id
           AND i.environment = 'production'
    """)
    _logger.info("saas_core 18.0.49.0.0: environments backfilled "
                 "(%d rows touched).", cr.rowcount)
