package controller

import (
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
	"github.com/freightright/odoo-saas-platform/operator/internal/resources"
)

func newTestInstance(name string) *saasv1alpha1.OdooInstance {
	return &saasv1alpha1.OdooInstance{
		ObjectMeta: metav1.ObjectMeta{Name: name},
		Spec: saasv1alpha1.OdooInstanceSpec{
			Version: "18.0",
			Image: saasv1alpha1.ImageSpec{
				Repository: "registry.example.com/odoo",
				Tag:        "18.0-abc123",
			},
			Domain: saasv1alpha1.DomainSpec{
				Hostname: name + ".example.com",
				TLS:      saasv1alpha1.TLSSpec{Enabled: false},
			},
			Storage: saasv1alpha1.InstanceStorageSpec{
				Filestore: saasv1alpha1.FilestoreSpec{Size: "1Gi"},
			},
			Workers: saasv1alpha1.WorkersSpec{Count: 2, MaxCronThreads: 1},
			Networking: saasv1alpha1.NetworkingSpec{
				NetworkPolicy: saasv1alpha1.NetworkPolicySpec{Enabled: true},
			},
			Resources: corev1.ResourceRequirements{
				Requests: corev1.ResourceList{
					corev1.ResourceCPU:    resource.MustParse("100m"),
					corev1.ResourceMemory: resource.MustParse("256Mi"),
				},
				Limits: corev1.ResourceList{
					corev1.ResourceCPU:    resource.MustParse("1"),
					corev1.ResourceMemory: resource.MustParse("1Gi"),
				},
			},
		},
	}
}

