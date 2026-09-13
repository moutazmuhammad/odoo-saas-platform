package v1alpha1

import (
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// DatabaseMode describes how PostgreSQL is provisioned and managed for an
// OdooInstance. The mode is deliberately an abstraction: the CRD never
// exposes PostgreSQL implementation details directly, so the underlying
// provisioning strategy can change (built-in Postgres today, a
// PostgreSQL-operator-managed cluster or an external managed database
// tomorrow) without changing the OdooInstance API.
// +kubebuilder:validation:Enum=Managed;CloudNativePG;External
type DatabaseMode string

const (
	// DatabaseModeManaged provisions a single-instance PostgreSQL Deployment
	// and PVC owned by this OdooInstance. This is the default, zero-dependency
	// mode suitable for the platform's initial rollout.
	DatabaseModeManaged DatabaseMode = "Managed"

	// DatabaseModeCloudNativePG delegates PostgreSQL lifecycle to the
	// CloudNativePG operator by creating a `postgresql.cnpg.io/v1 Cluster`
	// resource instead of a raw Deployment. Requires CloudNativePG to be
	// installed on the cluster. Recommended for production once adopted.
	DatabaseModeCloudNativePG DatabaseMode = "CloudNativePG"

	// DatabaseModeExternal points the Odoo instance at a database the
	// platform does not manage (e.g. a cloud-managed PostgreSQL instance).
	// The operator creates no database resources in this mode; it only
	// wires the connection details from CredentialsSecretRef into Odoo.
	DatabaseModeExternal DatabaseMode = "External"
)

// DatabaseSpec configures the PostgreSQL database backing an Odoo instance.
type DatabaseSpec struct {
	// Mode selects the database provisioning strategy. See DatabaseMode.
	// +kubebuilder:default=Managed
	Mode DatabaseMode `json:"mode,omitempty"`

	// Version is the PostgreSQL major version to provision. Ignored in
	// External mode.
	// +kubebuilder:default="16"
	// +kubebuilder:validation:Enum="13";"14";"15";"16";"17"
	Version string `json:"version,omitempty"`

	// Storage describes the persistent volume used by a Managed or
	// CloudNativePG database. Ignored in External mode.
	// +optional
	Storage *StorageRequestSpec `json:"storage,omitempty"`

	// CredentialsSecretRef references a Secret holding connection details
	// for External mode, or overrides the auto-generated credentials Secret
	// name for Managed/CloudNativePG mode. The Secret must contain the keys
	// `host`, `port`, `dbname`, `username`, `password` (host/port/dbname are
	// only required in External mode; the operator populates them for
	// Managed/CloudNativePG mode).
	//
	// The controller never reads or writes the password value into status,
	// events, or logs.
	// +optional
	CredentialsSecretRef *corev1.LocalObjectReference `json:"credentialsSecretRef,omitempty"`
}

// StorageRequestSpec is a minimal, storage-class-agnostic volume request.
type StorageRequestSpec struct {
	// Size is the requested persistent volume size, e.g. "50Gi".
	// +kubebuilder:validation:Required
	Size string `json:"size"`

	// StorageClassName selects a specific StorageClass. Leave empty to use
	// the cluster default StorageClass.
	// +optional
	StorageClassName *string `json:"storageClassName,omitempty"`
}

// FilestoreAccessMode is the volume access mode for the Odoo filestore.
// +kubebuilder:validation:Enum=ReadWriteOnce;ReadWriteMany
type FilestoreAccessMode string

const (
	FilestoreAccessModeRWO FilestoreAccessMode = "ReadWriteOnce"
	FilestoreAccessModeRWX FilestoreAccessMode = "ReadWriteMany"
)

// FilestoreSpec configures Odoo's persistent attachment/document store
// (Odoo's `filestore` directory, normally under /var/lib/odoo/.local/share/Odoo/filestore).
//
// Trade-off (documented per platform requirements): Odoo does not
// coordinate filestore writes across processes by itself beyond simple
// file locking, and there is no cache-invalidation protocol between
// replicas. RWO is the safe default for the common single-writer
// (single-replica) deployment model. RWX is only safe when the
// StorageClass provides a genuinely shared, POSIX-consistent filesystem
// (e.g. NFS, CephFS, EFS) AND spec.replicas is intentionally set above 1.
// The operator does not silently allow this combination.
type FilestoreSpec struct {
	// Size is the requested persistent volume size, e.g. "100Gi".
	// +kubebuilder:validation:Required
	Size string `json:"size"`

	// StorageClassName selects a specific StorageClass. Leave empty to use
	// the cluster default StorageClass.
	// +optional
	StorageClassName *string `json:"storageClassName,omitempty"`

	// AccessMode is the PVC access mode. ReadWriteOnce (default) is
	// required whenever spec.replicas is 1, which is the recommended
	// topology for Odoo (see architecture docs: cron workers and the
	// filestore are effectively singletons). ReadWriteMany is only
	// accepted when the operator can also verify replicas > 1 is
	// intentional; the controller rejects RWO with replicas > 1 and warns
	// prominently (Degraded condition) rather than corrupting data.
	// +kubebuilder:default=ReadWriteOnce
	AccessMode FilestoreAccessMode `json:"accessMode,omitempty"`
}

// InstanceStorageSpec groups the persistent storage volumes for an instance.
type InstanceStorageSpec struct {
	// Filestore is the Odoo attachment/document store volume.
	Filestore FilestoreSpec `json:"filestore"`
}

// ImageSpec pins the exact, immutable container image used to run Odoo for
// this instance. The platform's build pipeline is expected to produce one
// immutable image per (Odoo version, addon set) combination; this CRD only
// ever references a fully-resolved image, it never triggers a build.
type ImageSpec struct {
	// Repository is the container image repository, e.g.
	// "registry.example.com/odoo-saas/odoo".
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Repository string `json:"repository"`

	// Tag is the immutable image tag to run. Using a mutable tag such as
	// "latest" is strongly discouraged; the validating webhook rejects it
	// unless the platform-wide `allowMutableTags` flag is set (development
	// clusters only).
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Tag string `json:"tag"`

	// PullPolicy is the image pull policy.
	// +kubebuilder:default=IfNotPresent
	PullPolicy corev1.PullPolicy `json:"pullPolicy,omitempty"`

	// PullSecretRefs references image pull secrets required by the
	// registry hosting this image, copied into the tenant namespace by the
	// controller if not already present there.
	// +optional
	PullSecretRefs []corev1.LocalObjectReference `json:"pullSecretRefs,omitempty"`
}

// TLSSpec configures TLS termination for the instance's public hostname.
type TLSSpec struct {
	// Enabled requests TLS termination for Domain.Hostname.
	//
	// Deliberately no `omitempty`: this field defaults to true (non-zero),
	// so a Go client (including this controller) that round-trips the
	// object through Get/Update with Enabled explicitly set to false would,
	// with omitempty, cause the JSON encoder to drop the key entirely
	// (Go's encoding/json treats bool-false as "empty"), and the API
	// server's CRD default would then silently re-flip it back to true.
	// See docs/architecture.md ("A CRD Defaulting Gotcha").
	// +kubebuilder:default=true
	Enabled bool `json:"enabled"`

	// SecretName is the name of the Secret (kubernetes.io/tls type) holding
	// the certificate to serve. When empty and IssuerRef is set, the
	// controller creates a cert-manager Certificate resource requesting a
	// certificate into a generated Secret name. When both are empty, TLS is
	// effectively disabled even if Enabled is true (surfaced as a
	// Degraded/False Ready condition, never silently ignored).
	// +optional
	SecretName string `json:"secretName,omitempty"`

	// IssuerRef references a cert-manager Issuer or ClusterIssuer used to
	// request a certificate automatically. Ignored when SecretName is set.
	// +optional
	IssuerRef *ObjectReference `json:"issuerRef,omitempty"`
}

// ObjectReference is a minimal, generic reference to a same-namespace or
// cluster-scoped object (name + kind + optional apiGroup), used where
// corev1.ObjectReference would be too permissive.
type ObjectReference struct {
	// Name of the referent.
	// +kubebuilder:validation:Required
	Name string `json:"name"`

	// Kind of the referent, e.g. "ClusterIssuer" or "Issuer".
	// +kubebuilder:validation:Required
	Kind string `json:"kind"`

	// Group is the API group of the referent, e.g. "cert-manager.io".
	// +optional
	Group string `json:"group,omitempty"`
}

// DomainSpec configures external access to the Odoo instance.
type DomainSpec struct {
	// Hostname is the fully-qualified public hostname routed to this
	// instance, e.g. "customer-acme.example.com". The controller creates a
	// Gateway API HTTPRoute (preferred) or Ingress (fallback, depending on
	// platform configuration) binding this hostname to the instance's
	// Service.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Pattern=`^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)+$`
	Hostname string `json:"hostname"`

	// TLS configures certificate termination for Hostname.
	// +optional
	TLS TLSSpec `json:"tls,omitempty"`
}

// WorkersSpec configures Odoo's internal process model. These are *not*
// Kubernetes pod replicas: Odoo's own prefork/gevent worker model runs
// multiple OS processes inside a single pod to serve concurrent HTTP
// requests, with one dedicated process for longpolling/gevent (live chat,
// bus) and a small pool of cron workers. See docs/architecture.md ("Odoo
// Worker Model") for the full explanation of how this interacts with
// spec.replicas and spec.autoscaling.
type WorkersSpec struct {
	// Count is the number of Odoo HTTP worker processes (`--workers`). Set
	// to 0 to run Odoo in single-threaded dev mode (not recommended for
	// customer-facing instances; the controller surfaces a warning
	// condition). Values above 0 automatically enable the dedicated
	// longpolling/gevent worker.
	//
	// Deliberately no `omitempty`: 0 is a legitimate, documented explicit
	// value here, but it is also Go's int32 zero value, so with omitempty
	// a client round-tripping this object with Count explicitly set to 0
	// would have the key dropped and the API server would silently
	// re-default it to 2. See TLSSpec.Enabled's comment for the general
	// pattern (docs/architecture.md, "A CRD Defaulting Gotcha").
	// +kubebuilder:default=2
	// +kubebuilder:validation:Minimum=0
	// +kubebuilder:validation:Maximum=32
	Count int32 `json:"count"`

	// MaxCronThreads is the number of Odoo cron worker threads
	// (`--max-cron-threads`). Cron workers run scheduled jobs (e.g.
	// invoicing, mail queues) and must not be scaled per pod replica: only
	// one pod in the deployment should run cron threads > 0 to avoid
	// duplicate job execution, which the controller enforces by pinning
	// cron execution to the first replica.
	//
	// Deliberately no `omitempty`; see Count's comment above for why an
	// explicit 0 must survive a Go client round-trip against a non-zero
	// default.
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=0
	// +kubebuilder:validation:Maximum=8
	MaxCronThreads int32 `json:"maxCronThreads"`
}

// AutoscalingSpec is reserved for future horizontal scaling of the Odoo
// workload. It is intentionally inert in this API version: Odoo's
// singleton cron workers and (by default) RWO filestore make naive HPA
// unsafe, so the controller validates this field but does not act on it
// until autoscaling-safe topologies (e.g. a split stateless web tier) are
// implemented. Enabling it today only reserves the field in status/events;
// see docs/architecture.md ("Scaling").
type AutoscalingSpec struct {
	// Enabled requests Horizontal Pod Autoscaling for the stateless web
	// tier. Not yet implemented by the controller; validated but rejected
	// by the webhook in this API version.
	// +kubebuilder:default=false
	Enabled bool `json:"enabled,omitempty"`

	// +optional
	MinReplicas *int32 `json:"minReplicas,omitempty"`
	// +optional
	MaxReplicas *int32 `json:"maxReplicas,omitempty"`
	// +optional
	TargetCPUUtilizationPercentage *int32 `json:"targetCPUUtilizationPercentage,omitempty"`
}

// BackupDestinationType selects where backup artifacts are stored.
// +kubebuilder:validation:Enum=PVC;ObjectStorage
type BackupDestinationType string

const (
	BackupDestinationPVC         BackupDestinationType = "PVC"
	BackupDestinationObjectStore BackupDestinationType = "ObjectStorage"
)

// BackupDestinationSpec configures where CronJob-driven backups are written.
type BackupDestinationSpec struct {
	// Type selects the storage backend for backup artifacts.
	// +kubebuilder:default=PVC
	Type BackupDestinationType `json:"type,omitempty"`

	// ObjectStorageSecretRef references a Secret containing object storage
	// credentials (e.g. S3-compatible endpoint/access key/secret key) when
	// Type is ObjectStorage. Never inlined into the spec.
	// +optional
	ObjectStorageSecretRef *corev1.LocalObjectReference `json:"objectStorageSecretRef,omitempty"`

	// Bucket is the target bucket/container name when Type is ObjectStorage.
	// +optional
	Bucket string `json:"bucket,omitempty"`

	// Prefix is an optional key prefix within Bucket, useful to namespace
	// backups by tenant when sharing a bucket across instances.
	// +optional
	Prefix string `json:"prefix,omitempty"`
}

// BackupSpec configures scheduled backups of the database and filestore.
// Database and filestore backups are coordinated by a single Kubernetes
// CronJob/Job pair per run: the Job first issues `pg_dump` against the
// instance's database (or a CloudNativePG barman-cloud plugin hook, when
// DatabaseMode is CloudNativePG), then archives the filestore PVC content,
// and finally writes both artifacts, plus a manifest tying them together
// by timestamp, to Destination. Restore consumes the same manifest so a
// database dump is never restored against a mismatched filestore snapshot.
type BackupSpec struct {
	// Enabled turns on scheduled backups for this instance.
	// +kubebuilder:default=false
	Enabled bool `json:"enabled,omitempty"`

	// Schedule is a standard cron expression, e.g. "0 2 * * *". Required
	// when Enabled is true; enforced by the controller (not by CRD schema
	// validation, since it is conditional on another field) — see
	// validateSpec in internal/controller.
	// +optional
	Schedule string `json:"schedule,omitempty"`

	// Retention is the number of most recent backups to keep; older
	// backups are pruned by the backup CronJob.
	// +kubebuilder:default=7
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=365
	Retention int32 `json:"retention,omitempty"`

	// Destination configures where backup artifacts are stored.
	// +optional
	Destination BackupDestinationSpec `json:"destination,omitempty"`
}

// RestoreSourceSpec identifies the pre-existing backup artifacts an
// instance should be restored from during onboarding. It deliberately
// reuses BackupDestinationSpec's storage abstraction (Type/
// ObjectStorageSecretRef/Bucket/Prefix — PVC or ObjectStorage, the exact
// same fields the backup CronJob already writes to, see BackupSpec)
// instead of inventing a second one: a restore source is, structurally,
// exactly where a backup destination already points.
type RestoreSourceSpec struct {
	BackupDestinationSpec `json:",inline"`

	// BackupID selects which timestamped backup within the destination to
	// restore — matching the directory name (Type=PVC) or object-key
	// prefix (Type=ObjectStorage) a backup run wrote, tied together by
	// that run's manifest.json (see BackupSpec). Leave empty to restore
	// the most recent backup found at the destination.
	// +optional
	BackupID string `json:"backupId,omitempty"`
}

// RestoreSpec provisions an OdooInstance from an existing database +
// filestore backup instead of a brand-new empty database. When set, the
// controller runs a one-time restore Job (pg_restore against the
// database, filestore archive extraction) in place of the ordinary
// `odoo-init` Job, gating the web Deployment on its success exactly the
// way `odoo-init` already gates it for a fresh instance — see
// internal/controller/restore.go.
//
// Immutable once set (enforced by the XValidation rule on
// OdooInstanceSpec.Restore below): editing spec.restore on a live instance
// must never be able to silently trigger a second, destructive restore
// against a database Odoo may already be serving customer traffic from.
// To restore a different backup, provision a new OdooInstance.
type RestoreSpec struct {
	// Source identifies the backup artifacts to restore from.
	// +kubebuilder:validation:Required
	Source RestoreSourceSpec `json:"source"`
}

// NetworkPolicySpec toggles the tenant-isolation NetworkPolicy.
type NetworkPolicySpec struct {
	// Enabled creates a default-deny NetworkPolicy for the tenant namespace
	// that only permits ingress from the shared gateway namespace and
	// egress to the instance's own database, DNS, and HTTPS (for outbound
	// email relays, payment webhooks, etc). Disabling this is strongly
	// discouraged outside of local development.
	//
	// Deliberately no `omitempty`; see TLSSpec.Enabled's comment for why a
	// default=true bool must always be serialized explicitly.
	// +kubebuilder:default=true
	Enabled bool `json:"enabled"`
}

// NetworkingSpec groups tenant networking configuration.
type NetworkingSpec struct {
	// NetworkPolicy configures tenant network isolation.
	// +optional
	NetworkPolicy NetworkPolicySpec `json:"networkPolicy,omitempty"`

	// RouteAnnotations are copied verbatim onto the generated
	// HTTPRoute/Ingress object, e.g. for provider-specific routing
	// behavior. The operator does not interpret these values.
	// +optional
	RouteAnnotations map[string]string `json:"routeAnnotations,omitempty"`
}

// TenancySpec configures the isolated namespace provisioned for this
// instance and the guardrails applied inside it.
type TenancySpec struct {
	// NamespaceOverride sets an explicit target namespace name instead of
	// the default derived name (`odoo-tenant-<instance name>`). Immutable
	// once set: moving a live tenant between namespaces is not supported.
	// +optional
	// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="tenancy.namespaceOverride is immutable"
	NamespaceOverride string `json:"namespaceOverride,omitempty"`

	// ResourceQuota caps the total compute/storage/object count the tenant
	// namespace may consume, independent of what this single OdooInstance
	// requests. Defaults are applied by the operator when omitted; see
	// config/samples for platform-recommended defaults.
	// +optional
	ResourceQuota *corev1.ResourceList `json:"resourceQuota,omitempty"`
}

// OdooInstanceSpec defines the desired state of an Odoo SaaS instance. It is
// the entire contract between the SaaS control-plane API and Kubernetes:
// the API only ever creates/updates/deletes this object and reads back
// OdooInstanceStatus. It never creates Deployments, Services, PVCs,
// Secrets, or Jobs directly.
type OdooInstanceSpec struct {
	// Version is the Odoo major version for this instance, e.g. "17.0",
	// "18.0", "19.0". Changing Version triggers a controlled image update;
	// see docs/architecture.md ("Upgrade Strategy") for how this differs
	// from a routine image tag bump.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Pattern=`^[0-9]+\.0$`
	Version string `json:"version"`

	// Image is the immutable, fully-resolved container image to run.
	Image ImageSpec `json:"image"`

	// Domain configures external access and TLS.
	Domain DomainSpec `json:"domain"`

	// Database configures the PostgreSQL backend. See DatabaseMode.
	// +kubebuilder:default={"mode":"Managed","version":"16"}
	Database DatabaseSpec `json:"database,omitempty"`

	// Storage configures persistent volumes owned by this instance.
	Storage InstanceStorageSpec `json:"storage"`

	// Resources are the compute resource requests/limits applied to the
	// Odoo container. Requests and limits are both required by the
	// validating webhook to keep tenant capacity planning predictable.
	// +kubebuilder:validation:Required
	Resources corev1.ResourceRequirements `json:"resources"`

	// Addons is a declarative record of the addon set this instance's
	// image was built with. It is informational/validation metadata only:
	// addons are baked into Image at build time by the platform's image
	// pipeline, never installed or downloaded at runtime. See
	// docs/architecture.md ("Addons & Image Strategy").
	// +optional
	Addons []AddonSpec `json:"addons,omitempty"`

	// Workers configures Odoo's internal process model.
	// +kubebuilder:default={"count":2,"maxCronThreads":1}
	Workers WorkersSpec `json:"workers,omitempty"`

	// Replicas is the number of Odoo pod replicas. Defaults to 1, which is
	// the recommended topology (see WorkersSpec and FilestoreSpec docs).
	// Values greater than 1 require storage.filestore.accessMode set to
	// ReadWriteMany; the webhook rejects the combination otherwise.
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=10
	// +optional
	Replicas *int32 `json:"replicas,omitempty"`

	// Autoscaling is reserved for future use; see AutoscalingSpec.
	// +optional
	Autoscaling AutoscalingSpec `json:"autoscaling,omitempty"`

	// Backup configures scheduled database/filestore backups.
	// +optional
	Backup BackupSpec `json:"backup,omitempty"`

	// Restore, when set, provisions this instance from an existing
	// database/filestore backup instead of a new empty database (see
	// RestoreSpec). Leave unset for the normal new-instance flow, which
	// remains entirely unchanged.
	//
	// Immutable after creation (whatever value — nil or set — this field
	// has when the OdooInstance is first created is frozen for its
	// lifetime; matches the same "self == oldSelf" pattern used by
	// TenancySpec.NamespaceOverride, since CRD transition rules referencing
	// oldSelf only evaluate on UPDATE, never CREATE): an update that sets,
	// changes, or clears spec.restore after creation is rejected outright,
	// so this can never silently become a "restore again" request against
	// a database Odoo may already be serving traffic from.
	// +optional
	// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="spec.restore is immutable once set"
	Restore *RestoreSpec `json:"restore,omitempty"`

	// Networking configures tenant network isolation and routing.
	// +optional
	Networking NetworkingSpec `json:"networking,omitempty"`

	// Tenancy configures the isolated namespace provisioned for this
	// instance.
	// +optional
	Tenancy TenancySpec `json:"tenancy,omitempty"`

	// Suspended pauses the instance without deleting any data: the
	// controller scales the Odoo workload to zero replicas, keeps the
	// database/filestore/backups intact, and reports phase "Suspended".
	// Intended for billing suspension flows.
	// +kubebuilder:default=false
	// +optional
	Suspended bool `json:"suspended,omitempty"`
}

// AddonSpec records one addon expected to be present in Image.
type AddonSpec struct {
	// Name is the Odoo addon/module technical name, e.g. "sale", "account".
	// +kubebuilder:validation:Required
	Name string `json:"name"`
}

// OdooInstancePhase is a coarse, human/API-friendly summary of
// OdooInstanceStatus.Conditions, intended so the SaaS API can branch on a
// single field instead of interpreting condition lists.
type OdooInstancePhase string

const (
	PhasePending      OdooInstancePhase = "Pending"
	PhaseProvisioning OdooInstancePhase = "Provisioning"
	PhaseReady        OdooInstancePhase = "Ready"
	PhaseUpdating     OdooInstancePhase = "Updating"
	PhaseDegraded     OdooInstancePhase = "Degraded"
	PhaseFailed       OdooInstancePhase = "Failed"
	PhaseSuspended    OdooInstancePhase = "Suspended"
	PhaseDeleting     OdooInstancePhase = "Deleting"
)

// Condition types set by the controller on OdooInstance.status.conditions.
const (
	// ConditionReady is True only when the instance is fully reconciled and
	// serving traffic.
	ConditionReady = "Ready"
	// ConditionProgressing is True while the controller is actively working
	// towards the desired state (initial provisioning or an update).
	ConditionProgressing = "Progressing"
	// ConditionDegraded is True when the instance is running but not in the
	// desired state (e.g. a child resource failing health checks).
	ConditionDegraded = "Degraded"
	// ConditionDatabaseReady reflects readiness of the database dependency.
	ConditionDatabaseReady = "DatabaseReady"
	// ConditionStorageReady reflects readiness/binding of PVCs.
	ConditionStorageReady = "StorageReady"
	// ConditionRouteReady reflects readiness of the HTTPRoute/Ingress and,
	// when applicable, the TLS certificate.
	ConditionRouteReady = "RouteReady"
	// ConditionWorkloadReady reflects readiness of the Odoo Deployment(s).
	ConditionWorkloadReady = "WorkloadReady"
	// ConditionRestoreReady reflects completion of a one-time
	// restore-from-backup operation. Only ever set when spec.restore is
	// configured; a fresh, non-restored instance never carries this
	// condition at all (unlike DatabaseReady/StorageReady/WorkloadReady,
	// which always apply).
	ConditionRestoreReady = "RestoreReady"
)

// OdooInstanceStatus is the observed state of an OdooInstance, and the only
// thing the SaaS API needs to read to know whether/how an instance is
// usable. It never contains secret values.
type OdooInstanceStatus struct {
	// ObservedGeneration is the .metadata.generation last reconciled by the
	// controller. Compare to .metadata.generation to know whether Status
	// reflects the latest Spec.
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// Phase is a coarse summary of Conditions; see OdooInstancePhase.
	// +optional
	Phase OdooInstancePhase `json:"phase,omitempty"`

	// Conditions are the detailed, machine-readable status of the
	// instance's reconciliation, following the standard Kubernetes
	// conditions convention.
	// +optional
	// +patchMergeKey=type
	// +patchStrategy=merge
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty" patchStrategy:"merge" patchMergeKey:"type"`

	// URL is the fully-qualified external URL of the instance once ready,
	// e.g. "https://customer-acme.example.com".
	// +optional
	URL string `json:"url,omitempty"`

	// TenantNamespace is the namespace the controller provisioned (or
	// reused, when spec.tenancy.namespaceOverride is set) for this
	// instance's child resources.
	// +optional
	TenantNamespace string `json:"tenantNamespace,omitempty"`

	// AdminCredentialsSecretName names the Secret (in TenantNamespace)
	// holding the generated Odoo master/admin credentials. Never the
	// credentials themselves.
	// +optional
	AdminCredentialsSecretName string `json:"adminCredentialsSecretName,omitempty"`

	// DatabaseCredentialsSecretName names the Secret (in TenantNamespace)
	// holding database connection credentials. Never the credentials
	// themselves.
	// +optional
	DatabaseCredentialsSecretName string `json:"databaseCredentialsSecretName,omitempty"`

	// ObservedImage is the container image currently running, which may
	// lag Spec.Image during a controlled rollout.
	// +optional
	ObservedImage string `json:"observedImage,omitempty"`

	// Replicas is the total number of non-terminated Odoo pods targeted by
	// this instance's Deployment.
	// +optional
	Replicas int32 `json:"replicas,omitempty"`

	// ReadyReplicas is the number of those pods currently Ready.
	// +optional
	ReadyReplicas int32 `json:"readyReplicas,omitempty"`

	// LastBackupTime records completion of the most recent successful
	// backup Job, when Backup.Enabled.
	// +optional
	LastBackupTime *metav1.Time `json:"lastBackupTime,omitempty"`

	// LastBackupStatus is a short human-readable outcome of the most recent
	// backup Job attempt, e.g. "Succeeded" or "Failed".
	// +optional
	LastBackupStatus string `json:"lastBackupStatus,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Cluster,shortName=odoo
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="Version",type=string,JSONPath=`.spec.version`
// +kubebuilder:printcolumn:name="URL",type=string,JSONPath=`.status.url`
// +kubebuilder:printcolumn:name="Ready",type=string,JSONPath=`.status.conditions[?(@.type=="Ready")].status`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`
//
// OdooInstance is the single Kubernetes API a SaaS control plane needs to
// provision, update, scale, upgrade, and delete a customer Odoo instance.
// It is deliberately cluster-scoped: creating one causes the controller to
// provision a dedicated, isolated tenant namespace (namespace-per-tenant)
// rather than requiring the SaaS API to pre-create and manage namespaces
// itself. See docs/architecture.md ("Multi-Tenancy Model") for the
// trade-offs behind this decision.
type OdooInstance struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   OdooInstanceSpec   `json:"spec"`
	Status OdooInstanceStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
//
// OdooInstanceList contains a list of OdooInstance.
type OdooInstanceList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []OdooInstance `json:"items"`
}

func init() {
	SchemeBuilder.Register(&OdooInstance{}, &OdooInstanceList{})
}
