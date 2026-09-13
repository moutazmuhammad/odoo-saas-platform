package controller

import (
	"context"

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
	setOwner(instance, quota)
	if err := r.apply(ctx, quota); err != nil {
		return err
	}

	limits := resources.LimitRange(instance)
	setOwner(instance, limits)
	return r.apply(ctx, limits)
}