// TestReconcile_FullLifecycle walks an OdooInstance through the entire
// happy path a real cluster would, hand-driving the child-resource status
// transitions envtest cannot produce on its own (no kubelet/scheduler or
// built-in controllers run against envtest's apiserver).
func TestReconcile_FullLifecycle(t *testing.T) {
	r := testReconciler()
	instance := newTestInstance("acme")
	if err := k8sClient.Create(testCtx, instance); err != nil {
		t.Fatalf("creating instance: %v", err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, instance) })

	ns := saasv1alpha1.NamespacePrefix + "acme"

	// Step 1: first reconcile provisions tenancy, database, storage,
	// config and networking, but must NOT yet create the web Deployment
	// (gated behind the init Job, itself gated behind DatabaseReady).
	reconcileUntil(t, r, "acme", func(i *saasv1alpha1.OdooInstance) bool {
		var namespace corev1.Namespace
		return k8sClient.Get(testCtx, client.ObjectKey{Name: ns}, &namespace) == nil
	})

	assertExists(t, &corev1.ServiceAccount{}, ns, resources.OdooServiceAccountName(instance))
	assertExists(t, &corev1.ResourceQuota{}, ns, resources.ResourceQuotaName(instance))
	assertExists(t, &corev1.LimitRange{}, ns, resources.LimitRangeName(instance))
	assertExists(t, &corev1.Secret{}, ns, resources.AdminSecretName(instance))
	assertExists(t, &corev1.Secret{}, ns, resources.DatabaseSecretName(instance))
	assertExists(t, &corev1.PersistentVolumeClaim{}, ns, resources.FilestorePVCName(instance))
	assertExists(t, &appsv1.StatefulSet{}, ns, resources.DatabaseStatefulSetName(instance))
	assertExists(t, &corev1.Service{}, ns, resources.DatabaseServiceName(instance))
	assertExists(t, &corev1.ConfigMap{}, ns, resources.OdooConfigMapName(instance))
	assertExists(t, &networkingv1.NetworkPolicy{}, ns, resources.NetworkPolicyName(instance))
	assertExists(t, &networkingv1.Ingress{}, ns, resources.IngressName(instance))
	assertNotExists(t, &appsv1.Deployment{}, ns, resources.OdooDeploymentName(instance))
	assertNotExists(t, &batchv1.Job{}, ns, resources.OdooInitJobName(instance))

	// Step 2: simulate the managed PostgreSQL StatefulSet becoming ready
	// (a kubelet would normally drive this).
	patchStatefulSetReady(t, ns, resources.DatabaseStatefulSetName(instance))

	reconcileUntil(t, r, "acme", func(i *saasv1alpha1.OdooInstance) bool {
		var job batchv1.Job
		return k8sClient.Get(testCtx, client.ObjectKey{Namespace: ns, Name: resources.OdooInitJobName(instance)}, &job) == nil
	})
	assertNotExists(t, &appsv1.Deployment{}, ns, resources.OdooDeploymentName(instance))

	// Step 3: simulate the init Job completing successfully.
	patchJobSucceeded(t, ns, resources.OdooInitJobName(instance))

	reconcileUntil(t, r, "acme", func(i *saasv1alpha1.OdooInstance) bool {
		var dep appsv1.Deployment
		return k8sClient.Get(testCtx, client.ObjectKey{Namespace: ns, Name: resources.OdooDeploymentName(instance)}, &dep) == nil
	})
	assertExists(t, &corev1.Service{}, ns, resources.OdooServiceName(instance))

	// Step 4: simulate the web Deployment, Ingress, and filestore PVC (no
	// storage provisioner runs in envtest either) becoming ready.
	patchDeploymentReady(t, ns, resources.OdooDeploymentName(instance))
	patchIngressReady(t, ns, resources.IngressName(instance))
	patchPVCBound(t, ns, resources.FilestorePVCName(instance))

	final := reconcileUntil(t, r, "acme", func(i *saasv1alpha1.OdooInstance) bool {
		return i.Status.Phase == saasv1alpha1.PhaseReady
	})

	if final.Status.URL != "http://acme.example.com" {
		t.Errorf("unexpected status.url: %q", final.Status.URL)
	}
	if final.Status.TenantNamespace != ns {
		t.Errorf("unexpected status.tenantNamespace: %q", final.Status.TenantNamespace)
	}
	if cond := findCondition(final, saasv1alpha1.ConditionReady); cond == nil || cond.Status != metav1.ConditionTrue {
		t.Fatalf("expected Ready=True condition, got %+v", cond)
	}

	// Step 5: deletion must trigger the finalizer, delete the tenant
	// namespace, and remove the OdooInstance itself even though the
	// namespace's own "kubernetes" content finalizer can never clear in
	// envtest (no namespace controller runs there) — see finalizeInstance's
	// docs for why the controller does not wait on that.
	if err := k8sClient.Delete(testCtx, final); err != nil {
		t.Fatalf("deleting instance: %v", err)
	}
	if _, err := r.Reconcile(testCtx, ctrl.Request{NamespacedName: client.ObjectKey{Name: "acme"}}); err != nil {
		t.Fatalf("reconciling delete: %v", err)
	}

	var gone saasv1alpha1.OdooInstance
	if err := k8sClient.Get(testCtx, client.ObjectKey{Name: "acme"}, &gone); !errors.IsNotFound(err) {
		t.Fatalf("expected OdooInstance to be gone after finalizer removal, got err=%v", err)
	}

	var namespace corev1.Namespace
	if err := k8sClient.Get(testCtx, client.ObjectKey{Name: ns}, &namespace); err != nil {
		t.Fatalf("expected tenant namespace to still be observable (Terminating): %v", err)
	}
	if namespace.DeletionTimestamp.IsZero() {
		t.Errorf("expected tenant namespace to have a deletion timestamp set")
	}
}

// TestReconcile_InvalidSpecIsDegradedNotRetried verifies an unfixable spec
// problem surfaces as Degraded/Failed status instead of creating any child
// resources or looping forever on the same input.
func TestReconcile_InvalidSpecIsDegradedNotRetried(t *testing.T) {
	r := testReconciler()
	instance := newTestInstance("badversion")
	instance.Spec.Version = "99.0"
	if err := k8sClient.Create(testCtx, instance); err != nil {
		t.Fatalf("creating instance: %v", err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, instance) })

	final := reconcileUntil(t, r, "badversion", func(i *saasv1alpha1.OdooInstance) bool {
		return i.Status.Phase == saasv1alpha1.PhaseDegraded
	})

	cond := findCondition(final, saasv1alpha1.ConditionReady)
	if cond == nil || cond.Status != metav1.ConditionFalse || cond.Reason != "UnsupportedVersion" {
		t.Fatalf("expected Ready=False/UnsupportedVersion, got %+v", cond)
	}

	ns := saasv1alpha1.NamespacePrefix + "badversion"
	var namespace corev1.Namespace
	if err := k8sClient.Get(testCtx, client.ObjectKey{Name: ns}, &namespace); !errors.IsNotFound(err) {
		t.Fatalf("expected no tenant namespace to be created for an invalid spec, got err=%v", err)
	}
}

