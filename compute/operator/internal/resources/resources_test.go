package resources

import (
	"fmt"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
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

func TestOdooDeployment_WebRole_DisablesCronInWebContainer(t *testing.T) {
	instance := testInstance()
	instance.Spec.Replicas = ptr.To(int32(1))
	dep := OdooDeployment(instance, Platform{})

	container := dep.Spec.Template.Spec.Containers[0]
	if !containsArg(container.Args, "--max-cron-threads=0") {
		t.Errorf("expected web container to leave cron to its sidecar, args = %v", container.Args)
	}
}

func TestOdooDeployment_ZeroDowntimeRollout(t *testing.T) {
	dep := OdooDeployment(testInstance(), Platform{})

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

func TestOdooDeployment_AlwaysUsesOnePodAndRunsCron(t *testing.T) {
	instance := testInstance()
	instance.Spec.Replicas = ptr.To(int32(3))
	dep := OdooDeployment(instance, Platform{})
	if *dep.Spec.Replicas != 1 {
		t.Fatal("builder created horizontal replicas")
	}
	if !containsArg(dep.Spec.Template.Spec.Containers[1].Args, "--max-cron-threads=1") {
		t.Fatal("cron must run inside the only Odoo pod")
	}
}

func TestOdooDeployment_CronHasNoReadinessProbe(t *testing.T) {
	dep := OdooDeployment(testInstance(), Platform{})
	for _, c := range dep.Spec.Template.Spec.Containers {
		if c.Name != "cron" {
			continue
		}
		if c.ReadinessProbe != nil {
			t.Error("cron readiness probe would drop web traffic when cron fails")
		}
		if c.LivenessProbe == nil || c.StartupProbe == nil {
			t.Error("cron must keep its liveness and startup probes")
		}
		return
	}
	t.Fatal("no cron container")
}

func TestOdooDeployment_PinsVerifiedNonRootUser(t *testing.T) {
	instance := testInstance()
	dep := OdooDeployment(instance, Platform{})
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

	job := OdooRestoreJob(instance, Platform{RestoreToolImage: "ghcr.io/example/restore-tool:v1"})
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

	job := OdooRestoreJob(instance, Platform{RestoreToolImage: "ghcr.io/example/restore-tool:v1"})

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

	job := OdooRestoreJob(instance, Platform{RestoreToolImage: "ghcr.io/example/restore-tool:v1"})
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
	if ann := OdooDeployment(instance, Platform{}).Spec.Template.Annotations; len(ann) != 1 || ann[AnnotationPlatformAddons] == "" {
		t.Errorf("no filter: pod template annotations = %v, want only the platform-addons hash", ann)
	}
	instance.Spec.DatabaseFilter = "^acme_.+$"
	ann := OdooDeployment(instance, Platform{}).Spec.Template.Annotations
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
	c := OdooUpdateJob(instance, Platform{}).Spec.Template.Spec.Containers[0]
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
	args := OdooUpdateJob(instance, Platform{}).Spec.Template.Spec.Containers[0].Args
	if strings.Join(args, ",") != "odoo" {
		t.Errorf("update Job args = %v, want [odoo]", args)
	}
}

func TestOdooInitJob_KeepsImageEntrypoint(t *testing.T) {
	if cmd := OdooInitJob(testInstance(), Platform{}).Spec.Template.Spec.Containers[0].Command; cmd != nil {
		t.Errorf("init Job command = %v, want the image entrypoint", cmd)
	}
}

func dbmInstance() *saasv1alpha1.OdooInstance {
	instance := testInstance()
	instance.Spec.DatabaseFilter = "^acme_.+$"
	instance.Spec.DatabaseManager = &saasv1alpha1.DatabaseManagerSpec{Prefix: "acme_"}
	return instance
}

func TestOdooConf_DatabaseManagerOff_KeepsManagerDisabled(t *testing.T) {
	conf := odooConf(testInstance())
	if !strings.Contains(conf, "list_db = False\n") || strings.Contains(conf, "saas_dbm_") {
		t.Errorf("database manager must stay off by default:\n%s", conf)
	}
}

func TestOdooConf_DatabaseManagerOn(t *testing.T) {
	conf := odooConf(dbmInstance())
	for _, want := range []string{"list_db = True\n", "saas_dbm_prefix = acme_\n", "saas_dbm_key = __DBM_KEY__\n"} {
		if !strings.Contains(conf, want) {
			t.Errorf("missing %q:\n%s", want, conf)
		}
	}
	if !strings.Contains(renderInitContainerScript(), "s/__DBM_KEY__/$DBM_KEY/g") {
		t.Error("init script must substitute the database manager key")
	}
}

func TestOdooDeployment_DatabaseManager_LoadsPlatformAddon(t *testing.T) {
	instance := dbmInstance()
	instance.Spec.AddonsPaths = []string{"/opt/tenant-addons/repo"}
	dep := OdooDeployment(instance, Platform{})
	odoo := dep.Spec.Template.Spec.Containers[0]
	args := strings.Join(odoo.Args, " ")
	if !strings.Contains(args, "--load=base,web,saas_tenant_dbm") ||
		!strings.Contains(args, "--addons-path=/opt/saas-platform-addons,/opt/tenant-addons/repo") {
		t.Errorf("args = %v", odoo.Args)
	}
	mounted := false
	for _, m := range odoo.VolumeMounts {
		mounted = mounted || (m.Name == "platform-addons" && m.MountPath == PlatformAddonsPath && m.ReadOnly)
	}
	if !mounted {
		t.Errorf("platform addons not mounted read-only: %v", odoo.VolumeMounts)
	}
	if dep.Spec.Template.Annotations[AnnotationPlatformAddons] == "" {
		t.Error("addon hash annotation missing")
	}
	// Always, not just with the database manager on: saas_tenant_dbm
	// gates the raw /web/database/* endpoints, so even instances
	// without spec.databaseManager must load it (there it hard-locks
	// every endpoint because no DBM key is configured).
	defaultArgs := strings.Join(OdooDeployment(testInstance(), Platform{}).Spec.Template.Spec.Containers[0].Args, " ")
	if !strings.Contains(defaultArgs, "--load=base,web,saas_tenant_dbm") {
		t.Errorf("default instance args = %v, want saas_tenant_dbm loaded", defaultArgs)
	}
	if OdooDeployment(testInstance(), Platform{}).Spec.Template.Annotations[AnnotationPlatformAddons] == "" {
		t.Error("platform-addons hash annotation must be set on every instance")
	}
}

func TestPlatformAddonsConfigMap_ShipsTheAddonFiles(t *testing.T) {
	cm := PlatformAddonsConfigMap(dbmInstance())
	for _, key := range []string{"saas_tenant_dbm.__init__.py", "saas_tenant_dbm.__manifest__.py", "saas_tenant_dbm.controllers.py"} {
		if cm.Data[key] == "" {
			t.Errorf("ConfigMap key %s missing (keys: %v)", key, cm.Data)
		}
	}
	vol := platformAddonsVolume(dbmInstance())
	found := false
	for _, item := range vol.ConfigMap.Items {
		found = found || (item.Key == "saas_tenant_dbm.__init__.py" && item.Path == "saas_tenant_dbm/__init__.py")
	}
	if !found {
		t.Errorf("volume items don't rebuild the addon directory: %v", vol.ConfigMap.Items)
	}
}

func TestOdooDeployment_Shell_SidecarWithoutSecrets(t *testing.T) {
	instance := testInstance()
	instance.Spec.Shell = true
	pod := OdooDeployment(instance, Platform{}).Spec.Template.Spec
	if len(pod.Containers) != 3 || pod.Containers[1].Name != ShellContainerName {
		t.Fatalf("containers = %v, want odoo + shell + cron", pod.Containers)
	}
	shell := pod.Containers[1]
	for _, m := range shell.VolumeMounts {
		if m.Name == "etc-odoo" {
			t.Error("the shell must not mount odoo.conf")
		}
	}
	for _, e := range shell.Env {
		if e.ValueFrom != nil {
			t.Errorf("the shell must not get secret env: %v", e)
		}
	}
	if len(OdooDeployment(testInstance(), Platform{}).Spec.Template.Spec.Containers) != 2 {
		t.Error("no shell sidecar unless spec.shell is set")
	}
}

func TestOdooDeployment_LivenessOutlastsOdooRequestLimit(t *testing.T) {
	odoo := OdooDeployment(testInstance(), Platform{}).Spec.Template.Spec.Containers[0]
	live := odoo.LivenessProbe
	if window := live.PeriodSeconds * live.FailureThreshold; window <= 1800 {
		t.Errorf("liveness gives up after %ds, must exceed Odoo's 1800s limit_time_real", window)
	}
	if odoo.ReadinessProbe.FailureThreshold != 3 {
		t.Errorf("readiness must still react quickly, got failureThreshold=%d", odoo.ReadinessProbe.FailureThreshold)
	}
}

func dbSized(cpu, mem string) *saasv1alpha1.OdooInstance {
	instance := testInstance()
	instance.Spec.Database.Resources = &corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("125m"), corev1.ResourceMemory: resource.MustParse("256Mi")},
		Limits:   corev1.ResourceList{corev1.ResourceCPU: resource.MustParse(cpu), corev1.ResourceMemory: resource.MustParse(mem)},
	}
	return instance
}

