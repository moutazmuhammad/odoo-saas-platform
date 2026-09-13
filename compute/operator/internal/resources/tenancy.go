package resources

import (
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

// Namespace builds the isolated tenant namespace for this instance. It is
// intentionally the only cluster-scoped child resource the controller
// creates directly; every other child resource lives inside it and is
// therefore automatically garbage-collected on namespace deletion.
//
// Pod Security Standards are enforced at the "restricted" level via
// namespace labels, matching the hardened PodSpec the Odoo Deployment
// generates (see deployment.go).
func Namespace(instance *saasv1alpha1.OdooInstance) *corev1.Namespace {
	return &corev1.Namespace{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "Namespace"},
		ObjectMeta: metav1.ObjectMeta{
			Name: TenantNamespace(instance),
			Labels: mergeLabels(CommonLabels(instance), map[string]string{
				"pod-security.kubernetes.io/enforce": "restricted",
				"pod-security.kubernetes.io/audit":   "restricted",
				"pod-security.kubernetes.io/warn":    "restricted",
			}),
		},
	}
}

// ServiceAccount builds the minimally-privileged ServiceAccount the Odoo
// pod runs as. It is granted no RBAC permissions at all: Odoo does not
// need to talk to the Kubernetes API.
func ServiceAccount(instance *saasv1alpha1.OdooInstance) *corev1.ServiceAccount {
	automount := false
	return &corev1.ServiceAccount{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ServiceAccount"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      OdooServiceAccountName(instance),
			Namespace: TenantNamespace(instance),
			Labels:    CommonLabels(instance),
		},
		AutomountServiceAccountToken: &automount,
	}
}

// defaultTenantQuota is applied when spec.tenancy.resourceQuota is omitted,
// giving every tenant namespace a hard ceiling even if the SaaS API forgets
// to set one explicitly.
var defaultTenantQuota = corev1.ResourceList{
	corev1.ResourceRequestsCPU:    resource.MustParse("8"),
	corev1.ResourceRequestsMemory: resource.MustParse("16Gi"),
	corev1.ResourceLimitsCPU:      resource.MustParse("16"),
	corev1.ResourceLimitsMemory:   resource.MustParse("32Gi"),
	corev1.ResourcePods:           resource.MustParse("20"),
	"requests.storage":            resource.MustParse("500Gi"),
}

// ResourceQuota builds the tenant namespace's ResourceQuota, capping total
// consumption regardless of what a single OdooInstance requests. This
// bounds the blast radius of a misconfigured or compromised tenant.
func ResourceQuota(instance *saasv1alpha1.OdooInstance) *corev1.ResourceQuota {
	quota := defaultTenantQuota
	if instance.Spec.Tenancy.ResourceQuota != nil {
		quota = *instance.Spec.Tenancy.ResourceQuota
	}
	return &corev1.ResourceQuota{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ResourceQuota"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      ResourceQuotaName(instance),
			Namespace: TenantNamespace(instance),
			Labels:    CommonLabels(instance),
		},
		Spec: corev1.ResourceQuotaSpec{
			Hard: quota,
		},
	}
}

// LimitRange builds a default LimitRange so every container in the tenant
// namespace has sane implicit requests/limits even if a future child
// resource forgets to set them explicitly.
func LimitRange(instance *saasv1alpha1.OdooInstance) *corev1.LimitRange {
	return &corev1.LimitRange{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "LimitRange"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      LimitRangeName(instance),
			Namespace: TenantNamespace(instance),
			Labels:    CommonLabels(instance),
		},
		Spec: corev1.LimitRangeSpec{
			Limits: []corev1.LimitRangeItem{
				{
					Type: corev1.LimitTypeContainer,
					Default: corev1.ResourceList{
						corev1.ResourceCPU:    resource.MustParse("500m"),
						corev1.ResourceMemory: resource.MustParse("512Mi"),
					},
					DefaultRequest: corev1.ResourceList{
						corev1.ResourceCPU:    resource.MustParse("100m"),
						corev1.ResourceMemory: resource.MustParse("128Mi"),
					},
				},
			},
		},
	}
}

func mergeLabels(base map[string]string, extra map[string]string) map[string]string {
	out := make(map[string]string, len(base)+len(extra))
	for k, v := range base {
		out[k] = v
	}
	for k, v := range extra {
		out[k] = v
	}
	return out
}
