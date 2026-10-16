package resources

import (
	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	"strings"
	"testing"
)

func TestDatabaseAdminCredentialsIsolatedFromCustomerCode(t *testing.T) {
	instance := testInstance()
	instance.Spec.Restore = &saasv1alpha1.RestoreSpec{}
	postgres := DatabaseStatefulSet(instance, Platform{})
	for _, env := range postgres.Spec.Template.Spec.Containers[0].Env {
		if env.Name == "POSTGRES_USER" || env.Name == "POSTGRES_PASSWORD" {
			if env.ValueFrom.SecretKeyRef.Name != DatabaseAdminSecretName(instance) {
				t.Fatalf("PostgreSQL uses application credentials: %s", env.Name)
			}
		}
	}
	pods := []corev1.PodSpec{
		OdooDeployment(instance, Platform{}).Spec.Template.Spec,
		OdooInitJob(instance, Platform{}).Spec.Template.Spec,
		OdooRestoreJob(instance, Platform{}).Spec.Template.Spec,
		BackupCronJob(instance, Platform{}).Spec.JobTemplate.Spec.Template.Spec,
	}
	for _, pod := range pods {
		containers := append(append([]corev1.Container{}, pod.InitContainers...), pod.Containers...)
		for _, c := range containers {
			for _, env := range c.Env {
				if env.ValueFrom != nil && env.ValueFrom.SecretKeyRef != nil && env.ValueFrom.SecretKeyRef.Name == DatabaseAdminSecretName(instance) {
					t.Fatalf("admin credentials exposed to %s", c.Name)
				}
			}
			for _, env := range c.EnvFrom {
				if env.SecretRef != nil && env.SecretRef.Name == DatabaseAdminSecretName(instance) {
					t.Fatalf("admin Secret exposed via envFrom")
				}
			}
		}
		for _, volume := range pod.Volumes {
			if volume.Secret != nil && volume.Secret.SecretName == DatabaseAdminSecretName(instance) {
				t.Fatal("admin Secret mounted into customer pod")
			}
		}
	}
}

func TestDatabaseAccessJobUsesOnlyPlatformImageAndNoCustomerVolumes(t *testing.T) {
	instance := testInstance()
	job := DatabaseAccessJob(instance, Platform{PostgresImageRepository: "registry/platform/postgres"}, DatabaseRolesJobName, true)
	pod := job.Spec.Template.Spec
	if *pod.AutomountServiceAccountToken || len(pod.Volumes) != 0 || len(pod.InitContainers) != 0 {
		t.Fatal("privilege Job has unnecessary access")
	}
	c := pod.Containers[0]
	if !strings.HasPrefix(c.Image, "registry/platform/postgres:") {
		t.Fatal("privilege Job runs a customer image")
	}
	for _, env := range c.Env {
		if env.ValueFrom != nil && env.ValueFrom.SecretKeyRef.Name != DatabaseAdminSecretName(instance) {
			t.Fatal("managed privilege Job reads an unexpected Secret")
		}
	}
	audit := DatabaseAccessJob(instance, Platform{}, "audit", false).Spec.Template.Spec.Containers[0]
	for _, env := range audit.Env {
		if env.ValueFrom != nil && env.ValueFrom.SecretKeyRef.Name == DatabaseAdminSecretName(instance) {
			t.Fatal("external audit needs only the application login")
		}
	}
}

// kubeletExpand mimics how Kubernetes treats a container command: "$$"
// becomes "$" (and "$(NAME)" of a defined variable would be expanded).
func kubeletExpand(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		if s[i] == '$' && i+1 < len(s) && s[i+1] == '$' {
			b.WriteByte('$')
			i++
			continue
		}
		b.WriteByte(s[i])
	}
	return b.String()
}

func TestDatabaseAccessJobScriptReachesTheContainerVerbatim(t *testing.T) {
	for _, managed := range []bool{true, false} {
		want := databaseAuditScript
		if managed {
			want = databaseRolesScript
		}
		job := DatabaseAccessJob(testInstance(), Platform{}, DatabaseRolesJobName, managed)
		got := kubeletExpand(job.Spec.Template.Spec.Containers[0].Command[2])
		if got != want {
			t.Fatalf("managed=%v: the container would run a different script", managed)
		}
	}
	if !strings.Contains(databaseRolesScript, "DO $$ BEGIN") {
		t.Fatal("expected the PL/pgSQL DO block this test guards")
	}
}
