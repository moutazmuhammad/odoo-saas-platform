"""DataService — the two stateful primitives the platform composes everything from.

    snapshot(instance)                     -> saas.instance.backup (full-instance restic snapshot)
    materialize(snapshot, target=None, …)  -> restore the snapshot onto target

backup / restore / clone / migrate / DR are all compositions of these two. v1 delegates
to the already-proven implementations (restic full-instance backup + the 5-step restore);
it does NOT reimplement them, so behavior — verified on real infra — is unchanged.
"""

from __future__ import annotations

import logging
import shlex

_logger = logging.getLogger(__name__)


class DataService:
    """Constructed with an Odoo Environment; operates on saas.instance records."""

    def __init__(self, env):
        self.env = env

    # -- primitive 1 --------------------------------------------------------
    def snapshot(self, instance):
        """Create a full-instance snapshot (restic → object storage).

        Delegates to saas.instance.backup._perform_full_instance_backup (the same
        code path the daily cron + the test button use). Returns the resulting
        completed saas.instance.backup record.
        """
        instance.ensure_one()
        Backup = self.env['saas.instance.backup'].sudo()
        Backup._perform_full_instance_backup(instance)
        snap = Backup.search([
            ('instance_id', '=', instance.id),
            ('is_full_instance', '=', True),
            ('state', '=', 'done'),
        ], order='id desc', limit=1)
        if not snap:
            raise RuntimeError(
                "DataService.snapshot: no completed full-instance backup found "
                "for instance '%s'." % instance.subdomain)
        return snap

    # -- primitive 2 --------------------------------------------------------
    def materialize(self, snapshot, target=None, neutralize=False):
        """Restore ``snapshot`` onto ``target`` (default: the snapshot's own instance).

        Delegates to saas.instance._do_restore_full_instance (the proven 5-step
        restore: stop → wipe → restic FS restore → DB restore → up → nginx).
        ``neutralize`` (disable mail/cron for non-prod clones) is reserved for the
        clone primitive and not implemented yet.
        """
        snapshot.ensure_one()
        target = target or snapshot.instance_id
        target.ensure_one()
        if neutralize:
            raise NotImplementedError(
                "DataService.materialize(neutralize=True) is not implemented yet — "
                "it is wired with the clone primitive (Phase 1 later / Phase 7).")
        target._do_restore_full_instance(snapshot.id)
        return target

    # -- Phase 2.1.4: filestore migration (local disk -> object storage) -----
    def migrate_filestore_to_object_store(self, instance, *, recreate=True):
        """Move a tenant's existing local filestore onto its object-storage mount
        (JuiceFS), so switching it to object storage keeps every pre-existing
        attachment (incl. generated asset bundles). Requires the docker host to
        have ``object_filestore_mount`` set.

        Safe sequence when ``recreate`` (a tenant still on local disk):
          1. copy local filestore -> object mount while the tenant runs (pre-copy)
          2. stop the container
          3. final copy (catch deltas written during the pre-copy)
          4. re-render compose (adds the filestore bind-mount) + recreate + start

        ``recreate=False`` only copies — for a tenant already bind-mounted onto an
        (empty) object filestore, this back-fills its old files with no downtime.
        Idempotent (``cp -a`` overwrite); mirrors the platform's existing filestore
        copy convention. Returns the destination mount path.
        """
        instance.ensure_one()
        dst = instance._get_filestore_mount()
        if not dst:
            raise RuntimeError(
                "DataService.migrate_filestore_to_object_store: server '%s' has no "
                "object_filestore_mount set." % instance.docker_server_id.name)
        src = '%s/data/odoo/filestore' % instance._get_instance_path()
        server = instance.docker_server_id

        def _copy(ssh, uid):
            # copy CONTENTS of src into dst (preserve attrs), then chown to the
            # container uid so Odoo can read/write them.
            cmd = (
                'sudo mkdir -p %(dst)s && '
                'if [ -d %(src)s ]; then sudo cp -a %(src)s/. %(dst)s/ ; fi && '
                'sudo chown -R %(uid)s:%(uid)s %(dst)s'
            ) % {'src': shlex.quote(src), 'dst': shlex.quote(dst), 'uid': uid}
            rc, out, err = ssh.execute(cmd, timeout=600)
            if rc != 0:
                raise RuntimeError(
                    'filestore migration copy failed (rc=%s): %s' % (rc, (out + err)[-500:]))

        with server._get_ssh_connection() as ssh:
            uid = instance._get_container_uid(ssh)
            _copy(ssh, uid)  # pre-copy
            if recreate:
                driver = instance._compute_driver(connection=ssh)
                handle = instance._compute_handle()
                driver.stop(handle)
                _copy(ssh, uid)               # final sync after stop
                instance._render_and_write_configs(ssh)  # emits the bind-mount
                driver.destroy(handle)
                driver.start(handle)
        _logger.info("Migrated filestore of %s -> %s", instance.subdomain, dst)
        return dst
