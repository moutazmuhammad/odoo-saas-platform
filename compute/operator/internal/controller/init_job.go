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

// schemaGateResult mirrors databaseResult's shape for the same reason: a
// single place to report why the dependency the web Deployment is gated on
// is not yet satisfied. It is shared by the two mutually-exclusive
// one-time paths that bring a brand-new instance's database from nothing
// to something Odoo can serve — reconcileInitJob (a fresh empty database)
// and reconcileRestore (internal/controller/restore.go; an existing
// database restored from backup) — so the caller (Reconcile) can gate the
// web Deployment on whichever one applies without duplicating that gating
// logic per path.
type schemaGateResult struct {
	succeeded bool
	failed    bool
	reason    string
	message   string
}

// reconcileInitJob ensures the one-time database-initialization Job (see
// resources.OdooInitJob) exists and reports its outcome. It never recreates
// a Job that already exists purely to "try again" on its own — that is the
// BackoffLimit's job — so this stays idempotent even though the action the
// Job performs is a one-time, non-idempotent operation from Odoo's own
// point of view. Only used when spec.restore is unset; see reconcileRestore
// for the restore-from-backup counterpart.
func (r *OdooInstanceReconciler) reconcileInitJob(ctx context.Context, instance *saasv1alpha1.OdooInstance) (schemaGateResult, error) {
	ns := resources.TenantNamespace(instance)

	// Only ever create the Job, never re-apply an existing one: a Job's pod
	// template is immutable, and it embeds spec.image, so re-applying it
	// after an image change would fail every reconcile from then on (and
	// with it the rollout of the new image to the web Deployment).
	var live batchv1.Job
	if err := r.Get(ctx, types.NamespacedName{Namespace: ns, Name: resources.OdooInitJobName(instance)}, &live); err != nil {
		if !apierrors.IsNotFound(err) {
			return schemaGateResult{}, err
		}
		job := resources.OdooInitJob(instance)
		setOwner(instance, job)
		if err := r.apply(ctx, job); err != nil {
			return schemaGateResult{}, fmt.Errorf("applying database-init Job: %w", err)
		}
		return schemaGateResult{reason: "InitJobPending", message: "database-init Job created"}, nil
	}

	if live.Status.Succeeded > 0 {
		return schemaGateResult{succeeded: true, reason: "InitJobSucceeded", message: "database schema initialized"}, nil
	}
	for _, cond := range live.Status.Conditions {
		if cond.Type == batchv1.JobFailed && cond.Status == "True" {
			return schemaGateResult{
				failed:  true,
				reason:  "InitJobFailed",
				message: fmt.Sprintf("database-init Job failed after exhausting retries: %s", cond.Message),
			}, nil
		}
	}
	return schemaGateResult{reason: "InitJobRunning", message: "waiting for database schema initialization to complete"}, nil
}
