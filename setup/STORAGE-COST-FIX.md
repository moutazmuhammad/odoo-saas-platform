# One storage allowance, one Managed tenant volume

New tenants provisioned by the control plane use `spec.storage.sharedWithDatabase: true`.
The operator creates one PVC at `spec.storage.filestore.size` for the entire allowance:

- `postgres/`: mounted only in PostgreSQL at `/var/lib/postgresql/data`.
- `odoo/`: mounted in Odoo, shell, initialization, module-update, backup and restore containers.

A 10 GiB package requests one 10 GiB data PVC, not two 10 GiB PVCs. There is no fixed database/files split. PostgreSQL WAL, filesystem metadata, Odoo sessions and other runtime files also consume this physical space. Provider minimum volume sizes, replicated storage, snapshots and backup destinations can still increase the actual invoice; this change eliminates the duplicate tenant claim, not those provider costs.

The default tenant storage quota is the allowance plus 1 GiB auxiliary headroom. ResourceQuota is a ceiling, not another provisioned disk. Legacy separate layouts retain a two-allowance ceiling for upgrades, and downgrades retain room for existing claims because PVCs cannot shrink. Object-storage backups are preferred; a local backup PVC remains a separate retention cost.

## Compatibility and rollout

1. Build and publish operator image `0.1.27` (or a new immutable release tag containing this change).
2. Apply the updated CRD from `compute/charts/odoo-operator/crds/`. Helm does not upgrade CRDs automatically.
3. Upgrade the operator, using the existing cluster-specific values, and wait for a successful rollout.
4. Update the control-plane code and restart **both** Odoo and all durable job workers. An older operator must not receive new shared-layout tenants.
5. Validate a new tenant on the actual StorageClass: database initialization, file uploads, shell isolation, module upgrade, volume growth, backup/restore, rolling update and node recovery.

Shared layout supports the built-in `Managed` database only. CloudNativePG and external databases continue to require their own storage/accounting. Helm-created tenants retain separate layout unless `storage.sharedWithDatabase: true` is explicitly chosen at creation.

ReadWriteOnce consumers co-locate on the same node, including initialization/restore Jobs before a web pod exists. PostgreSQL prepares both directories as a non-root user; its directory is mode 0700. All shared-volume consumers use fsGroup 101 and `OnRootMismatch` to avoid recursively changing PostgreSQL permissions. Verify that the chosen CSI driver's ownership handling preserves this behavior. Instances now run exactly one Odoo pod; CPU/RAM growth does not create replicas or a separate cron pod. Cron runs in its own resource-limited container inside the same Odoo pod.

## Existing tenants: migrate, do not shrink

The storage layout is immutable. Existing separate volumes are not deleted or silently repointed to an empty directory. Updating this code alone does **not** reduce their existing invoices.

To migrate a tenant, arrange a maintenance window, stop writes and background work, make a final full-instance backup, then provision a new shared-layout instance using the existing restore-from-backup path. Choose a size that accommodates the restored database, WAL and Odoo files. Verify restored databases, attachments and access before switching traffic and tenant references. Keep the original volumes through the agreed recovery window. Only remove the old instance after verifying the new one and the backup; check the PV reclaim policy so a `Retain` volume is not left billed after its PVC disappears.

## Build cache: no permanently provisioned disk

BuildKit scratch space is now a job-local `emptyDir` capped at 20 GiB. The build container requests 4 GiB ephemeral storage and limits combined ephemeral use to 24 GiB. Ensure build nodes have adequate disk and bound concurrent builds. Kubernetes releases scratch space with the pod; the Job TTL is one hour. This uses node disk during builds, rather than an always-billed block-storage volume per tenant.

Builds use `--opt no-cache` and neither import nor export registry cache. Only deployable images are pushed; registry image retention still needs its own policy. Builds with custom repositories must reinstall dependencies on each build. Tenants without custom/product repositories deploy the ready Odoo base image directly without starting BuildKit. Repository preparation fetches at most two repositories concurrently within the same pod; unchanged modules continue to skip database upgrade work. No additional always-running builder is introduced.

Old BuildKit PVCs are not automatically deleted. After upgrading/restarting all workers, use the cleanup utility with the control-plane Python environment and the target cluster's kubeconfig:

```sh
python scripts/cleanup-legacy-build-caches.py --context <cluster-context>
python scripts/cleanup-legacy-build-caches.py --context <cluster-context> --delete
```

The first command previews eligible caches. The second deletes only managed `buildkit-cache-*` PVCs in `odoo-builds` that have no Pod or Job references. Retained completed Jobs also prevent deletion until their TTL cleanup. Deletion uses PVC UID/resource-version preconditions. Check the storage reclaim policy: `Retain` PVs need separate cleanup to stop billing. The utility never selects tenant data volumes.
