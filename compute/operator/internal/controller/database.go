package controller

import (
	"context"
	"fmt"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

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
// this is the abstraction boundary described in docs/architecture.md
// ("Database Architecture") that lets the platform swap Managed ->
// CloudNativePG -> External without ever touching the OdooInstance API.
func (r *OdooInstanceReconciler) reconcileDatabase(ctx context.Context, instance *saasv1alpha1.OdooInstance) (databaseResult, error) {
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

	password, err := r.existingOrNewPassword(ctx, ns, resources.DatabaseSecretName(instance), "password", 24)
	if err != nil {
		return databaseResult{}, err
	}

	secret := resources.DatabaseSecret(instance, resources.DatabaseSecretData{
		Host:     resources.DatabaseFQDN(instance),
		Port:     "5432",
		DBName:   resources.OdooDatabaseName(instance),
		Username: "odoo",
		Password: password,
	})
	setOwner(instance, secret)
	if err := r.apply(ctx, secret); err != nil {
		return databaseResult{}, err
	}

	svc := resources.DatabaseService(instance)
	setOwner(instance, svc)
	if err := r.apply(ctx, svc); err != nil {
		return databaseResult{}, err
	}

	sts := resources.DatabaseStatefulSet(instance)
	setOwner(instance, sts)
	if err := r.apply(ctx, sts); err != nil {
		return databaseResult{}, err
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
	return databaseResult{ready: true, reason: ReasonDatabaseReady, message: "Managed PostgreSQL is ready"}, nil
}

func (r *OdooInstanceReconciler) reconcileCloudNativePGDatabase(ctx context.Context, instance *saasv1alpha1.OdooInstance) (databaseResult, error) {
	ns := resources.TenantNamespace(instance)

	cluster := resources.CloudNativePGCluster(instance)
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
	return databaseResult{ready: true, reason: ReasonDatabaseReady, message: "CloudNativePG cluster is ready"}, nil
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

	// Note: the controller intentionally does not attempt an actual
	// PostgreSQL connection here; it does not carry a database driver
	// dependency by design (least privilege / minimal attack surface for a
	// cluster-privileged controller). Connectivity problems surface once
	// the Odoo pod itself fails its startup probe (WorkloadReady=False).
	return databaseResult{ready: true, reason: ReasonDatabaseReady, message: "external database credentials Secret present"}, nil
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
