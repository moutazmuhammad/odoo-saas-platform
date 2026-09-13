package controller

import (
	"context"
	"fmt"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

// fieldOwner is the Server-Side Apply field manager identity for this
// controller. Using SSA (rather than Get-then-CompareAndUpdate) is what
// makes reconciliation idempotent by construction per the platform
// requirement: running it once or a thousand times converges to the same
// state, and drift in any field this controller owns is corrected on the
// very next reconcile without bespoke diffing logic per resource kind.
const fieldOwner = "odoo-instance-controller"

// apply Server-Side-Applies a single desired child object. obj must already
// have GroupVersionKind set (every builder in internal/resources does
// this) and Name/Namespace populated. Ownership is set by the caller
// before apply so garbage collection and `kubectl get -o yaml` ownership
// are visible even though SSA itself does not require an owner reference.
func (r *OdooInstanceReconciler) apply(ctx context.Context, obj client.Object) error {
	if err := r.Client.Patch(ctx, obj, client.Apply, client.FieldOwner(fieldOwner), client.ForceOwnership); err != nil {
		return fmt.Errorf("applying %s %s/%s: %w", obj.GetObjectKind().GroupVersionKind().Kind, obj.GetNamespace(), obj.GetName(), err)
	}
	return nil
}

// setOwner sets instance as the owner of a namespaced child resource. Since
// OdooInstance is cluster-scoped, this is a valid owner reference
// (Kubernetes permits a namespaced object to be owned by a cluster-scoped
// one) and gives tooling/`kubectl get --show-labels` a direct ownership
// trail even though the primary deletion path is the tenant namespace
// teardown driven by the finalizer, not garbage collection. Controller
// (blockOwnerDeletion) is intentionally NOT set to true: it would prevent
// deleting the OdooInstance object itself before the namespace teardown
// finalizer step has run on some Kubernetes versions' foreground-deletion
// semantics, which would fight the explicit finalizer-driven ordering this
// controller relies on.
func setOwner(instance *saasv1alpha1.OdooInstance, obj client.Object) {
	gvk := saasv1alpha1.GroupVersion.WithKind("OdooInstance")
	obj.SetOwnerReferences([]metav1.OwnerReference{
		{
			APIVersion:         gvk.GroupVersion().String(),
			Kind:               gvk.Kind,
			Name:               instance.Name,
			UID:                instance.UID,
			Controller:         ptr.To(true),
			BlockOwnerDeletion: ptr.To(false),
		},
	})
}
