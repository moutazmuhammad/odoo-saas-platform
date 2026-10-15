package resources

import (
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

// ClusterDNSNamespace and ClusterDNSSelector identify the cluster DNS pods
// (CoreDNS on DOKS and MicroK8s) that tenants may query.
const ClusterDNSNamespace = "kube-system"

var ClusterDNSSelector = map[string]string{"k8s-app": "kube-dns"}

// PrivateEgressExcept lists IPv4 ranges tenants may never reach on HTTPS:
// RFC 1918 private (pods, services, nodes, VPC), carrier-grade NAT shared
// space, link-local (cloud metadata at 169.254.169.254) and loopback.
var PrivateEgressExcept = []string{
	"10.0.0.0/8",
	"172.16.0.0/12",
	"192.168.0.0/16",
	"100.64.0.0/10",
	"169.254.0.0/16",
	"127.0.0.0/8",
}

// TenantNetworkPolicy builds a default-deny-by-default NetworkPolicy for
// the tenant namespace. Ingress is permitted only from pods labeled as the
// shared platform gateway (gatewayNamespace/gatewaySelector, supplied by
// the operator's platform-wide configuration, not per-tenant, since the
// gateway is a shared platform component) plus intra-namespace traffic
// (Odoo -> its own database). Egress is permitted to the tenant's own
// database, the cluster DNS pods only, and HTTPS (443) to public addresses
// for outbound integrations (payment webhooks, transactional email, addon
// license checks, etc).
//
// No rule references another tenant namespace, and HTTPS excludes private,
// shared-address, link-local (cloud metadata) and loopback ranges, so
// tenant code cannot reach other tenants' pods, cluster services, node or
// VPC addresses, or the provider metadata service on 443. Enforcement
// depends on the cluster's CNI; verify it with live negative tests.
func TenantNetworkPolicy(instance *saasv1alpha1.OdooInstance, gatewayNamespace string, gatewaySelector map[string]string) *networkingv1.NetworkPolicy {
	tcp := corev1.ProtocolTCP
	httpPort := intstr.FromInt32(OdooHTTPPort)
	longpollPort := intstr.FromInt32(OdooLongpollingPort)
	dnsPort := intstr.FromInt32(53)
	httpsPort := intstr.FromInt32(443)
	pgPort := intstr.FromInt32(PostgreSQLPort)

	gatewayIngressPorts := []networkingv1.NetworkPolicyPort{
		{Protocol: &tcp, Port: &httpPort},
		{Protocol: &tcp, Port: &longpollPort},
	}
	if instance.Spec.Domain.TLS.Enabled {
		// cert-manager's HTTP-01 solver Pod (created and destroyed per
		// certificate issuance/renewal) is reached through the same
		// shared gateway as the Odoo Service, on this fixed port — see
		// AcmeHTTP01SolverPort. Without this, the default-deny policy
		// silently times out every challenge and no certificate is ever
		// issued for a NetworkPolicy-isolated tenant.
		acmePort := intstr.FromInt32(AcmeHTTP01SolverPort)
		gatewayIngressPorts = append(gatewayIngressPorts,
			networkingv1.NetworkPolicyPort{Protocol: &tcp, Port: &acmePort})
	}

	return &networkingv1.NetworkPolicy{
		TypeMeta: metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "NetworkPolicy"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      NetworkPolicyName(instance),
			Namespace: TenantNamespace(instance),
			Labels:    CommonLabels(instance),
		},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{},
			PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeIngress, networkingv1.PolicyTypeEgress},
			Ingress: []networkingv1.NetworkPolicyIngressRule{
				{
					// Shared gateway -> Odoo.
					From: []networkingv1.NetworkPolicyPeer{
						{
							NamespaceSelector: &metav1.LabelSelector{
								MatchLabels: map[string]string{"kubernetes.io/metadata.name": gatewayNamespace},
							},
							PodSelector: &metav1.LabelSelector{MatchLabels: gatewaySelector},
						},
					},
					Ports: gatewayIngressPorts,
				},
				{
					// Intra-namespace only (Odoo <-> its own database).
					From: []networkingv1.NetworkPolicyPeer{
						{PodSelector: &metav1.LabelSelector{}},
					},
				},
			},
			Egress: []networkingv1.NetworkPolicyEgressRule{
				{
					// DNS resolution, only through the cluster DNS pods.
					To: []networkingv1.NetworkPolicyPeer{
						{
							NamespaceSelector: &metav1.LabelSelector{
								MatchLabels: map[string]string{"kubernetes.io/metadata.name": ClusterDNSNamespace},
							},
							PodSelector: &metav1.LabelSelector{MatchLabels: ClusterDNSSelector},
						},
					},
					Ports: []networkingv1.NetworkPolicyPort{
						{Protocol: &tcp, Port: &dnsPort},
						{Protocol: func() *corev1.Protocol { p := corev1.ProtocolUDP; return &p }(), Port: &dnsPort},
					},
				},
				{
					// Intra-namespace (Odoo -> its own database).
					To: []networkingv1.NetworkPolicyPeer{
						{PodSelector: &metav1.LabelSelector{}},
					},
					Ports: []networkingv1.NetworkPolicyPort{
						{Protocol: &tcp, Port: &pgPort},
					},
				},
				{
					// Outbound HTTPS for integrations (email relays, payment
					// webhooks, license checks), to public addresses only.
					// Deliberately excludes plain HTTP egress.
					To: []networkingv1.NetworkPolicyPeer{
						{IPBlock: &networkingv1.IPBlock{CIDR: "0.0.0.0/0", Except: PrivateEgressExcept}},
					},
					Ports: []networkingv1.NetworkPolicyPort{
						{Protocol: &tcp, Port: &httpsPort},
					},
				},
			},
		},
	}
}
