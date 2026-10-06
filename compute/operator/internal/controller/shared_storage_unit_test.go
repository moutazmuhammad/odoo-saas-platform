package controller

import (
	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	"testing"
)

func TestSharedStorageRejectsExternalAndCNPG(t *testing.T) {
	r := &OdooInstanceReconciler{}
	for _, mode := range []saasv1alpha1.DatabaseMode{saasv1alpha1.DatabaseModeExternal, saasv1alpha1.DatabaseModeCloudNativePG} {
		instance := validSpecInstance()
		instance.Spec.Storage.SharedWithDatabase = true
		instance.Spec.Database.Mode = mode
		if err := r.validateSpec(instance); err == nil || err.reason != "SharedStorageRequiresManagedDatabase" {
			t.Fatal("shared volume would leave database data on an incompatible backend")
		}
	}
}

func TestQuotaPreservesLegacyAndDowngradedVolumes(t *testing.T) {
	for _, tc := range []struct {
		sizes  []string
		shared bool
		want   string
	}{{[]string{"10Gi", "10Gi"}, false, "21Gi"}, {[]string{"10Gi"}, true, "11Gi"}, {[]string{"20Gi"}, true, "21Gi"}, {nil, true, "11Gi"}, {nil, false, "21Gi"}} {
		hard := resource.MustParse("11Gi")
		original := corev1.ResourceList{corev1.ResourceRequestsStorage: hard}
		quota := &corev1.ResourceQuota{Spec: corev1.ResourceQuotaSpec{Hard: original}}
		var claims []corev1.PersistentVolumeClaim
		for _, size := range tc.sizes {
			claims = append(claims, corev1.PersistentVolumeClaim{Spec: corev1.PersistentVolumeClaimSpec{Resources: corev1.VolumeResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceStorage: resource.MustParse(size)}}}})
		}
		legacyAllowance := resource.Quantity{}
		if !tc.shared {
			legacyAllowance = resource.MustParse("10Gi")
		}
		preserveStorageQuota(quota, claims, hard, legacyAllowance)
		got := quota.Spec.Hard[corev1.ResourceRequestsStorage]
		if got.String() != tc.want {
			t.Fatalf("quota=%s, want %s", got.String(), tc.want)
		}
		originalHard := original[corev1.ResourceRequestsStorage]
		if originalHard.String() != "11Gi" {
			t.Fatal("quota adjustment mutated the instance's desired resource map")
		}
	}
}
