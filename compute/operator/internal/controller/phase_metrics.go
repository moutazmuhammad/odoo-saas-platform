package controller

import (
	"context"
	"time"

	"sigs.k8s.io/controller-runtime/pkg/client"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

// PhaseMetricsCollector periodically recomputes odoo_instances_by_phase by
// listing every OdooInstance. It is registered with the manager as a
// Runnable (see cmd/main.go) rather than updated inline during Reconcile,
// since a per-phase gauge needs the full population count, not just the
// one instance being reconciled.
type PhaseMetricsCollector struct {
	Client   client.Client
	Interval time.Duration
}

func (p *PhaseMetricsCollector) Start(ctx context.Context) error {
	interval := p.Interval
	if interval <= 0 {
		interval = 30 * time.Second
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	p.collect(ctx)
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			p.collect(ctx)
		}
	}
}

func (p *PhaseMetricsCollector) collect(ctx context.Context) {
	var list saasv1alpha1.OdooInstanceList
	if err := p.Client.List(ctx, &list); err != nil {
		return
	}
	counts := map[saasv1alpha1.OdooInstancePhase]int{}
	for _, item := range list.Items {
		counts[item.Status.Phase]++
	}
	for _, phase := range []saasv1alpha1.OdooInstancePhase{
		saasv1alpha1.PhasePending, saasv1alpha1.PhaseProvisioning, saasv1alpha1.PhaseReady,
		saasv1alpha1.PhaseUpdating, saasv1alpha1.PhaseDegraded, saasv1alpha1.PhaseFailed,
		saasv1alpha1.PhaseSuspended, saasv1alpha1.PhaseDeleting,
	} {
		instancesByPhase.WithLabelValues(string(phase)).Set(float64(counts[phase]))
	}
}
