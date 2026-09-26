package controller

import (
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
	"github.com/freightright/odoo-saas-platform/operator/internal/resources"
)

// readyInstance drives a new instance to phase Ready (see
// TestReconcile_FullLifecycle for what each step simulates).
func readyInstance(t *testing.T, r *OdooInstanceReconciler, name string) (*saasv1alpha1.OdooInstance, string) {
	t.Helper()
	instance := newTestInstance(name)
	if err := k8sClient.Create(testCtx, instance); err != nil {
		t.Fatalf("creating instance: %v", err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, instance) })
	ns := saasv1alpha1.NamespacePrefix + name

	reconcileUntil(t, r, name, func(i *saasv1alpha1.OdooInstance) bool {
		var namespace corev1.Namespace
		return k8sClient.Get(testCtx, client.ObjectKey{Name: ns}, &namespace) == nil
	})
	patchStatefulSetReady(t, ns, resources.DatabaseStatefulSetName(instance))
	reconcileUntil(t, r, name, func(i *saasv1alpha1.OdooInstance) bool {
		var job batchv1.Job
		return k8sClient.Get(testCtx, client.ObjectKey{Namespace: ns, Name: resources.OdooInitJobName(instance)}, &job) == nil
	})
	patchJobSucceeded(t, ns, resources.OdooInitJobName(instance))
	reconcileUntil(t, r, name, func(i *saasv1alpha1.OdooInstance) bool {
		var dep appsv1.Deployment
		return k8sClient.Get(testCtx, client.ObjectKey{Namespace: ns, Name: resources.OdooDeploymentName(instance)}, &dep) == nil
	})
	patchDeploymentReady(t, ns, resources.OdooDeploymentName(instance))
	patchIngressReady(t, ns, resources.IngressName(instance))
	patchPVCBound(t, ns, resources.FilestorePVCName(instance))
	final := reconcileUntil(t, r, name, func(i *saasv1alpha1.OdooInstance) bool {
		return i.Status.Phase == saasv1alpha1.PhaseReady
	})
	return final, ns
}

func updateSpec(t *testing.T, name string, mutate func(*saasv1alpha1.OdooInstance)) {
	t.Helper()
	var live saasv1alpha1.OdooInstance
	if err := k8sClient.Get(testCtx, client.ObjectKey{Name: name}, &live); err != nil {
		t.Fatalf("getting instance: %v", err)
	}
	original := live.DeepCopy()
	mutate(&live)
	if err := k8sClient.Patch(testCtx, &live, client.MergeFrom(original)); err != nil {
		t.Fatalf("patching instance spec: %v", err)
	}
}

func webDeployment(t *testing.T, ns string) *appsv1.Deployment {
	t.Helper()
	var dep appsv1.Deployment
	if err := k8sClient.Get(testCtx, client.ObjectKey{Namespace: ns, Name: "odoo"}, &dep); err != nil {
		t.Fatalf("getting web deployment: %v", err)
	}
	return &dep
}

func configData(t *testing.T, ns, name string) string {
	t.Helper()
	var cm corev1.ConfigMap
	if err := k8sClient.Get(testCtx, client.ObjectKey{Namespace: ns, Name: name}, &cm); err != nil {
		t.Fatalf("getting configmap %s: %v", name, err)
	}
	return cm.Data["odoo.conf.tmpl"]
}

// An image bump on a running instance must reach the web Deployment: the
// init Job (which embeds the image and whose pod template is immutable)
// must not be re-applied and block the reconcile.
func TestReconcile_ImageChangeRollsDeploymentWithoutTouchingInitJob(t *testing.T) {
	r := testReconciler()
	_, ns := readyInstance(t, r, "imgbump")

	updateSpec(t, "imgbump", func(i *saasv1alpha1.OdooInstance) { i.Spec.Image.Tag = "18.0-def456" })
	reconcileUntil(t, r, "imgbump", func(i *saasv1alpha1.OdooInstance) bool {
		return webDeployment(t, ns).Spec.Template.Spec.Containers[0].Image == "registry.example.com/odoo:18.0-def456"
	})
	final := reconcileUntil(t, r, "imgbump", func(i *saasv1alpha1.OdooInstance) bool { return true })
	if cond := findCondition(final, saasv1alpha1.ConditionDegraded); cond != nil && cond.Status == metav1.ConditionTrue {
		t.Fatalf("image change left the instance Degraded: %+v", cond)
	}
}

