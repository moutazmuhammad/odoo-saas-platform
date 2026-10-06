package controller

import (
	"context"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
	"github.com/freightright/odoo-saas-platform/operator/internal/resources"
)

const suspensionJobAnnotation = "saas.odoo.example.com/paused-by-suspension"

// Keep persistent data and workload specifications so resume is reversible.
//
// Only backup Jobs are paused: a backup is a read-only snapshot that simply
// reruns on resume. Init, restore, update and schema-gate Jobs write to the
// database, and suspending one kills its pod midway so it later restarts
// from scratch against a half-written database. Those Jobs are left to
// finish, and the database (which they depend on) is only stopped once none
// is still running; until then the returned bool is true and the caller
// requeues. Odoo itself is scaled to zero immediately either way.
func (r *OdooInstanceReconciler) suspendTenantWorkloads(ctx context.Context, instance *saasv1alpha1.OdooInstance) (bool, error) {
	ns := resources.TenantNamespace(instance)
	var schedules batchv1.CronJobList
	if err := r.List(ctx, &schedules, client.InNamespace(ns), client.MatchingLabels(resources.SelectorLabels(instance))); err != nil {
		return false, err
	}
	for i := range schedules.Items {
		schedule := &schedules.Items[i]
		if ptr.Deref(schedule.Spec.Suspend, false) {
			continue
		}
		before := schedule.DeepCopy()
		schedule.Spec.Suspend = ptr.To(true)
		if schedule.Annotations == nil {
			schedule.Annotations = map[string]string{}
		}
		schedule.Annotations[suspensionJobAnnotation] = "true"
		if err := r.Patch(ctx, schedule, client.MergeFrom(before)); err != nil {
			return false, err
		}
	}
	var jobs batchv1.JobList
	if err := r.List(ctx, &jobs, client.InNamespace(ns)); err != nil {
		return false, err
	}
	databaseInUse := false
	for i := range jobs.Items {
		job := &jobs.Items[i]
		if !tenantJob(instance, job) || jobFinished(job) || ptr.Deref(job.Spec.Suspend, false) {
			continue
		}
		if !backupJob(job) {
			databaseInUse = true
			continue
		}
		before := job.DeepCopy()
		job.Spec.Suspend = ptr.To(true)
		if job.Annotations == nil {
			job.Annotations = map[string]string{}
		}
		job.Annotations[suspensionJobAnnotation] = "true"
		if err := r.Patch(ctx, job, client.MergeFrom(before)); err != nil {
			return false, err
		}
	}
	for _, name := range []string{resources.OdooDeploymentName(instance), resources.OdooCronDeploymentName(instance)} {
		dep := &appsv1.Deployment{}
		if err := r.Get(ctx, types.NamespacedName{Namespace: ns, Name: name}, dep); err != nil {
			if apierrors.IsNotFound(err) {
				continue
			}
			return false, err
		}
		if ptr.Deref(dep.Spec.Replicas, 0) == 0 {
			continue
		}
		before := dep.DeepCopy()
		dep.Spec.Replicas = ptr.To(int32(0))
		if err := r.Patch(ctx, dep, client.MergeFrom(before)); err != nil {
			return false, err
		}
	}
	if databaseInUse {
		return true, nil
	}
	switch instance.Spec.Database.Mode {
	case saasv1alpha1.DatabaseModeExternal:
		// External databases may be shared; never stop another tenant's server.
		return false, nil
	case saasv1alpha1.DatabaseModeCloudNativePG:
		cluster := resources.CloudNativePGCluster(instance)
		if err := r.Get(ctx, client.ObjectKeyFromObject(cluster), cluster); err != nil {
			if apierrors.IsNotFound(err) {
				return false, nil
			}
			return false, err
		}
		before := cluster.DeepCopy()
		annotations := cluster.GetAnnotations()
		if annotations == nil {
			annotations = map[string]string{}
		}
		if annotations["cnpg.io/hibernation"] == "on" {
			return false, nil
		}
		annotations["cnpg.io/hibernation"] = "on"
		cluster.SetAnnotations(annotations)
		return false, r.Patch(ctx, cluster, client.MergeFrom(before))
	default:
		sts := &appsv1.StatefulSet{}
		if err := r.Get(ctx, types.NamespacedName{Namespace: ns, Name: resources.DatabaseStatefulSetName(instance)}, sts); err != nil {
			if apierrors.IsNotFound(err) {
				return false, nil
			}
			return false, err
		}
		if ptr.Deref(sts.Spec.Replicas, 0) == 0 {
			return false, nil
		}
		before := sts.DeepCopy()
		// Explicitly retain StatefulSet claims both on scale-down and deletion.
		sts.Spec.PersistentVolumeClaimRetentionPolicy = &appsv1.StatefulSetPersistentVolumeClaimRetentionPolicy{
			WhenScaled:  appsv1.RetainPersistentVolumeClaimRetentionPolicyType,
			WhenDeleted: appsv1.RetainPersistentVolumeClaimRetentionPolicyType,
		}
		sts.Spec.Replicas = ptr.To(int32(0))
		return false, r.Patch(ctx, sts, client.MergeFrom(before))
	}
}

