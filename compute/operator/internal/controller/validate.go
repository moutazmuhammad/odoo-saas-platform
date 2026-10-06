package controller

import (
	"fmt"
	"slices"

	"k8s.io/apimachinery/pkg/api/resource"
	"k8s.io/utils/ptr"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

// validationError is a spec problem the controller can detect but that
// only a spec change (not a retry) can fix. Reconcile surfaces it as
// Degraded/Failed status instead of endlessly retrying, per the platform
// requirement to avoid infinite destructive retries on unfixable input.
type validationError struct {
	reason  string
	message string
}

func (e *validationError) Error() string { return e.message }

// legacyReplicasMessage explains why a spec.replicas above one (only
// possible on objects created before the single-replica rule, which the
// CRD's transition rule keeps editable) is ignored. Empty when replicas is
// one. Deliberately not a validationError: blocking reconcile would also
// block suspension and the cleanup of retired multi-pod resources.
func legacyReplicasMessage(instance *saasv1alpha1.OdooInstance) string {
	if replicas := ptr.Deref(instance.Spec.Replicas, 1); replicas != 1 {
		return fmt.Sprintf("spec.replicas=%d is no longer supported and is treated as 1; set it to 1 and scale by increasing CPU and memory resources", replicas)
	}
	return ""
}

// validateSpec performs the checks that are impractical to express as pure
// CRD/CEL validation (they need operator-level configuration such as the
// supported-version list) and that would otherwise resurface confusingly
// late (e.g. as an opaque PVC or Deployment admission error).
func (r *OdooInstanceReconciler) validateSpec(instance *saasv1alpha1.OdooInstance) *validationError {
	if len(r.SupportedOdooVersions) > 0 && !slices.Contains(r.SupportedOdooVersions, instance.Spec.Version) {
		return &validationError{
			reason: "UnsupportedVersion",
			message: fmt.Sprintf("spec.version %q is not in the platform's supported version list (%v)",
				instance.Spec.Version, r.SupportedOdooVersions),
		}
	}

	if !r.AllowMutableTags && isMutableTag(instance.Spec.Image.Tag) {
		return &validationError{
			reason:  "MutableImageTag",
			message: fmt.Sprintf("spec.image.tag %q is a mutable tag; the platform requires immutable, content-addressed tags outside development clusters", instance.Spec.Image.Tag),
		}
	}

	if instance.Spec.Storage.SharedWithDatabase && instance.Spec.Database.Mode != "" && instance.Spec.Database.Mode != saasv1alpha1.DatabaseModeManaged {
		return &validationError{reason: "SharedStorageRequiresManagedDatabase", message: "storage.sharedWithDatabase requires database.mode=Managed"}
	}

	if _, err := resource.ParseQuantity(instance.Spec.Storage.Filestore.Size); err != nil {
		return &validationError{reason: "InvalidStorageSize", message: fmt.Sprintf("spec.storage.filestore.size %q is not a valid quantity (e.g. 10Gi)", instance.Spec.Storage.Filestore.Size)}
	}
	if instance.Spec.Database.Storage != nil {
		if _, err := resource.ParseQuantity(instance.Spec.Database.Storage.Size); err != nil {
			return &validationError{reason: "InvalidStorageSize", message: fmt.Sprintf("spec.database.storage.size %q is not a valid quantity (e.g. 20Gi)", instance.Spec.Database.Storage.Size)}
		}
	}

	if instance.Spec.Backup.Enabled && instance.Spec.Backup.Schedule == "" {
		return &validationError{
			reason:  "BackupScheduleRequired",
			message: "spec.backup.schedule is required when spec.backup.enabled is true",
		}
	}

	if instance.Spec.Backup.Enabled &&
		instance.Spec.Backup.Destination.Type == saasv1alpha1.BackupDestinationObjectStore &&
		instance.Spec.Backup.Destination.ObjectStorageSecretRef == nil {
		return &validationError{
			reason:  "BackupDestinationMisconfigured",
			message: "spec.backup.destination.objectStorageSecretRef is required when destination.type is ObjectStorage",
		}
	}

	if instance.Spec.Restore != nil {
		source := instance.Spec.Restore.Source
		switch source.Type {
		case saasv1alpha1.BackupDestinationObjectStore:
			if source.ObjectStorageSecretRef == nil {
				return &validationError{
					reason:  "RestoreSourceMisconfigured",
					message: "spec.restore.source.objectStorageSecretRef is required when source.type is ObjectStorage",
				}
			}
		case saasv1alpha1.BackupDestinationPVC, "":
			// No additional requirements the controller can check here: for
			// a PVC source, the PVC itself (named like BackupPVCName) must
			// already exist and be pre-populated by the platform's
			// onboarding pipeline before this instance is created — see
			// resources.OdooRestoreJob's doc comment.
		default:
			return &validationError{
				reason:  "InvalidRestoreSourceType",
				message: fmt.Sprintf("unknown spec.restore.source.type %q", source.Type),
			}
		}
	}

	switch instance.Spec.Database.Mode {
	case saasv1alpha1.DatabaseModeExternal:
		if instance.Spec.Database.CredentialsSecretRef == nil || instance.Spec.Database.CredentialsSecretRef.Name == "" {
			return &validationError{
				reason:  "ExternalDatabaseCredentialsRequired",
				message: "spec.database.credentialsSecretRef is required when spec.database.mode is External",
			}
		}
	case saasv1alpha1.DatabaseModeManaged, saasv1alpha1.DatabaseModeCloudNativePG, "":
		// no additional requirements
	default:
		return &validationError{
			reason:  "InvalidDatabaseMode",
			message: fmt.Sprintf("unknown spec.database.mode %q", instance.Spec.Database.Mode),
		}
	}

	return nil
}

// isMutableTag flags common non-content-addressed tags. This is a
// heuristic, not a security boundary by itself; combined with
// AllowMutableTags=false it nudges the platform's build pipeline towards
// immutable, reproducible image references as described in
// setup/01-ARCHITECTURE.md ("Addons & Image Strategy").
func isMutableTag(tag string) bool {
	switch tag {
	case "latest", "main", "master", "edge", "dev", "nightly":
		return true
	}
	return false
}
