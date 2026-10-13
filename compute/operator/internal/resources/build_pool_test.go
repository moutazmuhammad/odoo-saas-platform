package resources

import (
	"testing"

	"github.com/google/cel-go/cel"
)

func TestBuildPoolAdmissionEnforcesIsolation(t *testing.T) {
	var spec map[string]any
	for _, object := range renderedControlPlaneChart(t) {
		if object["kind"] == "ValidatingAdmissionPolicy" && object["metadata"].(map[string]any)["name"] == "veltnex-build-pool-isolation" {
			spec = object["spec"].(map[string]any)
		}
	}
	if spec == nil {
		t.Fatal("missing build-pool admission policy")
	}
	env, err := cel.NewEnv(cel.Variable("object", cel.DynType), cel.Variable("request", cel.DynType))
	if err != nil {
		t.Fatal(err)
	}
	compile := func(expression string) cel.Program {
		ast, issues := env.Compile(expression)
		if issues.Err() != nil {
			t.Fatal(issues.Err())
		}
		program, err := env.Program(ast)
		if err != nil {
			t.Fatal(err)
		}
		return program
	}
	condition := compile(spec["matchConditions"].([]any)[0].(map[string]any)["expression"].(string))
	var rules []cel.Program
	for _, rule := range spec["validations"].([]any) {
		rules = append(rules, compile(rule.(map[string]any)["expression"].(string)))
	}
	accepted := func(namespace, operation string, pod map[string]any) bool {
		bindings := map[string]any{"object": map[string]any{"metadata": map[string]any{"namespace": namespace}, "spec": pod}, "request": map[string]any{"operation": operation}}
		matches, _, err := condition.Eval(bindings)
		if err != nil {
			t.Fatal(err)
		}
		if matches.Value() != true {
			return true
		}
		for _, rule := range rules {
			out, _, err := rule.Eval(bindings)
			if err != nil {
				t.Fatal(err)
			}
			if out.Value() != true {
				return false
			}
		}
		return true
	}
	build := func() map[string]any {
		return map[string]any{
			"affinity": map[string]any{"nodeAffinity": map[string]any{"requiredDuringSchedulingIgnoredDuringExecution": map[string]any{
				"nodeSelectorTerms": []any{map[string]any{"matchExpressions": []any{map[string]any{"key": "saas.platform/pool", "operator": "In", "values": []any{"build"}}}}},
			}}},
			"tolerations": []any{map[string]any{"key": "saas.platform/pool", "operator": "Equal", "value": "build", "effect": "NoSchedule"}},
		}
	}
	if !accepted("odoo-builds", "CREATE", build()) || accepted("odoo-builds", "CREATE", map[string]any{}) {
		t.Fatal("build pool placement is not mandatory")
	}
	for _, mutate := range []func(map[string]any){
		func(p map[string]any) { delete(p, "tolerations") },
		func(p map[string]any) { p["shareProcessNamespace"] = true },
		func(p map[string]any) { p["nodeName"] = "tenant-node" },
		func(p map[string]any) {
			required := p["affinity"].(map[string]any)["nodeAffinity"].(map[string]any)["requiredDuringSchedulingIgnoredDuringExecution"].(map[string]any)
			required["nodeSelectorTerms"] = append(required["nodeSelectorTerms"].([]any), map[string]any{"matchExpressions": []any{map[string]any{"key": "kubernetes.io/os", "operator": "In", "values": []any{"linux"}}}})
		},
	} {
		pod := build()
		mutate(pod)
		if accepted("odoo-builds", "CREATE", pod) {
			t.Fatal("unsafe build placement accepted")
		}
	}
	for _, ns := range []string{"odoo-tenant-test", "saas-toolbox", "odoo-system"} {
		if !accepted(ns, "CREATE", map[string]any{}) || accepted(ns, "CREATE", build()) {
			t.Fatalf("namespace %s can tolerate build taint", ns)
		}
		for _, tolerance := range []map[string]any{
			{"operator": "Exists"}, {"key": "saas.platform/pool", "operator": "Exists"},
			{"key": "", "operator": "Exists", "effect": "NoSchedule"},
			{"key": "saas.platform/pool", "value": "build"},
		} {
			if accepted(ns, "UPDATE", map[string]any{"tolerations": []any{tolerance}}) {
				t.Fatal("wildcard build taint bypass accepted")
			}
		}
		if !accepted(ns, "UPDATE", map[string]any{"nodeName": "already-scheduled"}) {
			t.Fatal("normal scheduled-pod update rejected")
		}
	}
	if !accepted("kube-system", "CREATE", map[string]any{"tolerations": []any{map[string]any{"operator": "Exists"}}}) {
		t.Fatal("provider infrastructure blocked")
	}
}
