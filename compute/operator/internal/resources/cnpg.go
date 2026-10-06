package resources

import (
	corev1 "k8s.io/api/core/v1"
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
// (spec.database.mode), a single-instance PostgreSQL operator underneath.
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
	u.SetAnnotations(map[string]string{"cnpg.io/hibernation": "off"})

	storage := map[string]interface{}{"size": sizeStr}
	if storageClass != "" {
		storage["storageClass"] = storageClass
	}

	bootstrap := map[string]interface{}{"initdb": map[string]interface{}{"database": OdooDatabaseName(instance), "owner": "odoo"}}
	spec := map[string]interface{}{
		"instances":             int64(1),
		"imageName":             "ghcr.io/cloudnative-pg/postgresql:" + instance.Spec.Database.Version,
		"storage":               storage,
		"bootstrap":             bootstrap,
		"enableSuperuserAccess": false,
		// Live, middle-of-the-road defaults that keep connections alive
		// across long tenant operations instead of the austere upstream
		// defaults that silently reap them:
		//  - idle_in_transaction_session_timeout actually reaps orphaned
		//    transactions after the request time limit (those otherwise
		//    hold locks and get inherited as phantom "connection lost").
		//  - idle_session_timeout=0: Odoo/psycopg intentionally keeps idle
		//    clients in its per-worker pool; letting the server kills them
		//    makes the next query fail(reconnect churn).
		//  - TCP keepalives make dead peers fail fast (60s idle, 15s
		//    interval, 5 pings) rather than up to hours of IEC-TC
		//    (Ethernet) half-open sessions.
		//  - statement_timeout stays 0 — the request-time ceiling lives on
		//    the Odoo side; imposing it mid-report would slay queries a
		//    tenant is owed.
		//  - max_connections places room for web+cron of every replica
		//    plus per-DB restore/admin connections beyond the often-tight
		//    upstream 100.
		"postgresql": map[string]interface{}{"parameters": map[string]interface{}{
			"idle_in_transaction_session_timeout": "1800s",
			"tcp_keepalives_idle":                 "60",
			"tcp_keepalives_interval":             "15",
			"tcp_keepalives_count":                "5",
			"statement_timeout":                   "0",
			"max_connections":                     "200",
		}},
	}
	if r := instance.Spec.Database.Resources; r != nil {
		spec["resources"] = resourcesToMap(*r)
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

// resourcesToMap renders ResourceRequirements for an unstructured object.
func resourcesToMap(r corev1.ResourceRequirements) map[string]interface{} {
	out := map[string]interface{}{}
	for key, list := range map[string]corev1.ResourceList{"requests": r.Requests, "limits": r.Limits} {
		if len(list) == 0 {
			continue
		}
		m := map[string]interface{}{}
		for name, q := range list {
			m[string(name)] = q.String()
		}
		out[key] = m
	}
	return out
}