// A pending spec.update keeps the pods on the previous image until the
// update Job (run with the new image and addons paths) succeeds.
func TestReconcile_UpdateHoldsOldImageUntilUpdateJobSucceeds(t *testing.T) {
	r := testReconciler()
	_, ns := readyInstance(t, r, "updok")

	updateSpec(t, "updok", func(i *saasv1alpha1.OdooInstance) {
		i.Spec.Image.Tag = "18.0-build1"
		i.Spec.AddonsPaths = []string{"/opt/tenant-addons/repo1"}
		i.Spec.Update = &saasv1alpha1.UpdateSpec{Token: "build-1", Modules: []string{"my_module", "other"}}
	})
	var probe saasv1alpha1.OdooInstance
	probe.Spec.Update = &saasv1alpha1.UpdateSpec{Token: "build-1"}
	jobName := resources.OdooUpdateJobName(&probe)

	held := reconcileUntil(t, r, "updok", func(i *saasv1alpha1.OdooInstance) bool {
		var job batchv1.Job
		return k8sClient.Get(testCtx, client.ObjectKey{Namespace: ns, Name: jobName}, &job) == nil
	})
	if got := webDeployment(t, ns).Spec.Template.Spec.Containers[0].Image; got != "registry.example.com/odoo:18.0-abc123" {
		t.Fatalf("web Deployment switched image before the update Job succeeded: %s", got)
	}
	if strings.Contains(configData(t, ns, resources.OdooConfigMapName(held)), "addons_path") {
		t.Fatal("serving odoo.conf got the new addons_path before the update applied")
	}
	if !strings.Contains(configData(t, ns, resources.OdooUpdateConfigMapName(held)), "addons_path = /opt/tenant-addons/repo1") {
		t.Fatal("update Job's odoo.conf is missing the new addons_path")
	}
	var job batchv1.Job
	_ = k8sClient.Get(testCtx, client.ObjectKey{Namespace: ns, Name: jobName}, &job)
	c := job.Spec.Template.Spec.Containers[0]
	if c.Image != "registry.example.com/odoo:18.0-build1" || !hasArg(c.Args, "odoo") ||
		len(c.Env) != 1 || c.Env[0].Value != "my_module,other" {
		t.Fatalf("unexpected update Job container: image=%s args=%v env=%v", c.Image, c.Args, c.Env)
	}
	if cond := findCondition(held, saasv1alpha1.ConditionUpdateReady); cond == nil || cond.Reason != ReasonUpdateRunning {
		t.Fatalf("expected UpdateReady reason %s, got %+v", ReasonUpdateRunning, cond)
	}

	patchJobSucceeded(t, ns, jobName)
	final := reconcileUntil(t, r, "updok", func(i *saasv1alpha1.OdooInstance) bool {
		return i.Status.AppliedUpdateToken == "build-1"
	})
	dep := webDeployment(t, ns)
	if got := dep.Spec.Template.Spec.Containers[0].Image; got != "registry.example.com/odoo:18.0-build1" {
		t.Fatalf("web Deployment not rolled forward after the update: %s", got)
	}
	if dep.Spec.Template.Annotations[resources.AnnotationAddonsPaths] != "/opt/tenant-addons/repo1" {
		t.Fatalf("addons-paths annotation missing: %v", dep.Spec.Template.Annotations)
	}
	if !strings.Contains(configData(t, ns, resources.OdooConfigMapName(final)), "addons_path = /opt/tenant-addons/repo1") {
		t.Fatal("serving odoo.conf not updated after the update applied")
	}
	if cond := findCondition(final, saasv1alpha1.ConditionUpdateReady); cond == nil || cond.Status != metav1.ConditionTrue {
		t.Fatalf("expected UpdateReady=True, got %+v", cond)
	}
}

