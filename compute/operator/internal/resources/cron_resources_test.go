package resources

import (
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	"testing"
)

func TestCronSidecarIsSmallerAndDoesNotServeHTTP(t *testing.T) {
	instance := testInstance()
	instance.Spec.Resources.Limits = corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("1"), corev1.ResourceMemory: resource.MustParse("2Gi")}
	pod := OdooDeployment(instance).Spec.Template.Spec
	if len(pod.Containers) != 2 {
		t.Fatal("expected one web and one cron container in one pod")
	}
	web, cron := pod.Containers[0], pod.Containers[1]
	if cron.Name != "cron" || !containsArg(cron.Args, "--no-http") || !containsArg(cron.Args, "--workers=0") || !containsArg(cron.Args, "--max-cron-threads=1") {
		t.Fatalf("unexpected cron args: %v", cron.Args)
	}
	if !containsArg(web.Args, "--max-cron-threads=0") {
		t.Fatal("cron would execute in both containers")
	}
	if len(cron.Ports) != 0 || cron.LivenessProbe.HTTPGet != nil || cron.StartupProbe.HTTPGet != nil {
		t.Fatal("cron must not expose an HTTP listener")
	}
	for key, want := range (corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("250m"), corev1.ResourceMemory: resource.MustParse("512Mi")}) {
		got := cron.Resources.Limits[key]
		if got.Cmp(want) != 0 || got.Cmp(web.Resources.Limits[key]) >= 0 {
			t.Fatalf("cron %s limit = %s", key, got.String())
		}
		request := cron.Resources.Requests[key]
		if request.Sign() <= 0 || request.Cmp(got) > 0 {
			t.Fatal("cron request must be positive and below its limit")
		}
	}
	if cron.Image != web.Image {
		t.Fatal("cron must run the same deployed addon image")
	}
}

func TestCronLimitsStayCappedOnVerticalResize(t *testing.T) {
	instance := testInstance()
	instance.Spec.Resources.Limits = corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("8"), corev1.ResourceMemory: resource.MustParse("16Gi")}
	cron := CronResources(instance)
	if cpu := cron.Limits[corev1.ResourceCPU]; cpu.Cmp(resource.MustParse("500m")) != 0 {
		t.Fatal("cron CPU grew with the large web limit")
	}
	if memory := cron.Limits[corev1.ResourceMemory]; memory.Cmp(resource.MustParse("512Mi")) != 0 {
		t.Fatal("cron RAM grew with the large web limit")
	}
	instance.Spec.Workers.MaxCronThreads = 0
	pod := OdooDeployment(instance).Spec.Template.Spec
	if len(pod.Containers) != 1 || !containsArg(pod.Containers[0].Args, "--max-cron-threads=0") {
		t.Fatal("explicitly disabled cron still reserves resources")
	}
}
