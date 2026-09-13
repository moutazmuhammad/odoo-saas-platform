// Command manager runs the OdooInstance operator: a controller-runtime
// manager hosting the OdooInstance reconciler, a health/readiness server,
// and a Prometheus metrics endpoint. See internal/controller for the
// reconciliation logic and internal/resources for the desired-state
// builders it applies.
package main

import (
	"crypto/tls"
	"flag"
	"os"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	policyv1 "k8s.io/api/policy/v1"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	logzap "sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	"sigs.k8s.io/controller-runtime/pkg/webhook"
	gatewayv1 "sigs.k8s.io/gateway-api/apis/v1"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
	"github.com/freightright/odoo-saas-platform/operator/internal/controller"
)

var (
	scheme   = clientgoscheme.Scheme
	setupLog = ctrl.Log.WithName("setup")
)

func init() {
	utilruntime.Must(saasv1alpha1.AddToScheme(scheme))
	utilruntime.Must(appsv1.AddToScheme(scheme))
	utilruntime.Must(corev1.AddToScheme(scheme))
	utilruntime.Must(batchv1.AddToScheme(scheme))
	utilruntime.Must(networkingv1.AddToScheme(scheme))
	utilruntime.Must(policyv1.AddToScheme(scheme))
	// HTTPRoute is typed (internal/resources/route.go) and therefore must
	// be registered even though the object carries an explicit TypeMeta:
	// controller-runtime's client resolves the REST mapping from the
	// scheme, not from TypeMeta, for typed Get/Patch/Apply calls.
	// Registering this is safe even on a cluster without the Gateway API
	// CRDs installed — it only becomes a problem if an HTTPRoute is
	// actually applied, at which point the resulting API error surfaces
	// through the RouteReady condition rather than crashing the operator.
	//
	// cert-manager's Certificate and CloudNativePG's Cluster are handled
	// purely via the dynamic/unstructured client (internal/resources:
	// tls.go, cnpg.go), which needs no scheme registration at all — that
	// is what actually keeps the operator's dependency graph free of
	// those projects' Go modules and lets it start on a cluster where
	// they are not installed.
	utilruntime.Must(gatewayv1.AddToScheme(scheme))
}

func main() {
	var (
		metricsAddr          string
		probeAddr            string
		enableLeaderElection bool
		networkingProvider   string
		gatewayNamespace     string
		gatewayName          string
		gatewaySelectorCSV   string
		ingressClassName     string
		backupToolImage      string
		restoreToolImage     string
		supportedVersionsCSV string
		allowMutableTags     bool
		secureMetrics        bool
	)

	flag.StringVar(&metricsAddr, "metrics-bind-address", ":8443", "The address the metrics endpoint binds to.")
	flag.StringVar(&probeAddr, "health-probe-bind-address", ":8081", "The address the probe endpoint binds to.")
	flag.BoolVar(&enableLeaderElection, "leader-elect", false, "Enable leader election for controller manager. "+
		"Enabling this will ensure there is only one active controller manager.")
	flag.StringVar(&networkingProvider, "networking-provider", controller.NetworkingProviderGatewayAPI,
		"How to route external traffic to tenant instances: \"gateway-api\" (default) or \"ingress\".")
	flag.StringVar(&gatewayNamespace, "gateway-namespace", "gateway-system", "Namespace of the shared platform Gateway.")
	flag.StringVar(&gatewayName, "gateway-name", "odoo-saas-gateway", "Name of the shared platform Gateway.")
	flag.StringVar(&gatewaySelectorCSV, "gateway-pod-selector", "app.kubernetes.io/name=odoo-saas-gateway",
		"Comma-separated key=value labels matching the shared Gateway's own pods, used to scope the tenant NetworkPolicy ingress-allow rule.")
	flag.StringVar(&ingressClassName, "ingress-class-name", "", "IngressClassName used when --networking-provider=ingress.")
	flag.StringVar(&backupToolImage, "backup-tool-image", "", "Overrides the default backup CronJob image.")
	flag.StringVar(&restoreToolImage, "restore-tool-image", "", "Overrides the default restore-from-backup Job image.")
	flag.StringVar(&supportedVersionsCSV, "supported-odoo-versions", "17.0,18.0,19.0",
		"Comma-separated list of Odoo versions instances may request. Empty disables the check.")
	flag.BoolVar(&allowMutableTags, "allow-mutable-tags", false,
		"Allow mutable image tags such as \"latest\" (development clusters only).")
	flag.BoolVar(&secureMetrics, "metrics-secure", true, "Serve metrics via HTTPS.")
	zapOpts := logzap.Options{Development: false}
	zapOpts.BindFlags(flag.CommandLine)
	flag.Parse()

	ctrl.SetLogger(logzap.New(logzap.UseFlagOptions(&zapOpts)))

	var supportedVersions []string
	if supportedVersionsCSV != "" {
		supportedVersions = strings.Split(supportedVersionsCSV, ",")
	}

	var tlsOpts []func(*tls.Config)
	if !secureMetrics {
		tlsOpts = append(tlsOpts, func(c *tls.Config) { c.InsecureSkipVerify = true })
	}

	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme: scheme,
		Metrics: metricsserver.Options{
			BindAddress:   metricsAddr,
			SecureServing: secureMetrics,
			TLSOpts:       tlsOpts,
		},
		WebhookServer:          webhook.NewServer(webhook.Options{}),
		HealthProbeBindAddress: probeAddr,
		LeaderElection:         enableLeaderElection,
		LeaderElectionID:       "odoo-instance-operator.saas.odoo.example.com",
	})
	if err != nil {
		setupLog.Error(err, "unable to start manager")
		os.Exit(1)
	}

	reconciler := &controller.OdooInstanceReconciler{
		Client:                mgr.GetClient(),
		Scheme:                mgr.GetScheme(),
		Recorder:              mgr.GetEventRecorderFor("odoo-instance-controller"),
		NetworkingProvider:    networkingProvider,
		GatewayNamespace:      gatewayNamespace,
		GatewayName:           gatewayName,
		GatewaySelector:       parseSelector(gatewaySelectorCSV),
		BackupToolImage:       backupToolImage,
		RestoreToolImage:      restoreToolImage,
		SupportedOdooVersions: supportedVersions,
		AllowMutableTags:      allowMutableTags,
	}
	if ingressClassName != "" {
		reconciler.IngressClassName = ptr.To(ingressClassName)
	}

	if err := reconciler.SetupWithManager(mgr); err != nil {
		setupLog.Error(err, "unable to create controller", "controller", "OdooInstance")
		os.Exit(1)
	}

	if err := mgr.Add(&controller.PhaseMetricsCollector{Client: mgr.GetClient()}); err != nil {
		setupLog.Error(err, "unable to register phase metrics collector")
		os.Exit(1)
	}

	if err := mgr.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		setupLog.Error(err, "unable to set up health check")
		os.Exit(1)
	}
	if err := mgr.AddReadyzCheck("readyz", healthz.Ping); err != nil {
		setupLog.Error(err, "unable to set up ready check")
		os.Exit(1)
	}

	setupLog.Info("starting manager")
	if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
		setupLog.Error(err, "problem running manager")
		os.Exit(1)
	}
}

func parseSelector(csv string) map[string]string {
	out := map[string]string{}
	for _, pair := range strings.Split(csv, ",") {
		k, v, ok := strings.Cut(pair, "=")
		if !ok {
			continue
		}
		out[k] = v
	}
	return out
}
