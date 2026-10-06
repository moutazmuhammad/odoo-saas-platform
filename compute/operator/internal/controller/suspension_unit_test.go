package controller

import (
	"context"
	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
	"github.com/freightright/odoo-saas-platform/operator/internal/resources"
	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"testing"
)

func TestSuspensionStopsComputePreservesStorageAndResumesJobs(t *testing.T) {
	instance := validSpecInstance()
	instance.Spec.Suspended = true
	ns := resources.TenantNamespace(instance)
	meta := func(name string) metav1.ObjectMeta {
		return metav1.ObjectMeta{Name: name, Namespace: ns, Labels: resources.SelectorLabels(instance)}
	}
	dep := &appsv1.Deployment{ObjectMeta: meta("odoo"), Spec: appsv1.DeploymentSpec{Replicas: ptr.To(int32(1))}}
	sts := &appsv1.StatefulSet{ObjectMeta: meta("postgresql"), Spec: appsv1.StatefulSetSpec{Replicas: ptr.To(int32(1)), PersistentVolumeClaimRetentionPolicy: &appsv1.StatefulSetPersistentVolumeClaimRetentionPolicy{WhenScaled: appsv1.DeletePersistentVolumeClaimRetentionPolicyType}}}
	schedule := &batchv1.CronJob{ObjectMeta: meta("backup")}
	job := &batchv1.Job{ObjectMeta: metav1.ObjectMeta{Name: "active-backup", Namespace: ns}, Spec: batchv1.JobSpec{Template: corev1.PodTemplateSpec{ObjectMeta: metav1.ObjectMeta{Labels: resources.WithComponent(instance, "backup")}}}}
	manual := &batchv1.Job{ObjectMeta: meta("manual"), Spec: batchv1.JobSpec{Suspend: ptr.To(true)}}
	completed := &batchv1.Job{ObjectMeta: meta("completed"), Status: batchv1.JobStatus{Conditions: []batchv1.JobCondition{{Type: batchv1.JobComplete, Status: corev1.ConditionTrue}}}}
	unrelated := &batchv1.Job{ObjectMeta: metav1.ObjectMeta{Name: "other-tenant", Namespace: ns}}
	pvc := &corev1.PersistentVolumeClaim{ObjectMeta: meta("odoo-filestore")}
	scheme := runtime.NewScheme()
	_ = appsv1.AddToScheme(scheme)
	_ = batchv1.AddToScheme(scheme)
	_ = corev1.AddToScheme(scheme)
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(dep, sts, schedule, job, manual, completed, unrelated, pvc).Build()
	r := &OdooInstanceReconciler{Client: c}
	ctx := context.Background()
	for i := 0; i < 2; i++ {
		if waiting, err := r.suspendTenantWorkloads(ctx, instance); err != nil || waiting {
			t.Fatal(waiting, err)
		}
	}
	for _, obj := range []client.Object{dep, sts, schedule, job, manual, completed, unrelated, pvc} {
		if err := c.Get(ctx, client.ObjectKeyFromObject(obj), obj); err != nil {
			t.Fatal(err)
		}
	}
	if *dep.Spec.Replicas != 0 || *sts.Spec.Replicas != 0 {
		t.Fatal("tenant compute remains running")
	}
	if sts.Spec.PersistentVolumeClaimRetentionPolicy.WhenScaled != appsv1.RetainPersistentVolumeClaimRetentionPolicyType {
		t.Fatal("scale-down could delete tenant data")
	}
	if !ptr.Deref(schedule.Spec.Suspend, false) || !ptr.Deref(job.Spec.Suspend, false) {
		t.Fatal("background jobs still running")
	}
	if ptr.Deref(completed.Spec.Suspend, false) || ptr.Deref(unrelated.Spec.Suspend, false) {
		t.Fatal("unrelated or completed job modified")
	}
	if err := r.resumeTenantJobs(ctx, instance); err != nil {
		t.Fatal(err)
	}
	_ = c.Get(ctx, client.ObjectKeyFromObject(job), job)
	_ = c.Get(ctx, client.ObjectKeyFromObject(manual), manual)
	_ = c.Get(ctx, client.ObjectKeyFromObject(schedule), schedule)
	if ptr.Deref(schedule.Spec.Suspend, false) {
		t.Fatal("backup schedule did not resume")
	}
	if ptr.Deref(job.Spec.Suspend, false) || job.Annotations[suspensionJobAnnotation] != "" {
		t.Fatal("suspension-paused job did not resume")
	}
	if !ptr.Deref(manual.Spec.Suspend, false) {
		t.Fatal("user-paused job resumed unexpectedly")
	}
	instance.Spec.Suspended = false
	if *resources.DatabaseStatefulSet(instance, resources.Platform{}).Spec.Replicas != 1 || *resources.BackupCronJob(instance, resources.Platform{BackupToolImage: "backup:test"}).Spec.Suspend {
		t.Fatal("resume desired state remains suspended")
	}
}

