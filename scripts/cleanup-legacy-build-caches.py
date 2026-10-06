#!/usr/bin/env python3
"""List obsolete BuildKit PVCs; --delete removes only unreferenced caches.

Run after upgrading the control plane and restarting all job workers.
Tenant data PVCs are never selected. Default is a read-only preview.
"""
import argparse
from kubernetes import client, config

NAMESPACE = 'odoo-builds'
MANAGED_BY = 'saas-control-plane'


def referenced_claims(core, batch):
    pods = core.list_namespaced_pod(NAMESPACE).items
    jobs = batch.list_namespaced_job(NAMESPACE).items
    specs = [pod.spec for pod in pods] + [job.spec.template.spec for job in jobs]
    return {volume.persistent_volume_claim.claim_name
            for spec in specs for volume in (spec.volumes or [])
            if volume.persistent_volume_claim is not None}


def cleanup(core, batch, *, delete=False):
    claims = core.list_namespaced_persistent_volume_claim(
        NAMESPACE, label_selector='app.kubernetes.io/managed-by=' + MANAGED_BY).items
    references = referenced_claims(core, batch)
    for claim in claims:
        meta = claim.metadata
        if (not meta.name.startswith('buildkit-cache-')
                or (meta.labels or {}).get('app.kubernetes.io/managed-by') != MANAGED_BY):
            continue
        if meta.name in references or meta.deletion_timestamp:
            print('SKIP (referenced or deleting): ' + meta.name)
            continue
        if not delete:
            print('ELIGIBLE: ' + meta.name)
            continue
        # Recheck before each deletion. Completed pods/Jobs also block
        # removal until TTL cleanup, so no retained build can reuse the disk.
        if meta.name in referenced_claims(core, batch):
            print('SKIP (new reference): ' + meta.name)
            continue
        if not meta.uid or not meta.resource_version:
            raise RuntimeError('Missing PVC identity: ' + meta.name)
        core.delete_namespaced_persistent_volume_claim(
            meta.name, NAMESPACE,
            body=client.V1DeleteOptions(preconditions=client.V1Preconditions(
                uid=meta.uid, resource_version=meta.resource_version)))
        print('DELETED cache PVC: ' + meta.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', help='Kubeconfig context for the target cluster')
    parser.add_argument('--delete', action='store_true', help='Delete eligible unused cache PVCs')
    args = parser.parse_args()
    config.load_kube_config(context=args.context)
    cleanup(client.CoreV1Api(), client.BatchV1Api(), delete=args.delete)


if __name__ == '__main__':
    main()
