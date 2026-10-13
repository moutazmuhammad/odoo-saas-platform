package controller

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
	"github.com/freightright/odoo-saas-platform/operator/internal/resources"
)

// databaseResult is the outcome of reconciling the database dependency,
// reported back into the DatabaseReady condition.
type databaseResult struct {
	ready   bool
	reason  string
	message string
}

// reconcileDatabase is the single place that branches on
// spec.database.mode. Every other part of the controller only ever deals
// with "is the database ready, and what Secret holds its credentials" —
// this is the abstraction boundary described in setup/01-ARCHITECTURE.md
// ("Database Architecture") that lets the platform swap Managed ->
// CloudNativePG -> External without ever touching the OdooInstance API.
func (r *OdooInstanceReconciler) reconcileDatabase(ctx context.Context, instance *saasv1alpha1.OdooInstance) (databaseResult, error) {
	if resources.DatabaseSecretName(instance) == resources.DatabaseAdminSecretName(instance) {
		return databaseResult{}, fmt.Errorf("application database Secret must not reference the platform admin Secret")
	}
	switch instance.Spec.Database.Mode {
	case saasv1alpha1.DatabaseModeExternal:
		return r.reconcileExternalDatabase(ctx, instance)
	case saasv1alpha1.DatabaseModeCloudNativePG:
		return r.reconcileCloudNativePGDatabase(ctx, instance)
	default: // DatabaseModeManaged, and "" pre-defaulting
		return r.reconcileManagedDatabase(ctx, instance)
	}
}

func (r *OdooInstanceReconciler) reconcileManagedDatabase(ctx context.Context, instance *saasv1alpha1.OdooInstance) (databaseResult, error) {
	ns := resources.TenantNamespace(instance)
	// Existing bootstrap sessions must be terminated to remove system-catalog
	// ownership access. Never trigger that cutover on an unannounced rollout.
	var legacy corev1.Secret
	legacyErr := r.Get(ctx, types.NamespacedName{Namespace: ns, Name: resources.DatabaseSecretName(instance)}, &legacy)
	if legacyErr == nil && string(legacy.Data["username"]) != "odoo_owner" && instance.Annotations["saas.odoo.example.com/database-role-migration"] != "approved" {
		return databaseResult{reason: "DatabaseMigrationRequired", message: "legacy database role requires an announced maintenance window; set the database-role-migration annotation to approved during that window"}, nil
	}
	if legacyErr != nil && !apierrors.IsNotFound(legacyErr) {
		return databaseResult{}, legacyErr
	}

	admin, err := r.ensureDatabaseAdmin(ctx, instance)
	if err != nil {
		return databaseResult{}, err
	}
	// Preserve legacy application credentials until the role job succeeds.
	var app corev1.Secret
	err = r.Get(ctx, types.NamespacedName{Namespace: ns, Name: resources.DatabaseSecretName(instance)}, &app)
	if apierrors.IsNotFound(err) {
		if err := r.publishDatabaseOwner(ctx, instance, string(admin.Data["owner-password"])); err != nil {
			return databaseResult{}, err
		}
	} else if err != nil {
		return databaseResult{}, err
	}

	svc := resources.DatabaseService(instance)
	setOwner(instance, svc)
	if err := r.apply(ctx, svc); err != nil {
		return databaseResult{}, err
	}

	sts := resources.DatabaseStatefulSet(instance, r.platform())
	setOwner(instance, sts)
	var live appsv1.StatefulSet
	if err := r.Get(ctx, types.NamespacedName{Namespace: ns, Name: sts.Name}, &live); err == nil {
		// volumeClaimTemplates are immutable; the volume is grown through
		// its PVC instead (growDatabaseVolume).
		sts.Spec.VolumeClaimTemplates = live.Spec.VolumeClaimTemplates
	} else if !apierrors.IsNotFound(err) {
		return databaseResult{}, err
	}
	if err := r.apply(ctx, sts); err != nil {
		return databaseResult{}, err
	}
	if err := r.syncDatabasePod(ctx, instance); err != nil {
		return databaseResult{}, err
	}
	// Growing the volume can be refused (e.g. Longhorn without enough
	// schedulable space); that must not hold back the rest of the package
	// (Odoo resources, quota). Report it and retry on the next reconcile.
	if err := r.growDatabaseVolume(ctx, instance); err != nil {
		r.warnVolumeNotGrown(ctx, instance, err)
	}

	var existing appsv1.StatefulSet
	if err := r.Get(ctx, types.NamespacedName{Namespace: ns, Name: resources.DatabaseStatefulSetName(instance)}, &existing); err != nil {
		if apierrors.IsNotFound(err) {
			return databaseResult{reason: ReasonDatabaseNotReady, message: "PostgreSQL StatefulSet not yet observed"}, nil
		}
		return databaseResult{}, err
	}

	if existing.Status.ReadyReplicas < 1 {
		return databaseResult{reason: ReasonDatabaseNotReady, message: "PostgreSQL StatefulSet has no ready replicas yet"}, nil
	}
	result, err := r.reconcileDatabaseAccess(ctx, instance, resources.DatabaseRolesJobName, true)
	if err != nil || !result.ready {
		return result, err
	}
	if err := r.publishDatabaseOwner(ctx, instance, string(admin.Data["owner-password"])); err != nil {
		return databaseResult{}, err
	}
	// Erase the old exposed password once PostgreSQL has revoked it.
	if string(admin.Data["legacy-user"]) != "veltnex_admin" {
		admin.Data["legacy-user"] = []byte("veltnex_admin")
		admin.Data["legacy-password"] = admin.Data["password"]
		if err := r.Update(ctx, admin); err != nil {
			return databaseResult{}, err
		}
	}
	return result, nil
}

