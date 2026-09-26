// Package resources builds the desired-state Kubernetes objects for an
// OdooInstance. Every function here is a pure function: (instance) ->
// (desired object). None of them talk to the Kubernetes API. The
// controller (internal/controller) is solely responsible for diffing these
// desired objects against live cluster state and applying the difference,
// which keeps reconciliation idempotent and the resource-shape logic unit
// testable without envtest.
package resources

import (
	"fmt"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

// TenantNamespace returns the namespace this instance's child resources
// live in.
func TenantNamespace(instance *saasv1alpha1.OdooInstance) string {
	if instance.Spec.Tenancy.NamespaceOverride != "" {
		return instance.Spec.Tenancy.NamespaceOverride
	}
	return saasv1alpha1.NamespacePrefix + instance.Name
}

// OdooDeploymentName is the name of the Odoo Deployment.
func OdooDeploymentName(instance *saasv1alpha1.OdooInstance) string {
	return "odoo"
}

// OdooServiceName is the name of the Odoo Service.
func OdooServiceName(instance *saasv1alpha1.OdooInstance) string {
	return "odoo"
}

// OdooConfigMapName is the name of the Odoo configuration ConfigMap.
func OdooConfigMapName(instance *saasv1alpha1.OdooInstance) string {
	return "odoo-config"
}

// OdooUpdateConfigMapName is the ConfigMap the update Job renders its
// odoo.conf from (see OdooUpdateConfigMap).
func OdooUpdateConfigMapName(instance *saasv1alpha1.OdooInstance) string {
	return "odoo-config-update"
}

// OdooServiceAccountName is the name of the ServiceAccount the Odoo pod
// runs as.
func OdooServiceAccountName(instance *saasv1alpha1.OdooInstance) string {
	return "odoo"
}

// AdminSecretName is the name of the Secret holding generated Odoo
// master-password/admin credentials.
func AdminSecretName(instance *saasv1alpha1.OdooInstance) string {
	return "odoo-admin-credentials"
}

// DatabaseSecretName is the name of the Secret holding database connection
// credentials, honoring an explicit override.
func DatabaseSecretName(instance *saasv1alpha1.OdooInstance) string {
	if instance.Spec.Database.CredentialsSecretRef != nil && instance.Spec.Database.CredentialsSecretRef.Name != "" {
		return instance.Spec.Database.CredentialsSecretRef.Name
	}
	return "odoo-database-credentials"
}

// DatabaseStatefulSetName is the name of the managed PostgreSQL workload.
func DatabaseStatefulSetName(instance *saasv1alpha1.OdooInstance) string {
	return "postgresql"
}

// DatabaseServiceName is the name of the managed PostgreSQL Service.
func DatabaseServiceName(instance *saasv1alpha1.OdooInstance) string {
	return "postgresql"
}

// FilestorePVCName is the name of the Odoo filestore PVC.
func FilestorePVCName(instance *saasv1alpha1.OdooInstance) string {
	return "odoo-filestore"
}

// DatabasePVCName is the name of the managed PostgreSQL data PVC (used only
// as a volumeClaimTemplate name inside the StatefulSet).
func DatabasePVCName(instance *saasv1alpha1.OdooInstance) string {
	return "postgresql-data"
}

// HTTPRouteName is the name of the Gateway API HTTPRoute.
func HTTPRouteName(instance *saasv1alpha1.OdooInstance) string {
	return "odoo"
}

// IngressName is the name of the fallback Ingress.
func IngressName(instance *saasv1alpha1.OdooInstance) string {
	return "odoo"
}

// CertificateName is the name of the cert-manager Certificate.
func CertificateName(instance *saasv1alpha1.OdooInstance) string {
	return "odoo-tls"
}

// NetworkPolicyName is the name of the tenant-isolation NetworkPolicy.
func NetworkPolicyName(instance *saasv1alpha1.OdooInstance) string {
	return "odoo-tenant-isolation"
}

// BackupCronJobName is the name of the scheduled backup CronJob.
func BackupCronJobName(instance *saasv1alpha1.OdooInstance) string {
	return "odoo-backup"
}

// PodDisruptionBudgetName is the name of the Odoo PodDisruptionBudget.
func PodDisruptionBudgetName(instance *saasv1alpha1.OdooInstance) string {
	return "odoo"
}

// ResourceQuotaName is the name of the tenant namespace ResourceQuota.
func ResourceQuotaName(instance *saasv1alpha1.OdooInstance) string {
	return "odoo-tenant-quota"
}

// LimitRangeName is the name of the tenant namespace LimitRange.
func LimitRangeName(instance *saasv1alpha1.OdooInstance) string {
	return "odoo-tenant-limits"
}

// CommonLabels returns the labels every child resource of this instance
// must carry, used both when building desired objects and when listing
// existing ones for drift detection.
func CommonLabels(instance *saasv1alpha1.OdooInstance) map[string]string {
	return map[string]string{
		"app.kubernetes.io/name":       "odoo",
		"app.kubernetes.io/instance":   instance.Name,
		saasv1alpha1.LabelManagedBy:    saasv1alpha1.ManagedByValue,
		saasv1alpha1.LabelInstanceName: instance.Name,
	}
}

// SelectorLabels returns the stable subset of CommonLabels safe to use as a
// Service/Deployment selector (must never change for the lifetime of the
// instance).
func SelectorLabels(instance *saasv1alpha1.OdooInstance) map[string]string {
	return map[string]string{
		"app.kubernetes.io/name":       "odoo",
		"app.kubernetes.io/instance":   instance.Name,
		saasv1alpha1.LabelInstanceName: instance.Name,
	}
}

// WithComponent returns a copy of CommonLabels annotated with a component
// name, e.g. "database" or "backup".
func WithComponent(instance *saasv1alpha1.OdooInstance, component string) map[string]string {
	l := CommonLabels(instance)
	l[saasv1alpha1.LabelComponent] = component
	return l
}

// OdooDatabaseName is the PostgreSQL database name Odoo connects to. Using
// a fixed logical name ("odoo") is safe because every tenant has its own
// database server/namespace already; External mode instances instead take
// the name embedded in their credentials Secret.
func OdooDatabaseName(instance *saasv1alpha1.OdooInstance) string {
	return "odoo"
}

// DatabaseFQDN returns the in-cluster DNS name of the managed PostgreSQL
// Service.
func DatabaseFQDN(instance *saasv1alpha1.OdooInstance) string {
	return fmt.Sprintf("%s.%s.svc.cluster.local", DatabaseServiceName(instance), TenantNamespace(instance))
}
