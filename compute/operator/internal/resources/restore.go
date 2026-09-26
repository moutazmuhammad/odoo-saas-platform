package resources

import (
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

// DefaultRestoreToolImage is the image running the one-time restore Job
// (see OdooRestoreJob). It must bundle: pg_restore matching (or newer
// than) the instance's PostgreSQL major version, tar/gzip, and (when
// Spec.Restore.Source.Type is ObjectStorage) an object-storage CLI —
// exactly the same toolchain BackupCronJob's image already requires (see
// backup.go), just exercised in reverse. In practice a platform can ship a
// single image providing both `run-backup.sh` and `run-restore.sh`; this
// is kept as an independently overridable default (--restore-tool-image)
// in case a platform ever wants to version backup and restore tooling
// separately.
const DefaultRestoreToolImage = "docker.io/moutazmuhammad/odoo-saas-backup-tool:0.1.0"

// OdooRestoreJobName is the name of the one-time restore Job.
func OdooRestoreJobName(instance *saasv1alpha1.OdooInstance) string {
	return "odoo-restore"
}

// OdooRestoreJob builds the Job that provisions a brand-new instance from
// an existing backup instead of an empty database: it locates the backup
// identified by Spec.Restore.Source (defaulting to the most recent one at
// that destination when BackupID is empty), downloads the database dump
// and filestore archive, runs pg_restore against the instance's own
// already-provisioned-but-empty database, and extracts the filestore
// archive into the instance's filestore PVC. It is the direct inverse of
// BackupCronJob (see backup.go) and is only ever created in place of
// OdooInitJob (see init_job.go): the two are mutually exclusive
// initialization paths for the same one-time "get this instance's
// database from nothing to something Odoo can serve" step, gated behind
// the same DatabaseReady dependency and gating the same web Deployment in
// turn (see internal/controller/restore.go and .../init_job.go).
//
// Like OdooInitJob, this is modeled as reconcile-towards-"this Job exists
// and has succeeded": internal/controller/restore.go never recreates it
// once Status.Succeeded > 0, and re-applying the same desired Job object
// on every reconcile is a no-op once the Job exists (a Job's pod template
// is immutable after creation), so "reconcile ran N times" never means
// "the restore ran N times."
//
// Known limitation for Source.Type == PVC (documented rather than
// silently assumed away): unlike BackupCronJob, this builder does NOT
// create the source PVC — it must already exist, pre-populated with the
// customer's dump + filestore archive by the platform's onboarding/
// migration pipeline, before this OdooInstance is created, exactly like
// External database mode requires its credentials Secret to pre-exist
// (see reconcileExternalDatabase). It intentionally reuses the same name
// BackupPVCName produces, since that is the one deterministic PVC name
// this platform's backup abstraction already defines for this tenant; a
// platform that also enables spec.backup with Destination.Type=PVC on a
// restored instance will have both mechanisms share that single volume.
func OdooRestoreJob(instance *saasv1alpha1.OdooInstance, restoreToolImage string) *batchv1.Job {
	labels := WithComponent(instance, "restore")
	source := instance.Spec.Restore.Source

	env := []corev1.EnvVar{
		{Name: "INSTANCE_NAME", Value: instance.Name},
		{Name: "SOURCE_TYPE", Value: string(source.Type)},
		{Name: "SOURCE_BUCKET", Value: source.Bucket},
		{Name: "SOURCE_PREFIX", Value: source.Prefix},
		{Name: "BACKUP_ID", Value: source.BackupID},
		{Name: "ODOO_DATA_DIR", Value: "/var/lib/odoo"},
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
				},
			},
		},
		{Name: "tmp", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}},
	}
	mounts := []corev1.VolumeMount{
		// Not ReadOnly, unlike the backup Job's mount of this same PVC:
		// this Job is the one place that WRITES the restored filestore
		// content, and it must be the only writer active before the web
		// Deployment (and therefore Odoo itself) ever starts — see
		// internal/controller/restore.go's gating.
		{Name: "filestore", MountPath: "/var/lib/odoo"},
		{Name: "tmp", MountPath: "/tmp"},
	}

	if source.Type == saasv1alpha1.BackupDestinationObjectStore {
		if source.ObjectStorageSecretRef != nil {
			env = append(env,
				envFromSecret("OBJECT_STORAGE_ENDPOINT", source.ObjectStorageSecretRef.Name, "endpoint"),
				envFromSecret("OBJECT_STORAGE_ACCESS_KEY", source.ObjectStorageSecretRef.Name, "access-key"),
				envFromSecret("OBJECT_STORAGE_SECRET_KEY", source.ObjectStorageSecretRef.Name, "secret-key"),
			)
		}
	} else {
		volumes = append(volumes, corev1.Volume{
			Name: "restore-source",
			VolumeSource: corev1.VolumeSource{
				PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
					ClaimName: BackupPVCName(instance),
					ReadOnly:  true,
				},
			},
		})
		mounts = append(mounts, corev1.VolumeMount{Name: "restore-source", MountPath: "/backups", ReadOnly: true})
	}

	return &batchv1.Job{
		TypeMeta: metav1.TypeMeta{APIVersion: "batch/v1", Kind: "Job"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      OdooRestoreJobName(instance),
			Namespace: TenantNamespace(instance),
			Labels:    labels,
		},
		Spec: batchv1.JobSpec{
			BackoffLimit: ptr.To(int32(2)),
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: labels},
				Spec: corev1.PodSpec{
					RestartPolicy:                 corev1.RestartPolicyOnFailure,
					ServiceAccountName:            OdooServiceAccountName(instance),
					AutomountServiceAccountToken:  ptr.To(false),
					TerminationGracePeriodSeconds: ptr.To(int64(60)),
					SecurityContext: &corev1.PodSecurityContext{
						RunAsNonRoot: ptr.To(true),
						SeccompProfile: &corev1.SeccompProfile{
							Type: corev1.SeccompProfileTypeRuntimeDefault,
						},
						// Matches the FSGroup used by OdooInitJob/OdooDeployment
						// so the filestore this Job writes is group-writable/
						// readable by the Odoo container that mounts the same
						// PVC afterwards.
						FSGroup: ptr.To(int64(odooImageGID)),
					},
					Containers: []corev1.Container{
						{
							Name: "restore",
							// A platform-built tool image, not the Odoo image
							// itself: use genericHardenedSecurityContext (no
							// pinned numeric UID), never containerSecurityContext
							// — see that function's own doc comment for why.
							Image:           restoreToolImage,
							ImagePullPolicy: corev1.PullIfNotPresent,
							Command:         []string{"/usr/local/bin/run-restore.sh"},
							Env:             env,
							VolumeMounts:    mounts,
							Resources: corev1.ResourceRequirements{
								Requests: corev1.ResourceList{
									corev1.ResourceCPU:              resource.MustParse("250m"),
									corev1.ResourceMemory:           resource.MustParse("512Mi"),
									corev1.ResourceEphemeralStorage: resource.MustParse("1Gi"),
								},
								Limits: corev1.ResourceList{
									corev1.ResourceCPU:              resource.MustParse("2"),
									corev1.ResourceMemory:           resource.MustParse("2Gi"),
									corev1.ResourceEphemeralStorage: resource.MustParse("10Gi"),
								},
							},
							SecurityContext: genericHardenedSecurityContext(),
						},
					},
					Volumes: volumes,
				},
			},
		},
	}
}