func (r *OdooInstanceReconciler) reconcileCloudNativePGDatabase(ctx context.Context, instance *saasv1alpha1.OdooInstance) (databaseResult, error) {
	ns := resources.TenantNamespace(instance)

	cluster := resources.CloudNativePGCluster(instance, r.platform())
	setOwner(instance, cluster)
	if err := r.apply(ctx, cluster); err != nil {
		return databaseResult{}, fmt.Errorf("applying CloudNativePG Cluster (is the CloudNativePG operator installed?): %w", err)
	}

	var cnpgSecret corev1.Secret
	err := r.Get(ctx, types.NamespacedName{Namespace: ns, Name: resources.CloudNativePGConnectionSecretName(instance)}, &cnpgSecret)
	if apierrors.IsNotFound(err) {
		return databaseResult{reason: ReasonDatabaseNotReady, message: "waiting for CloudNativePG to provision the application connection Secret"}, nil
	}
	if err != nil {
		return databaseResult{}, err
	}

	username := firstNonEmpty(cnpgSecret.Data["username"], cnpgSecret.Data["user"])
	dbname := firstNonEmpty(cnpgSecret.Data["dbname"], cnpgSecret.Data["database"])
	host := cnpgSecret.Data["host"]
	port := firstNonEmptyString(string(cnpgSecret.Data["port"]), "5432")

	if len(username) == 0 || len(dbname) == 0 || len(host) == 0 {
		return databaseResult{reason: ReasonDatabaseNotReady, message: "CloudNativePG connection Secret is missing expected keys"}, nil
	}

	mirrored := resources.DatabaseSecret(instance, resources.DatabaseSecretData{
		Host:     string(host),
		Port:     port,
		DBName:   string(dbname),
		Username: string(username),
		Password: string(cnpgSecret.Data["password"]),
	})
	setOwner(instance, mirrored)
	if err := r.apply(ctx, mirrored); err != nil {
		return databaseResult{}, err
	}

	ready, reason := cnpgClusterReady(cluster, ctx, r.Client, ns, resources.DatabaseStatefulSetName(instance))
	if !ready {
		return databaseResult{reason: ReasonDatabaseNotReady, message: reason}, nil
	}
	return r.auditDatabaseAccess(ctx, instance, mirrored)
}

