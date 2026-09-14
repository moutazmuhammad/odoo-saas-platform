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

# Arbitrary non-root uid:gid — unlike the Odoo image, nothing in the
# operator depends on this specific value (see deployment.go's comment on
# odooImageUID/GID, which is Odoo-image-specific, not general).
USER 10001:10001

ENTRYPOINT ["/usr/local/bin/run-backup.sh"]
