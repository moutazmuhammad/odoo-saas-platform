package resources

import (
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

// FilestorePVC builds the Odoo filestore/data PersistentVolumeClaim.
//
// ReadWriteOnce is the default for the single Odoo pod.
func FilestorePVC(instance *saasv1alpha1.OdooInstance) (*corev1.PersistentVolumeClaim, error) {
	size, err := resource.ParseQuantity(instance.Spec.Storage.Filestore.Size)
	if err != nil {
		return nil, err
	}

	accessMode := corev1.ReadWriteOnce
	if instance.Spec.Storage.Filestore.AccessMode == saasv1alpha1.FilestoreAccessModeRWX {
		accessMode = corev1.ReadWriteMany
	}

	return &corev1.PersistentVolumeClaim{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "PersistentVolumeClaim"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      FilestorePVCName(instance),
			Namespace: TenantNamespace(instance),
			Labels:    CommonLabels(instance),
		},
		Spec: corev1.PersistentVolumeClaimSpec{
			AccessModes: []corev1.PersistentVolumeAccessMode{accessMode},
			Resources: corev1.VolumeResourceRequirements{
				Requests: corev1.ResourceList{
					corev1.ResourceStorage: size,
				},
			},
			StorageClassName: instance.Spec.Storage.Filestore.StorageClassName,
		},
	}, nil
}

// Only expose the Odoo subdirectory to web, shell, and backup/restore pods.
// PostgreSQL's private directory is never mounted into customer containers.
func odooDataMount(instance *saasv1alpha1.OdooInstance, path string, readOnly bool) corev1.VolumeMount {
	mount := corev1.VolumeMount{Name: "filestore", MountPath: path, ReadOnly: readOnly}
	if instance.Spec.Storage.SharedWithDatabase {
		mount.SubPath = "odoo"
	}
	return mount
}

// The first pod may schedule anywhere; later RWO consumers stay on that
// node, including initialization and restore Jobs before web pods exist.
func sharedDataCoLocation(instance *saasv1alpha1.OdooInstance) *corev1.Affinity {
	if instance.Spec.Storage.Filestore.AccessMode == saasv1alpha1.FilestoreAccessModeRWX {
		return nil
	}
	return &corev1.Affinity{PodAffinity: &corev1.PodAffinity{
		RequiredDuringSchedulingIgnoredDuringExecution: []corev1.PodAffinityTerm{{
			LabelSelector: &metav1.LabelSelector{MatchLabels: SelectorLabels(instance)},
			TopologyKey:   "kubernetes.io/hostname",
		}},
	}}
}