func TestSuspensionLeavesDatabaseJobsRunningAndDefersDatabaseStop(t *testing.T) {
	instance := validSpecInstance()
	instance.Spec.Suspended = true
	ns := resources.TenantNamespace(instance)
	meta := func(name string) metav1.ObjectMeta {
		return metav1.ObjectMeta{Name: name, Namespace: ns, Labels: resources.SelectorLabels(instance)}
	}
	dep := &appsv1.Deployment{ObjectMeta: meta("odoo"), Spec: appsv1.DeploymentSpec{Replicas: ptr.To(int32(1))}}
	sts := &appsv1.StatefulSet{ObjectMeta: meta("postgresql"), Spec: appsv1.StatefulSetSpec{Replicas: ptr.To(int32(1))}}
	update := &batchv1.Job{ObjectMeta: meta("odoo-update")}
	scheme := runtime.NewScheme()
	_ = appsv1.AddToScheme(scheme)
	_ = batchv1.AddToScheme(scheme)
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(dep, sts, update).Build()
	r := &OdooInstanceReconciler{Client: c}
	ctx := context.Background()
	waiting, err := r.suspendTenantWorkloads(ctx, instance)
	if err != nil || !waiting {
		t.Fatalf("waiting = %v, err = %v; want to wait for the update Job", waiting, err)
	}
	for _, obj := range []client.Object{dep, sts, update} {
		if err := c.Get(ctx, client.ObjectKeyFromObject(obj), obj); err != nil {
			t.Fatal(err)
		}
	}
	if ptr.Deref(update.Spec.Suspend, false) {
		t.Fatal("database-writing Job paused midway")
	}
	if *dep.Spec.Replicas != 0 {
		t.Fatal("Odoo still serving while suspended")
	}
	if *sts.Spec.Replicas != 1 {
		t.Fatal("database stopped under a running Job")
	}
	update.Status.Conditions = []batchv1.JobCondition{{Type: batchv1.JobComplete, Status: corev1.ConditionTrue}}
	if err := c.Status().Update(ctx, update); err != nil {
		t.Fatal(err)
	}
	if waiting, err := r.suspendTenantWorkloads(ctx, instance); err != nil || waiting {
		t.Fatal(waiting, err)
	}
	_ = c.Get(ctx, client.ObjectKeyFromObject(sts), sts)
	if *sts.Spec.Replicas != 0 {
		t.Fatal("database not stopped after the Job finished")
	}
}

func TestSuspensionHibernatesCNPGAndLeavesExternalDatabaseAlone(t *testing.T) {
	for _, mode := range []saasv1alpha1.DatabaseMode{saasv1alpha1.DatabaseModeCloudNativePG, saasv1alpha1.DatabaseModeExternal} {
		instance := validSpecInstance()
		instance.Spec.Database.Mode = mode
		cluster := resources.CloudNativePGCluster(instance, resources.Platform{})
		scheme := runtime.NewScheme()
		_ = appsv1.AddToScheme(scheme)
		_ = batchv1.AddToScheme(scheme)
		c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(cluster).Build()
		r := &OdooInstanceReconciler{Client: c}
		if _, err := r.suspendTenantWorkloads(context.Background(), instance); err != nil {
			t.Fatal(err)
		}
		if err := c.Get(context.Background(), client.ObjectKeyFromObject(cluster), cluster); err != nil {
			t.Fatal(err)
		}
		want := "off"
		if mode == saasv1alpha1.DatabaseModeCloudNativePG {
			want = "on"
		}
		if cluster.GetAnnotations()["cnpg.io/hibernation"] != want {
			t.Fatalf("mode %s: wrong database state", mode)
		}
		if resources.CloudNativePGCluster(instance, resources.Platform{}).GetAnnotations()["cnpg.io/hibernation"] != "off" {
			t.Fatal("CNPG resume leaves hibernation enabled")
		}
	}
}