// TestReconcile_ExternalDatabaseWaitsForCredentialsSecret verifies External
// mode never creates database child resources and correctly blocks on a
// missing/incomplete credentials Secret rather than erroring.
func TestReconcile_ExternalDatabaseWaitsForCredentialsSecret(t *testing.T) {
	r := testReconciler()
	instance := newTestInstance("external-db")
	instance.Spec.Database = saasv1alpha1.DatabaseSpec{
		Mode:                 saasv1alpha1.DatabaseModeExternal,
		CredentialsSecretRef: &corev1.LocalObjectReference{Name: "customer-provided-db"},
	}
	if err := k8sClient.Create(testCtx, instance); err != nil {
		t.Fatalf("creating instance: %v", err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, instance) })

	ns := saasv1alpha1.NamespacePrefix + "external-db"
	final := reconcileUntil(t, r, "external-db", func(i *saasv1alpha1.OdooInstance) bool {
		var namespace corev1.Namespace
		return k8sClient.Get(testCtx, client.ObjectKey{Name: ns}, &namespace) == nil
	})

	cond := findCondition(final, saasv1alpha1.ConditionDatabaseReady)
	if cond == nil || cond.Status != metav1.ConditionFalse {
		t.Fatalf("expected DatabaseReady=False while credentials Secret is missing, got %+v", cond)
	}
	assertNotExists(t, &appsv1.StatefulSet{}, ns, resources.DatabaseStatefulSetName(instance))

	// Now provide the Secret and confirm it is picked up without the
	// controller ever having created it itself.
	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: "customer-provided-db", Namespace: ns},
		StringData: map[string]string{
			"host": "external-pg.example.com", "port": "5432",
			"dbname": "odoo", "username": "odoo", "password": "s3cr3t",
		},
	}
	if err := k8sClient.Create(testCtx, secret); err != nil {
		t.Fatalf("creating external credentials secret: %v", err)
	}

	final = reconcileUntil(t, r, "external-db", func(i *saasv1alpha1.OdooInstance) bool {
		c := findCondition(i, saasv1alpha1.ConditionDatabaseReady)
		return c != nil && c.Status == metav1.ConditionTrue
	})
	_ = final
}

// TestReconcile_RestoreConfigured_GatesWebDeploymentUntilJobSucceeds walks
// a restore-onboarded instance through the same shape of lifecycle
// TestReconcile_FullLifecycle exercises for a fresh instance, but verifies
// the mutually-exclusive path: the restore Job (not odoo-init) is what
// gates the web Deployment, and the ordinary odoo-init Job must never be
// created at all for a restore-configured instance.
func TestReconcile_RestoreConfigured_GatesWebDeploymentUntilJobSucceeds(t *testing.T) {
	r := testReconciler()
	instance := newTestInstance("restored")
	instance.Spec.Restore = &saasv1alpha1.RestoreSpec{
		Source: saasv1alpha1.RestoreSourceSpec{
			BackupDestinationSpec: saasv1alpha1.BackupDestinationSpec{
				Type:                   saasv1alpha1.BackupDestinationObjectStore,
				Bucket:                 "customer-migrations",
				ObjectStorageSecretRef: &corev1.LocalObjectReference{Name: "migration-creds"},
			},
		},
	}
	if err := k8sClient.Create(testCtx, instance); err != nil {
		t.Fatalf("creating instance: %v", err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, instance) })

	ns := saasv1alpha1.NamespacePrefix + "restored"

	reconcileUntil(t, r, "restored", func(i *saasv1alpha1.OdooInstance) bool {
		var namespace corev1.Namespace
		return k8sClient.Get(testCtx, client.ObjectKey{Name: ns}, &namespace) == nil
	})
	patchStatefulSetReady(t, ns, resources.DatabaseStatefulSetName(instance))

	reconcileUntil(t, r, "restored", func(i *saasv1alpha1.OdooInstance) bool {
		var job batchv1.Job
		return k8sClient.Get(testCtx, client.ObjectKey{Namespace: ns, Name: resources.OdooRestoreJobName(instance)}, &job) == nil
	})
	assertNotExists(t, &batchv1.Job{}, ns, resources.OdooInitJobName(instance))
	assertNotExists(t, &appsv1.Deployment{}, ns, resources.OdooDeploymentName(instance))

	restored := reconcileUntil(t, r, "restored", func(i *saasv1alpha1.OdooInstance) bool {
		c := findCondition(i, saasv1alpha1.ConditionRestoreReady)
		return c != nil
	})
	if c := findCondition(restored, saasv1alpha1.ConditionRestoreReady); c == nil || c.Status != metav1.ConditionFalse {
		t.Fatalf("expected RestoreReady=False while the Job is still running, got %+v", c)
	}
	assertNotExists(t, &appsv1.Deployment{}, ns, resources.OdooDeploymentName(instance))

	// Simulate the restore Job completing successfully.
	patchJobSucceeded(t, ns, resources.OdooRestoreJobName(instance))

	final := reconcileUntil(t, r, "restored", func(i *saasv1alpha1.OdooInstance) bool {
		var dep appsv1.Deployment
		return k8sClient.Get(testCtx, client.ObjectKey{Namespace: ns, Name: resources.OdooDeploymentName(instance)}, &dep) == nil
	})
	if c := findCondition(final, saasv1alpha1.ConditionRestoreReady); c == nil || c.Status != metav1.ConditionTrue {
		t.Fatalf("expected RestoreReady=True once the Job succeeded, got %+v", c)
	}
	assertNotExists(t, &batchv1.Job{}, ns, resources.OdooInitJobName(instance))
}

