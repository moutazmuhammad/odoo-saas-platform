package resources

import (
	"fmt"
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
// single-instance `Cluster` CR (PITR and managed recovery) behind the same
// OdooInstance API — see internal/controller/database.go.
//
// Connection details are wired via secretKeyRef against DatabaseSecretName,
// not passed as literal values, so this builder never handles the
// plaintext password itself.
func DatabaseStatefulSet(instance *saasv1alpha1.OdooInstance) *appsv1.StatefulSet {
	labels := WithComponent(instance, "database")

	var storageClass *string
	if instance.Spec.Database.Storage != nil {
		storageClass = instance.Spec.Database.Storage.StorageClassName
	}
	// An unparsable size never reaches here: the CRD pattern and
	// validateSpec reject it first. Fall back rather than panic regardless.
	size := resource.MustParse("20Gi")
	if instance.Spec.Database.Storage != nil {
		if q, err := resource.ParseQuantity(instance.Spec.Database.Storage.Size); err == nil {
			size = q
		}
	}

	envFrom := func(key string) corev1.EnvVarSource {
		return corev1.EnvVarSource{
			SecretKeyRef: &corev1.SecretKeySelector{
				LocalObjectReference: corev1.LocalObjectReference{Name: DatabaseSecretName(instance)},
				Key:                  key,
			},
		}
	}

	sts := &appsv1.StatefulSet{
		TypeMeta: metav1.TypeMeta{APIVersion: "apps/v1", Kind: "StatefulSet"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      DatabaseStatefulSetName(instance),
			Namespace: TenantNamespace(instance),
			Labels:    labels,
		},
		Spec: appsv1.StatefulSetSpec{
			Replicas:    ptr.To(int32(1)),
			ServiceName: DatabaseServiceName(instance),
			// OnDelete: a resources change must not restart the only
			// database pod. The controller resizes it in place and deletes
			// it only for an image change (see reconcileManagedDatabase).
			UpdateStrategy: appsv1.StatefulSetUpdateStrategy{Type: appsv1.OnDeleteStatefulSetStrategyType},
			Selector:       &metav1.LabelSelector{MatchLabels: labels},
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
							Args:      PostgresTuningArgs(DatabaseResources(instance)),
							Resources: DatabaseResources(instance),
							// Resized in place by the controller (pods/resize).
							ResizePolicy: []corev1.ContainerResizePolicy{
								{ResourceName: corev1.ResourceCPU, RestartPolicy: corev1.NotRequired},
								{ResourceName: corev1.ResourceMemory, RestartPolicy: corev1.NotRequired},
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
	if instance.Spec.Storage.SharedWithDatabase {
		sts.Spec.VolumeClaimTemplates = nil
		pod := &sts.Spec.Template.Spec
		pod.SecurityContext.FSGroup = ptr.To(int64(odooImageGID))
		pod.SecurityContext.FSGroupChangePolicy = ptr.To(corev1.FSGroupChangeOnRootMismatch)
		pod.Affinity = sharedDataCoLocation(instance)
		pod.Volumes = append(pod.Volumes, corev1.Volume{Name: DatabasePVCName(instance), VolumeSource: corev1.VolumeSource{
			PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: FilestorePVCName(instance)},
		}})
		pod.Containers[0].VolumeMounts[0].SubPath = "postgres"
		// Non-root creation preserves PostgreSQL's required data ownership.
		// All consumers use one fsGroup and skip recursive ownership changes
		// while the volume root matches; but if it ever mismatches, the
		// kubelet's recursive fsGroup pass makes every directory group
		// writable, and PostgreSQL refuses to start unless PGDATA (a
		// subdirectory, never the volume root) is 0700 or 0750. So restore
		// 0700 on PGDATA before every start.
		pod.InitContainers = []corev1.Container{{
			Name: "prepare-shared-data", Image: pod.Containers[0].Image,
			Command:         []string{"sh", "-ec", "mkdir -p /tenant-data/postgres; chmod 700 /tenant-data/postgres; if [ -d /tenant-data/postgres/pgdata ]; then chmod 700 /tenant-data/postgres/pgdata; fi; if [ ! -d /tenant-data/odoo ]; then mkdir -m 2770 /tenant-data/odoo; fi"},
			VolumeMounts:    []corev1.VolumeMount{{Name: DatabasePVCName(instance), MountPath: "/tenant-data"}},
			SecurityContext: &corev1.SecurityContext{RunAsUser: ptr.To(int64(999)), RunAsNonRoot: ptr.To(true), AllowPrivilegeEscalation: ptr.To(false), ReadOnlyRootFilesystem: ptr.To(true), Capabilities: &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}}},
			Resources:       corev1.ResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("10m"), corev1.ResourceMemory: resource.MustParse("32Mi")}, Limits: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("100m"), corev1.ResourceMemory: resource.MustParse("128Mi")}},
		}}
	}
	return sts
}

