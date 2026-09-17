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
import time

from odoo import _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# How often to poll namespace-creation / RestoreReady while migrating a
# tenant onto Kubernetes (migrate_to_kubernetes). Generous relative to
# reconcile-loop latency, short enough not to blow past a caller's own
# timeout budget by more than one tick.
_MIGRATION_POLL_INTERVAL = 5


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

    # -- Phase 2 (/ROADMAP.md §5): legacy -> Kubernetes migration primitive --
    def migrate_to_kubernetes(self, instance, target_server, *,
                               subdomain_suffix='-k8s', timeout=1800):
        """Produce a parallel, health- and data-verified Kubernetes copy of
        ``instance`` (currently on compute_driver='ssh_docker'), restored
        from a fresh dump of its real data.

        Never touches or flips traffic for ``instance`` — deciding when/
        whether to cut a customer's traffic over is a separate, later step
        (/ROADMAP.md §5 Phase 2's later work items). This only needs to
        prove a healthy, data-verified Kubernetes instance exists.

        On any failure, the new instance/CR/namespace/Secret are deleted
        and ``instance`` is left completely unmodified — there is nothing
        to "roll back" on the source side. Returns the new, verified
        ``saas.instance`` record on success.
        """
        instance.ensure_one()
        if instance.docker_server_id.compute_driver != 'ssh_docker':
            raise UserError(_(
                "migrate_to_kubernetes: '%s' is not on the legacy "
                "ssh_docker driver (compute_driver=%s) — nothing to "
                "migrate."
            ) % (instance.subdomain, instance.docker_server_id.compute_driver))
        if target_server.compute_driver != 'kubernetes':
            raise UserError(_(
                "migrate_to_kubernetes: target server '%s' is not a "
                "Kubernetes server."
            ) % target_server.name)
        if not (target_server.region_id and target_server.region_id.kubeconfig):
            raise UserError(_(
                "migrate_to_kubernetes: target server '%s' has no region "
                "kubeconfig configured."
            ) % target_server.name)

        Instance = self.env['saas.instance'].sudo()
        Backup = self.env['saas.instance.backup'].sudo()
        target = Instance.create({
            'subdomain': instance.subdomain + subdomain_suffix,
            'domain_id': instance.domain_id.id,
            'partner_id': instance.partner_id.id,
            'odoo_version_id': instance.odoo_version_id.id,
            'is_hosting': instance.is_hosting,
            'saas_product_id': instance.saas_product_id.id,
            'plan_id': instance.plan_id.id,
            'billing_period': instance.billing_period,
            'docker_server_id': target_server.id,
            'migration_source_instance_id': instance.id,
            'migration_state': 'dumping',
            'state': 'draft',
        })
        handle = None
        try:
            bucket, prefix, stamp = Backup.dump_for_k8s_migration(
                instance, target.subdomain)
            target.migration_state = 'uploading'

            cfg = Backup._get_backup_config()
            driver = target._compute_driver()
            # Mirrors KubernetesDriver._cr_name exactly (container_name is
            # always 'odoo_<sub>') — needed before create() to name the
            # restore-credentials Secret up front.
            cr_name = target._get_container_name().replace('_', '-').lower()
            secret_name = '%s-restore-creds' % cr_name

            from ..drivers.base import ComputeSpec
            spec = ComputeSpec(
                container_name=target._get_container_name(),
                image=target.odoo_version_id._get_docker_image(),
                instance_path=target._get_instance_path(),
                http_port=target.xmlrpc_port or 0,
                longpolling_port=target.longpolling_port or 0,
                db_name=target.subdomain,
                db_host='',
                env={
                    'domain': target.name,
                    'odoo_version': target.odoo_version_id.name,
                    'restore': {
                        'bucket': bucket,
                        'prefix': prefix,
                        'secret_name': secret_name,
                        'backup_id': stamp,
                    },
                },
            )
            handle = driver.create(spec)
            target.migration_state = 'restoring'

            self._ensure_restore_secret(
                driver, handle.instance_path, secret_name, cfg, timeout=timeout)
            self._wait_for_restore_ready(driver, cr_name, timeout=timeout)

            target.migration_state = 'verifying'
            self._wait_until_healthy(driver, handle, timeout=timeout)
            self._verify_migrated_data(instance, target, driver, handle)

            target.migration_state = 'verified'
            self._delete_migration_dump(prefix, stamp)
            self.env['saas.audit.log']._saas_audit(
                'instance_migrate_k8s', model='saas.instance', res_id=target.id,
                res_name=target.subdomain,
                detail='Migrated from %s to Kubernetes server %s'
                       % (instance.subdomain, target_server.name))
            return target
        except Exception:
            self._cleanup_failed_migration(target, handle)
            raise

    def _ensure_restore_secret(self, driver, namespace, secret_name, cfg, timeout=1800):
        """Poll for the tenant namespace (created asynchronously by the
        operator's reconcileTenancy once the CR exists), then create the
        ObjectStorage credentials Secret ``spec.restore.source.
        objectStorageSecretRef`` points at. The operator provisions no
        such Secret itself — this is the one piece of day-0 setup the
        caller owns. A benign ordering race is expected here: the restore
        Job's pod simply retries mounting until the Secret exists.
        """
        from kubernetes import client as k8s_client
        from kubernetes.client.rest import ApiException

        core_api = driver._core_api()
        deadline = time.time() + timeout
        while True:
            try:
                ns = core_api.read_namespace(namespace)
                # A namespace name is reused across attempts (it's derived
                # deterministically from the target subdomain) — a prior
                # failed attempt's namespace can still be mid-termination
                # when a retry starts (observed against a real cluster:
                # the API still returns it as existing, but any write into
                # it 403s with NamespaceTerminating). Wait for it to be
                # fully gone (404) or genuinely Active, not merely present.
                phase = (ns.status.phase if ns.status else None)
                if phase != 'Terminating':
                    break
            except ApiException as e:
                if e.status != 404:
                    raise RuntimeError(
                        'checking namespace %s failed: %s' % (namespace, e)) from e
            if time.time() > deadline:
                raise RuntimeError(
                    'namespace %s was not created (or still terminating from '
                    'a prior attempt) within %ss' % (namespace, timeout))
            time.sleep(_MIGRATION_POLL_INTERVAL)

        secret = k8s_client.V1Secret(
            metadata=k8s_client.V1ObjectMeta(name=secret_name),
            string_data={
                'endpoint': cfg.get('endpoint') or '',
                'access-key': cfg.get('access_key') or '',
                'secret-key': cfg.get('secret_key') or '',
            },
        )
        try:
            core_api.create_namespaced_secret(namespace, secret)
        except ApiException as e:
            if e.status != 409:  # already exists — tolerate a retry
                raise RuntimeError(
                    'creating restore secret %s in %s failed: %s'
                    % (secret_name, namespace, e)) from e

    def _wait_for_restore_ready(self, driver, cr_name, timeout=1800):
        """Poll OdooInstance.status.conditions for RestoreReady, set by
        the operator's reconcileRestore once the one-time restore Job
        finishes (compute/operator/internal/controller/restore.go).

        ``status=False`` alone is NOT terminal: reconcileRestore reports
        RestorePending/RestoreRunning with status=False while the restore
        Job is still in progress (a real, live-verified state — a fresh
        restore observably sits here for a while before either succeeding
        or failing) — only ``reason=RestoreFailed`` means the restore
        Job actually exhausted its retries and failed.
        """
        deadline = time.time() + timeout
        while True:
            cr = driver._get_cr(cr_name)
            conditions = ((cr or {}).get('status') or {}).get('conditions') or []
            restore_cond = next(
                (c for c in conditions if c.get('type') == 'RestoreReady'), None)
            if restore_cond:
                if restore_cond.get('status') == 'True':
                    return
                if restore_cond.get('reason') == 'RestoreFailed':
                    raise RuntimeError(
                        'restore failed for %s: %s' % (
                            cr_name, restore_cond.get('message') or 'RestoreFailed'))
            if time.time() > deadline:
                raise RuntimeError(
                    'restore for %s did not become ready within %ss'
                    % (cr_name, timeout))
            time.sleep(_MIGRATION_POLL_INTERVAL)

    def _wait_until_healthy(self, driver, handle, timeout=1800):
        """Poll health() until the restored instance's web Deployment is
        actually serving, not just restored. RestoreReady only means the
        one-time restore Job succeeded — the operator still has to
        reconcile and roll out the web Deployment afterwards, which
        observably takes a bit (image pull, readiness probe warm-up):
        a single post-restore health() check can catch it mid-rollout
        (status='restarting', phase='Provisioning') and wrongly read that
        as a failure.
        """
        deadline = time.time() + timeout
        health = driver.health(handle)
        while not health.running:
            if time.time() > deadline:
                raise RuntimeError(
                    "restored instance did not become healthy within %ss "
                    "(status=%s, detail=%s)" % (timeout, health.status, health.detail))
            time.sleep(_MIGRATION_POLL_INTERVAL)
            health = driver.health(handle)

    def _verify_migrated_data(self, source_instance, target, driver, handle,
                               model='res.users'):
        """Compare a row count between the source and the freshly restored
        target. health()/RestoreReady only prove the pod is up and the
        restore Job exited 0 — not that real tenant data is actually
        there; a restore that silently no-ops or restores an empty/
        garbage DB would still pass both. Reads each side's DB connection
        info from its own container's /etc/odoo/odoo.conf (the official
        Odoo image's default config path) so this needs no assumptions
        about the target's DB backend (Managed/CloudNativePG/External).
        """
        query_cmd = (
            "python3 -c \""
            "import configparser, subprocess, sys;"
            "c = configparser.ConfigParser(); c.read('/etc/odoo/odoo.conf');"
            "o = c['options'];"
            "r = subprocess.run(['psql', '-tAc', 'select count(*) from %s',"
            "'-h', o['db_host'], '-p', o.get('db_port', '5432'),"
            "'-U', o['db_user'], o['db_name']],"
            "env={'PGPASSWORD': o['db_password']}, capture_output=True, text=True);"
            "sys.stdout.write(r.stdout.strip() or ('ERR:' + r.stderr.strip()))\""
        ) % model.replace('.', '_')

        src = source_instance._compute_driver().exec(
            source_instance._compute_handle(), query_cmd, timeout=60)
        tgt = driver.exec(handle, query_cmd, timeout=60)

        if not src.ok or not src.stdout.strip().isdigit():
            raise RuntimeError(
                'could not read source row count for %s: %s'
                % (model, src.stdout or src.stderr))
        if not tgt.ok or not tgt.stdout.strip().isdigit():
            raise RuntimeError(
                'could not read restored row count for %s: %s'
                % (model, tgt.stdout or tgt.stderr))

        src_count = int(src.stdout.strip())
        tgt_count = int(tgt.stdout.strip())
        if tgt_count != src_count:
            raise RuntimeError(
                'data verification failed: source has %d %s rows, '
                'restored instance has %d' % (src_count, model, tgt_count))

    def _delete_migration_dump(self, prefix, stamp):
        """Best-effort: the migration dump is a one-time bootstrap
        artifact, superseded by the new instance's own scheduled backups
        once verified — delete it so the bucket doesn't accumulate stray
        per-migration dumps."""
        Backup = self.env['saas.instance.backup'].sudo()
        for name in ('db.dump', 'filestore.tar.gz', 'manifest.json'):
            try:
                Backup._delete_bucket_path('%s/%s/%s' % (prefix, stamp, name))
            except Exception:
                _logger.warning(
                    "Failed to delete migration dump object %s/%s/%s",
                    prefix, stamp, name, exc_info=True)

    def _cleanup_failed_migration(self, target, handle=None):
        """Best-effort, never raises. Tears down whatever was created for
        a failed migration attempt: the CR (which cascades to the whole
        tenant namespace via the operator's finalizer) and the target
        saas.instance record. The SOURCE instance is never touched here
        or anywhere upstream — "rollback" only ever means "don't finish
        creating the new one."
        """
        if handle is not None:
            try:
                target._compute_driver().destroy(handle)
            except Exception:
                _logger.warning(
                    "Failed to destroy Kubernetes resources for failed "
                    "migration target %s", target.subdomain, exc_info=True)
        try:
            target.migration_state = 'failed'
        except Exception:
            pass
        try:
            target.sudo().unlink()
        except Exception:
            _logger.warning(
                "Failed to unlink failed migration target %s",
                target.subdomain, exc_info=True)
