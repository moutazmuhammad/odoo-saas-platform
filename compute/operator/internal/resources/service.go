package resources

import (
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

const (
	// OdooHTTPPort is Odoo's main HTTP port.
	OdooHTTPPort = 8069
	// OdooLongpollingPort is Odoo's dedicated gevent worker port, used for
	// longpolling (live chat, discuss/bus notifications). Only listened on
	// when Workers.Count > 0.
	OdooLongpollingPort = 8072
	// PostgreSQLPort is the standard PostgreSQL port.
	PostgreSQLPort = 5432
	// AcmeHTTP01SolverPort is the fixed port cert-manager's acmesolver
	// image listens on for every HTTP-01 challenge Pod it creates
	// (regardless of Odoo version or tenant). The tenant NetworkPolicy
	// must allow the shared gateway to reach it on this port, or the
	// challenge times out and no certificate is ever issued — see
	// TenantNetworkPolicy.
	AcmeHTTP01SolverPort = 8089
)

// OdooService builds the ClusterIP Service in front of the Odoo Deployment.
// It is the single stable network identity the HTTPRoute/Ingress and any
// in-cluster caller (e.g. a future backend job runner) target.
func OdooService(instance *saasv1alpha1.OdooInstance) *corev1.Service {
	return &corev1.Service{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "Service"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      OdooServiceName(instance),
			Namespace: TenantNamespace(instance),
			Labels:    CommonLabels(instance),
		},
		Spec: corev1.ServiceSpec{
			// Scoped to the web role: SelectorLabels alone also matches the
			// database StatefulSet's pod (and the cron Deployment's, and the
			// init Job's while it runs), since none of those set the
			// role=web label this excludes. Reproduced live: without this,
			// the Service round-robins real HTTP traffic across the odoo
			// web pod and the postgresql pod, which doesn't listen on 8069,
			// causing intermittent 502s from the Ingress/Gateway.
			Selector: mergeLabels(SelectorLabels(instance), map[string]string{"saas.odoo.example.com/role": string(RoleWeb)}),
			Ports: []corev1.ServicePort{
				{
					Name:       "http",
					Port:       OdooHTTPPort,
					TargetPort: intstr.FromInt32(OdooHTTPPort),
					Protocol:   corev1.ProtocolTCP,
				},
				{
					Name:       "longpolling",
					Port:       OdooLongpollingPort,
					TargetPort: intstr.FromInt32(OdooLongpollingPort),
					Protocol:   corev1.ProtocolTCP,
				},
			},
		},
	}
}

// DatabaseService builds the headless-free ClusterIP Service fronting the
// managed PostgreSQL StatefulSet. Only used in DatabaseModeManaged.
func DatabaseService(instance *saasv1alpha1.OdooInstance) *corev1.Service {
	return &corev1.Service{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "Service"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      DatabaseServiceName(instance),
			Namespace: TenantNamespace(instance),
			Labels:    WithComponent(instance, "database"),
		},
		Spec: corev1.ServiceSpec{
			Selector: WithComponent(instance, "database"),
			Ports: []corev1.ServicePort{
				{
					Name:       "postgresql",
					Port:       PostgreSQLPort,
					TargetPort: intstr.FromInt32(PostgreSQLPort),
					Protocol:   corev1.ProtocolTCP,
				},
			},
		},
	}
}