// DatabasePodName is the managed PostgreSQL StatefulSet's only pod.
func DatabasePodName(instance *saasv1alpha1.OdooInstance) string {
	return DatabaseStatefulSetName(instance) + "-0"
}

// DatabaseDataPVCName is the PVC the StatefulSet's volume claim template
// created for that pod.
func DatabaseDataPVCName(instance *saasv1alpha1.OdooInstance) string {
	if instance.Spec.Storage.SharedWithDatabase {
		return FilestorePVCName(instance)
	}
	return DatabasePVCName(instance) + "-" + DatabasePodName(instance)
}

// DatabaseStorageSize is the requested database volume size (default 20Gi).
func DatabaseStorageSize(instance *saasv1alpha1.OdooInstance) (resource.Quantity, error) {
	if instance.Spec.Storage.SharedWithDatabase {
		q, err := resource.ParseQuantity(instance.Spec.Storage.Filestore.Size)
		if err != nil {
			return resource.Quantity{}, fmt.Errorf("invalid spec.storage.filestore.size %q: %w", instance.Spec.Storage.Filestore.Size, err)
		}
		return q, nil
	}
	if instance.Spec.Database.Storage != nil && instance.Spec.Database.Storage.Size != "" {
		q, err := resource.ParseQuantity(instance.Spec.Database.Storage.Size)
		if err != nil {
			return resource.Quantity{}, fmt.Errorf("invalid spec.database.storage.size %q: %w", instance.Spec.Database.Storage.Size, err)
		}
		return q, nil
	}
	return resource.MustParse("20Gi"), nil
}

// DatabaseResources is spec.database.resources, or the platform default.
func DatabaseResources(instance *saasv1alpha1.OdooInstance) corev1.ResourceRequirements {
	if r := instance.Spec.Database.Resources; r != nil && (len(r.Limits) > 0 || len(r.Requests) > 0) {
		return *r.DeepCopy()
	}
	return corev1.ResourceRequirements{
		Requests: corev1.ResourceList{
			corev1.ResourceCPU:    resource.MustParse("250m"),
			corev1.ResourceMemory: resource.MustParse("512Mi"),
		},
		Limits: corev1.ResourceList{
			corev1.ResourceCPU:    resource.MustParse("2"),
			corev1.ResourceMemory: resource.MustParse("2Gi"),
		},
	}
}

// PostgresTuningArgs sizes PostgreSQL's memory settings to the
// container's memory limit, the usual 25% / 50% split: shared_buffers 25%,
// effective_cache_size 50%, work_mem limit/64 (min 4MB),
// maintenance_work_mem limit/16 (min 32MB). Without a limit, nothing is
// overridden.
func PostgresTuningArgs(res corev1.ResourceRequirements) []string {
	mem, ok := res.Limits[corev1.ResourceMemory]
	if !ok || mem.Value() <= 0 {
		return nil
	}
	mb := mem.Value() / (1024 * 1024)
	maxOf := func(a, b int64) int64 {
		if a > b {
			return a
		}
		return b
	}
	return []string{
		"-c", fmt.Sprintf("shared_buffers=%dMB", maxOf(mb/4, 32)),
		"-c", fmt.Sprintf("effective_cache_size=%dMB", maxOf(mb/2, 64)),
		"-c", fmt.Sprintf("work_mem=%dMB", maxOf(mb/64, 4)),
		"-c", fmt.Sprintf("maintenance_work_mem=%dMB", maxOf(mb/16, 32)),
	}
}
