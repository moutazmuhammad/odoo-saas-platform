package controller

import (
	"context"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	policyv1 "k8s.io/api/policy/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
	"github.com/freightright/odoo-saas-platform/operator/internal/resources"
)

// reconcileStorage ensures the Odoo filestore PVC exists and reports
// whether it is Bound.
func (r *OdooInstanceReconciler) reconcileStorage(ctx context.Context, instance *saasv1alpha1.OdooInstance) (bool, error) {
	pvc, err := resources.FilestorePVC(instance)
	if err != nil {
		return false, err
	}
	setOwner(instance, pvc)
	// Volumes never shrink: a smaller spec size (e.g. a plan downgrade)
	// keeps the live size instead of failing the apply.
	var current corev1.PersistentVolumeClaim
	growing := false
	if err := r.Get(ctx, types.NamespacedName{Namespace: pvc.Namespace, Name: pvc.Name}, &current); err == nil {
		have := current.Spec.Resources.Requests[corev1.ResourceStorage]
		want := pvc.Spec.Resources.Requests[corev1.ResourceStorage]
		if have.Cmp(want) > 0 {
			pvc.Spec.Resources.Requests[corev1.ResourceStorage] = have
		}
		growing = want.Cmp(have) > 0
	} else if !apierrors.IsNotFound(err) {
		return false, err
	}
	if err := r.apply(ctx, pvc); err != nil {
		if !growing {
			return false, err
		}
		// The expansion was refused (e.g. not enough schedulable space):
		// keep the current size, report it, and let the rest proceed.
		r.warnVolumeNotGrown(ctx, instance, err)
		pvc.Spec.Resources.Requests[corev1.ResourceStorage] = current.Spec.Resources.Requests[corev1.ResourceStorage]
		if err := r.apply(ctx, pvc); err != nil {
			return false, err
		}
	}

	var live corev1.PersistentVolumeClaim
	if err := r.Get(ctx, types.NamespacedName{Namespace: pvc.Namespace, Name: pvc.Name}, &live); err != nil {
		if apierrors.IsNotFound(err) {
			return false, nil
		}
		return false, err
	}
	return live.Status.Phase == corev1.ClaimBound, nil
}

// reconcileAdminSecret ensures the Odoo master-password Secret exists,
// preserving any value already present (see existingOrNewPassword).
func (r *OdooInstanceReconciler) reconcileAdminSecret(ctx context.Context, instance *saasv1alpha1.OdooInstance) error {
	ns, name := resources.TenantNamespace(instance), resources.AdminSecretName(instance)
	password, err := r.existingOrNewPassword(ctx, ns, name, "master-password", 32)
	if err != nil {
		return err
	}
	dbmKey, err := r.existingOrNewPassword(ctx, ns, name, "dbmanager-key", 48)
	if err != nil {
		return err
	}
	secret := resources.AdminSecret(instance, resources.AdminSecretData{MasterPassword: password, DBManagerKey: dbmKey})
	setOwner(instance, secret)
	return r.apply(ctx, secret)
}

// reconcileConfig applies the odoo.conf ConfigMap. Split out from
// reconcileWorkload because the database-init Job (see
// internal/controller/init_job.go) also depends on it and must run before
// the web Deployment is allowed to exist — see reconcileWorkload's doc
// comment.
func (r *OdooInstanceReconciler) reconcileConfig(ctx context.Context, instance *saasv1alpha1.OdooInstance) error {
	cm := resources.OdooConfigMap(instance)
	setOwner(instance, cm)
	if err := r.apply(ctx, cm); err != nil {
		return err
	}
	addons := resources.PlatformAddonsConfigMap(instance)
	setOwner(instance, addons)
	return r.apply(ctx, addons)
}

// reconcileWorkload applies the Odoo Service and single web/cron Deployment.
// The caller (Reconcile) only invokes this
// once the database-init Job has succeeded: creating these earlier would
// let customers observe Odoo pods serving `KeyError: 'ir.http'` 500s
// against a database with no schema yet.
func (r *OdooInstanceReconciler) reconcileWorkload(ctx context.Context, instance *saasv1alpha1.OdooInstance) error {
	svc := resources.OdooService(instance)
	setOwner(instance, svc)
	if err := r.apply(ctx, svc); err != nil {
		return err
	}

	web := resources.OdooDeployment(instance)
	setOwner(instance, web)
	if err := r.apply(ctx, web); err != nil {
		return err
	}

	ns := resources.TenantNamespace(instance)
	// Remove the retired dedicated cron workload; this instance runs cron itself.
	var existing appsv1.Deployment
	err := r.Get(ctx, types.NamespacedName{Namespace: ns, Name: resources.OdooCronDeploymentName(instance)}, &existing)
	if err == nil {
		if err := r.Delete(ctx, &existing); err != nil && !apierrors.IsNotFound(err) {
			return err
		}
	} else if !apierrors.IsNotFound(err) {
		return err
	}

	return nil
}

// workloadStatus reports readiness of the single Odoo deployment.
func (r *OdooInstanceReconciler) workloadStatus(ctx context.Context, instance *saasv1alpha1.OdooInstance) (ready bool, readyReplicas, totalReplicas int32, image string, err error) {
	ns := resources.TenantNamespace(instance)

	var web appsv1.Deployment
	if err = r.Get(ctx, types.NamespacedName{Namespace: ns, Name: resources.OdooDeploymentName(instance)}, &web); err != nil {
		if apierrors.IsNotFound(err) {
			return false, 0, 0, "", nil
		}
		return false, 0, 0, "", err
	}

	readyReplicas = web.Status.ReadyReplicas
	totalReplicas = web.Status.Replicas
	if len(web.Spec.Template.Spec.Containers) > 0 {
		image = web.Spec.Template.Spec.Containers[0].Image
	}
	webReady := web.Status.ReadyReplicas >= 1 && web.Status.ReadyReplicas == web.Status.Replicas

	return webReady, readyReplicas, totalReplicas, image, nil
}

// reconcilePDB removes retired multi-pod disruption budgets.
func (r *OdooInstanceReconciler) reconcilePDB(ctx context.Context, instance *saasv1alpha1.OdooInstance) error {
	return r.deletePDBIfExists(ctx, resources.TenantNamespace(instance), resources.PodDisruptionBudgetName(instance))
}

func (r *OdooInstanceReconciler) deletePDBIfExists(ctx context.Context, namespace, name string) error {
	var existing policyv1.PodDisruptionBudget
	err := r.Get(ctx, types.NamespacedName{Namespace: namespace, Name: name}, &existing)
	if apierrors.IsNotFound(err) {
		return nil
	}
	if err != nil {
		return err
	}
	if err := r.Delete(ctx, &existing); err != nil && !apierrors.IsNotFound(err) {
		return err
	}
	return nil
}