func TestDatabaseStatefulSet_SizedFromSpecAndTuned(t *testing.T) {
	sts := DatabaseStatefulSet(dbSized("500m", "1Gi"), Platform{})
	c := sts.Spec.Template.Spec.Containers[0]
	if got := c.Resources.Limits[corev1.ResourceMemory]; got.String() != "1Gi" {
		t.Errorf("memory limit = %s, want 1Gi", got.String())
	}
	args := strings.Join(c.Args, " ")
	for _, want := range []string{"shared_buffers=256MB", "effective_cache_size=512MB", "work_mem=16MB", "maintenance_work_mem=64MB"} {
		if !strings.Contains(args, want) {
			t.Errorf("args %q missing %s", args, want)
		}
	}
	if sts.Spec.UpdateStrategy.Type != "OnDelete" {
		t.Error("the database StatefulSet must not roll its only pod on a resources change")
	}
	for _, p := range c.ResizePolicy {
		if p.RestartPolicy != corev1.NotRequired {
			t.Errorf("resize policy %v would restart PostgreSQL", p)
		}
	}
}

func TestDatabaseStatefulSet_DefaultsWithoutSpec(t *testing.T) {
	c := DatabaseStatefulSet(testInstance(), Platform{}).Spec.Template.Spec.Containers[0]
	if got := c.Resources.Limits[corev1.ResourceCPU]; got.String() != "2" {
		t.Errorf("default cpu limit = %s, want 2", got.String())
	}
	if !strings.Contains(strings.Join(c.Args, " "), "shared_buffers=512MB") {
		t.Errorf("default tuning args = %v", c.Args)
	}
}

