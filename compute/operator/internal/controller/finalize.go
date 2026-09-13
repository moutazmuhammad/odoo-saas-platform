package controller

import (
	"context"
	"fmt"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/wait"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/log"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
	"github.com/freightright/odoo-saas-platform/operator/internal/resources"
)

// finalizeInstance runs before the tenant namespace (and everything in it)
// is deleted. It is intentionally minimal in this API version: it triggers
// one best-effort final backup Job when backups are enabled, waits briefly
// for it, and then deletes the tenant namespace, which cascades deletion of
// every child resource regardless of individual ownerReferences.
//
// Known, documented limitation: deleting the namespace also deletes its
// PVCs (filestore, database, local backups). There is no
// "retain the PVC but delete everything else" option in Kubernetes once
// the owning namespace itself is removed. Real data retention after tenant
// deletion therefore depends on the final backup landing in
// spec.backup.destination BEFORE this finalizer removes itself — an
// ObjectStorage destination is strongly recommended for any tenant whose
// data must outlive deletion, precisely because it lives outside the
// tenant namespace being torn down.
func (r *OdooInstanceReconciler) finalizeInstance(ctx context.Context, instance *saasv1alpha1.OdooInstance) error {
	logger := log.FromContext(ctx)
	ns := resources.TenantNamespace(instance)

	var nsObj corev1.Namespace
	err := r.Get(ctx, types.NamespacedName{Name: ns}, &nsObj)
	if apierrors.IsNotFound(err) {
		return nil // already gone, nothing left to finalize
	}
	if err != nil {
		return fmt.Errorf("getting tenant namespace %q: %w", ns, err)
	}
	if !nsObj.DeletionTimestamp.IsZero() {
		return nil // deletion already in progress, let it finish
	}

	if instance.Spec.Backup.Enabled {
		if err := r.runFinalBackup(ctx, instance); err != nil {
			// Do not block deletion forever on a failed final backup: log
			// and proceed, surfaced via an Event so the SaaS platform/operator
			// can alert on it, per "never infinite destructive retries".
			logger.Error(err, "final backup before deletion did not complete; proceeding with tenant namespace deletion")
			r.Recorder.Eventf(instance, corev1.EventTypeWarning, "FinalBackupIncomplete",
				"deleting tenant namespace %q without a confirmed final backup: %v", ns, err)
		}
	}

	if err := r.Delete(ctx, &nsObj); err != nil && !apierrors.IsNotFound(err) {
		return fmt.Errorf("deleting tenant namespace %q: %w", ns, err)
	}
	return nil
}

// runFinalBackup runs the backup CronJob's pod spec as a one-off Job and
// waits (bounded) for it to complete before namespace teardown proceeds.
func (r *OdooInstanceReconciler) runFinalBackup(ctx context.Context, instance *saasv1alpha1.OdooInstance) error {
	cronJob := resources.BackupCronJob(instance, r.effectiveBackupToolImage(instance))

	job := &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{
			Name:      resources.BackupCronJobName(instance) + "-final",
			Namespace: resources.TenantNamespace(instance),
			Labels:    cronJob.Labels,
		},
		Spec: cronJob.Spec.JobTemplate.Spec,
	}
	if err := controllerutil.SetOwnerReference(instance, job, r.Scheme); err != nil {
		return err
	}

	if err := r.Create(ctx, job); err != nil && !apierrors.IsAlreadyExists(err) {
		return fmt.Errorf("creating final backup Job: %w", err)
	}

	return wait.PollUntilContextTimeout(ctx, backupPollInterval, backupPollTimeout, true, func(ctx context.Context) (bool, error) {
		var current batchv1.Job
		if err := r.Get(ctx, types.NamespacedName{Namespace: job.Namespace, Name: job.Name}, &current); err != nil {
			return false, err
		}
		return current.Status.Succeeded > 0, nil
	})
}

func (r *OdooInstanceReconciler) effectiveBackupToolImage(instance *saasv1alpha1.OdooInstance) string {
	if r.BackupToolImage != "" {
		return r.BackupToolImage
	}
	return resources.DefaultBackupToolImage
}

const (
	backupPollInterval = 5 * time.Second
	backupPollTimeout  = 2 * time.Minute
)
