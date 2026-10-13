package controller

import (
	"context"
	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
	"github.com/freightright/odoo-saas-platform/operator/internal/resources"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"testing"
)

func TestTenantNamespaceCannotTargetPlatformOrForeignNamespaces(t *testing.T) {
	instance := &saasv1alpha1.OdooInstance{ObjectMeta: metav1.ObjectMeta{Name: "safety", UID: "tenant-uid"}}
	r := &OdooInstanceReconciler{OperatorNamespace: "odoo-system", GatewayNamespace: "ingress", ControlPlaneNamespace: "kube-system"}
	for _, name := range []string{"kube-system", "cert-manager", "monitoring", "ingress", "odoo-system", "saas-toolbox", "odoo-builds", "odoo-tenant-", "odoo-tenant-invalid.name"} {
		instance.Spec.Tenancy.NamespaceOverride = name
		if err := r.validateTenantNamespace(instance); err == nil {
			t.Fatalf("unsafe namespace accepted: %s", name)
		}
		if err := r.finalizeInstance(context.Background(), instance); err == nil {
			t.Fatalf("unsafe namespace deletion accepted: %s", name)
		}
	}
	instance.Spec.Tenancy.NamespaceOverride = ""
	if err := r.validateTenantNamespace(instance); err != nil {
		t.Fatal(err)
	}
	r.OperatorNamespace = resources.TenantNamespace(instance)
	if err := r.validateTenantNamespace(instance); err == nil {
		t.Fatal("operator namespace collision accepted")
	}
}

func TestExistingTenantNamespaceRequiresMatchingOwner(t *testing.T) {
	instance := &saasv1alpha1.OdooInstance{ObjectMeta: metav1.ObjectMeta{Name: "safety", UID: "tenant-uid"}}
	ns := resources.Namespace(instance)
	scheme := runtime.NewScheme()
	_ = corev1.AddToScheme(scheme)
	r := &OdooInstanceReconciler{Client: fake.NewClientBuilder().WithScheme(scheme).WithObjects(ns).Build()}
	if err := r.requireOwnedTenantNamespace(context.Background(), instance); err == nil {
		t.Fatal("unowned namespace adopted")
	}
	if err := r.finalizeInstance(context.Background(), instance); err == nil {
		t.Fatal("unowned namespace deletion allowed")
	}
	setOwner(instance, ns)
	if err := r.Update(context.Background(), ns); err != nil {
		t.Fatal(err)
	}
	if err := r.requireOwnedTenantNamespace(context.Background(), instance); err != nil {
		t.Fatal(err)
	}
	instance.UID = "different-owner"
	if err := r.requireOwnedTenantNamespace(context.Background(), instance); err == nil {
		t.Fatal("foreign namespace adopted")
	}
}