func TestOdooDeployment_PerProcessMemoryLimits(t *testing.T) {
	instance := testInstance()
	instance.Spec.Resources.Limits = corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("1000Mi")}
	args := strings.Join(OdooDeployment(instance, Platform{}).Spec.Template.Spec.Containers[0].Args, " ")
	limit := int64(1000 * 1024 * 1024)
	for _, want := range []string{
		"--limit-memory-soft=" + strconvI(limit*60/100),
		"--limit-memory-hard=" + strconvI(limit*75/100),
		// Long-running tenant ops (reports, imports, direct SQL on huge
		// tables) must not be wall-clock killed by Odoo's tiny stock
		// defaults of 120s real / 60s CPU.
		"--limit-time-real=1800",
		"--limit-time-cpu=1800",
	} {
		if !strings.Contains(args, want) {
			t.Errorf("args %q missing %s", args, want)
		}
	}
}

func TestCloudNativePGCluster_CarriesDatabaseResources(t *testing.T) {
	u := CloudNativePGCluster(dbSized("1", "2Gi"), Platform{})
	got, _, _ := unstructuredNestedString(u.Object, "spec", "resources", "limits", "memory")
	if got != "2Gi" {
		t.Errorf("CNPG memory limit = %q, want 2Gi", got)
	}
	// The cluster should no longer rely on stock PG defaults for idle/idle-in-
	// txn reaping: long tenant requests previously left half-killed
	// connections.
	paramsIface, _, _ := unstructured.NestedMap(u.Object, "spec", "postgresql", "parameters")
	params := map[string]string{}
	for k, v := range paramsIface {
		if s, ok := v.(string); ok {
			params[k] = s
		}
	}
	wantParams := map[string]string{
		"idle_in_transaction_session_timeout": "1800s",
		"tcp_keepalives_idle":                 "60",
		"tcp_keepalives_interval":             "15",
		"tcp_keepalives_count":                "5",
		"statement_timeout":                   "0",
		"max_connections":                     "200",
	}
	for k, v := range wantParams {
		if params[k] != v {
			t.Errorf("postgresql.parameters[%q] = %q, want %q (got %v)", k, params[k], v, params)
		}
	}
}

func strconvI(v int64) string { return fmt.Sprintf("%d", v) }

func unstructuredNestedString(obj map[string]interface{}, fields ...string) (string, bool, error) {
	return unstructured.NestedString(obj, fields...)
}

