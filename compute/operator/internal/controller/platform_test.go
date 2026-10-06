package controller

import (
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
	"github.com/freightright/odoo-saas-platform/operator/internal/resources"
)

// TestReconcile_PlatformPullSecretCopiedIntoTenantNamespace covers
// --platform-pull-secret end to end: a missing source is reported but does
// not stop the tenant, the copy appears once the source exists, follows
// source changes, and is removed once the flag is unset.
func TestReconcile_PlatformPullSecretCopiedIntoTenantNamespace(t *testing.T) {
	const operatorNS = "odoo-system-pull-test"
	if err := k8sClient.Create(testCtx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: operatorNS}}); err != nil {
		t.Fatalf("creating operator namespace: %v", err)
	}

	r := testReconciler()
	r.PlatformPullSecret = "registry-creds"
	r.OperatorNamespace = operatorNS

	instance := newTestInstance("platform-pull")
	instance.Spec.Image.PullSecretRefs = []corev1.LocalObjectReference{{Name: "tenant-registry"}}
	if err := k8sClient.Create(testCtx, instance); err != nil {
		t.Fatalf("creating instance: %v", err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, instance) })
	ns := saasv1alpha1.NamespacePrefix + "platform-pull"
	copyKey := client.ObjectKey{Namespace: ns, Name: resources.PlatformPullSecretName}

	// Source missing: condition set, tenant still reconciled (database
	// StatefulSet created and listing both pull secrets).
	reconcileUntil(t, r, "platform-pull", func(i *saasv1alpha1.OdooInstance) bool {
		c := findCondition(i, ConditionPlatformPullSecretMissing)
		var sts appsv1.StatefulSet
		return c != nil && c.Status == metav1.ConditionTrue &&
			k8sClient.Get(testCtx, client.ObjectKey{Namespace: ns, Name: resources.DatabaseStatefulSetName(instance)}, &sts) == nil
	})
	var sts appsv1.StatefulSet
	if err := k8sClient.Get(testCtx, client.ObjectKey{Namespace: ns, Name: resources.DatabaseStatefulSetName(instance)}, &sts); err != nil {
		t.Fatalf("getting database StatefulSet: %v", err)
	}
	want := []corev1.LocalObjectReference{{Name: "tenant-registry"}, {Name: resources.PlatformPullSecretName}}
	if got := sts.Spec.Template.Spec.ImagePullSecrets; len(got) != 2 || got[0] != want[0] || got[1] != want[1] {
		t.Errorf("database imagePullSecrets = %v, want %v", got, want)
	}
	assertNotExists(t, &corev1.Secret{}, ns, resources.PlatformPullSecretName)

	source := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: "registry-creds", Namespace: operatorNS},
		Type:       corev1.SecretTypeDockerConfigJson,
		Data:       map[string][]byte{corev1.DockerConfigJsonKey: []byte(`{"auths":{"registry.internal":{"auth":"YTpi"}}}`)},
	}
	if err := k8sClient.Create(testCtx, source); err != nil {
		t.Fatalf("creating source secret: %v", err)
	}
	reconcileUntil(t, r, "platform-pull", func(i *saasv1alpha1.OdooInstance) bool {
		var copied corev1.Secret
		return findCondition(i, ConditionPlatformPullSecretMissing) == nil && k8sClient.Get(testCtx, copyKey, &copied) == nil
	})
	var copied corev1.Secret
	if err := k8sClient.Get(testCtx, copyKey, &copied); err != nil {
		t.Fatalf("getting copied secret: %v", err)
	}
	if copied.Type != corev1.SecretTypeDockerConfigJson || string(copied.Data[corev1.DockerConfigJsonKey]) != string(source.Data[corev1.DockerConfigJsonKey]) {
		t.Errorf("copied secret = type %q data %q", copied.Type, copied.Data[corev1.DockerConfigJsonKey])
	}
	if copied.Labels[saasv1alpha1.LabelManagedBy] != saasv1alpha1.ManagedByValue || len(copied.OwnerReferences) != 1 {
		t.Errorf("copied secret not labelled/owned like other tenant objects: labels=%v owners=%v", copied.Labels, copied.OwnerReferences)
	}

	// Rotated source credentials reach the tenant on the next reconcile.
	source.Data[corev1.DockerConfigJsonKey] = []byte(`{"auths":{"registry.internal":{"auth":"Yzpk"}}}`)
	if err := k8sClient.Update(testCtx, source); err != nil {
		t.Fatalf("updating source secret: %v", err)
	}
	reconcileUntil(t, r, "platform-pull", func(*saasv1alpha1.OdooInstance) bool {
		var s corev1.Secret
		return k8sClient.Get(testCtx, copyKey, &s) == nil && string(s.Data[corev1.DockerConfigJsonKey]) == string(source.Data[corev1.DockerConfigJsonKey])
	})

	// Flag unset: the copy is removed.
	r.PlatformPullSecret = ""
	reconcileUntil(t, r, "platform-pull", func(*saasv1alpha1.OdooInstance) bool {
		var s corev1.Secret
		return k8sClient.Get(testCtx, copyKey, &s) != nil
	})
}
