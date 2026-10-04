package resources

import (
	"fmt"
	"testing"

	networkingv1 "k8s.io/api/networking/v1"
	gatewayv1 "sigs.k8s.io/gateway-api/apis/v1"
)

func TestRealtimeRoutingAcrossOdooVersionsAndWorkerModes(t *testing.T) {
	for _, version := range []string{"14.0", "15.0", "16.0", "17.0", "18.0", "19.0"} {
		for _, workers := range []int32{0, 2} {
			t.Run(fmt.Sprintf("%s/workers=%d", version, workers), func(t *testing.T) {
				instance := testInstance()
				instance.Spec.Version = version
				instance.Spec.Workers.Count = workers
				realtime := int32(8069)
				if workers > 0 {
					realtime = 8072
				}
				want := map[string]int32{"/": 8069, "/websocket": realtime, "/longpolling": realtime}
				route := HTTPRoute(instance, "gateway-system", "public")
				if len(route.Spec.Rules) != len(want) {
					t.Fatalf("gateway route must cover HTTP, WebSocket and legacy longpolling")
				}
				for _, rule := range route.Spec.Rules {
					match := rule.Matches[0].Path
					backend := rule.BackendRefs[0]
					port, ok := want[*match.Value]
					if !ok || *match.Type != gatewayv1.PathMatchPathPrefix || int32(*backend.Port) != port || string(backend.Name) != OdooServiceName(instance) {
						t.Errorf("gateway path %s has incorrect match/backend: %+v", *match.Value, rule)
					}
				}
				ingress := Ingress(instance, nil)
				paths := ingress.Spec.Rules[0].HTTP.Paths
				if len(paths) != len(want) {
					t.Fatalf("ingress must cover HTTP, WebSocket and legacy longpolling")
				}
				for _, path := range paths {
					port, ok := want[path.Path]
					if !ok || *path.PathType != networkingv1.PathTypePrefix || path.Backend.Service.Port.Number != port || path.Backend.Service.Name != OdooServiceName(instance) {
						t.Errorf("ingress path %s has incorrect match/backend: %+v", path.Path, path)
					}
				}
			})
		}
	}
}
