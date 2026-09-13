package resources

import (
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

// FilestorePVC builds the Odoo filestore/data PersistentVolumeClaim.
//
// Access mode trade-off (see FilestoreSpec docs for the full rationale):
// ReadWriteOnce is the default and is required for replicas == 1.
// ReadWriteMany is only used when the caller has already validated
// replicas > 1 is intentional and the StorageClass genuinely supports RWX
// (e.g. NFS/CephFS/EFS-backed); the controller performs that validation
// before ever calling this builder with FilestoreAccessModeRWX.
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
