package controller

import (
	"context"
	"fmt"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
	"github.com/freightright/odoo-saas-platform/operator/internal/resources"
)

// NetworkingProviderGatewayAPI and NetworkingProviderIngress are the
// operator-wide (not per-tenant) routing strategies. This is a platform
// decision, not a tenant one: every instance on a given operator
// installation is routed the same way, through the same shared entry
// point, which is what lets the platform scale from 1 to 1000+ tenants
// without a per-tenant LoadBalancer. See docs/architecture.md
// ("Networking").
const (
	NetworkingProviderGatewayAPI = "gateway-api"
	NetworkingProviderIngress    = "ingress"
)

// reconcileNetworking creates the routing resource (HTTPRoute or Ingress),
// the cert-manager Certificate when requested, and the tenant
// NetworkPolicy. It returns whether the route is ready to receive traffic.
func (r *OdooInstanceReconciler) reconcileNetworking(ctx context.Context, instance *saasv1alpha1.OdooInstance) (bool, string, error) {
	if instance.Spec.Networking.NetworkPolicy.Enabled {
		np := resources.TenantNetworkPolicy(instance, r.GatewayNamespace, r.GatewaySelector)
		setOwner(instance, np)
		if err := r.apply(ctx, np); err != nil {
			return false, "", err
		}
	}

	tls := instance.Spec.Domain.TLS
	if tls.Enabled && tls.SecretName == "" && tls.IssuerRef != nil {
		cert := resources.Certificate(instance)
		setOwner(instance, cert)
		if err := r.apply(ctx, cert); err != nil {
			return false, "", fmt.Errorf("applying cert-manager Certificate (is cert-manager installed?): %w", err)
		}
		if ready, msg := r.certificateReady(ctx, instance); !ready {
			return false, msg, nil
		}
	} else if tls.Enabled && tls.SecretName == "" {
		return false, "spec.domain.tls.enabled is true but neither secretName nor issuerRef is set", nil
	}

	switch r.NetworkingProvider {
	case NetworkingProviderIngress:
		ing := resources.Ingress(instance, r.IngressClassName)
		setOwner(instance, ing)
		if err := r.apply(ctx, ing); err != nil {
			return false, "", err
		}
		// Re-read live status: the object we just Server-Side-Applied only
		// reflects our own desired fields, not status written back by the
		// Ingress controller in the same round trip.
		if !r.ingressHasLoadBalancer(ctx, instance) {
			return false, "waiting for the Ingress controller to assign a load balancer address", nil
		}
		return true, "", nil
	default: // NetworkingProviderGatewayAPI
		route := resources.HTTPRoute(instance, r.GatewayNamespace, r.GatewayName)
		setOwner(instance, route)
		if err := r.apply(ctx, route); err != nil {
			return false, fmt.Sprintf("applying HTTPRoute (is the Gateway API installed?): %v", err), nil
		}
		ready, msg := r.httpRouteAccepted(ctx, instance)
		return ready, msg, nil
	}
}

func (r *OdooInstanceReconciler) ingressHasLoadBalancer(ctx context.Context, instance *saasv1alpha1.OdooInstance) bool {
	u := &unstructured.Unstructured{}
	u.SetAPIVersion("networking.k8s.io/v1")
	u.SetKind("Ingress")
	if err := r.Get(ctx, types.NamespacedName{Namespace: resources.TenantNamespace(instance), Name: resources.IngressName(instance)}, u); err != nil {
		return false
	}
	entries, found, _ := unstructured.NestedSlice(u.Object, "status", "loadBalancer", "ingress")
	return found && len(entries) > 0
}

// httpRouteAccepted checks the HTTPRoute's "Accepted" condition, written by
// the Gateway API implementation controller, not by us.
func (r *OdooInstanceReconciler) httpRouteAccepted(ctx context.Context, instance *saasv1alpha1.OdooInstance) (bool, string) {
	u := &unstructured.Unstructured{}
	u.SetAPIVersion("gateway.networking.k8s.io/v1")
	u.SetKind("HTTPRoute")
	if err := r.Get(ctx, types.NamespacedName{Namespace: resources.TenantNamespace(instance), Name: resources.HTTPRouteName(instance)}, u); err != nil {
		if apierrors.IsNotFound(err) {
			return false, "HTTPRoute not yet observed"
		}
		return false, err.Error()
	}
	parents, found, _ := unstructured.NestedSlice(u.Object, "status", "parents")
	if !found || len(parents) == 0 {
		return false, "waiting for the Gateway to acknowledge the HTTPRoute"
	}
	for _, p := range parents {
		parent, ok := p.(map[string]interface{})
		if !ok {
			continue
		}
		conditions, _, _ := unstructured.NestedSlice(parent, "conditions")
		for _, c := range conditions {
			cond, ok := c.(map[string]interface{})
			if !ok {
				continue
			}
			if cond["type"] == "Accepted" && cond["status"] == "True" {
				return true, ""
			}
		}
	}
	return false, "Gateway has not yet accepted the HTTPRoute"
}

func (r *OdooInstanceReconciler) certificateReady(ctx context.Context, instance *saasv1alpha1.OdooInstance) (bool, string) {
	u := &unstructured.Unstructured{}
	u.SetAPIVersion("cert-manager.io/v1")
	u.SetKind("Certificate")
	if err := r.Get(ctx, types.NamespacedName{Namespace: resources.TenantNamespace(instance), Name: resources.CertificateName(instance)}, u); err != nil {
		return false, "waiting for cert-manager Certificate to be observed"
	}
	conditions, _, _ := unstructured.NestedSlice(u.Object, "status", "conditions")
	for _, c := range conditions {
		cond, ok := c.(map[string]interface{})
		if !ok {
			continue
		}
		if cond["type"] == "Ready" && cond["status"] == "True" {
			return true, ""
		}
	}
	return false, "waiting for cert-manager to issue the TLS certificate"
}