// A failed update Job leaves the previous image serving and the instance
// Ready; UpdateReady says why.
func TestReconcile_FailedUpdateKeepsPreviousImageServing(t *testing.T) {
	r := testReconciler()
	_, ns := readyInstance(t, r, "updfail")

	updateSpec(t, "updfail", func(i *saasv1alpha1.OdooInstance) {
		i.Spec.Image.Tag = "18.0-broken"
		i.Spec.Update = &saasv1alpha1.UpdateSpec{Token: "build-bad", Modules: []string{"my_module"}}
	})
	var probe saasv1alpha1.OdooInstance
	probe.Spec.Update = &saasv1alpha1.UpdateSpec{Token: "build-bad"}
	jobName := resources.OdooUpdateJobName(&probe)
	reconcileUntil(t, r, "updfail", func(i *saasv1alpha1.OdooInstance) bool {
		var job batchv1.Job
		return k8sClient.Get(testCtx, client.ObjectKey{Namespace: ns, Name: jobName}, &job) == nil
	})
	patchJobFailed(t, ns, jobName)

	final := reconcileUntil(t, r, "updfail", func(i *saasv1alpha1.OdooInstance) bool {
		c := findCondition(i, saasv1alpha1.ConditionUpdateReady)
		return c != nil && c.Reason == ReasonUpdateFailed
	})
	if got := webDeployment(t, ns).Spec.Template.Spec.Containers[0].Image; got != "registry.example.com/odoo:18.0-abc123" {
		t.Fatalf("failed update switched the serving image: %s", got)
	}
	if final.Status.Phase != saasv1alpha1.PhaseReady {
		t.Fatalf("failed update should not take the instance out of Ready, phase=%s", final.Status.Phase)
	}
	if final.Status.AppliedUpdateToken == "build-bad" {
		t.Fatal("failed update was marked applied")
	}

	// A new token (the fix) supersedes it and removes the stale Job.
	updateSpec(t, "updfail", func(i *saasv1alpha1.OdooInstance) {
		i.Spec.Image.Tag = "18.0-fixed"
		i.Spec.Update = &saasv1alpha1.UpdateSpec{Token: "build-fix", Modules: []string{"my_module"}}
	})
	reconcileUntil(t, r, "updfail", func(i *saasv1alpha1.OdooInstance) bool {
		var job batchv1.Job
		return k8sClient.Get(testCtx, client.ObjectKey{Namespace: ns, Name: jobName}, &job) != nil ||
			!job.DeletionTimestamp.IsZero()
	})
}

// An update without modules rolls the new image straight out.
func TestReconcile_UpdateWithoutModulesRollsOutDirectly(t *testing.T) {
	r := testReconciler()
	_, ns := readyInstance(t, r, "updnomods")

	updateSpec(t, "updnomods", func(i *saasv1alpha1.OdooInstance) {
		i.Spec.Image.Tag = "18.0-build2"
		i.Spec.Update = &saasv1alpha1.UpdateSpec{Token: "build-2"}
	})
	reconcileUntil(t, r, "updnomods", func(i *saasv1alpha1.OdooInstance) bool {
		return i.Status.AppliedUpdateToken == "build-2" &&
			webDeployment(t, ns).Spec.Template.Spec.Containers[0].Image == "registry.example.com/odoo:18.0-build2"
	})
}

func TestSplitImage(t *testing.T) {
	cases := map[string][2]string{
		"odoo:18.0":                           {"odoo", "18.0"},
		"localhost:32000/tenant-acme:18.0-ab": {"localhost:32000/tenant-acme", "18.0-ab"},
	}
	for in, want := range cases {
		repo, tag, ok := splitImage(in)
		if !ok || repo != want[0] || tag != want[1] {
			t.Errorf("splitImage(%q) = %q, %q, %v", in, repo, tag, ok)
		}
	}
	if _, _, ok := splitImage("localhost:32000/untagged"); ok {
		t.Error("untagged image with a registry port must not parse")
	}
}

func hasArg(args []string, want string) bool {
	for _, a := range args {
		if a == want {
			return true
		}
	}
	return false
}
