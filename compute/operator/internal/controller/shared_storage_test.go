package controller

import (
	"github.com/freightright/odoo-saas-platform/operator/internal/resources"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"testing"
)

func TestSharedStorageSchemaAndReconcile(t *testing.T) {
	instance := newTestInstance("shared-budget")
	instance.Spec.Storage.SharedWithDatabase = true
	instance.Spec.Storage.Filestore.Size = "10Gi"
	if err := k8sClient.Create(testCtx, instance); err != nil {
		t.Fatal(err)
	}
	r := testReconciler()
	if err := r.reconcileTenancy(testCtx, instance); err != nil {
		t.Fatal(err)
	}
	if _, err := r.reconcileDatabase(testCtx, instance); err != nil {
		t.Fatal(err)
	}
	if _, err := r.reconcileStorage(testCtx, instance); err != nil {
		t.Fatal(err)
	}
	ns := resources.TenantNamespace(instance)
	var claims corev1.PersistentVolumeClaimList
	if err := k8sClient.List(testCtx, &claims, client.InNamespace(ns)); err != nil {
		t.Fatal(err)
	}
	if len(claims.Items) != 1 {
		t.Fatalf("expected exactly one tenant claim; got %d", len(claims.Items))
	}
	size := claims.Items[0].Spec.Resources.Requests[corev1.ResourceStorage]
	if size.Cmp(resource.MustParse("10Gi")) != 0 {
		t.Fatal("storage budget doubled")
	}
	var sts appsv1.StatefulSet
	if err := k8sClient.Get(testCtx, client.ObjectKey{Namespace: ns, Name: resources.DatabaseStatefulSetName(instance)}, &sts); err != nil {
		t.Fatal(err)
	}
	if len(sts.Spec.VolumeClaimTemplates) != 0 {
		t.Fatal("database controller would create a second claim")
	}
	original := instance.DeepCopy()
	instance.Spec.Storage.SharedWithDatabase = false
	if err := k8sClient.Patch(testCtx, instance, client.MergeFrom(original)); err == nil {
		t.Fatal("layout must be immutable to protect existing data")
	}
	legacy := newTestInstance("legacy-layout-lock")
	if err := k8sClient.Create(testCtx, legacy); err != nil {
		t.Fatal(err)
	}
	original = legacy.DeepCopy()
	legacy.Spec.Storage.SharedWithDatabase = true
	if err := k8sClient.Patch(testCtx, legacy, client.MergeFrom(original)); err == nil {
		t.Fatal("legacy data must not be detached by enabling shared storage")
	}
}
