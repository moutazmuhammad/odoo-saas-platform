package resources

import (
	"reflect"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

// tenantPodSpecs builds every pod the operator creates for a tenant.
func tenantPodSpecs(instance *saasv1alpha1.OdooInstance, platform Platform) map[string]corev1.PodSpec {
	updating := instance.DeepCopy()
	updating.Spec.Update = &saasv1alpha1.UpdateSpec{Token: "t1", Modules: []string{"sale"}}
	restoring := instance.DeepCopy()
	restoring.Spec.Restore = &saasv1alpha1.RestoreSpec{Source: saasv1alpha1.RestoreSourceSpec{Type: saasv1alpha1.BackupDestinationPVC}}
	return map[string]corev1.PodSpec{
		"web":      OdooDeployment(instance, platform).Spec.Template.Spec,
		"init":     OdooInitJob(instance, platform).Spec.Template.Spec,
		"update":   OdooUpdateJob(updating, platform).Spec.Template.Spec,
		"backup":   BackupCronJob(instance, platform).Spec.JobTemplate.Spec.Template.Spec,
		"restore":  OdooRestoreJob(restoring, platform).Spec.Template.Spec,
		"database": DatabaseStatefulSet(instance, platform).Spec.Template.Spec,
	}
}

func cnpgPullSecrets(instance *saasv1alpha1.OdooInstance, platform Platform) []string {
	list, _, _ := unstructured.NestedSlice(CloudNativePGCluster(instance, platform).Object, "spec", "imagePullSecrets")
	var names []string
	for _, item := range list {
		names = append(names, item.(map[string]interface{})["name"].(string))
	}
	return names
}

func pullSecretNames(refs []corev1.LocalObjectReference) []string {
	var names []string
	for _, ref := range refs {
		names = append(names, ref.Name)
	}
	return names
}

func TestPullSecrets_EveryTenantPod(t *testing.T) {
	tenantRef := []corev1.LocalObjectReference{{Name: "tenant-registry"}}
	cases := []struct {
		name     string
		refs     []corev1.LocalObjectReference
		platform Platform
		want     []string
	}{
		{name: "none", want: nil},
		{name: "tenant refs only", refs: tenantRef, want: []string{"tenant-registry"}},
		{name: "platform only", platform: Platform{PullSecret: true}, want: []string{PlatformPullSecretName}},
		{name: "both", refs: tenantRef, platform: Platform{PullSecret: true}, want: []string{"tenant-registry", PlatformPullSecretName}},
		{
			name:     "deduplicated",
			refs:     []corev1.LocalObjectReference{{Name: "tenant-registry"}, {Name: PlatformPullSecretName}, {Name: "tenant-registry"}},
			platform: Platform{PullSecret: true},
			want:     []string{"tenant-registry", PlatformPullSecretName},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			instance := testInstance()
			instance.Spec.Image.PullSecretRefs = tc.refs
			for kind, pod := range tenantPodSpecs(instance, tc.platform) {
				if got := pullSecretNames(pod.ImagePullSecrets); !reflect.DeepEqual(got, tc.want) {
					t.Errorf("%s imagePullSecrets = %v, want %v", kind, got, tc.want)
				}
			}
			if got := cnpgPullSecrets(instance, tc.platform); !reflect.DeepEqual(got, tc.want) {
				t.Errorf("CNPG Cluster imagePullSecrets = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestPlatform_ZeroValueKeepsDefaultImages(t *testing.T) {
	instance := testInstance()
	instance.Spec.Database.Version = "16"
	pods := tenantPodSpecs(instance, Platform{})
	if got := pods["backup"].Containers[0].Image; got != DefaultBackupToolImage {
		t.Errorf("backup image = %q, want %q", got, DefaultBackupToolImage)
	}
	if got := pods["restore"].Containers[0].Image; got != DefaultRestoreToolImage {
		t.Errorf("restore image = %q, want %q", got, DefaultRestoreToolImage)
	}
	if got, want := pods["database"].Containers[0].Image, "docker.io/library/postgres:16"; got != want {
		t.Errorf("postgres image = %q, want %q", got, want)
	}
	if got, want := CloudNativePGCluster(instance, Platform{}).Object["spec"].(map[string]interface{})["imageName"], "ghcr.io/cloudnative-pg/postgresql:16"; got != want {
		t.Errorf("CNPG imageName = %v, want %q", got, want)
	}
}

func TestPlatform_ImageOverrides(t *testing.T) {
	instance := testInstance()
	instance.Spec.Database.Version = "16"
	instance.Spec.Storage.SharedWithDatabase = true
	platform := Platform{
		BackupToolImage:             "registry.internal/backup-tool:1",
		PostgresImageRepository:     "registry.internal/postgres",
		CNPGPostgresImageRepository: "registry.internal/cnpg-postgresql",
	}
	pods := tenantPodSpecs(instance, platform)
	if got := pods["backup"].Containers[0].Image; got != "registry.internal/backup-tool:1" {
		t.Errorf("backup image = %q", got)
	}
	// Restore falls back to the backup tool image: both ship as one image.
	if got := pods["restore"].Containers[0].Image; got != "registry.internal/backup-tool:1" {
		t.Errorf("restore image = %q", got)
	}
	db := pods["database"]
	if got := db.Containers[0].Image; got != "registry.internal/postgres:16" {
		t.Errorf("postgres image = %q", got)
	}
	if got := db.InitContainers[0].Image; got != "registry.internal/postgres:16" {
		t.Errorf("postgres init container image = %q", got)
	}
	if got := CloudNativePGCluster(instance, platform).Object["spec"].(map[string]interface{})["imageName"]; got != "registry.internal/cnpg-postgresql:16" {
		t.Errorf("CNPG imageName = %v", got)
	}

	platform.RestoreToolImage = "registry.internal/restore-tool:2"
	instance.Spec.Restore = &saasv1alpha1.RestoreSpec{Source: saasv1alpha1.RestoreSourceSpec{Type: saasv1alpha1.BackupDestinationPVC}}
	if got := OdooRestoreJob(instance, platform).Spec.Template.Spec.Containers[0].Image; got != "registry.internal/restore-tool:2" {
		t.Errorf("restore image with explicit override = %q", got)
	}
}

func TestPlatformPullSecret_CopiesOnlyDockerConfig(t *testing.T) {
	source := &corev1.Secret{
		Type: corev1.SecretTypeDockerConfigJson,
		Data: map[string][]byte{corev1.DockerConfigJsonKey: []byte(`{"auths":{}}`), "extra": []byte("x")},
	}
	secret := PlatformPullSecret(testInstance(), source)
	if secret.Name != PlatformPullSecretName || secret.Namespace != "odoo-tenant-acme" {
		t.Errorf("secret = %s/%s", secret.Namespace, secret.Name)
	}
	if secret.Type != corev1.SecretTypeDockerConfigJson {
		t.Errorf("type = %q", secret.Type)
	}
	if len(secret.Data) != 1 || string(secret.Data[corev1.DockerConfigJsonKey]) != `{"auths":{}}` {
		t.Errorf("data = %v", secret.Data)
	}
	if secret.Labels[saasv1alpha1.LabelManagedBy] != saasv1alpha1.ManagedByValue {
		t.Errorf("labels = %v", secret.Labels)
	}
}