// TestReconcile_RestoreFailure_NeverCreatesWebDeployment verifies a failed
// restore Job surfaces as Degraded/WorkloadReady=False, never creates the
// web Deployment, and — reconciling repeatedly afterwards — never
// recreates the failed Job to "try again" on its own (matching
// odoo-init's own documented failure-handling contract).
func TestReconcile_RestoreFailure_NeverCreatesWebDeployment(t *testing.T) {
	r := testReconciler()
	instance := newTestInstance("restore-failed")
	instance.Spec.Restore = &saasv1alpha1.RestoreSpec{
		Source: saasv1alpha1.RestoreSourceSpec{
			BackupDestinationSpec: saasv1alpha1.BackupDestinationSpec{Type: saasv1alpha1.BackupDestinationPVC},
		},
	}
	if err := k8sClient.Create(testCtx, instance); err != nil {
		t.Fatalf("creating instance: %v", err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, instance) })

	ns := saasv1alpha1.NamespacePrefix + "restore-failed"
	reconcileUntil(t, r, "restore-failed", func(i *saasv1alpha1.OdooInstance) bool {
		var namespace corev1.Namespace
		return k8sClient.Get(testCtx, client.ObjectKey{Name: ns}, &namespace) == nil
	})
	patchStatefulSetReady(t, ns, resources.DatabaseStatefulSetName(instance))
	reconcileUntil(t, r, "restore-failed", func(i *saasv1alpha1.OdooInstance) bool {
		var job batchv1.Job
		return k8sClient.Get(testCtx, client.ObjectKey{Namespace: ns, Name: resources.OdooRestoreJobName(instance)}, &job) == nil
	})

	patchJobFailed(t, ns, resources.OdooRestoreJobName(instance))
	var jobBefore batchv1.Job
	if err := k8sClient.Get(testCtx, client.ObjectKey{Namespace: ns, Name: resources.OdooRestoreJobName(instance)}, &jobBefore); err != nil {
		t.Fatalf("getting restore job: %v", err)
	}

	final := reconcileUntil(t, r, "restore-failed", func(i *saasv1alpha1.OdooInstance) bool {
		return i.Status.Phase == saasv1alpha1.PhaseDegraded
	})
	if c := findCondition(final, saasv1alpha1.ConditionRestoreReady); c == nil || c.Status != metav1.ConditionFalse || c.Reason != "RestoreFailed" {
		t.Fatalf("expected RestoreReady=False/RestoreFailed, got %+v", c)
	}
	if c := findCondition(final, saasv1alpha1.ConditionDegraded); c == nil || c.Status != metav1.ConditionTrue {
		t.Fatalf("expected Degraded=True after restore failure, got %+v", c)
	}
	assertNotExists(t, &appsv1.Deployment{}, ns, resources.OdooDeploymentName(instance))

	// Reconcile several more times: the failed Job must never be recreated
	// (same UID/ResourceVersion) — a fresh Job would mean the restore
	// silently ran again.
	for i := 0; i < 3; i++ {
		if _, err := r.Reconcile(testCtx, ctrl.Request{NamespacedName: client.ObjectKey{Name: "restore-failed"}}); err != nil {
			t.Fatalf("reconcile #%d returned error: %v", i, err)
		}
	}
	var jobAfter batchv1.Job
	if err := k8sClient.Get(testCtx, client.ObjectKey{Namespace: ns, Name: resources.OdooRestoreJobName(instance)}, &jobAfter); err != nil {
		t.Fatalf("getting restore job after further reconciles: %v", err)
	}
	if jobAfter.UID != jobBefore.UID {
		t.Fatalf("restore Job was recreated after failure (UID changed from %s to %s) — the restore must never silently re-run", jobBefore.UID, jobAfter.UID)
	}
	assertNotExists(t, &appsv1.Deployment{}, ns, resources.OdooDeploymentName(instance))
}

