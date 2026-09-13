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
	if err := r.apply(ctx, pvc); err != nil {
		return false, err
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
	password, err := r.existingOrNewPassword(ctx, resources.TenantNamespace(instance), resources.AdminSecretName(instance), "master-password", 32)
	if err != nil {
		return err
	}
	secret := resources.AdminSecret(instance, resources.AdminSecretData{MasterPassword: password})
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
	return r.apply(ctx, cm)
}

// reconcileWorkload applies the Odoo Service and the web Deployment (plus a
// dedicated cron Deployment when replicas > 1 — see
// resources.NeedsCronDeployment). The caller (Reconcile) only invokes this
// once the database-init Job has succeeded: creating these earlier would
// let customers observe Odoo pods serving `KeyError: 'ir.http'` 500s
// against a database with no schema yet.
func (r *OdooInstanceReconciler) reconcileWorkload(ctx context.Context, instance *saasv1alpha1.OdooInstance) error {
	svc := resources.OdooService(instance)
	setOwner(instance, svc)
	if err := r.apply(ctx, svc); err != nil {
		return err
	}

	web := resources.OdooDeployment(instance, resources.RoleWeb)
	setOwner(instance, web)
	if err := r.apply(ctx, web); err != nil {
		return err
	}

	ns := resources.TenantNamespace(instance)
	if resources.NeedsCronDeployment(instance) {
		cron := resources.OdooDeployment(instance, resources.RoleCron)
		setOwner(instance, cron)
		if err := r.apply(ctx, cron); err != nil {
			return err
		}
	} else {
		// A previous reconcile may have created the cron Deployment before
		// replicas was scaled back down to 1; remove it so cron does not
		// run in two places at once.
		var existing appsv1.Deployment
		err := r.Get(ctx, types.NamespacedName{Namespace: ns, Name: resources.OdooCronDeploymentName(instance)}, &existing)
		if err == nil {
			if err := r.Delete(ctx, &existing); err != nil && !apierrors.IsNotFound(err) {
				return err
			}
		} else if !apierrors.IsNotFound(err) {
			return err
		}
	}

	return nil
}

// workloadStatus aggregates readiness across the web (and, when present,
// cron) Deployments.
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

	if !resources.NeedsCronDeployment(instance) {
		return webReady, readyReplicas, totalReplicas, image, nil
	}

	var cron appsv1.Deployment
	if err = r.Get(ctx, types.NamespacedName{Namespace: ns, Name: resources.OdooCronDeploymentName(instance)}, &cron); err != nil {
		if apierrors.IsNotFound(err) {
			return false, readyReplicas, totalReplicas, image, nil
		}
		return false, 0, 0, "", err
	}
	cronReady := cron.Status.ReadyReplicas >= 1
	return webReady && cronReady, readyReplicas, totalReplicas, image, nil
}

// reconcilePDB creates/removes the PodDisruptionBudget to match the
// current replica count (see resources.PodDisruptionBudget docs for why it
// is only meaningful above one replica).
func (r *OdooInstanceReconciler) reconcilePDB(ctx context.Context, instance *saasv1alpha1.OdooInstance) error {
	ns := resources.TenantNamespace(instance)
	replicas := int32(1)
	if instance.Spec.Replicas != nil {
		replicas = *instance.Spec.Replicas
	}

	if replicas <= 1 {
		return r.deletePDBIfExists(ctx, ns, resources.PodDisruptionBudgetName(instance))
	}

	pdb := resources.PodDisruptionBudget(instance)
	setOwner(instance, pdb)
	return r.apply(ctx, pdb)
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
