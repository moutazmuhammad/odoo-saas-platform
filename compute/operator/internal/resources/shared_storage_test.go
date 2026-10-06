package resources

import (
	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	"testing"
)

func TestSharedStorageUsesOneClaimAndIsolatedDirectories(t *testing.T) {
	instance := testInstance()
	instance.Spec.Storage.SharedWithDatabase = true
	sts := DatabaseStatefulSet(instance)
	if len(sts.Spec.VolumeClaimTemplates) != 0 {
		t.Fatal("shared database must not provision a second claim")
	}
	pod := sts.Spec.Template.Spec
	if len(pod.InitContainers) != 1 || pod.InitContainers[0].Name != "prepare-shared-data" {
		t.Fatal("missing non-root directory preparation")
	}
	if *pod.InitContainers[0].SecurityContext.RunAsUser != 999 {
		t.Fatal("database directory must be owned by postgres")
	}
	if *pod.SecurityContext.FSGroup != odooImageGID || *pod.SecurityContext.FSGroupChangePolicy != corev1.FSGroupChangeOnRootMismatch {
		t.Fatal("shared volume must preserve private directory permissions")
	}
	if pod.Containers[0].VolumeMounts[0].SubPath != "postgres" {
		t.Fatal("postgres must mount its private directory")
	}
	claim := ""
	for _, v := range pod.Volumes {
		if v.Name == DatabasePVCName(instance) {
			claim = v.PersistentVolumeClaim.ClaimName
		}
	}
	if claim != FilestorePVCName(instance) || DatabaseDataPVCName(instance) != claim {
		t.Fatal("database must use the tenant's only claim")
	}
	size, err := DatabaseStorageSize(instance)
	if err != nil {
		t.Fatal(err)
	}
	if size.String() != "10Gi" {
		t.Fatal("database resizing must use the total shared budget")
	}
	restored := instance.DeepCopy()
	restored.Spec.Restore = &saasv1alpha1.RestoreSpec{}
	consumers := []corev1.PodSpec{
		OdooDeployment(instance).Spec.Template.Spec,
		OdooInitJob(instance).Spec.Template.Spec,
		OdooRestoreJob(restored, DefaultRestoreToolImage).Spec.Template.Spec,
		BackupCronJob(instance, DefaultBackupToolImage).Spec.JobTemplate.Spec.Template.Spec,
	}
	for _, p := range consumers {
		if p.Affinity == nil || p.Affinity.PodAffinity == nil || len(p.Affinity.PodAffinity.RequiredDuringSchedulingIgnoredDuringExecution) == 0 {
			t.Fatal("RWO consumers must co-locate even before web pods exist")
		}
		if p.SecurityContext.FSGroupChangePolicy == nil || *p.SecurityContext.FSGroupChangePolicy != corev1.FSGroupChangeOnRootMismatch {
			t.Fatal("consumer may recursively change postgres directory permissions")
		}
		for _, c := range append(p.InitContainers, p.Containers...) {
			for _, m := range c.VolumeMounts {
				if m.Name == "filestore" && m.SubPath != "odoo" {
					t.Fatalf("%s exposes the postgres directory", c.Name)
				}
			}
		}
	}
	instance.Spec.Shell = true
	web := OdooDeployment(instance).Spec.Template.Spec
	for _, c := range web.Containers {
		for _, m := range c.VolumeMounts {
			if m.Name == "filestore" && m.SubPath != "odoo" {
				t.Fatal("shell can access database files")
			}
		}
	}
}
