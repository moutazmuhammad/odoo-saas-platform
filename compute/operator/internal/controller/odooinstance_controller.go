// Package controller implements the OdooInstance reconciliation loop: the
// only place in this codebase that talks to the Kubernetes API to bring
// child resources in line with an OdooInstance's spec. See
// internal/resources for the pure desired-state builders this file diffs
// against live cluster state via Server-Side Apply.
package controller

import (
	"context"
	"fmt"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/log"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
	"github.com/freightright/odoo-saas-platform/operator/internal/resources"
)

// OdooInstanceReconciler reconciles an OdooInstance object. Every field
// below except client.Client/Scheme/Recorder is platform-wide operator
// configuration (set once at operator startup via cmd/main.go flags), never
// per-tenant: per-tenant configuration lives exclusively in
// OdooInstance.Spec.
type OdooInstanceReconciler struct {
	client.Client
	Scheme   *runtime.Scheme
	Recorder record.EventRecorder

	// NetworkingProvider selects how external traffic reaches tenant
	// instances: NetworkingProviderGatewayAPI (default, preferred) or
	// NetworkingProviderIngress.
	NetworkingProvider string
	// GatewayNamespace/GatewayName identify the shared platform Gateway
	// every HTTPRoute is parented to.
	GatewayNamespace string
	GatewayName      string
	// GatewaySelector labels the Gateway's own pods, used to scope the
	// tenant NetworkPolicy's ingress-allow rule as tightly as possible.
	GatewaySelector map[string]string
	// IngressClassName is used only when NetworkingProvider is "ingress".
	IngressClassName *string

	// BackupToolImage overrides resources.DefaultBackupToolImage.
	BackupToolImage string
	// RestoreToolImage overrides resources.DefaultRestoreToolImage.
	RestoreToolImage string

	// SupportedOdooVersions restricts spec.version to a known-good list.
	// Empty means "no restriction" (development convenience only).
	SupportedOdooVersions []string
	// AllowMutableTags disables the immutable-tag validation; intended for
	// local/dev clusters only.
	AllowMutableTags bool
}

// +kubebuilder:rbac:groups=saas.odoo.example.com,resources=odooinstances,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=saas.odoo.example.com,resources=odooinstances/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=saas.odoo.example.com,resources=odooinstances/finalizers,verbs=update
// +kubebuilder:rbac:groups=core,resources=namespaces,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=core,resources=resourcequotas;limitranges;serviceaccounts;configmaps;services,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=core,resources=secrets,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=core,resources=persistentvolumeclaims,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=core,resources=pods,verbs=get;list;watch
// +kubebuilder:rbac:groups=core,resources=events,verbs=create;patch
// +kubebuilder:rbac:groups=apps,resources=deployments;statefulsets,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=batch,resources=cronjobs;jobs,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=policy,resources=poddisruptionbudgets,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=networking.k8s.io,resources=networkpolicies;ingresses,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=gateway.networking.k8s.io,resources=httproutes,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=cert-manager.io,resources=certificates,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=postgresql.cnpg.io,resources=clusters,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=coordination.k8s.io,resources=leases,verbs=get;list;watch;create;update;patch;delete

