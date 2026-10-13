package controller

import (
	"context"
	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
	"github.com/freightright/odoo-saas-platform/operator/internal/resources"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"testing"
)

func privilegeTestReconciler(objects ...client.Object) (*OdooInstanceReconciler, *saasv1alpha1.OdooInstance) {
	scheme := runtime.NewScheme()
	_ = corev1.AddToScheme(scheme)
	_ = batchv1.AddToScheme(scheme)
	_ = saasv1alpha1.AddToScheme(scheme)
	instance := &saasv1alpha1.OdooInstance{ObjectMeta: metav1.ObjectMeta{Name: "privilege-test"}}
	c := fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(&batchv1.Job{}).WithObjects(objects...).Build()
	return &OdooInstanceReconciler{Client: c, Scheme: scheme}, instance
}

func TestDatabaseAdminStagesStableSeparateCredentials(t *testing.T) {
	ctx := context.Background()
	r, instance := privilegeTestReconciler()
	ns := resources.TenantNamespace(instance)
	old := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: resources.DatabaseSecretName(instance)},
		Data: map[string][]byte{"username": []byte("odoo"), "password": []byte("previously-exposed")}}
	if err := r.Create(ctx, old); err != nil {
		t.Fatal(err)
	}
	first, err := r.ensureDatabaseAdmin(ctx, instance)
	if err != nil {
		t.Fatal(err)
	}
	if string(first.Data["legacy-password"]) != "previously-exposed" || string(first.Data["legacy-user"]) != "odoo" {
		t.Fatal("legacy login not staged for migration")
	}
	if string(first.Data["password"]) == string(first.Data["owner-password"]) || string(first.Data["password"]) == "previously-exposed" {
		t.Fatal("credentials not separated/rotated")
	}
	second, err := r.ensureDatabaseAdmin(ctx, instance)
	if err != nil {
		t.Fatal(err)
	}
	if string(first.Data["password"]) != string(second.Data["password"]) || string(first.Data["owner-password"]) != string(second.Data["owner-password"]) {
		t.Fatal("retry rotated staged credentials")
	}
	var unchanged corev1.Secret
	if err := r.Get(ctx, client.ObjectKeyFromObject(old), &unchanged); err != nil {
		t.Fatal(err)
	}
	if string(unchanged.Data["password"]) != "previously-exposed" {
		t.Fatal("application credentials changed before SQL migration succeeded")
	}
}

func TestDatabaseAccessGateWaitsAndReportsFailure(t *testing.T) {
	ctx := context.Background()
	r, instance := privilegeTestReconciler()
	first, err := r.reconcileDatabaseAccess(ctx, instance, resources.DatabaseRolesJobName, true)
	if err != nil || first.ready {
		t.Fatalf("job creation bypassed gate: %+v %v", first, err)
	}
	var job batchv1.Job
	key := types.NamespacedName{Namespace: resources.TenantNamespace(instance), Name: resources.DatabaseRolesJobName}
	if err := r.Get(ctx, key, &job); err != nil {
		t.Fatal(err)
	}
	job.Status.Conditions = []batchv1.JobCondition{{Type: batchv1.JobFailed, Status: corev1.ConditionTrue}}
	if err := r.Status().Update(ctx, &job); err != nil {
		t.Fatal(err)
	}
	failed, err := r.reconcileDatabaseAccess(ctx, instance, resources.DatabaseRolesJobName, true)
	if err != nil || failed.ready || failed.reason != "DatabaseAccessFailed" {
		t.Fatalf("failed job accepted: %+v %v", failed, err)
	}
	job.Status.Conditions = nil
	job.Status.Succeeded = 1
	if err := r.Status().Update(ctx, &job); err != nil {
		t.Fatal(err)
	}
	done, err := r.reconcileDatabaseAccess(ctx, instance, resources.DatabaseRolesJobName, true)
	if err != nil || !done.ready {
		t.Fatalf("successful check not accepted: %+v %v", done, err)
	}
}

func TestCredentialDigestChangesOnlyWithConnectionData(t *testing.T) {
	a := &corev1.Secret{StringData: map[string]string{"username": "odoo_owner", "password": "test"}}
	b := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{ResourceVersion: "another"}, Data: map[string][]byte{"username": []byte("odoo_owner"), "password": []byte("test")}}
	if databaseCredentialsDigest(a) != databaseCredentialsDigest(b) {
		t.Fatal("StringData and Data or metadata cause different rollout identities")
	}
	b.Data["password"] = []byte("changed")
	if databaseCredentialsDigest(a) == databaseCredentialsDigest(b) {
		t.Fatal("changed credentials must force audit and rollout")
	}
}

func TestLegacyCutoverRequiresMaintenanceWindow(t *testing.T) {
	ctx := context.Background()
	r, instance := privilegeTestReconciler()
	secret := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Namespace: resources.TenantNamespace(instance), Name: resources.DatabaseSecretName(instance)},
		Data: map[string][]byte{"username": []byte("odoo"), "password": []byte("old")}}
	if err := r.Create(ctx, secret); err != nil {
		t.Fatal(err)
	}
	result, err := r.reconcileManagedDatabase(ctx, instance)
	if err != nil || result.ready || result.reason != "DatabaseMigrationRequired" {
		t.Fatalf("unannounced cutover allowed: %+v %v", result, err)
	}
	var admin corev1.Secret
	if err := r.Get(ctx, types.NamespacedName{Namespace: secret.Namespace, Name: resources.DatabaseAdminSecretName(instance)}, &admin); err == nil {
		t.Fatal("migration mutated admin credentials before a maintenance window")
	}
}

func TestApplicationCannotReferenceAdminSecret(t *testing.T) {
	r, instance := privilegeTestReconciler()
	instance.Spec.Database.CredentialsSecretRef = &corev1.LocalObjectReference{Name: resources.DatabaseAdminSecretName(instance)}
	if _, err := r.reconcileDatabase(context.Background(), instance); err == nil {
		t.Fatal("application allowed to mount platform admin Secret")
	}
}
