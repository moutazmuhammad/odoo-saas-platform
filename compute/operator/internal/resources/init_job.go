package resources

import (
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

// OdooInitJobName is the name of the one-time database-initialization Job.
func OdooInitJobName(instance *saasv1alpha1.OdooInstance) string {
	return "odoo-init"
}

// OdooInitJob builds the Job that initializes a brand-new Odoo database:
// creates the schema and installs the `base` module (Odoo requires this
// before serving any HTTP request against a database — without it, every
// request fails with `KeyError: 'ir.http'` because the model registry has
// nothing to load). This is a genuinely one-time, imperative action (Odoo's
// own architecture has no other way to create a database), so the
// controller models it as: reconcile towards "this Job exists and has
// succeeded," which is itself idempotent (internal/controller/init_job.go
// never re-creates it once Status.Succeeded > 0) even though the action it
// performs is not repeatable.
//
// The web Deployment is intentionally not created until this Job succeeds
// (see internal/controller/workload.go) so customers never see the
// `KeyError: 'ir.http'` 500s a partially-initialized pod would otherwise
// serve during the gap between pod start and schema creation.
func OdooInitJob(instance *saasv1alpha1.OdooInstance) *batchv1.Job {
	args := []string{
		"-c", "/etc/odoo/odoo.conf",
		"-d", OdooDatabaseName(instance),
		"-i", "base",
		"--without-demo=all",
		"--stop-after-init",
	}
	return odooOneShotJob(instance, OdooInitJobName(instance), "init", "init-db",
		OdooConfigMapName(instance), args, corev1.RestartPolicyOnFailure, ptr.To(int32(3)), nil)
}

// odooOneShotJob is the pod shape shared by the database-init and module
// update Jobs: render odoo.conf from configMapName in an init container,
// then run Odoo once with args against the tenant database and filestore.
func odooOneShotJob(instance *saasv1alpha1.OdooInstance, name, component, containerName, configMapName string,
	args []string, restartPolicy corev1.RestartPolicy, backoffLimit *int32, activeDeadlineSeconds *int64) *batchv1.Job {
	labels := WithComponent(instance, component)
	image := instance.Spec.Image.Repository + ":" + instance.Spec.Image.Tag

	return &batchv1.Job{
		TypeMeta: metav1.TypeMeta{APIVersion: "batch/v1", Kind: "Job"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: TenantNamespace(instance),
			Labels:    labels,
		},
		Spec: batchv1.JobSpec{
			BackoffLimit:          backoffLimit,
			ActiveDeadlineSeconds: activeDeadlineSeconds,
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: labels},
				Spec: corev1.PodSpec{
					RestartPolicy:                 restartPolicy,
					ServiceAccountName:            OdooServiceAccountName(instance),
					AutomountServiceAccountToken:  ptr.To(false),
					TerminationGracePeriodSeconds: ptr.To(int64(60)),
					ImagePullSecrets:              instance.Spec.Image.PullSecretRefs,
					SecurityContext: &corev1.PodSecurityContext{
						RunAsNonRoot: ptr.To(true),
						SeccompProfile: &corev1.SeccompProfile{
							Type: corev1.SeccompProfileTypeRuntimeDefault,
						},
						FSGroup: ptr.To(int64(odooImageGID)),
					},
					InitContainers: []corev1.Container{
						{
							Name:            "render-config",
							Image:           image,
							ImagePullPolicy: instance.Spec.Image.PullPolicy,
							Command:         []string{"sh", "-c", renderInitContainerScript()},
							Env:             odooConfigEnvVars(instance),
							VolumeMounts: []corev1.VolumeMount{
								{Name: "config-template", MountPath: "/template", ReadOnly: true},
								{Name: "etc-odoo", MountPath: "/etc/odoo"},
							},
							SecurityContext: containerSecurityContext(),
						},
					},
					Containers: []corev1.Container{
						{
							Name:            containerName,
							Image:           image,
							ImagePullPolicy: instance.Spec.Image.PullPolicy,
							Args:            args,
							VolumeMounts: []corev1.VolumeMount{
								{Name: "etc-odoo", MountPath: "/etc/odoo", ReadOnly: true},
								{Name: "filestore", MountPath: "/var/lib/odoo"},
								{Name: "tmp", MountPath: "/tmp"},
							},
							SecurityContext: containerSecurityContext(),
						},
					},
					Volumes: []corev1.Volume{
						{
							Name: "config-template",
							VolumeSource: corev1.VolumeSource{
								ConfigMap: &corev1.ConfigMapVolumeSource{
									LocalObjectReference: corev1.LocalObjectReference{Name: configMapName},
								},
							},
						},
						{Name: "etc-odoo", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}},
						{
							Name: "filestore",
							VolumeSource: corev1.VolumeSource{
								PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
									ClaimName: FilestorePVCName(instance),
								},
							},
						},
						{Name: "tmp", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}},
					},
				},
			},
		},
	}
}