// Reconcile implements the single reconciliation loop for OdooInstance. It
// is idempotent: every child resource is (re)computed from instance.Spec on
// every call and Server-Side-Applied, so running it once or a thousand
// times converges to the same state, and a deleted/drifted child resource
// is recreated/corrected on the very next reconcile without any
// first-time-only branch.
func (r *OdooInstanceReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx)
	start := time.Now()

	var instance saasv1alpha1.OdooInstance
	if err := r.Get(ctx, req.NamespacedName, &instance); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}
	logger = logger.WithValues("odooinstance", instance.Name)
	logger.V(1).Info("reconciling")

	reconcileTotal.WithLabelValues(instance.Name).Inc()
	defer func() {
		reconcileDuration.WithLabelValues(instance.Name).Observe(time.Since(start).Seconds())
	}()

	// Deletion path.
	if !instance.DeletionTimestamp.IsZero() {
		return r.reconcileDelete(ctx, &instance)
	}

	// Ensure the finalizer is present before creating any external state,
	// so a crash between "created a child resource" and "finalizer set"
	// can never leave the instance undeletable.
	if !controllerutil.ContainsFinalizer(&instance, saasv1alpha1.Finalizer) {
		// Patch (not a whole-object Update): a full-object Update
		// round-trips every field through this controller's Go struct,
		// and any field combining a non-zero CRD default with `omitempty`
		// would have its Go zero value silently dropped from the request
		// and then re-defaulted server-side, clobbering a customer's
		// explicit choice. Patching only metadata.finalizers avoids that
		// entire class of bug for this write, whatever else the object
		// contains now or gains later.
		original := instance.DeepCopy()
		controllerutil.AddFinalizer(&instance, saasv1alpha1.Finalizer)
		if err := r.Patch(ctx, &instance, client.MergeFrom(original)); err != nil {
			return ctrl.Result{}, fmt.Errorf("adding finalizer: %w", err)
		}
		return ctrl.Result{Requeue: true}, nil
	}

	if verr := r.validateSpec(&instance); verr != nil {
		reconcileErrors.WithLabelValues(instance.Name, verr.reason).Inc()
		setCondition(&instance, saasv1alpha1.ConditionReady, metav1.ConditionFalse, verr.reason, verr.message)
		setCondition(&instance, saasv1alpha1.ConditionDegraded, metav1.ConditionTrue, verr.reason, verr.message)
		setCondition(&instance, saasv1alpha1.ConditionProgressing, metav1.ConditionFalse, verr.reason, "waiting for spec to be corrected")
		r.Recorder.Event(&instance, corev1.EventTypeWarning, verr.reason, verr.message)
		return r.updateStatusAndReturn(ctx, &instance, ctrl.Result{}, nil)
	}

	if instance.Spec.Suspended {
		return r.reconcileSuspended(ctx, &instance)
	}

	setCondition(&instance, saasv1alpha1.ConditionProgressing, metav1.ConditionTrue, ReasonReconciling, "reconciling desired state")
	// Reset Degraded at the start of every pass rather than only at full
	// readiness: a transient problem (e.g. a since-fixed InitJobFailed)
	// must stop being reported once its specific cause clears, not linger
	// until every other dependency also happens to become ready. Anything
	// still actually wrong re-sets this to True further down this same
	// pass (handleReconcileError, InitJobFailed).
	setCondition(&instance, saasv1alpha1.ConditionDegraded, metav1.ConditionFalse, ReasonReconciling, "")

	if err := r.reconcileTenancy(ctx, &instance); err != nil {
		return r.handleReconcileError(ctx, &instance, "TenancyReconcileFailed", err)
	}

	dbResult, err := r.reconcileDatabase(ctx, &instance)
	if err != nil {
		return r.handleReconcileError(ctx, &instance, "DatabaseReconcileFailed", err)
	}
	if dbResult.ready {
		setCondition(&instance, saasv1alpha1.ConditionDatabaseReady, metav1.ConditionTrue, dbResult.reason, dbResult.message)
	} else {
		setCondition(&instance, saasv1alpha1.ConditionDatabaseReady, metav1.ConditionFalse, dbResult.reason, dbResult.message)
	}

	storageReady, err := r.reconcileStorage(ctx, &instance)
	if err != nil {
		return r.handleReconcileError(ctx, &instance, "StorageReconcileFailed", err)
	}
	if storageReady {
		setCondition(&instance, saasv1alpha1.ConditionStorageReady, metav1.ConditionTrue, ReasonStorageBound, "filestore PVC is bound")
	} else {
		setCondition(&instance, saasv1alpha1.ConditionStorageReady, metav1.ConditionFalse, ReasonStorageNotBound, "waiting for filestore PVC to bind")
	}

	if err := r.reconcileAdminSecret(ctx, &instance); err != nil {
		return r.handleReconcileError(ctx, &instance, "SecretReconcileFailed", err)
	}

	// serving is what the live pods are rendered from: the instance itself,
	// or — while a spec.update is pending — a copy pinned to the image the
	// pods already run (see servingInstance). Status is always written to
	// instance.
	serving, held, err := r.servingInstance(ctx, &instance)
	if err != nil {
		return r.handleReconcileError(ctx, &instance, "UpdateReconcileFailed", err)
	}
	if err := r.reconcileConfig(ctx, serving); err != nil {
		return r.handleReconcileError(ctx, &instance, "ConfigReconcileFailed", err)
	}

	var (
		workloadReady                bool
		readyReplicas, totalReplicas int32
		observedImage                string
	)
	switch {
	case !dbResult.ready:
		// Deliberately NOT also gated on storageReady: most production
		// StorageClasses use VolumeBindingMode=WaitForFirstConsumer, so the
		// filestore PVC only transitions to Bound once some pod actually
		// references it and gets scheduled. The init Job below is that
		// first pod; requiring the PVC to already be Bound before running
		// it would deadlock forever. StorageReady remains a genuine
		// dependency in spirit — the init Job's pod will simply sit
		// Pending until the volume binds/provisions — it just cannot be a
		// precondition to *creating* that pod.
		setCondition(&instance, saasv1alpha1.ConditionWorkloadReady, metav1.ConditionFalse, ReasonWorkloadNotReady,
			"waiting for the database to become ready before initializing the schema")
	default:
		// spec.restore, when set, replaces the ordinary odoo-init Job with
		// a one-time restore-from-backup Job as the thing gating the web
		// Deployment; the two are mutually exclusive (see schemaGateResult's
		// doc comment in init_job.go). Everything downstream of this point
		// treats the two identically.
		var (
			gateResult schemaGateResult
			gateErr    error
		)
		if instance.Spec.Restore != nil {
			gateResult, gateErr = r.reconcileRestore(ctx, &instance)
			if gateErr != nil {
				return r.handleReconcileError(ctx, &instance, "RestoreReconcileFailed", gateErr)
			}
			restoreStatus := metav1.ConditionFalse
			if gateResult.succeeded {
				restoreStatus = metav1.ConditionTrue
			}
			setCondition(&instance, saasv1alpha1.ConditionRestoreReady, restoreStatus, gateResult.reason, gateResult.message)
		} else {
			gateResult, gateErr = r.reconcileInitJob(ctx, &instance)
			if gateErr != nil {
				return r.handleReconcileError(ctx, &instance, "InitJobReconcileFailed", gateErr)
			}
		}
		switch {
		case gateResult.succeeded:
			if held {
				upd, err := r.reconcileUpdateJob(ctx, &instance)
				if err != nil {
					return r.handleReconcileError(ctx, &instance, "UpdateReconcileFailed", err)
				}
				switch {
				case upd.succeeded:
					instance.Status.AppliedUpdateToken = instance.Spec.Update.Token
					setCondition(&instance, saasv1alpha1.ConditionUpdateReady, metav1.ConditionTrue, ReasonUpdateApplied, upd.message)
					r.Recorder.Eventf(&instance, corev1.EventTypeNormal, ReasonUpdateApplied, "%s; rolling out the new image", upd.message)
					serving, held = &instance, false
					if err := r.reconcileConfig(ctx, serving); err != nil {
						return r.handleReconcileError(ctx, &instance, "ConfigReconcileFailed", err)
					}
				case upd.failed:
					setCondition(&instance, saasv1alpha1.ConditionUpdateReady, metav1.ConditionFalse, ReasonUpdateFailed,
						upd.message+"; the previous image keeps serving")
					r.Recorder.Eventf(&instance, corev1.EventTypeWarning, ReasonUpdateFailed, "%s", upd.message)
				default:
					setCondition(&instance, saasv1alpha1.ConditionUpdateReady, metav1.ConditionFalse, ReasonUpdateRunning, upd.message)
				}
			} else if instance.Spec.Update != nil && instance.Spec.Update.Token == instance.Status.AppliedUpdateToken {
				setCondition(&instance, saasv1alpha1.ConditionUpdateReady, metav1.ConditionTrue, ReasonUpdateApplied, "update applied")
			}
			if err := r.reconcileWorkload(ctx, serving); err != nil {
				return r.handleReconcileError(ctx, &instance, "WorkloadReconcileFailed", err)
			}
			workloadReady, readyReplicas, totalReplicas, observedImage, err = r.workloadStatus(ctx, serving)
			if err != nil {
				return r.handleReconcileError(ctx, &instance, "WorkloadStatusFailed", err)
			}
			if workloadReady {
				setCondition(&instance, saasv1alpha1.ConditionWorkloadReady, metav1.ConditionTrue, ReasonWorkloadReady, "Odoo workload is ready")
			} else {
				setCondition(&instance, saasv1alpha1.ConditionWorkloadReady, metav1.ConditionFalse, ReasonWorkloadNotReady, "waiting for Odoo pods to become ready")
			}
		case gateResult.failed:
			setCondition(&instance, saasv1alpha1.ConditionWorkloadReady, metav1.ConditionFalse, gateResult.reason, gateResult.message)
			setCondition(&instance, saasv1alpha1.ConditionDegraded, metav1.ConditionTrue, gateResult.reason, gateResult.message)
			r.Recorder.Eventf(&instance, corev1.EventTypeWarning, gateResult.reason, "%s", gateResult.message)
		default:
			setCondition(&instance, saasv1alpha1.ConditionWorkloadReady, metav1.ConditionFalse, gateResult.reason, gateResult.message)
		}
	}
	instance.Status.Replicas = totalReplicas
	instance.Status.ReadyReplicas = readyReplicas
	instance.Status.ObservedImage = observedImage

	if err := r.reconcilePDB(ctx, &instance); err != nil {
		return r.handleReconcileError(ctx, &instance, "PDBReconcileFailed", err)
	}

	routeReady, routeMsg, err := r.reconcileNetworking(ctx, &instance)
	if err != nil {
		return r.handleReconcileError(ctx, &instance, "NetworkingReconcileFailed", err)
	}
	if routeReady {
		setCondition(&instance, saasv1alpha1.ConditionRouteReady, metav1.ConditionTrue, ReasonRouteReady, "route is accepted")
		instance.Status.URL = instanceURL(&instance)
	} else {
		setCondition(&instance, saasv1alpha1.ConditionRouteReady, metav1.ConditionFalse, ReasonRouteNotReady, routeMsg)
	}

	if err := r.reconcileBackup(ctx, &instance); err != nil {
		return r.handleReconcileError(ctx, &instance, "BackupReconcileFailed", err)
	}

	instance.Status.TenantNamespace = resources.TenantNamespace(&instance)
	instance.Status.AdminCredentialsSecretName = resources.AdminSecretName(&instance)
	instance.Status.DatabaseCredentialsSecretName = resources.DatabaseSecretName(&instance)

	overallReady := dbResult.ready && storageReady && workloadReady && routeReady
	if overallReady {
		setCondition(&instance, saasv1alpha1.ConditionReady, metav1.ConditionTrue, ReasonReconcileSucceeded, "instance is fully reconciled and serving traffic")
		setCondition(&instance, saasv1alpha1.ConditionProgressing, metav1.ConditionFalse, ReasonReconcileSucceeded, "no further action needed")
		setCondition(&instance, saasv1alpha1.ConditionDegraded, metav1.ConditionFalse, ReasonReconcileSucceeded, "")
	} else {
		setCondition(&instance, saasv1alpha1.ConditionReady, metav1.ConditionFalse, ReasonWorkloadNotReady, "one or more dependencies are not yet ready")
	}

	requeue := 2 * time.Minute
	if held {
		requeue = 15 * time.Second
	}
	return r.updateStatusAndReturn(ctx, &instance, ctrl.Result{RequeueAfter: requeue}, nil)
}

