"""Cluster connection moves from the Region to the cluster (18.0.55.0.0).

A region can now hold several clusters, so the kubeconfig (formerly a
separate saas.kubeconfig record linked from the region), TLS issuer,
registry and Prometheus settings live on each ``saas.server``. Copy every
region's settings onto its clusters (un-regioned clusters belong to the
default region). The old region columns and the saas_kubeconfig table are
still present here; Odoo drops them after this script.

Encrypted values (kubeconfig, registry password) are Fernet tokens not
bound to a record, so they are copied as they are.
"""

COPIED = ('registry_host', 'registry_push_host', 'registry_prefix',
          'registry_username', 'registry_password', 'registry_insecure',
          'builder_image', 'git_image',
          'prometheus_namespace', 'prometheus_service')


def migrate(cr, version):
    cr.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'saas_region' AND column_name = 'kubeconfig_id'
    """)
    if not cr.fetchone():
        return
    cr.execute("SELECT id FROM saas_region WHERE is_default AND active ORDER BY sequence, id LIMIT 1")
    row = cr.fetchone()
    default_region = row[0] if row else None

    sets = ', '.join('%s = r.%s' % (c, c) for c in COPIED)
    cr.execute("""
        UPDATE saas_server s SET
            kubeconfig_enc = COALESCE(s.kubeconfig_enc, k.kubeconfig_enc),
            tls_cluster_issuer = COALESCE(NULLIF(r.tls_cluster_issuer, ''), s.tls_cluster_issuer),
            {sets}
        FROM saas_region r
        LEFT JOIN saas_kubeconfig k ON k.id = r.kubeconfig_id
        WHERE r.id = COALESCE(s.region_id, %s)
    """.format(sets=sets), (default_region,))
