package controller

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

// setCondition upserts a condition on instance.Status.Conditions, following
// the standard Kubernetes conditions convention: LastTransitionTime only
// changes when Status flips, ObservedGeneration always tracks the
// generation being reconciled.
func setCondition(instance *saasv1alpha1.OdooInstance, conditionType string, status metav1.ConditionStatus, reason, message string) {
	now := metav1.Now()
	for i := range instance.Status.Conditions {
		c := &instance.Status.Conditions[i]
		if c.Type != conditionType {
			continue
		}
		if c.Status != status {
			c.LastTransitionTime = now
		}
		c.Status = status
		c.Reason = reason
		c.Message = message
		c.ObservedGeneration = instance.Generation
		return
	}
	instance.Status.Conditions = append(instance.Status.Conditions, metav1.Condition{
		Type:               conditionType,
		Status:             status,
		Reason:             reason,
		Message:            message,
		LastTransitionTime: now,
		ObservedGeneration: instance.Generation,
	})
}

func conditionStatus(instance *saasv1alpha1.OdooInstance, conditionType string) metav1.ConditionStatus {
	for _, c := range instance.Status.Conditions {
		if c.Type == conditionType {
			return c.Status
		}
	}
	return metav1.ConditionUnknown
}

// computePhase derives the coarse OdooInstancePhase from the detailed
// Conditions, so the SaaS API only needs to read a single field for the
// common case while Conditions remain available for deeper diagnostics.
func computePhase(instance *saasv1alpha1.OdooInstance) saasv1alpha1.OdooInstancePhase {
	if instance.Spec.Suspended {
		return saasv1alpha1.PhaseSuspended
	}
	if !instance.DeletionTimestamp.IsZero() {
		return saasv1alpha1.PhaseDeleting
	}
	if conditionStatus(instance, saasv1alpha1.ConditionReady) == metav1.ConditionTrue {
		return saasv1alpha1.PhaseReady
	}
	if conditionStatus(instance, saasv1alpha1.ConditionDegraded) == metav1.ConditionTrue {
		return saasv1alpha1.PhaseDegraded
	}
	if conditionStatus(instance, saasv1alpha1.ConditionProgressing) == metav1.ConditionTrue {
		if instance.Status.ObservedGeneration > 0 && instance.Status.ObservedGeneration < instance.Generation {
			return saasv1alpha1.PhaseUpdating
		}
		return saasv1alpha1.PhaseProvisioning
	}
	return saasv1alpha1.PhasePending
}

const (
	ReasonReconciling        = "Reconciling"
	ReasonReconcileSucceeded = "ReconciliationSucceeded"
	ReasonReconcileError     = "ReconciliationError"
	ReasonInvalidSpec        = "InvalidSpec"
	ReasonDatabaseNotReady   = "DatabaseNotReady"
	ReasonDatabaseReady      = "DatabaseReady"
	ReasonStorageNotBound    = "StorageNotBound"
	ReasonStorageBound       = "StorageBound"
	ReasonRouteNotReady      = "RouteNotReady"
	ReasonRouteReady         = "RouteReady"
	ReasonWorkloadNotReady   = "WorkloadNotReady"
	ReasonWorkloadReady      = "WorkloadReady"
	ReasonSuspended          = "Suspended"
	ReasonDeleting           = "Deleting"
)