func (r *OdooInstanceReconciler) reconcileDelete(ctx context.Context, instance *saasv1alpha1.OdooInstance) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(instance, saasv1alpha1.Finalizer) {
		return ctrl.Result{}, nil
	}
	if err := r.finalizeInstance(ctx, instance); err != nil {
		r.Recorder.Eventf(instance, corev1.EventTypeWarning, "FinalizationFailed", "%v", err)
		return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
	}
	// Patch, not Update: see the AddFinalizer comment above.
	original := instance.DeepCopy()
	controllerutil.RemoveFinalizer(instance, saasv1alpha1.Finalizer)
	if err := r.Patch(ctx, instance, client.MergeFrom(original)); err != nil {
		return ctrl.Result{}, fmt.Errorf("removing finalizer: %w", err)
	}
	return ctrl.Result{}, nil
}

func (r *OdooInstanceReconciler) reconcileSuspended(ctx context.Context, instance *saasv1alpha1.OdooInstance) (ctrl.Result, error) {
	ns := resources.TenantNamespace(instance)
	for _, name := range []string{resources.OdooDeploymentName(instance), resources.OdooCronDeploymentName(instance)} {
		var dep appsv1.Deployment
		err := r.Get(ctx, types.NamespacedName{Namespace: ns, Name: name}, &dep)
		if apierrors.IsNotFound(err) {
			continue
		}
		if err != nil {
			return ctrl.Result{}, err
		}
		if ptr.Deref(dep.Spec.Replicas, 0) != 0 {
			dep.Spec.Replicas = ptr.To(int32(0))
			if err := r.Update(ctx, &dep); err != nil {
				return ctrl.Result{}, err
			}
		}
	}
	setCondition(instance, saasv1alpha1.ConditionReady, metav1.ConditionFalse, ReasonSuspended, "instance is suspended")
	setCondition(instance, saasv1alpha1.ConditionProgressing, metav1.ConditionFalse, ReasonSuspended, "")
	instance.Status.URL = ""
	return r.updateStatusAndReturn(ctx, instance, ctrl.Result{}, nil)
}

