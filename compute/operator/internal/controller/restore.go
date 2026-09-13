package controller

import (
	"context"
	"fmt"

	batchv1 "k8s.io/api/batch/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
	"github.com/freightright/odoo-saas-platform/operator/internal/resources"
)

// reconcileRestore is the restore-from-backup counterpart to
// reconcileInitJob: when spec.restore is set, Reconcile calls this INSTEAD
// of reconcileInitJob to bring a brand-new instance's database and
// filestore to a servable state (see resources.OdooRestoreJob), gating the
// web Deployment exactly the same way — see the shared schemaGateResult
// type (init_job.go) and Reconcile's use of it.
//
// Idempotent for the same reason reconcileInitJob is: it never recreates
// the Job once observed Succeeded (BackoffLimit alone governs in-Job
// retries), and Server-Side-Applying the same desired Job object on every
// reconcile is a no-op once the Job exists (a Job's pod template is
// immutable after creation), so "reconcile ran N times" never means "the
// restore ran N times." Combined with spec.restore's CRD-level immutability
// (see OdooInstanceSpec.Restore), the restore Job's desired shape can never
// change out from under an already-running or already-succeeded restore
// either.
func (r *OdooInstanceReconciler) reconcileRestore(ctx context.Context, instance *saasv1alpha1.OdooInstance) (schemaGateResult, error) {
	ns := resources.TenantNamespace(instance)

	job := resources.OdooRestoreJob(instance, r.effectiveRestoreToolImage(instance))
	setOwner(instance, job)
	if err := r.apply(ctx, job); err != nil {
		return schemaGateResult{}, fmt.Errorf("applying restore Job: %w", err)
	}

	var live batchv1.Job
	if err := r.Get(ctx, types.NamespacedName{Namespace: ns, Name: resources.OdooRestoreJobName(instance)}, &live); err != nil {
		if apierrors.IsNotFound(err) {
			return schemaGateResult{reason: "RestorePending", message: "restore Job not yet observed"}, nil
		}
		return schemaGateResult{}, err
	}

	if live.Status.Succeeded > 0 {
		return schemaGateResult{succeeded: true, reason: "RestoreSucceeded", message: "database and filestore restored from backup"}, nil
	}
	for _, cond := range live.Status.Conditions {
		if cond.Type == batchv1.JobFailed && cond.Status == "True" {
			return schemaGateResult{
				failed:  true,
				reason:  "RestoreFailed",
				message: fmt.Sprintf("restore Job failed after exhausting retries: %s", cond.Message),
			}, nil
		}
	}
	return schemaGateResult{reason: "RestoreRunning", message: "restoring database and filestore from backup"}, nil
}

func (r *OdooInstanceReconciler) effectiveRestoreToolImage(instance *saasv1alpha1.OdooInstance) string {
	if r.RestoreToolImage != "" {
		return r.RestoreToolImage
	}
	return resources.DefaultRestoreToolImage
}
