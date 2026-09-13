package controller

import (
	"context"

	batchv1 "k8s.io/api/batch/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
	"github.com/freightright/odoo-saas-platform/operator/internal/resources"
)

// reconcileBackup creates or removes the scheduled backup CronJob (and its
// destination PVC, for the PVC destination type) to match
// spec.backup.enabled, and records the most recent run's outcome into
// status.
func (r *OdooInstanceReconciler) reconcileBackup(ctx context.Context, instance *saasv1alpha1.OdooInstance) error {
	ns := resources.TenantNamespace(instance)

	if !instance.Spec.Backup.Enabled {
		var existing batchv1.CronJob
		err := r.Get(ctx, types.NamespacedName{Namespace: ns, Name: resources.BackupCronJobName(instance)}, &existing)
		if err == nil {
			if err := r.Delete(ctx, &existing); err != nil && !apierrors.IsNotFound(err) {
				return err
			}
		} else if !apierrors.IsNotFound(err) {
			return err
		}
		return nil
	}

	if instance.Spec.Backup.Destination.Type == saasv1alpha1.BackupDestinationPVC || instance.Spec.Backup.Destination.Type == "" {
		pvc := resources.BackupPVC(instance)
		setOwner(instance, pvc)
		if err := r.apply(ctx, pvc); err != nil {
			return err
		}
	}

	cronJob := resources.BackupCronJob(instance, r.effectiveBackupToolImage(instance))
	setOwner(instance, cronJob)
	if err := r.apply(ctx, cronJob); err != nil {
		return err
	}

	r.recordLastBackupStatus(ctx, instance, ns)
	return nil
}

// recordLastBackupStatus is best-effort: it inspects the CronJob's most
// recent Job to populate status.lastBackupTime/lastBackupStatus. Failure to
// observe this never fails reconciliation.
func (r *OdooInstanceReconciler) recordLastBackupStatus(ctx context.Context, instance *saasv1alpha1.OdooInstance, ns string) {
	var jobs batchv1.JobList
	if err := r.List(ctx, &jobs, client.InNamespace(ns), client.MatchingLabels(resources.WithComponent(instance, "backup"))); err != nil {
		return
	}
	var latest *batchv1.Job
	for i := range jobs.Items {
		j := &jobs.Items[i]
		if latest == nil || j.CreationTimestamp.After(latest.CreationTimestamp.Time) {
			latest = j
		}
	}
	if latest == nil {
		return
	}
	if latest.Status.Succeeded > 0 {
		instance.Status.LastBackupStatus = "Succeeded"
		if latest.Status.CompletionTime != nil {
			instance.Status.LastBackupTime = latest.Status.CompletionTime
		}
	} else if latest.Status.Failed > 0 {
		instance.Status.LastBackupStatus = "Failed"
	} else {
		instance.Status.LastBackupStatus = "Running"
	}
}