// TestReconcile_RestoreIdempotent_MultipleReconcilesDoNotRerunRestore
// verifies that once a restore has succeeded, repeated reconciliation
// (the platform requirement: "reconcile / reconcile / reconcile / reconcile
// must NOT recreate the restore Job or restore the database again") is a
// true no-op with respect to the restore Job itself.
func TestReconcile_RestoreIdempotent_MultipleReconcilesDoNotRerunRestore(t *testing.T) {
	r := testReconciler()
	instance := newTestInstance("restore-idempotent")
	instance.Spec.Restore = &saasv1alpha1.RestoreSpec{
		Source: saasv1alpha1.RestoreSourceSpec{
			BackupDestinationSpec: saasv1alpha1.BackupDestinationSpec{Type: saasv1alpha1.BackupDestinationPVC},
		},
	}
	if err := k8sClient.Create(testCtx, instance); err != nil {
		t.Fatalf("creating instance: %v", err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, instance) })

	ns := saasv1alpha1.NamespacePrefix + "restore-idempotent"
	reconcileUntil(t, r, "restore-idempotent", func(i *saasv1alpha1.OdooInstance) bool {
		var namespace corev1.Namespace
		return k8sClient.Get(testCtx, client.ObjectKey{Name: ns}, &namespace) == nil
	})
	patchStatefulSetReady(t, ns, resources.DatabaseStatefulSetName(instance))
	reconcileUntil(t, r, "restore-idempotent", func(i *saasv1alpha1.OdooInstance) bool {
		var job batchv1.Job
		return k8sClient.Get(testCtx, client.ObjectKey{Namespace: ns, Name: resources.OdooRestoreJobName(instance)}, &job) == nil
	})
	patchJobSucceeded(t, ns, resources.OdooRestoreJobName(instance))

	reconcileUntil(t, r, "restore-idempotent", func(i *saasv1alpha1.OdooInstance) bool {
		var dep appsv1.Deployment
		return k8sClient.Get(testCtx, client.ObjectKey{Namespace: ns, Name: resources.OdooDeploymentName(instance)}, &dep) == nil
	})
	var jobBefore batchv1.Job
	if err := k8sClient.Get(testCtx, client.ObjectKey{Namespace: ns, Name: resources.OdooRestoreJobName(instance)}, &jobBefore); err != nil {
		t.Fatalf("getting restore job: %v", err)
	}

	for i := 0; i < 4; i++ {
		if _, err := r.Reconcile(testCtx, ctrl.Request{NamespacedName: client.ObjectKey{Name: "restore-idempotent"}}); err != nil {
			t.Fatalf("reconcile #%d returned error: %v", i, err)
		}
	}

	var jobAfter batchv1.Job
	if err := k8sClient.Get(testCtx, client.ObjectKey{Namespace: ns, Name: resources.OdooRestoreJobName(instance)}, &jobAfter); err != nil {
		t.Fatalf("getting restore job after further reconciles: %v", err)
	}
	if jobAfter.UID != jobBefore.UID || jobAfter.ResourceVersion != jobBefore.ResourceVersion {
		t.Fatalf("restore Job was recreated/modified across idempotent reconciles: before=%+v after=%+v", jobBefore.ObjectMeta, jobAfter.ObjectMeta)
	}
}