// cnpgClusterReady re-reads the live Cluster status rather than trusting
// the just-applied desired object (SSA responses do not reliably reflect
// status written by the CloudNativePG controller in the same round trip).
func cnpgClusterReady(desired *unstructured.Unstructured, ctx context.Context, c client.Client, ns, name string) (bool, string) {
	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(desired.GroupVersionKind())
	if err := c.Get(ctx, types.NamespacedName{Namespace: ns, Name: name}, live); err != nil {
		return false, "waiting for CloudNativePG Cluster status"
	}
	phase, found, _ := unstructured.NestedString(live.Object, "status", "phase")
	if !found {
		return false, "CloudNativePG Cluster has no status yet"
	}
	if phase != "Cluster in healthy state" {
		return false, fmt.Sprintf("CloudNativePG Cluster phase: %s", phase)
	}
	return true, ""
}

func (r *OdooInstanceReconciler) reconcileExternalDatabase(ctx context.Context, instance *saasv1alpha1.OdooInstance) (databaseResult, error) {
	ns := resources.TenantNamespace(instance)
	name := instance.Spec.Database.CredentialsSecretRef.Name

	var secret corev1.Secret
	if err := r.Get(ctx, types.NamespacedName{Namespace: ns, Name: name}, &secret); err != nil {
		if apierrors.IsNotFound(err) {
			return databaseResult{
				reason: ReasonDatabaseNotReady,
				message: fmt.Sprintf(
					"external database credentials Secret %q not found in namespace %q; the SaaS platform (or an External Secrets integration) must create it before the instance can become ready",
					name, ns),
			}, nil
		}
		return databaseResult{}, err
	}

	for _, key := range []string{"host", "port", "dbname", "username", "password"} {
		if len(secret.Data[key]) == 0 {
			return databaseResult{
				reason:  ReasonDatabaseNotReady,
				message: fmt.Sprintf("external database credentials Secret %q is missing required key %q", name, key),
			}, nil
		}
	}

	return r.auditDatabaseAccess(ctx, instance, &secret)
}

// existingOrNewPassword reuses a password already stored in `key` of
// Secret name/namespace if the Secret exists, and only generates a fresh
// one when it does not. This is the crux of not rotating customer database
// passwords on every reconcile.
func (r *OdooInstanceReconciler) existingOrNewPassword(ctx context.Context, namespace, name, key string, length int) (string, error) {
	var existing corev1.Secret
	err := r.Get(ctx, types.NamespacedName{Namespace: namespace, Name: name}, &existing)
	if err == nil {
		if v, ok := existing.Data[key]; ok && len(v) > 0 {
			return string(v), nil
		}
	} else if !apierrors.IsNotFound(err) {
		return "", err
	}
	return resources.GeneratePassword(length)
}

func firstNonEmpty(candidates ...[]byte) []byte {
	for _, c := range candidates {
		if len(c) > 0 {
			return c
		}
	}
	return nil
}

func firstNonEmptyString(candidates ...string) string {
	for _, c := range candidates {
		if c != "" {
			return c
		}
	}
	return ""
}

// syncDatabasePod brings the running PostgreSQL pod in line with the
// StatefulSet (which uses OnDelete): resources are resized in place
// through pods/resize, so a package change never restarts the database;
// only an image change deletes the pod for the StatefulSet to recreate.
func (r *OdooInstanceReconciler) syncDatabasePod(ctx context.Context, instance *saasv1alpha1.OdooInstance) error {
	var pod corev1.Pod
	key := types.NamespacedName{Namespace: resources.TenantNamespace(instance), Name: resources.DatabasePodName(instance)}
	if err := r.Get(ctx, key, &pod); err != nil {
		return client.IgnoreNotFound(err)
	}
	if pod.DeletionTimestamp != nil || len(pod.Spec.Containers) == 0 {
		return nil
	}
	desired := resources.DatabaseStatefulSet(instance, r.platform()).Spec.Template.Spec.Containers[0]
	current := pod.Spec.Containers[0]
	if current.Image != desired.Image {
		return client.IgnoreNotFound(r.Delete(ctx, &pod))
	}
	if sameResources(current.Resources, desired.Resources) {
		return nil
	}
	patch := client.MergeFrom(pod.DeepCopy())
	pod.Spec.Containers[0].Resources = desired.Resources
	if err := r.SubResource("resize").Patch(ctx, &pod, patch); err != nil {
		return fmt.Errorf("resizing PostgreSQL pod %s in place: %w", key, err)
	}
	return nil
}

