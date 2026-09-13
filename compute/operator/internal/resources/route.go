package resources

import (
	networkingv1 "k8s.io/api/networking/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	gatewayv1 "sigs.k8s.io/gateway-api/apis/v1"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

// HTTPRoute builds a Gateway API HTTPRoute binding instance.Spec.Domain.Hostname
// to the Odoo Service, parented to the shared platform Gateway. This is the
// preferred routing mechanism (see docs/architecture.md, "Networking"):
// one shared Gateway/load balancer serves every tenant, and each
// OdooInstance only ever describes its own hostname, never a
// provider-specific Ingress/LoadBalancer resource.
func HTTPRoute(instance *saasv1alpha1.OdooInstance, gatewayNamespace, gatewayName string) *gatewayv1.HTTPRoute {
	hostname := gatewayv1.Hostname(instance.Spec.Domain.Hostname)
	ns := gatewayv1.Namespace(gatewayNamespace)
	port := gatewayv1.PortNumber(OdooHTTPPort)

	return &gatewayv1.HTTPRoute{
		TypeMeta: metav1.TypeMeta{APIVersion: gatewayv1.GroupVersion.String(), Kind: "HTTPRoute"},
		ObjectMeta: metav1.ObjectMeta{
			Name:        HTTPRouteName(instance),
			Namespace:   TenantNamespace(instance),
			Labels:      CommonLabels(instance),
			Annotations: instance.Spec.Networking.RouteAnnotations,
		},
		Spec: gatewayv1.HTTPRouteSpec{
			CommonRouteSpec: gatewayv1.CommonRouteSpec{
				ParentRefs: []gatewayv1.ParentReference{
					{
						Name:      gatewayv1.ObjectName(gatewayName),
						Namespace: &ns,
					},
				},
			},
			Hostnames: []gatewayv1.Hostname{hostname},
			Rules: []gatewayv1.HTTPRouteRule{
				{
					BackendRefs: []gatewayv1.HTTPBackendRef{
						{
							BackendRef: gatewayv1.BackendRef{
								BackendObjectReference: gatewayv1.BackendObjectReference{
									Name: gatewayv1.ObjectName(OdooServiceName(instance)),
									Port: &port,
								},
							},
						},
					},
				},
			},
		},
	}
}

// Ingress builds the fallback classic Ingress, used only when the platform
// is configured without Gateway API support (see the operator's
// `--networking-provider` flag). Functionally equivalent routing to
// HTTPRoute, kept as a distinct builder so the Gateway API path never
// carries Ingress-specific annotation conventions.
func Ingress(instance *saasv1alpha1.OdooInstance, ingressClassName *string) *networkingv1.Ingress {
	pathType := networkingv1.PathTypePrefix
	var tls []networkingv1.IngressTLS
	if instance.Spec.Domain.TLS.Enabled {
		tls = []networkingv1.IngressTLS{
			{
				Hosts:      []string{instance.Spec.Domain.Hostname},
				SecretName: tlsSecretName(instance),
			},
		}
	}

	return &networkingv1.Ingress{
		TypeMeta: metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "Ingress"},
		ObjectMeta: metav1.ObjectMeta{
			Name:        IngressName(instance),
			Namespace:   TenantNamespace(instance),
			Labels:      CommonLabels(instance),
			Annotations: instance.Spec.Networking.RouteAnnotations,
		},
		Spec: networkingv1.IngressSpec{
			IngressClassName: ingressClassName,
			TLS:              tls,
			Rules: []networkingv1.IngressRule{
				{
					Host: instance.Spec.Domain.Hostname,
					IngressRuleValue: networkingv1.IngressRuleValue{
						HTTP: &networkingv1.HTTPIngressRuleValue{
							Paths: []networkingv1.HTTPIngressPath{
								{
									Path:     "/",
									PathType: &pathType,
									Backend: networkingv1.IngressBackend{
										Service: &networkingv1.IngressServiceBackend{
											Name: OdooServiceName(instance),
											Port: networkingv1.ServiceBackendPort{Number: OdooHTTPPort},
										},
									},
								},
							},
						},
					},
				},
			},
		},
	}
}

func tlsSecretName(instance *saasv1alpha1.OdooInstance) string {
	if instance.Spec.Domain.TLS.SecretName != "" {
		return instance.Spec.Domain.TLS.SecretName
	}
	return CertificateName(instance) + "-tls"
}
