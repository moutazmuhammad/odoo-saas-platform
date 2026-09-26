package controller

import (
	"context"
	"fmt"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
	"github.com/freightright/odoo-saas-platform/operator/internal/resources"
)

const (
	ReasonUpdateApplied = "UpdateApplied"
	ReasonUpdateRunning = "UpdateRunning"
	ReasonUpdateFailed  = "UpdateFailed"
)

// updatePending reports whether spec.update carries a token that has not
// been applied yet.
func updatePending(instance *saasv1alpha1.OdooInstance) bool {
	u := instance.Spec.Update
	return u != nil && u.Token != "" && u.Token != instance.Status.AppliedUpdateToken
}

// servingInstance returns the instance to render the serving workload
// (Deployments + odoo.conf) from. Normally that is instance itself. While a
// spec.update is pending, it is a copy pinned to the image and addons paths
// the live web Deployment already runs, so the customer keeps being served
// by the previous, known-good image until the update Job has succeeded
// against the new one. held reports whether the pinned copy is in effect.
//
// A pending update on an instance with no web Deployment yet (first
// rollout) has nothing to protect and nothing installed to upgrade, so it
// is marked applied straight away.
func (r *OdooInstanceReconciler) servingInstance(ctx context.Context, instance *saasv1alpha1.OdooInstance) (serving *saasv1alpha1.OdooInstance, held bool, err error) {
	if !updatePending(instance) {
		return instance, false, nil
	}
	var live appsv1.Deployment
	key := types.NamespacedName{Namespace: resources.TenantNamespace(instance), Name: resources.OdooDeploymentName(instance)}
	if err := r.Get(ctx, key, &live); err != nil {
		if apierrors.IsNotFound(err) {
			instance.Status.AppliedUpdateToken = instance.Spec.Update.Token
			return instance, false, nil
		}
		return nil, false, err
	}
	containers := live.Spec.Template.Spec.Containers
	if len(containers) == 0 {
		return instance, false, nil
	}
	repo, tag, ok := splitImage(containers[0].Image)
	if !ok {
		return nil, false, fmt.Errorf("cannot parse live image %q", containers[0].Image)
	}
	pinned := instance.DeepCopy()
	pinned.Spec.Image.Repository = repo
	pinned.Spec.Image.Tag = tag
	pinned.Spec.AddonsPaths = resources.LiveAddonsPaths(live.Spec.Template.Annotations)
	return pinned, true, nil
}

// splitImage splits "repo:tag" at the last colon that belongs to the tag
// (a registry host:port colon is followed by a "/").
func splitImage(image string) (repo, tag string, ok bool) {
	i := strings.LastIndex(image, ":")
	if i <= 0 || strings.Contains(image[i:], "/") {
		return "", "", false
	}
	return image[:i], image[i+1:], true
}

type updateResult struct {
	succeeded bool
	failed    bool
	message   string
}

// reconcileUpdateJob runs the pending spec.update's module upgrade: one
// Job per token (resources.OdooUpdateJob), created once and then only
// observed. Update Jobs of superseded tokens are removed.
func (r *OdooInstanceReconciler) reconcileUpdateJob(ctx context.Context, instance *saasv1alpha1.OdooInstance) (updateResult, error) {
	ns := resources.TenantNamespace(instance)
	name := resources.OdooUpdateJobName(instance)
	if err := r.deleteStaleUpdateJobs(ctx, instance, name); err != nil {
		return updateResult{}, err
	}
	if len(instance.Spec.Update.Modules) == 0 {
		return updateResult{succeeded: true, message: "no modules to upgrade"}, nil
	}

	cm := resources.OdooUpdateConfigMap(instance)
	setOwner(instance, cm)
	if err := r.apply(ctx, cm); err != nil {
		return updateResult{}, err
	}

	var live batchv1.Job
	if err := r.Get(ctx, types.NamespacedName{Namespace: ns, Name: name}, &live); err != nil {
		if !apierrors.IsNotFound(err) {
			return updateResult{}, err
		}
		job := resources.OdooUpdateJob(instance)
		setOwner(instance, job)
		if err := r.apply(ctx, job); err != nil {
			return updateResult{}, fmt.Errorf("applying update Job: %w", err)
		}
		return updateResult{message: "update Job created"}, nil
	}
	if live.Status.Succeeded > 0 {
		return updateResult{succeeded: true, message: "modules upgraded"}, nil
	}
	for _, cond := range live.Status.Conditions {
		if cond.Type == batchv1.JobFailed && cond.Status == "True" {
			return updateResult{failed: true, message: fmt.Sprintf("update Job %s failed: %s", name, cond.Message)}, nil
		}
	}
	return updateResult{message: fmt.Sprintf("upgrading modules (Job %s)", name)}, nil
}

func (r *OdooInstanceReconciler) deleteStaleUpdateJobs(ctx context.Context, instance *saasv1alpha1.OdooInstance, keep string) error {
	var jobs batchv1.JobList
	if err := r.List(ctx, &jobs, client.InNamespace(resources.TenantNamespace(instance)),
		client.MatchingLabels{saasv1alpha1.LabelComponent: resources.UpdateJobComponent}); err != nil {
		return err
	}
	for i := range jobs.Items {
		if jobs.Items[i].Name == keep {
			continue
		}
		if err := r.Delete(ctx, &jobs.Items[i], client.PropagationPolicy(metav1.DeletePropagationBackground)); err != nil && !apierrors.IsNotFound(err) {
			return err
		}
	}
	return nil
}