// growDatabaseVolume expands the database PVC when spec.database.storage
// asks for more than it has (online on an expandable StorageClass such as
// Longhorn). Smaller sizes are ignored: volumes never shrink.
func (r *OdooInstanceReconciler) growDatabaseVolume(ctx context.Context, instance *saasv1alpha1.OdooInstance) error {
	var pvc corev1.PersistentVolumeClaim
	key := types.NamespacedName{Namespace: resources.TenantNamespace(instance), Name: resources.DatabaseDataPVCName(instance)}
	if err := r.Get(ctx, key, &pvc); err != nil {
		return client.IgnoreNotFound(err)
	}
	want, err := resources.DatabaseStorageSize(instance)
	if err != nil {
		return err
	}
	have := pvc.Spec.Resources.Requests[corev1.ResourceStorage]
	if want.Cmp(have) <= 0 {
		return nil
	}
	patch := client.MergeFrom(pvc.DeepCopy())
	pvc.Spec.Resources.Requests[corev1.ResourceStorage] = want
	if err := r.Patch(ctx, &pvc, patch); err != nil {
		return fmt.Errorf("growing database volume %s to %s: %w", key, want.String(), err)
	}
	return nil
}

func sameResources(a, b corev1.ResourceRequirements) bool {
	for _, name := range []corev1.ResourceName{corev1.ResourceCPU, corev1.ResourceMemory} {
		for _, pair := range [][2]corev1.ResourceList{{a.Requests, b.Requests}, {a.Limits, b.Limits}} {
			x, xok := pair[0][name]
			y, yok := pair[1][name]
			if xok != yok || (xok && x.Cmp(y) != 0) {
				return false
			}
		}
	}
	return true
}

// warnVolumeNotGrown records a refused volume expansion as a Warning event
// and log line; the reconcile carries on and retries it later.
func (r *OdooInstanceReconciler) warnVolumeNotGrown(ctx context.Context, instance *saasv1alpha1.OdooInstance, err error) {
	log.FromContext(ctx).Error(err, "volume not grown; will retry")
	if r.Recorder != nil {
		r.Recorder.Event(instance, corev1.EventTypeWarning, ReasonVolumeNotGrown, err.Error())
	}
}

