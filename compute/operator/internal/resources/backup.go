package resources

import (
	"strconv"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

// BackupToolImage is the image running scheduled backup Jobs. It must
// bundle: a `pg_dump` matching (or newer than) the instance's PostgreSQL
// major version, `tar`/`gzip`, and (when any instance uses
// BackupDestinationObjectStore) an object-storage CLI such as `aws` or
// `rclone`. This is a platform-operated build, analogous to the Odoo image
// itself: the operator never assumes a specific tool is present without
// the platform having published this image.
//
// Overridable per operator deployment via the --backup-tool-image flag
// (see cmd/main.go); this constant is only the compiled-in default.
const DefaultBackupToolImage = "docker.io/moutazmuhammad/odoo-saas-backup-tool:0.1.0"

// BackupCronJob builds the scheduled backup CronJob. Each run coordinates a
// database dump and a filestore archive into one timestamped, atomically
// published directory (or object-storage prefix) plus a manifest.json
// tying the two together, so restore never mixes a database snapshot with
// a mismatched filestore snapshot. See docs/architecture.md ("Backups").
//
// Known limitation (documented rather than hidden): when Destination.Type
// is PVC, the backup Pod must run on the same node as the Odoo pod that
// currently holds the (default ReadWriteOnce) filestore volume, which is
// why a pod affinity to the Odoo selector is required below. This is a
// direct consequence of the RWO-by-default filestore trade-off described
// in FilestoreSpec; it disappears once an instance's filestore is RWX or
// is moved to object storage (see docs/architecture.md, "Filestore
// Architecture").
func BackupCronJob(instance *saasv1alpha1.OdooInstance, backupToolImage string) *batchv1.CronJob {
	labels := WithComponent(instance, "backup")

	env := []corev1.EnvVar{
		{Name: "RETENTION", Value: strconv.Itoa(int(instance.Spec.Backup.Retention))},
		{Name: "INSTANCE_NAME", Value: instance.Name},
		{Name: "DESTINATION_TYPE", Value: string(instance.Spec.Backup.Destination.Type)},
		{Name: "DESTINATION_BUCKET", Value: instance.Spec.Backup.Destination.Bucket},
		{Name: "DESTINATION_PREFIX", Value: instance.Spec.Backup.Destination.Prefix},
		envFromSecret("DB_HOST", DatabaseSecretName(instance), "host"),
		envFromSecret("DB_PORT", DatabaseSecretName(instance), "port"),
		envFromSecret("DB_USER", DatabaseSecretName(instance), "username"),
		envFromSecret("DB_NAME", DatabaseSecretName(instance), "dbname"),
		envFromSecret("PGPASSWORD", DatabaseSecretName(instance), "password"),
	}

	volumes := []corev1.Volume{
		{
			Name: "filestore",
			VolumeSource: corev1.VolumeSource{
				PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
					ClaimName: FilestorePVCName(instance),
					ReadOnly:  true,
				},
			},
		},
		{Name: "tmp", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}},
	}
	mounts := []corev1.VolumeMount{
		{Name: "filestore", MountPath: "/filestore", ReadOnly: true},
		{Name: "tmp", MountPath: "/tmp"},
	}

	if instance.Spec.Backup.Destination.Type == saasv1alpha1.BackupDestinationPVC || instance.Spec.Backup.Destination.Type == "" {
		volumes = append(volumes, corev1.Volume{
			Name: "backups",
			VolumeSource: corev1.VolumeSource{
				PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
					ClaimName: BackupPVCName(instance),
				},
			},
		})
		mounts = append(mounts, corev1.VolumeMount{Name: "backups", MountPath: "/backups"})
	} else if instance.Spec.Backup.Destination.ObjectStorageSecretRef != nil {
		env = append(env,
			envFromSecret("OBJECT_STORAGE_ENDPOINT", instance.Spec.Backup.Destination.ObjectStorageSecretRef.Name, "endpoint"),
			envFromSecret("OBJECT_STORAGE_ACCESS_KEY", instance.Spec.Backup.Destination.ObjectStorageSecretRef.Name, "access-key"),
			envFromSecret("OBJECT_STORAGE_SECRET_KEY", instance.Spec.Backup.Destination.ObjectStorageSecretRef.Name, "secret-key"),
		)
	}

	podAffinity := &corev1.Affinity{
		PodAffinity: &corev1.PodAffinity{
			RequiredDuringSchedulingIgnoredDuringExecution: []corev1.PodAffinityTerm{
				{
					LabelSelector: &metav1.LabelSelector{MatchLabels: SelectorLabels(instance)},
					TopologyKey:   "kubernetes.io/hostname",
				},
			},
		},
	}

	jobSpec := batchv1.JobSpec{
		BackoffLimit: ptr.To(int32(2)),
		Template: corev1.PodTemplateSpec{
			ObjectMeta: metav1.ObjectMeta{Labels: labels},
			Spec: corev1.PodSpec{
				RestartPolicy:                corev1.RestartPolicyOnFailure,
				ServiceAccountName:           OdooServiceAccountName(instance),
				AutomountServiceAccountToken: ptr.To(false),
				Affinity:                     podAffinity,
				SecurityContext: &corev1.PodSecurityContext{
					RunAsNonRoot: ptr.To(true),
					SeccompProfile: &corev1.SeccompProfile{
						Type: corev1.SeccompProfileTypeRuntimeDefault,
					},
				},
				Containers: []corev1.Container{
					{
						Name:            "backup",
						Image:           backupToolImage,
						ImagePullPolicy: corev1.PullIfNotPresent,
						Command:         []string{"/usr/local/bin/run-backup.sh"},
						Env:             env,
						VolumeMounts:    mounts,
						Resources: corev1.ResourceRequirements{
							Requests: corev1.ResourceList{
								corev1.ResourceCPU:    resource.MustParse("100m"),
								corev1.ResourceMemory: resource.MustParse("256Mi"),
							},
							Limits: corev1.ResourceList{
								corev1.ResourceCPU:    resource.MustParse("1"),
								corev1.ResourceMemory: resource.MustParse("1Gi"),
							},
						},
						SecurityContext: genericHardenedSecurityContext(),
					},
				},
				Volumes: volumes,
			},
		},
	}

	return &batchv1.CronJob{
		TypeMeta: metav1.TypeMeta{APIVersion: "batch/v1", Kind: "CronJob"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      BackupCronJobName(instance),
			Namespace: TenantNamespace(instance),
			Labels:    labels,
		},
		Spec: batchv1.CronJobSpec{
			Schedule:                   instance.Spec.Backup.Schedule,
			ConcurrencyPolicy:          batchv1.ForbidConcurrent,
			SuccessfulJobsHistoryLimit: ptr.To(int32(3)),
			FailedJobsHistoryLimit:     ptr.To(int32(3)),
			JobTemplate: batchv1.JobTemplateSpec{
				Spec: jobSpec,
			},
		},
	}
}

// BackupPVCName is the name of the PVC storing local backup artifacts when
// Destination.Type is PVC.
func BackupPVCName(instance *saasv1alpha1.OdooInstance) string {
	return "odoo-backups"
}

// BackupPVC builds the backup destination PVC used when
// Destination.Type is PVC (or unset).
func BackupPVC(instance *saasv1alpha1.OdooInstance) *corev1.PersistentVolumeClaim {
	return &corev1.PersistentVolumeClaim{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "PersistentVolumeClaim"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      BackupPVCName(instance),
			Namespace: TenantNamespace(instance),
			Labels:    WithComponent(instance, "backup"),
		},
		Spec: corev1.PersistentVolumeClaimSpec{
			AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
			Resources: corev1.VolumeResourceRequirements{
				Requests: corev1.ResourceList{
					corev1.ResourceStorage: resource.MustParse("50Gi"),
				},
			},
		},
	}
}

func envFromSecret(name, secretName, key string) corev1.EnvVar {
	return corev1.EnvVar{
		Name: name,
		ValueFrom: &corev1.EnvVarSource{
			SecretKeyRef: &corev1.SecretKeySelector{
				LocalObjectReference: corev1.LocalObjectReference{Name: secretName},
				Key:                  key,
			},
		},
	}
}
