package controller

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	policyv1 "k8s.io/api/policy/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

// This suite runs the reconciler directly (not via a full manager) against
// a real envtest API server (kube-apiserver + etcd, no kubelet/scheduler or
// built-in controllers). That means Deployment/StatefulSet/Job status
// fields never progress on their own the way they would in a live cluster
// — tests that need to observe "readiness" therefore patch child resource
// status by hand to simulate what a kubelet/controller-manager would do,
// exactly like the reconciler itself does not implement mock behavior.
var (
	testEnv   *envtest.Environment
	testCfg   *rest.Config
	k8sClient client.Client
	testCtx   context.Context
	testStop  context.CancelFunc
)

func TestMain(m *testing.M) {
	logf.SetLogger(zap.New(zap.WriteTo(nil), zap.UseDevMode(true)))

	testCtx, testStop = context.WithCancel(context.Background())

	testEnv = &envtest.Environment{
		CRDDirectoryPaths:     []string{filepath.Join("..", "..", "config", "crd", "bases")},
		ErrorIfCRDPathMissing: true,
	}

	var err error
	testCfg, err = testEnv.Start()
	if err != nil {
		panic(err)
	}

	scheme := buildTestScheme()
	k8sClient, err = client.New(testCfg, client.Options{Scheme: scheme})
	if err != nil {
		panic(err)
	}

	code := m.Run()

	testStop()
	if err := testEnv.Stop(); err != nil {
		panic(err)
	}
	os.Exit(code)
}

func buildTestScheme() *runtime.Scheme {
	scheme := runtime.NewScheme()
	_ = clientgoscheme.AddToScheme(scheme)
	_ = saasv1alpha1.AddToScheme(scheme)
	_ = appsv1.AddToScheme(scheme)
	_ = corev1.AddToScheme(scheme)
	_ = batchv1.AddToScheme(scheme)
	_ = networkingv1.AddToScheme(scheme)
	_ = policyv1.AddToScheme(scheme)
	return scheme
}

// testReconciler returns a reconciler configured for the Ingress networking
// path (so tests never require the Gateway API CRDs to be present in the
// envtest apiserver) with backups/TLS/CloudNativePG left at their
// zero-dependency defaults.
func testReconciler() *OdooInstanceReconciler {
	ingressClass := "test"
	return &OdooInstanceReconciler{
		Client:                k8sClient,
		Scheme:                k8sClient.Scheme(),
		Recorder:              &discardRecorder{},
		NetworkingProvider:    NetworkingProviderIngress,
		IngressClassName:      &ingressClass,
		SupportedOdooVersions: []string{"17.0", "18.0", "19.0"},
	}
}

// reconcileUntil calls Reconcile repeatedly (envtest has no built-in
// requeue scheduler) until check returns true or the timeout elapses,
// failing the test on timeout. Each iteration also re-fetches instance.
func reconcileUntil(t *testing.T, r *OdooInstanceReconciler, name string, check func(*saasv1alpha1.OdooInstance) bool) *saasv1alpha1.OdooInstance {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for {
		_, err := r.Reconcile(testCtx, ctrl.Request{NamespacedName: client.ObjectKey{Name: name}})
		if err != nil {
			t.Logf("reconcile returned error (may be expected mid-sequence): %v", err)
		}
		var instance saasv1alpha1.OdooInstance
		if getErr := k8sClient.Get(testCtx, client.ObjectKey{Name: name}, &instance); getErr == nil {
			if check(&instance) {
				return &instance
			}
		}
		if time.Now().After(deadline) {
			var instance saasv1alpha1.OdooInstance
			_ = k8sClient.Get(testCtx, client.ObjectKey{Name: name}, &instance)
			t.Logf("last observed status for %q: phase=%s conditions=%+v", name, instance.Status.Phase, instance.Status.Conditions)
			t.Fatalf("condition not met before deadline for instance %q", name)
		}
		time.Sleep(100 * time.Millisecond)
	}
}

// discardRecorder is a no-op record.EventRecorder for tests.
type discardRecorder struct{}

func (d *discardRecorder) Event(object runtime.Object, eventtype, reason, message string) {}
func (d *discardRecorder) Eventf(object runtime.Object, eventtype, reason, messageFmt string, args ...interface{}) {
}
func (d *discardRecorder) AnnotatedEventf(object runtime.Object, annotations map[string]string, eventtype, reason, messageFmt string, args ...interface{}) {
}