// ensureDatabaseAdmin stages both new passwords before any SQL runs. A retry
// after a partially completed migration therefore uses the same credentials.
func (r *OdooInstanceReconciler) ensureDatabaseAdmin(ctx context.Context, instance *saasv1alpha1.OdooInstance) (*corev1.Secret, error) {
	ns := resources.TenantNamespace(instance)
	var admin corev1.Secret
	err := r.Get(ctx, types.NamespacedName{Namespace: ns, Name: resources.DatabaseAdminSecretName(instance)}, &admin)
	if err == nil {
		for _, key := range []string{"username", "password", "owner-password", "legacy-user", "legacy-password"} {
			if len(admin.Data[key]) == 0 {
				return nil, fmt.Errorf("database admin Secret is missing %s", key)
			}
		}
		return &admin, nil
	}
	if !apierrors.IsNotFound(err) {
		return nil, err
	}
	adminPassword, err := resources.GeneratePassword(32)
	if err != nil {
		return nil, err
	}
	ownerPassword, err := resources.GeneratePassword(32)
	if err != nil {
		return nil, err
	}
	legacyUser, legacyPassword := "veltnex_admin", adminPassword
	var app corev1.Secret
	err = r.Get(ctx, types.NamespacedName{Namespace: ns, Name: resources.DatabaseSecretName(instance)}, &app)
	if err == nil {
		legacyUser, legacyPassword = string(app.Data["username"]), string(app.Data["password"])
		if legacyUser == "" || legacyPassword == "" {
			return nil, fmt.Errorf("legacy database Secret is incomplete")
		}
		if legacyUser == "odoo_owner" {
			return nil, fmt.Errorf("database admin Secret missing for an already migrated instance; restore it from backup")
		}
		if legacyUser != "odoo" {
			return nil, fmt.Errorf("unsupported legacy database role: migrate it explicitly before enabling P0.24")
		}
	} else if !apierrors.IsNotFound(err) {
		return nil, err
	}
	admin = corev1.Secret{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "Secret"},
		ObjectMeta: metav1.ObjectMeta{Name: resources.DatabaseAdminSecretName(instance), Namespace: ns, Labels: resources.CommonLabels(instance)},
		Type:       corev1.SecretTypeOpaque,
		Data: map[string][]byte{"username": []byte("veltnex_admin"), "password": []byte(adminPassword),
			"owner-password": []byte(ownerPassword), "legacy-user": []byte(legacyUser), "legacy-password": []byte(legacyPassword)},
	}
	setOwner(instance, &admin)
	if err := r.Create(ctx, &admin); err != nil {
		return nil, err
	}
	return &admin, nil
}

func (r *OdooInstanceReconciler) publishDatabaseOwner(ctx context.Context, instance *saasv1alpha1.OdooInstance, password string) error {
	secret := resources.DatabaseSecret(instance, resources.DatabaseSecretData{
		Host: resources.DatabaseFQDN(instance), Port: "5432", DBName: resources.OdooDatabaseName(instance),
		Username: "odoo_owner", Password: password,
	})
	setOwner(instance, secret)
	return r.apply(ctx, secret)
}

func (r *OdooInstanceReconciler) reconcileDatabaseAccess(ctx context.Context, instance *saasv1alpha1.OdooInstance, name string, managed bool) (databaseResult, error) {
	var job batchv1.Job
	err := r.Get(ctx, types.NamespacedName{Namespace: resources.TenantNamespace(instance), Name: name}, &job)
	if apierrors.IsNotFound(err) {
		desired := resources.DatabaseAccessJob(instance, r.platform(), name, managed)
		setOwner(instance, desired)
		if err := r.Create(ctx, desired); err != nil {
			return databaseResult{}, err
		}
		return databaseResult{reason: "DatabaseAccessPending", message: "waiting for database role setup and privilege checks"}, nil
	}
	if err != nil {
		return databaseResult{}, err
	}
	if job.Status.Succeeded > 0 {
		return databaseResult{ready: true, reason: ReasonDatabaseReady, message: "database application privileges verified"}, nil
	}
	for _, condition := range job.Status.Conditions {
		if condition.Type == batchv1.JobFailed && condition.Status == corev1.ConditionTrue {
			return databaseResult{reason: "DatabaseAccessFailed", message: "database privilege setup/check failed; inspect platform Job logs"}, nil
		}
	}
	return databaseResult{reason: "DatabaseAccessPending", message: "waiting for database role setup and privilege checks"}, nil
}

func (r *OdooInstanceReconciler) auditDatabaseAccess(ctx context.Context, instance *saasv1alpha1.OdooInstance, secret *corev1.Secret) (databaseResult, error) {
	// Recheck when connection credentials change; never put secrets in names.
	name := "odoo-database-audit-" + databaseCredentialsDigest(secret)[:16]
	return r.reconcileDatabaseAccess(ctx, instance, name, false)
}

func databaseCredentialsDigest(secret *corev1.Secret) string {
	hash := sha256.New()
	for _, key := range []string{"host", "port", "dbname", "username", "password"} {
		value := secret.Data[key]
		if value == nil {
			value = []byte(secret.StringData[key])
		}
		hash.Write(value)
		hash.Write([]byte{0})
	}
	return hex.EncodeToString(hash.Sum(nil))
}
