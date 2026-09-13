package resources

import (
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

// CloudNativePG integration is intentionally built on the dynamic client
// (unstructured), exactly like the cert-manager Certificate: it keeps the
// operator's compiled dependency graph free of a second, independently
// versioned Kubernetes-adjacent API, and lets the same OdooInstance CRD
// support "no CloudNativePG installed" clusters without a broken import.
const (
	cnpgAPIVersion = "postgresql.cnpg.io/v1"
	cnpgKind       = "Cluster"
)

// CloudNativePGCluster builds a CloudNativePG `Cluster` resource as the
// managed database for DatabaseModeCloudNativePG. This is the production
// upgrade path from DatabaseModeManaged: same OdooInstance API field
// (spec.database.mode), a real HA/PITR-capable operator underneath.
//
// The Cluster's connection Secret is created by CloudNativePG itself
// (named "<cluster-name>-app"); the controller reads it back to populate
// this instance's own DatabaseSecretName, so Odoo always consumes
// credentials through the same Secret shape regardless of database mode.
func CloudNativePGCluster(instance *saasv1alpha1.OdooInstance) *unstructured.Unstructured {
	sizeStr := "20Gi"
	var storageClass string
	if instance.Spec.Database.Storage != nil {
		sizeStr = instance.Spec.Database.Storage.Size
		if instance.Spec.Database.Storage.StorageClassName != nil {
			storageClass = *instance.Spec.Database.Storage.StorageClassName
		}
	}

	u := &unstructured.Unstructured{}
	u.SetAPIVersion(cnpgAPIVersion)
	u.SetKind(cnpgKind)
	u.SetName(DatabaseStatefulSetName(instance))
	u.SetNamespace(TenantNamespace(instance))
	u.SetLabels(WithComponent(instance, "database"))

	storage := map[string]interface{}{"size": sizeStr}
	if storageClass != "" {
		storage["storageClass"] = storageClass
	}

	spec := map[string]interface{}{
		"instances":             int64(1),
		"imageName":             "ghcr.io/cloudnative-pg/postgresql:" + instance.Spec.Database.Version,
		"storage":               storage,
		"bootstrap":             map[string]interface{}{"initdb": map[string]interface{}{"database": OdooDatabaseName(instance), "owner": "odoo"}},
		"enableSuperuserAccess": false,
	}
	_ = unstructured.SetNestedMap(u.Object, spec, "spec")
	return u
}

// CloudNativePGConnectionSecretName is the name of the Secret CloudNativePG
// generates with application connection credentials for the Cluster built
// above (CloudNativePG's `<cluster>-app` naming convention).
func CloudNativePGConnectionSecretName(instance *saasv1alpha1.OdooInstance) string {
	return DatabaseStatefulSetName(instance) + "-app"
}
