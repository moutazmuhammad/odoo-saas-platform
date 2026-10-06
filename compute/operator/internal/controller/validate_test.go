package controller

import (
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/utils/ptr"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

func validSpecInstance() *saasv1alpha1.OdooInstance {
	instance := &saasv1alpha1.OdooInstance{}
	instance.Name = "acme"
	instance.Spec = saasv1alpha1.OdooInstanceSpec{
		Version: "18.0",
		Image:   saasv1alpha1.ImageSpec{Repository: "registry.example.com/odoo", Tag: "18.0-abc123"},
		Domain:  saasv1alpha1.DomainSpec{Hostname: "acme.example.com"},
		Storage: saasv1alpha1.InstanceStorageSpec{
			Filestore: saasv1alpha1.FilestoreSpec{Size: "10Gi", AccessMode: saasv1alpha1.FilestoreAccessModeRWO},
		},
	}
	return instance
}

func TestValidateSpec_RejectsUnsupportedVersion(t *testing.T) {
	r := &OdooInstanceReconciler{SupportedOdooVersions: []string{"17.0", "18.0"}}
	instance := validSpecInstance()
	instance.Spec.Version = "16.0"

	err := r.validateSpec(instance)
	if err == nil || err.reason != "UnsupportedVersion" {
		t.Fatalf("validateSpec() = %v, want reason UnsupportedVersion", err)
	}
}

func TestValidateSpec_RejectsMutableTagByDefault(t *testing.T) {
	r := &OdooInstanceReconciler{}
	instance := validSpecInstance()
	instance.Spec.Image.Tag = "latest"

	err := r.validateSpec(instance)
	if err == nil || err.reason != "MutableImageTag" {
		t.Fatalf("validateSpec() = %v, want reason MutableImageTag", err)
	}
}

func TestValidateSpec_AllowsMutableTagWhenOptedIn(t *testing.T) {
	r := &OdooInstanceReconciler{AllowMutableTags: true}
	instance := validSpecInstance()
	instance.Spec.Image.Tag = "latest"

	if err := r.validateSpec(instance); err != nil {
		t.Fatalf("validateSpec() = %v, want nil when AllowMutableTags is set", err)
	}
}

func TestValidateSpec_ToleratesLegacyReplicas(t *testing.T) {
	instance := validSpecInstance()
	instance.Spec.Replicas = ptr.To(int32(2))
	// Rejecting would also block suspension and legacy cleanup; the
	// builders run one pod and the controller only warns.
	if err := (&OdooInstanceReconciler{}).validateSpec(instance); err != nil {
		t.Fatalf("legacy replicas blocked reconcile: %v", err)
	}
	if legacyReplicasMessage(instance) == "" {
		t.Fatal("legacy replicas not surfaced")
	}
	instance.Spec.Replicas = ptr.To(int32(1))
	if legacyReplicasMessage(instance) != "" {
		t.Fatal("warning for a supported replica count")
	}
}

func TestValidateSpec_RejectsUnparsableStorageSize(t *testing.T) {
	instance := validSpecInstance()
	instance.Spec.Storage.Filestore.Size = "ten gigs"
	if err := (&OdooInstanceReconciler{}).validateSpec(instance); err == nil || err.reason != "InvalidStorageSize" {
		t.Fatalf("validateSpec() = %v, want reason InvalidStorageSize", err)
	}
	instance = validSpecInstance()
	instance.Spec.Database.Storage = &saasv1alpha1.StorageRequestSpec{Size: "lots"}
	if err := (&OdooInstanceReconciler{}).validateSpec(instance); err == nil || err.reason != "InvalidStorageSize" {
		t.Fatalf("validateSpec() = %v, want reason InvalidStorageSize", err)
	}
}

func TestValidateSpec_RequiresScheduleWhenBackupEnabled(t *testing.T) {
	r := &OdooInstanceReconciler{}
	instance := validSpecInstance()
	instance.Spec.Backup = saasv1alpha1.BackupSpec{Enabled: true}

	err := r.validateSpec(instance)
	if err == nil || err.reason != "BackupScheduleRequired" {
		t.Fatalf("validateSpec() = %v, want reason BackupScheduleRequired", err)
	}
}

func TestValidateSpec_RequiresObjectStorageSecretRef(t *testing.T) {
	r := &OdooInstanceReconciler{}
	instance := validSpecInstance()
	instance.Spec.Backup = saasv1alpha1.BackupSpec{
		Enabled:  true,
		Schedule: "0 2 * * *",
		Destination: saasv1alpha1.BackupDestinationSpec{
			Type: saasv1alpha1.BackupDestinationObjectStore,
		},
	}

	err := r.validateSpec(instance)
	if err == nil || err.reason != "BackupDestinationMisconfigured" {
		t.Fatalf("validateSpec() = %v, want reason BackupDestinationMisconfigured", err)
	}
}

func TestValidateSpec_RequiresExternalDatabaseCredentials(t *testing.T) {
	r := &OdooInstanceReconciler{}
	instance := validSpecInstance()
	instance.Spec.Database = saasv1alpha1.DatabaseSpec{Mode: saasv1alpha1.DatabaseModeExternal}

	err := r.validateSpec(instance)
	if err == nil || err.reason != "ExternalDatabaseCredentialsRequired" {
		t.Fatalf("validateSpec() = %v, want reason ExternalDatabaseCredentialsRequired", err)
	}

	instance.Spec.Database.CredentialsSecretRef = &corev1.LocalObjectReference{Name: "db-creds"}
	if err := r.validateSpec(instance); err != nil {
		t.Fatalf("validateSpec() = %v, want nil once credentialsSecretRef is set", err)
	}
}

func TestValidateSpec_RequiresObjectStorageSecretRefForRestore(t *testing.T) {
	r := &OdooInstanceReconciler{}
	instance := validSpecInstance()
	instance.Spec.Restore = &saasv1alpha1.RestoreSpec{
		Source: saasv1alpha1.RestoreSourceSpec{
			BackupDestinationSpec: saasv1alpha1.BackupDestinationSpec{Type: saasv1alpha1.BackupDestinationObjectStore},
		},
	}

	err := r.validateSpec(instance)
	if err == nil || err.reason != "RestoreSourceMisconfigured" {
		t.Fatalf("validateSpec() = %v, want reason RestoreSourceMisconfigured", err)
	}

	instance.Spec.Restore.Source.ObjectStorageSecretRef = &corev1.LocalObjectReference{Name: "migration-creds"}
	if err := r.validateSpec(instance); err != nil {
		t.Fatalf("validateSpec() = %v, want nil once objectStorageSecretRef is set", err)
	}
}

func TestValidateSpec_AllowsPVCRestoreSourceWithoutExtraConfig(t *testing.T) {
	r := &OdooInstanceReconciler{}
	instance := validSpecInstance()
	instance.Spec.Restore = &saasv1alpha1.RestoreSpec{
		Source: saasv1alpha1.RestoreSourceSpec{
			BackupDestinationSpec: saasv1alpha1.BackupDestinationSpec{Type: saasv1alpha1.BackupDestinationPVC},
		},
	}

	if err := r.validateSpec(instance); err != nil {
		t.Fatalf("validateSpec() = %v, want nil for a PVC restore source", err)
	}
}

func TestValidateSpec_AcceptsFullyValidSpec(t *testing.T) {
	r := &OdooInstanceReconciler{SupportedOdooVersions: []string{"18.0"}}
	instance := validSpecInstance()
	if err := r.validateSpec(instance); err != nil {
		t.Fatalf("validateSpec() = %v, want nil", err)
	}
}

func TestComputePhase(t *testing.T) {
	tests := []struct {
		name  string
		setup func(*saasv1alpha1.OdooInstance)
		want  saasv1alpha1.OdooInstancePhase
	}{
		{
			name:  "suspended takes priority",
			setup: func(i *saasv1alpha1.OdooInstance) { i.Spec.Suspended = true },
			want:  saasv1alpha1.PhaseSuspended,
		},
		{
			name: "ready when Ready condition true",
			setup: func(i *saasv1alpha1.OdooInstance) {
				setCondition(i, saasv1alpha1.ConditionReady, "True", "x", "")
			},
			want: saasv1alpha1.PhaseReady,
		},
		{
			name: "degraded when Degraded condition true",
			setup: func(i *saasv1alpha1.OdooInstance) {
				setCondition(i, saasv1alpha1.ConditionDegraded, "True", "x", "")
			},
			want: saasv1alpha1.PhaseDegraded,
		},
		{
			name:  "pending with no conditions at all",
			setup: func(i *saasv1alpha1.OdooInstance) {},
			want:  saasv1alpha1.PhasePending,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			instance := &saasv1alpha1.OdooInstance{}
			tt.setup(instance)
			if got := computePhase(instance); got != tt.want {
				t.Errorf("computePhase() = %q, want %q", got, tt.want)
			}
		})
	}
}
