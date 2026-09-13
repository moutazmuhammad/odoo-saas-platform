package resources

import (
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

// DatabaseStatefulSet builds the single-replica managed PostgreSQL
// StatefulSet used in DatabaseModeManaged. A StatefulSet (rather than a
// Deployment) is used here specifically because it provides a stable
// volumeClaimTemplate/identity contract for a genuinely stateful, singleton
// workload — the same property that makes a Deployment the *wrong* choice
// is exactly why it is the *right* choice for PostgreSQL. See
// docs/architecture.md ("Question 2: Deployment or StatefulSet").
//
// This built-in mode is intentionally minimal (single instance, no
// streaming replication/failover) and is meant as the zero-dependency
// default for early-stage platform adoption. Production deployments should
// migrate a tenant to DatabaseModeCloudNativePG, which provisions a real
// `Cluster` CR (HA, PITR, automated failover) behind the exact same
// OdooInstance API — see internal/controller/database.go.
//
// Connection details are wired via secretKeyRef against DatabaseSecretName,
// not passed as literal values, so this builder never handles the
// plaintext password itself.
func DatabaseStatefulSet(instance *saasv1alpha1.OdooInstance) *appsv1.StatefulSet {
	labels := WithComponent(instance, "database")

	sizeStr := "20Gi"
	var storageClass *string
	if instance.Spec.Database.Storage != nil {
		sizeStr = instance.Spec.Database.Storage.Size
		storageClass = instance.Spec.Database.Storage.StorageClassName
	}
	size := resource.MustParse(sizeStr)

	envFrom := func(key string) corev1.EnvVarSource {
		return corev1.EnvVarSource{
			SecretKeyRef: &corev1.SecretKeySelector{
				LocalObjectReference: corev1.LocalObjectReference{Name: DatabaseSecretName(instance)},
				Key:                  key,
			},
		}
	}

	return &appsv1.StatefulSet{
		TypeMeta: metav1.TypeMeta{APIVersion: "apps/v1", Kind: "StatefulSet"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      DatabaseStatefulSetName(instance),
			Namespace: TenantNamespace(instance),
			Labels:    labels,
		},
		Spec: appsv1.StatefulSetSpec{
			Replicas:    ptr.To(int32(1)),
			ServiceName: DatabaseServiceName(instance),
			Selector:    &metav1.LabelSelector{MatchLabels: labels},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: labels},
				Spec: corev1.PodSpec{
					AutomountServiceAccountToken: ptr.To(false),
					SecurityContext: &corev1.PodSecurityContext{
						RunAsNonRoot: ptr.To(true),
						RunAsUser:    ptr.To(int64(999)),
						FSGroup:      ptr.To(int64(999)),
						SeccompProfile: &corev1.SeccompProfile{
							Type: corev1.SeccompProfileTypeRuntimeDefault,
						},
					},
					Containers: []corev1.Container{
						{
							Name:            "postgresql",
							Image:           "docker.io/library/postgres:" + instance.Spec.Database.Version,
							ImagePullPolicy: corev1.PullIfNotPresent,
							Env: []corev1.EnvVar{
								{Name: "POSTGRES_DB", ValueFrom: ptr.To(envFrom("dbname"))},
								{Name: "POSTGRES_USER", ValueFrom: ptr.To(envFrom("username"))},
								{Name: "POSTGRES_PASSWORD", ValueFrom: ptr.To(envFrom("password"))},
								{Name: "PGDATA", Value: "/var/lib/postgresql/data/pgdata"},
							},
							Ports: []corev1.ContainerPort{
								{Name: "postgresql", ContainerPort: PostgreSQLPort, Protocol: corev1.ProtocolTCP},
							},
							Resources: corev1.ResourceRequirements{
								Requests: corev1.ResourceList{
									corev1.ResourceCPU:    resource.MustParse("250m"),
									corev1.ResourceMemory: resource.MustParse("512Mi"),
								},
								Limits: corev1.ResourceList{
									corev1.ResourceCPU:    resource.MustParse("2"),
									corev1.ResourceMemory: resource.MustParse("2Gi"),
								},
							},
							VolumeMounts: []corev1.VolumeMount{
								{Name: DatabasePVCName(instance), MountPath: "/var/lib/postgresql/data"},
								{Name: "tmp", MountPath: "/tmp"},
								{Name: "run", MountPath: "/var/run/postgresql"},
							},
							ReadinessProbe: &corev1.Probe{
								ProbeHandler: corev1.ProbeHandler{
									Exec: &corev1.ExecAction{Command: []string{"pg_isready", "-U", "postgres"}},
								},
								InitialDelaySeconds: 5,
								PeriodSeconds:       10,
								TimeoutSeconds:      5,
							},
							LivenessProbe: &corev1.Probe{
								ProbeHandler: corev1.ProbeHandler{
									Exec: &corev1.ExecAction{Command: []string{"pg_isready", "-U", "postgres"}},
								},
								InitialDelaySeconds: 30,
								PeriodSeconds:       30,
								TimeoutSeconds:      5,
							},
							SecurityContext: &corev1.SecurityContext{
								AllowPrivilegeEscalation: ptr.To(false),
								ReadOnlyRootFilesystem:   ptr.To(true),
								RunAsNonRoot:             ptr.To(true),
								Capabilities: &corev1.Capabilities{
									Drop: []corev1.Capability{"ALL"},
								},
							},
						},
					},
					Volumes: []corev1.Volume{
						{Name: "tmp", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}},
						{Name: "run", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}},
					},
				},
			},
			VolumeClaimTemplates: []corev1.PersistentVolumeClaim{
				{
					ObjectMeta: metav1.ObjectMeta{Name: DatabasePVCName(instance), Labels: labels},
					Spec: corev1.PersistentVolumeClaimSpec{
						AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
						Resources: corev1.VolumeResourceRequirements{
							Requests: corev1.ResourceList{corev1.ResourceStorage: size},
						},
						StorageClassName: storageClass,
					},
				},
			},
		},
	}
}
