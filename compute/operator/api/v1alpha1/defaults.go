package v1alpha1

// DefaultOdooInstanceSpec returns an OdooInstanceSpec pre-populated with
// every field that carries a non-zero recommended default (Workers,
// Database.Mode/Version, Storage.Filestore.AccessMode,
// Networking.NetworkPolicy.Enabled, Replicas), so a Go client — including a
// SaaS control-plane API built against this package — can start from it and
// override only the fields it actually needs to customize, rather than
// constructing an OdooInstanceSpec{} literal from scratch.
//
// This exists because several fields intentionally do NOT use the `json:
// "...,omitempty"` tag (see TLSSpec.Enabled and WorkersSpec.Count/
// MaxCronThreads for the reasoning): Go's encoding/json elides a field at
// its zero value only when `omitempty` is set, so those fields are always
// serialized explicitly. That is what makes this controller's own writes
// safe against silently clobbering an explicit customer choice back to a
// CRD default (see docs/architecture.md, "A CRD Defaulting Gotcha") — but
// its direct consequence is that any Go client constructing a partial
// OdooInstanceSpec struct literal gets the Go zero value for those fields
// (false/0), NOT the CRD's documented default, unless it fills them in
// itself. Building on top of this function is the supported way to do
// that; relying on CRD-schema defaulting alone from a typed Go client is
// not.
func DefaultOdooInstanceSpec() OdooInstanceSpec {
	return OdooInstanceSpec{
		Database: DatabaseSpec{
			Mode:    DatabaseModeManaged,
			Version: "16",
		},
		Storage: InstanceStorageSpec{
			Filestore: FilestoreSpec{
				AccessMode: FilestoreAccessModeRWO,
			},
		},
		Workers: WorkersSpec{
			Count:          2,
			MaxCronThreads: 1,
		},
		Replicas: func() *int32 { r := int32(1); return &r }(),
		Networking: NetworkingSpec{
			NetworkPolicy: NetworkPolicySpec{Enabled: true},
		},
		Domain: DomainSpec{
			TLS: TLSSpec{Enabled: true},
		},
		Backup: BackupSpec{
			Retention: 7,
			Destination: BackupDestinationSpec{
				Type: BackupDestinationPVC,
			},
		},
	}
}
