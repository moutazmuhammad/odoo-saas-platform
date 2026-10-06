package resources

import (
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

// PlatformPullSecretName is the tenant-namespace copy of the operator's
// --platform-pull-secret: registry credentials for the images the platform
// itself ships (backup tool, PostgreSQL), as opposed to the tenant's own
// Odoo image credentials in spec.image.pullSecretRefs.
const PlatformPullSecretName = "odoo-platform-pull"

const (
	// DefaultPostgresImageRepository is the Managed PostgreSQL image
	// repository; the tag is always spec.database.version.
	DefaultPostgresImageRepository = "docker.io/library/postgres"
	// DefaultCNPGPostgresImageRepository is the CloudNativePG operand image
	// repository; the tag is always spec.database.version.
	DefaultCNPGPostgresImageRepository = "ghcr.io/cloudnative-pg/postgresql"
)

// Platform is the operator-wide (never per-tenant) configuration the pod
// builders need, set once from cmd/main.go flags. The zero value means
// "compiled-in defaults, no platform pull secret", so moving the platform
// to a private registry is purely an operator-flag change.
type Platform struct {
	// PullSecret lists PlatformPullSecretName in every tenant pod's
	// imagePullSecrets. Set exactly when --platform-pull-secret is, so
	// pod templates never flap with the copy's transient availability.
	PullSecret bool
	// BackupToolImage overrides DefaultBackupToolImage.
	BackupToolImage string
	// RestoreToolImage overrides DefaultRestoreToolImage; falls back to
	// BackupToolImage, since both normally ship as one image.
	RestoreToolImage string
	// PostgresImageRepository overrides DefaultPostgresImageRepository.
	PostgresImageRepository string
	// CNPGPostgresImageRepository overrides DefaultCNPGPostgresImageRepository.
	CNPGPostgresImageRepository string
}

func (p Platform) backupToolImage() string {
	return firstNonEmpty(p.BackupToolImage, DefaultBackupToolImage)
}

func (p Platform) restoreToolImage() string {
	return firstNonEmpty(p.RestoreToolImage, p.BackupToolImage, DefaultRestoreToolImage)
}

func (p Platform) postgresImage(version string) string {
	return firstNonEmpty(p.PostgresImageRepository, DefaultPostgresImageRepository) + ":" + version
}

func (p Platform) cnpgPostgresImage(version string) string {
	return firstNonEmpty(p.CNPGPostgresImageRepository, DefaultCNPGPostgresImageRepository) + ":" + version
}

// imagePullSecrets is spec.image.pullSecretRefs (written into the tenant
// namespace by the control plane) plus, when enabled, the platform pull
// secret, without duplicates. Every tenant pod uses the same list: the
// kubelet tries each, so extra entries are harmless.
func (p Platform) imagePullSecrets(instance *saasv1alpha1.OdooInstance) []corev1.LocalObjectReference {
	refs := append([]corev1.LocalObjectReference(nil), instance.Spec.Image.PullSecretRefs...)
	if p.PullSecret {
		refs = append(refs, corev1.LocalObjectReference{Name: PlatformPullSecretName})
	}
	var out []corev1.LocalObjectReference
	seen := map[string]bool{}
	for _, ref := range refs {
		if ref.Name == "" || seen[ref.Name] {
			continue
		}
		seen[ref.Name] = true
		out = append(out, ref)
	}
	return out
}

// PlatformPullSecret builds the tenant copy of the operator's platform pull
// secret from source (the Secret named by --platform-pull-secret in the
// operator's namespace). Only the registry credentials are copied.
func PlatformPullSecret(instance *saasv1alpha1.OdooInstance, source *corev1.Secret) *corev1.Secret {
	return &corev1.Secret{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "Secret"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      PlatformPullSecretName,
			Namespace: TenantNamespace(instance),
			Labels:    WithComponent(instance, "platform-pull"),
		},
		Type: corev1.SecretTypeDockerConfigJson,
		Data: map[string][]byte{
			corev1.DockerConfigJsonKey: source.Data[corev1.DockerConfigJsonKey],
		},
	}
}

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if v != "" {
			return v
		}
	}
	return ""
}