func TestOdooDeployment_RWOFilestoreKeepsPodsOnOneNode(t *testing.T) {
	instance := testInstance()
	aff := OdooDeployment(instance, Platform{}).Spec.Template.Spec.Affinity
	if aff == nil || aff.PodAffinity == nil || len(aff.PodAffinity.RequiredDuringSchedulingIgnoredDuringExecution) != 1 {
		t.Fatalf("web pods with an RWO filestore need required same-node affinity, got %+v", aff)
	}
	term := aff.PodAffinity.RequiredDuringSchedulingIgnoredDuringExecution[0]
	if term.TopologyKey != "kubernetes.io/hostname" || term.LabelSelector.MatchLabels["saas.odoo.example.com/role"] != "web" {
		t.Errorf("affinity term = %+v", term)
	}
	instance.Spec.Storage.Filestore.AccessMode = saasv1alpha1.FilestoreAccessModeRWX
	if OdooDeployment(instance, Platform{}).Spec.Template.Spec.Affinity != nil {
		t.Error("RWX filestore: pods may spread across nodes")
	}
}

func TestBackupAndRestorePassHostingPrefix(t *testing.T) {
	instance := testInstance()
	instance.Name = "odoo-acme"
	instance.Spec.DatabaseManager = &saasv1alpha1.DatabaseManagerSpec{Prefix: "acme_"}
	instance.Spec.Backup = saasv1alpha1.BackupSpec{Schedule: "0 2 * * *", Destination: saasv1alpha1.BackupDestinationSpec{Type: saasv1alpha1.BackupDestinationPVC}}
	instance.Spec.Restore = &saasv1alpha1.RestoreSpec{Source: saasv1alpha1.RestoreSourceSpec{BackupDestinationSpec: saasv1alpha1.BackupDestinationSpec{Type: saasv1alpha1.BackupDestinationPVC}}}
	backup := BackupCronJob(instance, Platform{}).Spec.JobTemplate.Spec.Template.Spec.Containers[0]
	restore := OdooRestoreJob(instance, Platform{}).Spec.Template.Spec.Containers[0]
	for _, c := range []corev1.Container{backup, restore} {
		if got := envValue(c.Env, "DATABASE_PREFIX"); got != "acme_" {
			t.Errorf("DATABASE_PREFIX = %q, want acme_", got)
		}
		if got := envValue(c.Env, "INSTANCE_NAME"); got != "odoo-acme" {
			t.Errorf("INSTANCE_NAME = %q, want odoo-acme", got)
		}
	}
}

func TestDatabaseManagerCapacityConfig(t *testing.T) {
	instance := dbmInstance()
	instance.Spec.DatabaseManager.MaxDatabases = 1
	if !strings.Contains(odooConf(instance), "saas_dbm_max_databases = 1") {
		t.Fatal("production database limit missing from tenant configuration")
	}
	if podTemplateAnnotations(instance)["saas.odoo.example.com/database-limit"] != "1" {
		t.Fatal("changing database limit must roll serving pods")
	}
	instance.Spec.DatabaseManager.MaxDatabases = 0
	if !strings.Contains(odooConf(instance), "saas_dbm_max_databases = 0") {
		t.Fatal("unlimited database configuration missing")
	}
}

func TestOdooDatabaseName_SpecNameDrivesInitJobConfigAndUpdate(t *testing.T) {
	instance := testInstance()
	if got := OdooDatabaseName(instance); got != "odoo" {
		t.Fatalf("default database name = %q, want odoo", got)
	}
	instance.Spec.Database.Name = "acme_main"
	if got := OdooDatabaseName(instance); got != "acme_main" {
		t.Fatalf("database name = %q, want acme_main", got)
	}
	args := strings.Join(OdooInitJob(instance, Platform{}).Spec.Template.Spec.Containers[0].Args, " ")
	if !strings.Contains(args, "-d acme_main") {
		t.Errorf("init Job args = %q, want -d acme_main", args)
	}
	if conf := OdooConfigMap(instance).Data["odoo.conf.tmpl"]; !strings.Contains(conf, "db_name = __DB_NAME__") {
		t.Errorf("config template must keep the db_name placeholder the init container fills from the Secret:\n%s", conf)
	}
	instance.Spec.Update = &saasv1alpha1.UpdateSpec{Token: "b", Modules: []string{"sale"}}
	if got := strings.Join(updateDatabases(instance), ","); got != "acme_main" {
		t.Errorf("update databases = %q, want acme_main", got)
	}
}