func (r *OdooInstanceReconciler) handleReconcileError(ctx context.Context, instance *saasv1alpha1.OdooInstance, reason string, err error) (ctrl.Result, error) {
	reconcileErrors.WithLabelValues(instance.Name, reason).Inc()
	setCondition(instance, saasv1alpha1.ConditionDegraded, metav1.ConditionTrue, reason, err.Error())
	r.Recorder.Eventf(instance, corev1.EventTypeWarning, reason, "%v", err)
	// Deliberately still write status (best-effort) before returning the
	// error, so the SaaS API observes Degraded immediately rather than
	// only after exponential backoff produces a subsequent successful
	// status write.
	_, _ = r.updateStatusAndReturn(ctx, instance, ctrl.Result{}, nil)
	return ctrl.Result{}, err
}

func (r *OdooInstanceReconciler) updateStatusAndReturn(ctx context.Context, instance *saasv1alpha1.OdooInstance, result ctrl.Result, retErr error) (ctrl.Result, error) {
	instance.Status.ObservedGeneration = instance.Generation
	instance.Status.Phase = computePhase(instance)
	if err := r.Status().Update(ctx, instance); err != nil {
		if apierrors.IsConflict(err) {
			return ctrl.Result{Requeue: true}, nil
		}
		return ctrl.Result{}, fmt.Errorf("updating status: %w", err)
	}
	return result, retErr
}

func instanceURL(instance *saasv1alpha1.OdooInstance) string {
	scheme := "http"
	if instance.Spec.Domain.TLS.Enabled {
		scheme = "https"
	}
	return fmt.Sprintf("%s://%s", scheme, instance.Spec.Domain.Hostname)
}

// SetupWithManager wires the controller into the manager, watching
// OdooInstance plus every namespaced child kind it owns so an
// out-of-band edit or deletion of a child resource triggers a prompt
// re-reconcile instead of waiting for the next periodic RequeueAfter.
func (r *OdooInstanceReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&saasv1alpha1.OdooInstance{}).
		Owns(&appsv1.Deployment{}).
		Owns(&appsv1.StatefulSet{}).
		Owns(&corev1.Service{}).
		Owns(&corev1.Secret{}).
		Owns(&corev1.ConfigMap{}).
		Owns(&corev1.PersistentVolumeClaim{}).
		Owns(&batchv1.Job{}).
		Complete(r)
}
