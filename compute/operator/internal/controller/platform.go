package controller

import (
	"context"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
	"github.com/freightright/odoo-saas-platform/operator/internal/resources"
)

// platform is the operator-wide configuration every pod builder receives.
func (r *OdooInstanceReconciler) platform() resources.Platform {
	return resources.Platform{
		PullSecret:                  r.PlatformPullSecret != "",
		BackupToolImage:             r.BackupToolImage,
		RestoreToolImage:            r.RestoreToolImage,
		PostgresImageRepository:     r.PostgresImageRepository,
		CNPGPostgresImageRepository: r.CNPGPostgresImageRepository,
	}
}

// reconcilePlatformPullSecret keeps the tenant copy of --platform-pull-secret
// (resources.PlatformPullSecretName) in sync with its source in the
// operator's namespace, or removes it once the flag is unset. A missing or
// malformed source never fails the tenant: it is reported through
// ConditionPlatformPullSecretMissing and a Warning event, and reconciliation
// continues (public images still pull).
func (r *OdooInstanceReconciler) reconcilePlatformPullSecret(ctx context.Context, instance *saasv1alpha1.OdooInstance) error {
	tenantKey := types.NamespacedName{Namespace: resources.TenantNamespace(instance), Name: resources.PlatformPullSecretName}

	if r.PlatformPullSecret == "" {
		removeCondition(instance, ConditionPlatformPullSecretMissing)
		var stale corev1.Secret
		if err := r.Get(ctx, tenantKey, &stale); err != nil {
			return client.IgnoreNotFound(err)
		}
		if stale.Labels[saasv1alpha1.LabelManagedBy] != saasv1alpha1.ManagedByValue {
			return nil
		}
		return client.IgnoreNotFound(r.Delete(ctx, &stale))
	}

	var source corev1.Secret
	sourceKey := types.NamespacedName{Namespace: r.OperatorNamespace, Name: r.PlatformPullSecret}
	problem := ""
	if err := r.Get(ctx, sourceKey, &source); err != nil {
		if !apierrors.IsNotFound(err) {
			return err
		}
		problem = fmt.Sprintf("platform pull secret %s not found; tenant pods pull platform images without it", sourceKey)
	} else if source.Type != corev1.SecretTypeDockerConfigJson || len(source.Data[corev1.DockerConfigJsonKey]) == 0 {
		problem = fmt.Sprintf("platform pull secret %s is not a populated %s Secret; not copied", sourceKey, corev1.SecretTypeDockerConfigJson)
	}
	if problem != "" {
		if conditionStatus(instance, ConditionPlatformPullSecretMissing) != metav1.ConditionTrue {
			log.FromContext(ctx).Info(problem)
			r.Recorder.Event(instance, corev1.EventTypeWarning, ReasonPlatformPullSecretMissing, problem)
		}
		setCondition(instance, ConditionPlatformPullSecretMissing, metav1.ConditionTrue, ReasonPlatformPullSecretMissing, problem)
		return nil
	}

	secret := resources.PlatformPullSecret(instance, &source)
	setOwner(instance, secret)
	if err := r.apply(ctx, secret); err != nil {
		return err
	}
	removeCondition(instance, ConditionPlatformPullSecretMissing)
	return nil
}