func jobFinished(job *batchv1.Job) bool {
	for _, condition := range job.Status.Conditions {
		if (condition.Type == batchv1.JobComplete || condition.Type == batchv1.JobFailed) && condition.Status == "True" {
			return true
		}
	}
	return false
}

// Only resume jobs paused by us, after PostgreSQL is ready. User-paused jobs stay paused.
func (r *OdooInstanceReconciler) resumeTenantJobs(ctx context.Context, instance *saasv1alpha1.OdooInstance) error {
	var schedules batchv1.CronJobList
	if err := r.List(ctx, &schedules, client.InNamespace(resources.TenantNamespace(instance)), client.MatchingLabels(resources.SelectorLabels(instance))); err != nil {
		return err
	}
	for i := range schedules.Items {
		schedule := &schedules.Items[i]
		if schedule.Annotations[suspensionJobAnnotation] != "true" {
			continue
		}
		before := schedule.DeepCopy()
		schedule.Spec.Suspend = ptr.To(false)
		delete(schedule.Annotations, suspensionJobAnnotation)
		if err := r.Patch(ctx, schedule, client.MergeFrom(before)); err != nil {
			return err
		}
	}
	var jobs batchv1.JobList
	if err := r.List(ctx, &jobs, client.InNamespace(resources.TenantNamespace(instance))); err != nil {
		return err
	}
	for i := range jobs.Items {
		job := &jobs.Items[i]
		if !tenantJob(instance, job) || job.Annotations[suspensionJobAnnotation] != "true" {
			continue
		}
		before := job.DeepCopy()
		job.Spec.Suspend = ptr.To(false)
		delete(job.Annotations, suspensionJobAnnotation)
		if err := r.Patch(ctx, job, client.MergeFrom(before)); err != nil {
			return err
		}
	}
	return nil
}

// Older backup Jobs inherited instance labels only on the pod template.
func tenantJob(instance *saasv1alpha1.OdooInstance, job *batchv1.Job) bool {
	selector := labels.SelectorFromSet(resources.SelectorLabels(instance))
	return selector.Matches(labels.Set(job.Labels)) || selector.Matches(labels.Set(job.Spec.Template.Labels))
}

// backupJob reports whether a tenant Job was spawned from the backup
// CronJob (or created from it by hand), which carries the backup component
// label on the Job or, for older Jobs, on its pod template.
func backupJob(job *batchv1.Job) bool {
	return job.Labels[saasv1alpha1.LabelComponent] == "backup" ||
		job.Spec.Template.Labels[saasv1alpha1.LabelComponent] == "backup"
}
