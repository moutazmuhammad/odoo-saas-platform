package resources

import (
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

func testInstance() *saasv1alpha1.OdooInstance {
	return &saasv1alpha1.OdooInstance{
		ObjectMeta: metav1.ObjectMeta{Name: "acme"},
		Spec: saasv1alpha1.OdooInstanceSpec{
			Version: "18.0",
			Image:   saasv1alpha1.ImageSpec{Repository: "registry.example.com/odoo", Tag: "18.0-abc"},
			Domain:  saasv1alpha1.DomainSpec{Hostname: "acme.example.com"},
			Storage: saasv1alpha1.InstanceStorageSpec{
				Filestore: saasv1alpha1.FilestoreSpec{Size: "10Gi", AccessMode: saasv1alpha1.FilestoreAccessModeRWO},
			},
			Workers: saasv1alpha1.WorkersSpec{Count: 2, MaxCronThreads: 1},
		},
	}
}

func TestTenantNamespace_DefaultsToPrefixedInstanceName(t *testing.T) {
	instance := testInstance()
	if got, want := TenantNamespace(instance), "odoo-tenant-acme"; got != want {
		t.Errorf("TenantNamespace() = %q, want %q", got, want)
	}
}

func TestTenantNamespace_HonorsOverride(t *testing.T) {
	instance := testInstance()
	instance.Spec.Tenancy.NamespaceOverride = "custom-ns"
	if got, want := TenantNamespace(instance), "custom-ns"; got != want {
		t.Errorf("TenantNamespace() = %q, want %q", got, want)
	}
}

func TestFilestorePVC_UsesRequestedAccessModeAndSize(t *testing.T) {
	instance := testInstance()
	pvc, err := FilestorePVC(instance)
	if err != nil {
		t.Fatalf("FilestorePVC() error = %v", err)
	}
	if len(pvc.Spec.AccessModes) != 1 || pvc.Spec.AccessModes[0] != corev1.ReadWriteOnce {
		t.Errorf("expected ReadWriteOnce access mode, got %v", pvc.Spec.AccessModes)
	}
	size := pvc.Spec.Resources.Requests[corev1.ResourceStorage]
	if size.String() != "10Gi" {
		t.Errorf("expected 10Gi storage request, got %s", size.String())
	}
}

func TestFilestorePVC_RejectsUnparsableSize(t *testing.T) {
	instance := testInstance()
	instance.Spec.Storage.Filestore.Size = "not-a-quantity"
	if _, err := FilestorePVC(instance); err == nil {
		t.Error("expected an error for an unparsable filestore size, got nil")
	}
}

func gatewayIngressPorts(t *testing.T, np *networkingv1.NetworkPolicy) []int32 {
	t.Helper()
	for _, rule := range np.Spec.Ingress {
		for _, peer := range rule.From {
			if peer.NamespaceSelector != nil {
				var ports []int32
				for _, p := range rule.Ports {
					ports = append(ports, p.Port.IntVal)
				}
				return ports
			}
		}
	}
	t.Fatal("no ingress rule found with a NamespaceSelector (the shared-gateway rule)")
	return nil
}

func TestTenantNetworkPolicy_TLSDisabled_OmitsAcmeSolverPort(t *testing.T) {
	instance := testInstance()
	instance.Spec.Domain.TLS.Enabled = false
	np := TenantNetworkPolicy(instance, "ingress", map[string]string{"app.kubernetes.io/name": "traefik"})
	ports := gatewayIngressPorts(t, np)
	for _, p := range ports {
		if p == AcmeHTTP01SolverPort {
			t.Errorf("gateway ingress rule allows port %d with TLS disabled; the ACME solver port should only be opened when TLS is enabled", AcmeHTTP01SolverPort)
		}
	}
}

// Regression test: without this port, cert-manager's HTTP-01 solver Pod is
// unreachable through the shared gateway and every certificate request
// silently times out — see TenantNetworkPolicy's AcmeHTTP01SolverPort
// comment. Caught live provisioning a real OdooInstance with
// domain.tls.enabled=true against a real cert-manager ClusterIssuer.
func TestTenantNetworkPolicy_TLSEnabled_AllowsAcmeSolverPort(t *testing.T) {
	instance := testInstance()
	instance.Spec.Domain.TLS.Enabled = true
	np := TenantNetworkPolicy(instance, "ingress", map[string]string{"app.kubernetes.io/name": "traefik"})
	ports := gatewayIngressPorts(t, np)
	for _, p := range ports {
		if p == AcmeHTTP01SolverPort {
			return
		}
	}
	t.Errorf("gateway ingress rule does not allow port %d with TLS enabled; got ports %v", AcmeHTTP01SolverPort, ports)
}

func TestTenantNetworkPolicy_TLSEnabled_StillAllowsOdooPorts(t *testing.T) {
	instance := testInstance()
	instance.Spec.Domain.TLS.Enabled = true
	np := TenantNetworkPolicy(instance, "ingress", map[string]string{"app.kubernetes.io/name": "traefik"})
	ports := gatewayIngressPorts(t, np)
	want := map[int32]bool{OdooHTTPPort: false, OdooLongpollingPort: false}
	for _, p := range ports {
		if _, ok := want[p]; ok {
			want[p] = true
		}
	}
	for port, found := range want {
		if !found {
			t.Errorf("expected gateway ingress rule to still allow Odoo port %d, got ports %v", port, ports)
		}
	}
}

func TestOdooDeployment_WebRole_RunsCronWhenSingleReplica(t *testing.T) {
	instance := testInstance()
	instance.Spec.Replicas = ptr.To(int32(1))
	dep := OdooDeployment(instance, RoleWeb)

	container := dep.Spec.Template.Spec.Containers[0]
	if !containsArg(container.Args, "--max-cron-threads=1") {
		t.Errorf("expected single-replica web Deployment to run cron itself, args = %v", container.Args)
	}
	if NeedsCronDeployment(instance) {
		t.Error("NeedsCronDeployment() = true for replicas=1, want false")
	}
}

func TestOdooDeployment_ZeroDowntimeRollout(t *testing.T) {
	dep := OdooDeployment(testInstance(), RoleWeb)

	ru := dep.Spec.Strategy.RollingUpdate
	if ru == nil || ru.MaxUnavailable.IntValue() != 0 || ru.MaxSurge.IntValue() != 1 {
		t.Fatalf("want RollingUpdate maxUnavailable=0 maxSurge=1, got %+v", dep.Spec.Strategy)
	}
	c := dep.Spec.Template.Spec.Containers[0]
	if c.Lifecycle == nil || c.Lifecycle.PreStop == nil || c.Lifecycle.PreStop.Exec == nil {
		t.Fatal("odoo container has no preStop delay; in-flight requests drop during rollouts")
	}
	grace := *dep.Spec.Template.Spec.TerminationGracePeriodSeconds
	if int64(preStopDelaySeconds) >= grace {
		t.Errorf("preStop delay %ds must be below the %ds grace period", preStopDelaySeconds, grace)
	}
}

func TestOdooDeployment_WebRole_DisablesCronWhenMultiReplica(t *testing.T) {
	instance := testInstance()
	instance.Spec.Replicas = ptr.To(int32(3))
	instance.Spec.Storage.Filestore.AccessMode = saasv1alpha1.FilestoreAccessModeRWX
	dep := OdooDeployment(instance, RoleWeb)

	container := dep.Spec.Template.Spec.Containers[0]
	if !containsArg(container.Args, "--max-cron-threads=0") {
		t.Errorf("expected multi-replica web Deployment to disable its own cron threads, args = %v", container.Args)
	}
	if !NeedsCronDeployment(instance) {
		t.Error("NeedsCronDeployment() = false for replicas=3, want true")
	}

	cron := OdooDeployment(instance, RoleCron)
	cronContainer := cron.Spec.Template.Spec.Containers[0]
	if !containsArg(cronContainer.Args, "--workers=0") {
		t.Errorf("expected dedicated cron Deployment to run zero HTTP workers, args = %v", cronContainer.Args)
	}
	if !containsArg(cronContainer.Args, "--max-cron-threads=1") {
		t.Errorf("expected dedicated cron Deployment to run the configured cron threads, args = %v", cronContainer.Args)
	}
}

func TestOdooDeployment_PinsVerifiedNonRootUser(t *testing.T) {
	instance := testInstance()
	dep := OdooDeployment(instance, RoleWeb)
	for _, c := range append(dep.Spec.Template.Spec.InitContainers, dep.Spec.Template.Spec.Containers...) {
		if c.SecurityContext == nil || c.SecurityContext.RunAsUser == nil {
			t.Errorf("container %q has no explicit RunAsUser; the official Odoo image declares a non-numeric USER and will fail runAsNonRoot verification without one", c.Name)
			continue
		}
		if *c.SecurityContext.RunAsUser != odooImageUID {
			t.Errorf("container %q RunAsUser = %d, want %d", c.Name, *c.SecurityContext.RunAsUser, odooImageUID)
		}
	}
}

func TestGeneratePassword_LengthAndUniqueness(t *testing.T) {
	a, err := GeneratePassword(24)
	if err != nil {
		t.Fatalf("GeneratePassword() error = %v", err)
	}
	if len(a) != 24 {
		t.Errorf("GeneratePassword(24) length = %d, want 24", len(a))
	}
	b, err := GeneratePassword(24)
	if err != nil {
		t.Fatalf("GeneratePassword() error = %v", err)
	}
	if a == b {
		t.Error("two GeneratePassword() calls returned the same value")
	}
	for _, r := range a {
		if !((r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9')) {
			t.Errorf("password contains non-alphanumeric rune %q; the odoo.conf template substitution relies on this to be sed-safe", r)
		}
	}
}

func TestOdooRestoreJob_ObjectStorageSource_WiresCredentialsNoPVCMount(t *testing.T) {
	instance := testInstance()
	instance.Spec.Restore = &saasv1alpha1.RestoreSpec{
		Source: saasv1alpha1.RestoreSourceSpec{
			BackupDestinationSpec: saasv1alpha1.BackupDestinationSpec{
				Type:                   saasv1alpha1.BackupDestinationObjectStore,
				Bucket:                 "customer-migrations",
				Prefix:                 "acme",
				ObjectStorageSecretRef: &corev1.LocalObjectReference{Name: "acme-migration-creds"},
			},
			BackupID: "2026-09-01T02-00-00Z",
		},
	}

	job := OdooRestoreJob(instance, "ghcr.io/example/restore-tool:v1")
	if job.Name != "odoo-restore" {
		t.Errorf("OdooRestoreJob() name = %q, want %q", job.Name, "odoo-restore")
	}
	container := job.Spec.Template.Spec.Containers[0]

	wantEnv := map[string]string{"SOURCE_TYPE": "ObjectStorage", "SOURCE_BUCKET": "customer-migrations", "SOURCE_PREFIX": "acme", "BACKUP_ID": "2026-09-01T02-00-00Z"}
	for name, want := range wantEnv {
		if got := envValue(container.Env, name); got != want {
			t.Errorf("env %s = %q, want %q", name, got, want)
		}
	}
	for _, secretEnv := range []string{"OBJECT_STORAGE_ENDPOINT", "OBJECT_STORAGE_ACCESS_KEY", "OBJECT_STORAGE_SECRET_KEY"} {
		if !envSourcedFromSecret(container.Env, secretEnv, "acme-migration-creds") {
			t.Errorf("expected env %s to be sourced from Secret %q", secretEnv, "acme-migration-creds")
		}
	}
	for _, v := range job.Spec.Template.Spec.Volumes {
		if v.Name == "restore-source" {
			t.Error("expected no restore-source (backup PVC) volume when Source.Type is ObjectStorage")
		}
	}
}

func TestOdooRestoreJob_PVCSource_MountsBackupPVCReadOnly(t *testing.T) {
	instance := testInstance()
	instance.Spec.Restore = &saasv1alpha1.RestoreSpec{
		Source: saasv1alpha1.RestoreSourceSpec{
			BackupDestinationSpec: saasv1alpha1.BackupDestinationSpec{Type: saasv1alpha1.BackupDestinationPVC},
		},
	}

	job := OdooRestoreJob(instance, "ghcr.io/example/restore-tool:v1")

	var found *corev1.Volume
	for i := range job.Spec.Template.Spec.Volumes {
		if job.Spec.Template.Spec.Volumes[i].Name == "restore-source" {
			found = &job.Spec.Template.Spec.Volumes[i]
		}
	}
	if found == nil {
		t.Fatal("expected a restore-source volume mounting the backup PVC")
	}
	if found.PersistentVolumeClaim == nil || found.PersistentVolumeClaim.ClaimName != BackupPVCName(instance) {
		t.Errorf("restore-source volume = %+v, want PVC claim %q", found, BackupPVCName(instance))
	}
	if !found.PersistentVolumeClaim.ReadOnly {
		t.Error("expected the backup PVC to be mounted read-only during restore")
	}

	container := job.Spec.Template.Spec.Containers[0]
	var mounted bool
	for _, m := range container.VolumeMounts {
		if m.Name == "restore-source" && m.MountPath == "/backups" && m.ReadOnly {
			mounted = true
		}
	}
	if !mounted {
		t.Errorf("expected restore-source mounted read-only at /backups, got %+v", container.VolumeMounts)
	}
}

func TestOdooRestoreJob_FilestoreMountedWritable(t *testing.T) {
	instance := testInstance()
	instance.Spec.Restore = &saasv1alpha1.RestoreSpec{
		Source: saasv1alpha1.RestoreSourceSpec{
			BackupDestinationSpec: saasv1alpha1.BackupDestinationSpec{Type: saasv1alpha1.BackupDestinationPVC},
		},
	}

	job := OdooRestoreJob(instance, "ghcr.io/example/restore-tool:v1")
	container := job.Spec.Template.Spec.Containers[0]
	for _, m := range container.VolumeMounts {
		if m.Name == "filestore" {
			if m.ReadOnly {
				t.Error("expected the filestore PVC to be mounted read-write so the restore Job can write into it")
			}
			if m.MountPath != "/var/lib/odoo" {
				t.Errorf("filestore mount path = %q, want /var/lib/odoo (matches OdooInitJob/OdooDeployment)", m.MountPath)
			}
			return
		}
	}
	t.Fatal("expected a filestore volume mount")
}

func envValue(env []corev1.EnvVar, name string) string {
	for _, e := range env {
		if e.Name == name {
			return e.Value
		}
	}
	return ""
}

func envSourcedFromSecret(env []corev1.EnvVar, name, secretName string) bool {
	for _, e := range env {
		if e.Name == name {
			return e.ValueFrom != nil && e.ValueFrom.SecretKeyRef != nil && e.ValueFrom.SecretKeyRef.Name == secretName
		}
	}
	return false
}

func containsArg(args []string, want string) bool {
	for _, a := range args {
		if a == want {
			return true
		}
	}
	return false
}

func TestOdooConf_DefaultDatabaseFilterIsOwnDatabase(t *testing.T) {
	conf := odooConf(testInstance())
	if !strings.Contains(conf, "dbfilter = ^__DB_NAME__$\n") {
		t.Errorf("default dbfilter missing:\n%s", conf)
	}
}

func TestOdooConf_DatabaseFilterOverride(t *testing.T) {
	instance := testInstance()
	instance.Spec.DatabaseFilter = "^acme_.+$"
	conf := odooConf(instance)
	if !strings.Contains(conf, "dbfilter = ^acme_.+$\n") || strings.Contains(conf, "^__DB_NAME__$") {
		t.Errorf("dbfilter override not applied:\n%s", conf)
	}
	if !strings.Contains(conf, "db_name = __DB_NAME__\n") {
		t.Errorf("db_name must stay the instance's own database:\n%s", conf)
	}
}

func TestOdooDeployment_DatabaseFilterChangeRollsPods(t *testing.T) {
	instance := testInstance()
	if ann := OdooDeployment(instance, RoleWeb).Spec.Template.Annotations; ann != nil {
		t.Errorf("no filter: pod template annotations = %v, want none", ann)
	}
	instance.Spec.DatabaseFilter = "^acme_.+$"
	ann := OdooDeployment(instance, RoleWeb).Spec.Template.Annotations
	if ann[AnnotationDatabaseFilter] != "^acme_.+$" {
		t.Errorf("pod template annotations = %v, want the database filter", ann)
	}
}

func TestOdooUpdateJob_UpgradesOwnAndExtraDatabasesOneByOne(t *testing.T) {
	instance := testInstance()
	instance.Spec.Update = &saasv1alpha1.UpdateSpec{
		Token: "build-1", Modules: []string{"sale", "stock"},
		Databases: []string{"acme_prod", "odoo", "acme_test", "acme_prod"},
	}
	c := OdooUpdateJob(instance).Spec.Template.Spec.Containers[0]
	if len(c.Command) != 4 || c.Command[0] != "sh" || !strings.Contains(c.Command[2], `-d "$db" -u "$SAAS_MODULES"`) {
		t.Errorf("update Job command = %v, want a per-database loop", c.Command)
	}
	if strings.Join(c.Args, ",") != "odoo,acme_prod,acme_test" {
		t.Errorf("update Job args = %v, want [odoo acme_prod acme_test]", c.Args)
	}
	if len(c.Env) != 1 || c.Env[0].Name != "SAAS_MODULES" || c.Env[0].Value != "sale,stock" {
		t.Errorf("update Job env = %v, want SAAS_MODULES=sale,stock", c.Env)
	}
}

func TestOdooUpdateJob_DefaultsToOwnDatabase(t *testing.T) {
	instance := testInstance()
	instance.Spec.Update = &saasv1alpha1.UpdateSpec{Token: "build-1", Modules: []string{"sale"}}
	args := OdooUpdateJob(instance).Spec.Template.Spec.Containers[0].Args
	if strings.Join(args, ",") != "odoo" {
		t.Errorf("update Job args = %v, want [odoo]", args)
	}
}

func TestOdooInitJob_KeepsImageEntrypoint(t *testing.T) {
	if cmd := OdooInitJob(testInstance()).Spec.Template.Spec.Containers[0].Command; cmd != nil {
		t.Errorf("init Job command = %v, want the image entrypoint", cmd)
	}
}
