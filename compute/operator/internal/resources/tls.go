package resources

import (
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

// certManagerGVK avoids taking a compile-time dependency on
// github.com/cert-manager/cert-manager's Go types (which pulls in a large,
// independently-versioned module graph) purely to construct one resource
// kind. The controller creates this via the dynamic/unstructured client and
// treats cert-manager as an optional, swappable integration: instances that
// set Domain.TLS.SecretName directly never need this resource at all, and
// clusters without cert-manager installed simply never receive it.
const (
	certManagerAPIVersion = "cert-manager.io/v1"
	certManagerKind       = "Certificate"
)

// Certificate builds a cert-manager Certificate requesting a TLS
// certificate for instance.Spec.Domain.Hostname, issued by IssuerRef, into
// a Secret this instance's Ingress/HTTPRoute can reference.
func Certificate(instance *saasv1alpha1.OdooInstance) *unstructured.Unstructured {
	issuerRef := instance.Spec.Domain.TLS.IssuerRef
	u := &unstructured.Unstructured{}
	u.SetAPIVersion(certManagerAPIVersion)
	u.SetKind(certManagerKind)
	u.SetName(CertificateName(instance))
	u.SetNamespace(TenantNamespace(instance))
	u.SetLabels(CommonLabels(instance))

	spec := map[string]interface{}{
		"secretName": tlsSecretName(instance),
		"dnsNames":   []interface{}{instance.Spec.Domain.Hostname},
		"issuerRef": map[string]interface{}{
			"name": issuerRef.Name,
			"kind": issuerRef.Kind,
		},
	}
	if issuerRef.Group != "" {
		spec["issuerRef"].(map[string]interface{})["group"] = issuerRef.Group
	}
	_ = unstructured.SetNestedMap(u.Object, spec, "spec")
	return u
}
