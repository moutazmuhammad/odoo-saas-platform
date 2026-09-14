# Backup + restore Job image (compute/operator/internal/resources/backup.go
# DefaultBackupToolImage and restore.go DefaultRestoreToolImage — the same
# image serves both Jobs, see the latter's doc comment). Bundles
# pg_dump/pg_restore (matching the OdooInstance's supported PostgreSQL
# majors), tar/gzip, and this repo's run-backup.sh/run-restore.sh.
#
# Runs non-root with a read-only rootfs (see genericHardenedSecurityContext
# in compute/operator/internal/resources/deployment.go) — every write goes
# to a mounted volume (/tmp, /backups, /var/lib/odoo), never to the image
# itself.
FROM postgres:16-bookworm

RUN apt-get update && \
    apt-get install -y --no-install-recommends postgresql-client-16 && \
    rm -rf /var/lib/apt/lists/*

COPY run-backup.sh run-restore.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/run-backup.sh /usr/local/bin/run-restore.sh

# Must be Odoo's OWN uid:gid (100:101 — see deployment.go's odooImageUID/
# odooImageGID), not an arbitrary non-root account: the filestore PVC's
# `addons/` and `sessions/` subdirectories are created by the Odoo
# container as mode 0700 (owner-only, confirmed live: `drwx------ odoo
# odoo`), so ANY other uid — including one sharing the FSGroup — gets zero
# access; FSGroup only ever grants group bits, and 0700 has none to grant.
# Only running as uid 100 itself (matching, not root) can read them.
USER 100:101

ENTRYPOINT ["/usr/local/bin/run-backup.sh"]
