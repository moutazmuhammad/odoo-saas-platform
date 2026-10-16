package resources

import (
	_ "embed"
	"strings"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
)

//go:embed database_roles.sh
var databaseRolesScript string

//go:embed database_audit.sh
var databaseAuditScript string

const DatabaseRolesJobName = "odoo-database-roles-v1"

// DatabaseAccessJob is platform code only: no customer image, filestore,
// addons, service-account token or customer-supplied command is used.
func DatabaseAccessJob(instance *saasv1alpha1.OdooInstance, platform Platform, name string, managed bool) *batchv1.Job {
	labels := WithComponent(instance, "database-access")
	script := databaseAuditScript
	env := []corev1.EnvVar{
		envFromSecret("PGHOST", DatabaseSecretName(instance), "host"),
		envFromSecret("PGPORT", DatabaseSecretName(instance), "port"),
		envFromSecret("PGUSER", DatabaseSecretName(instance), "username"),
		envFromSecret("PGPASSWORD", DatabaseSecretName(instance), "password"),
		envFromSecret("DB_NAME", DatabaseSecretName(instance), "dbname"),
	}
	if managed {
		script = databaseRolesScript
		admin := DatabaseAdminSecretName(instance)
		env = []corev1.EnvVar{
			{Name: "DB_HOST", Value: DatabaseFQDN(instance)},
			{Name: "DB_PORT", Value: "5432"},
			{Name: "DB_NAME", Value: OdooDatabaseName(instance)},
			envFromSecret("ADMIN_PASSWORD", admin, "password"),
			envFromSecret("OWNER_PASSWORD", admin, "owner-password"),
			envFromSecret("LEGACY_USER", admin, "legacy-user"),
			envFromSecret("LEGACY_PASSWORD", admin, "legacy-password"),
		}
	}
	return &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: TenantNamespace(instance), Labels: labels},
		Spec: batchv1.JobSpec{
			BackoffLimit: ptr.To(int32(3)), ActiveDeadlineSeconds: ptr.To(int64(1800)),
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: labels},
				Spec: corev1.PodSpec{
					RestartPolicy:                corev1.RestartPolicyNever,
					AutomountServiceAccountToken: ptr.To(false),
					ImagePullSecrets:             platform.imagePullSecrets(instance),
					SecurityContext: &corev1.PodSecurityContext{RunAsNonRoot: ptr.To(true), RunAsUser: ptr.To(int64(999)),
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault}},
					Containers: []corev1.Container{{Name: "database-access", Image: platform.postgresImage(instance.Spec.Database.Version),
						Command: []string{"sh", "-c", literalCommand(script)}, Env: env,
						SecurityContext: &corev1.SecurityContext{AllowPrivilegeEscalation: ptr.To(false), ReadOnlyRootFilesystem: ptr.To(true),
							Capabilities: &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}}},
						Resources: corev1.ResourceRequirements{
							Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("10m"), corev1.ResourceMemory: resource.MustParse("32Mi")},
							Limits:   corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("500m"), corev1.ResourceMemory: resource.MustParse("128Mi")},
						},
					}},
				},
			},
		},
	}
}

// literalCommand makes a script reach the container exactly as written.
// Kubernetes rewrites "$$" to "$" (and expands "$(NAME)") in command and
// args, which broke the PL/pgSQL "DO $$ ... $$" blocks; "$$" is its escape
// for a literal "$".
func literalCommand(script string) string {
	return strings.ReplaceAll(script, "$", "$$")
}