func findCondition(instance *saasv1alpha1.OdooInstance, conditionType string) *metav1.Condition {
	for i := range instance.Status.Conditions {
		if instance.Status.Conditions[i].Type == conditionType {
			return &instance.Status.Conditions[i]
		}
	}
	return nil
}

func assertExists(t *testing.T, obj client.Object, namespace, name string) {
	t.Helper()
	if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: namespace, Name: name}, obj); err != nil {
		t.Errorf("expected %T %s/%s to exist: %v", obj, namespace, name, err)
	}
}

func assertNotExists(t *testing.T, obj client.Object, namespace, name string) {
	t.Helper()
	err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: namespace, Name: name}, obj)
	if !errors.IsNotFound(err) {
		t.Errorf("expected %T %s/%s to NOT exist yet, got err=%v", obj, namespace, name, err)
	}
}

func patchStatefulSetReady(t *testing.T, namespace, name string) {
	t.Helper()
	var sts appsv1.StatefulSet
	if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: namespace, Name: name}, &sts); err != nil {
		t.Fatalf("getting statefulset %s/%s: %v", namespace, name, err)
	}
	sts.Status.ReadyReplicas = 1
	sts.Status.Replicas = 1
	if err := k8sClient.Status().Update(testCtx, &sts); err != nil {
		t.Fatalf("patching statefulset status: %v", err)
	}
}

func patchJobSucceeded(t *testing.T, namespace, name string) {
	t.Helper()
	var job batchv1.Job
	if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: namespace, Name: name}, &job); err != nil {
		t.Fatalf("getting job %s/%s: %v", namespace, name, err)
	}
	job.Status.Succeeded = 1
	if err := k8sClient.Status().Update(testCtx, &job); err != nil {
		t.Fatalf("patching job status: %v", err)
	}
}

func patchJobFailed(t *testing.T, namespace, name string) {
	t.Helper()
	var job batchv1.Job
	if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: namespace, Name: name}, &job); err != nil {
		t.Fatalf("getting job %s/%s: %v", namespace, name, err)
	}
	job.Status.Failed = 1
	job.Status.Conditions = append(job.Status.Conditions, batchv1.JobCondition{
		Type:    batchv1.JobFailed,
		Status:  corev1.ConditionTrue,
		Reason:  "BackoffLimitExceeded",
		Message: "simulated failure for test",
	})
	if err := k8sClient.Status().Update(testCtx, &job); err != nil {
		t.Fatalf("patching job status: %v", err)
	}
}

func patchDeploymentReady(t *testing.T, namespace, name string) {
	t.Helper()
	var dep appsv1.Deployment
	if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: namespace, Name: name}, &dep); err != nil {
		t.Fatalf("getting deployment %s/%s: %v", namespace, name, err)
	}
	dep.Status.ReadyReplicas = ptr.Deref(dep.Spec.Replicas, 1)
	dep.Status.Replicas = ptr.Deref(dep.Spec.Replicas, 1)
	if err := k8sClient.Status().Update(testCtx, &dep); err != nil {
		t.Fatalf("patching deployment status: %v", err)
	}
}

func patchIngressReady(t *testing.T, namespace, name string) {
	t.Helper()
	var ing networkingv1.Ingress
	if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: namespace, Name: name}, &ing); err != nil {
		t.Fatalf("getting ingress %s/%s: %v", namespace, name, err)
	}
	ing.Status.LoadBalancer.Ingress = []networkingv1.IngressLoadBalancerIngress{{IP: "203.0.113.10"}}
	if err := k8sClient.Status().Update(testCtx, &ing); err != nil {
		t.Fatalf("patching ingress status: %v", err)
	}
}

func patchPVCBound(t *testing.T, namespace, name string) {
	t.Helper()
	var pvc corev1.PersistentVolumeClaim
	if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: namespace, Name: name}, &pvc); err != nil {
		t.Fatalf("getting pvc %s/%s: %v", namespace, name, err)
	}
	pvc.Status.Phase = corev1.ClaimBound
	if err := k8sClient.Status().Update(testCtx, &pvc); err != nil {
		t.Fatalf("patching pvc status: %v", err)
	}
}
