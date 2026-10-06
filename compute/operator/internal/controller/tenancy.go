package controller

import (
	"context"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	"sigs.k8s.io/controller-runtime/pkg/client"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
	"github.com/freightright/odoo-saas-platform/operator/internal/resources"
)

// reconcileTenancy ensures the isolated tenant namespace and its guardrails
// (ResourceQuota, LimitRange, ServiceAccount) exist. This is the first
// reconciliation step: nothing else in this file can be applied before its
// target namespace exists.
func (r *OdooInstanceReconciler) reconcileTenancy(ctx context.Context, instance *saasv1alpha1.OdooInstance) error {
	ns := resources.Namespace(instance)
	setOwner(instance, ns)
	if err := r.apply(ctx, ns); err != nil {
		return err
	}

	sa := resources.ServiceAccount(instance)
	setOwner(instance, sa)
	if err := r.apply(ctx, sa); err != nil {
		return err
	}

	quota := resources.ResourceQuota(instance)
	// Existing two-volume tenants and downgrades retain their larger PVCs.
	// Never lower the ceiling below provisioned storage: doing so would
	// prevent unrelated resizes and recovery while their data is retained.
	if hard, ok := quota.Spec.Hard[corev1.ResourceRequestsStorage]; ok {
		var claims corev1.PersistentVolumeClaimList
		if err := r.List(ctx, &claims, client.InNamespace(ns.Name)); err != nil {
			return err
		}
		legacyAllowance := resource.Quantity{}
		if !instance.Spec.Storage.SharedWithDatabase {
			legacyAllowance, _ = resource.ParseQuantity(instance.Spec.Storage.Filestore.Size)
		}
		preserveStorageQuota(quota, claims.Items, hard, legacyAllowance)
	}
	setOwner(instance, quota)
	if err := r.apply(ctx, quota); err != nil {
		return err
	}

	limits := resources.LimitRange(instance)
	setOwner(instance, limits)
	return r.apply(ctx, limits)
}

// PVCs cannot shrink. Preserve room for existing data and the one-GiB
// auxiliary claim when a legacy layout or plan downgrade retains more.
func preserveStorageQuota(quota *corev1.ResourceQuota, claims []corev1.PersistentVolumeClaim, hard resource.Quantity, legacyAllowance resource.Quantity) {
	// Legacy layouts still grow two claims on upgrades until migrated.
	// Keep larger explicit quotas intact rather than doubling them again.
	if legacyAllowance.Sign() > 0 {
		desired := legacyAllowance.DeepCopy()
		desired.Add(legacyAllowance)
		desired.Add(resource.MustParse("1Gi"))
		if hard.Cmp(desired) < 0 {
			hard = desired
			quota.Spec.Hard = quota.Spec.Hard.DeepCopy()
			quota.Spec.Hard[corev1.ResourceRequestsStorage] = hard
		}
	}
	minimum := resource.MustParse("1Gi")
	for _, claim := range claims {
		minimum.Add(claim.Spec.Resources.Requests[corev1.ResourceStorage])
	}
	if hard.Cmp(minimum) < 0 {
		quota.Spec.Hard = quota.Spec.Hard.DeepCopy()
		quota.Spec.Hard[corev1.ResourceRequestsStorage] = minimum
	}
}
