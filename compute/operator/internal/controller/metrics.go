package controller

import (
	"github.com/prometheus/client_golang/prometheus"
	"sigs.k8s.io/controller-runtime/pkg/metrics"
)

// Controller-level observability, exposed on the manager's existing
// /metrics endpoint (see cmd/main.go) alongside the controller-runtime
// workqueue/reconcile metrics that ship for free. These specifically cover
// the platform requirements: reconciliation count, errors, duration, and
// (via instancesByPhase, updated from the status-write path) instances by
// phase.
var (
	reconcileTotal = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "odoo_instance_reconciles_total",
		Help: "Total number of OdooInstance reconciliations, per instance.",
	}, []string{"instance"})

	reconcileErrors = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "odoo_instance_reconcile_errors_total",
		Help: "Total number of OdooInstance reconciliation errors, per instance and reason.",
	}, []string{"instance", "reason"})

	reconcileDuration = prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "odoo_instance_reconcile_duration_seconds",
		Help:    "Duration of OdooInstance reconciliations, per instance.",
		Buckets: prometheus.DefBuckets,
	}, []string{"instance"})

	instancesByPhase = prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Name: "odoo_instances_by_phase",
		Help: "Number of OdooInstance objects currently in each phase.",
	}, []string{"phase"})
)

func init() {
	metrics.Registry.MustRegister(reconcileTotal, reconcileErrors, reconcileDuration, instancesByPhase)
}
