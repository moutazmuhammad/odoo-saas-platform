package resources

import (
	"encoding/json"
	"os"
	"os/exec"
	"strings"
	"testing"

	"github.com/google/cel-go/cel"
	"sigs.k8s.io/yaml"
)

func renderedControlPlaneChart(t *testing.T) []map[string]any {
	t.Helper()
	helm := os.Getenv("HELM_BIN")
	if helm == "" {
		helm = "helm"
	}
	output, err := exec.Command(helm, "template", "odoo-operator", "../../../charts/odoo-operator", "--namespace", "odoo-system").CombinedOutput()
	if err != nil {
		t.Fatalf("rendering chart: %v: %s", err, output)
	}
	var objects []map[string]any
	for _, document := range strings.Split(string(output), "\n---") {
		var object map[string]any
		if err := yaml.Unmarshal([]byte(document), &object); err != nil {
			t.Fatal(err)
		}
		if object["kind"] != nil {
			objects = append(objects, object)
		}
	}
	return objects
}

func TestControlPlaneRBACNoClusterSecretAccessOrGlobalTenantBinding(t *testing.T) {
	for _, object := range renderedControlPlaneChart(t) {
		kind := object["kind"]
		name := object["metadata"].(map[string]any)["name"]
		if kind == "ClusterRoleBinding" {
			role := object["roleRef"].(map[string]any)["name"]
			if role == TenantAccessRoleName || role == "cluster-admin" {
				t.Fatalf("unsafe global binding %v", role)
			}
		}
		if kind != "ClusterRole" || (name != "veltnex-control-plane" && name != "veltnex-toolbox-read") {
			continue
		}
		for _, rule := range object["rules"].([]any) {
			r := rule.(map[string]any)
			encoded, _ := json.Marshal(r)
			if strings.Contains(string(encoded), `"*"`) {
				t.Fatal("wildcard permission")
			}
			for _, resource := range r["resources"].([]any) {
				if resource == "secrets" || resource == "serviceaccounts/token" {
					t.Fatal("global credential access")
				}
				for _, verb := range r["verbs"].([]any) {
					readOnly := verb == "get" || verb == "list" || verb == "watch"
					if !readOnly && (resource != "odooinstances" || name != "veltnex-control-plane") {
						t.Fatalf("unexpected cluster write: %v", r)
					}
				}
			}
		}
	}
	binding := TenantControlPlaneBinding(testInstance(), "kube-system", "saas-control-plane")
	if binding.Namespace != TenantNamespace(testInstance()) || binding.RoleRef.Name != TenantAccessRoleName || binding.Subjects[0].Namespace != "kube-system" {
		t.Fatal("tenant delegation targets the wrong namespace/identity")
	}
}

func TestControlPlaneAdmissionCELRejectsCredentialAndHostEscapes(t *testing.T) {
	var spec map[string]any
	for _, object := range renderedControlPlaneChart(t) {
		if object["kind"] == "ValidatingAdmissionPolicy" && object["metadata"].(map[string]any)["name"] == "veltnex-control-plane-workloads" {
			spec = object["spec"].(map[string]any)
		}
	}
	if spec == nil {
		t.Fatal("missing workload admission guard")
	}
	env, err := cel.NewEnv(cel.Variable("object", cel.DynType), cel.Variable("variables", cel.DynType))
	if err != nil {
		t.Fatal(err)
	}
	compile := func(expression string) cel.Program {
		ast, issues := env.Compile(expression)
		if issues.Err() != nil {
			t.Fatalf("invalid CEL %s: %v", expression, issues.Err())
		}
		program, err := env.Program(ast)
		if err != nil {
			t.Fatal(err)
		}
		return program
	}
	containers := compile(spec["variables"].([]any)[0].(map[string]any)["expression"].(string))
	var validations []cel.Program
	for _, rule := range spec["validations"].([]any) {
		validations = append(validations, compile(rule.(map[string]any)["expression"].(string)))
	}
	for _, condition := range spec["matchConditions"].([]any) {
		compile(condition.(map[string]any)["expression"].(string))
	}
	accepted := func(object map[string]any) bool {
		vars, _, err := containers.Eval(map[string]any{"object": object})
		if err != nil {
			t.Fatal(err)
		}
		for _, program := range validations {
			out, _, err := program.Eval(map[string]any{"object": object, "variables": map[string]any{"containers": vars}})
			if err != nil {
				t.Fatalf("CEL evaluation failed: %v", err)
			}
			if out.Value() != true {
				return false
			}
		}
		return true
	}
	build := func() map[string]any {
		return map[string]any{"metadata": map[string]any{"namespace": "odoo-builds"}, "spec": map[string]any{
			"automountServiceAccountToken": false, "serviceAccountName": "default",
			"initContainers": []any{map[string]any{"name": "fetch"}},
			"containers": []any{map[string]any{"name": "build", "securityContext": map[string]any{
				"runAsUser": int64(1000), "seccompProfile": map[string]any{"type": "Unconfined"}}}},
		}}
	}
	if !accepted(build()) {
		t.Fatal("current rootless build rejected")
	}
	cases := map[string]func(map[string]any){
		"unsafe sysctl": func(s map[string]any) {
			s["securityContext"] = map[string]any{"sysctls": []any{map[string]any{"name": "kernel.shm_rmid_forced", "value": "0"}}}
		},
		"unmasked proc": func(s map[string]any) {
			s["containers"].([]any)[0].(map[string]any)["securityContext"].(map[string]any)["procMount"] = "Unmasked"
		},
		"host network": func(s map[string]any) { s["hostNetwork"] = true },
		"host mount": func(s map[string]any) {
			s["volumes"] = []any{map[string]any{"name": "host", "hostPath": map[string]any{"path": "/"}}}
		},
		"privileged": func(s map[string]any) {
			s["containers"].([]any)[0].(map[string]any)["securityContext"].(map[string]any)["privileged"] = true
		},
		"node capability": func(s map[string]any) {
			s["containers"].([]any)[0].(map[string]any)["securityContext"].(map[string]any)["capabilities"] = map[string]any{"add": []any{"SYS_ADMIN"}}
		},
		"other account":   func(s map[string]any) { s["serviceAccountName"] = "powerful" },
		"automatic token": func(s map[string]any) { s["automountServiceAccountToken"] = true },
		"projected token": func(s map[string]any) {
			s["volumes"] = []any{map[string]any{"name": "token", "projected": map[string]any{"sources": []any{map[string]any{"serviceAccountToken": map[string]any{"path": "token"}}}}}}
		},
		"ephemeral privileged": func(s map[string]any) {
			s["ephemeralContainers"] = []any{map[string]any{"securityContext": map[string]any{"privileged": true}}}
		},
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			object := build()
			mutate(object["spec"].(map[string]any))
			if accepted(object) {
				t.Fatal("unsafe build accepted")
			}
		})
	}
	toolbox := build()
	toolbox["metadata"].(map[string]any)["namespace"] = "saas-toolbox"
	s := toolbox["spec"].(map[string]any)
	delete(s, "initContainers")
	s["serviceAccountName"] = "saas-toolbox"
	s["securityContext"] = map[string]any{"runAsNonRoot": true, "runAsUser": int64(1000), "seccompProfile": map[string]any{"type": "RuntimeDefault"}}
	container := s["containers"].([]any)[0].(map[string]any)
	container["securityContext"] = map[string]any{"allowPrivilegeEscalation": false, "capabilities": map[string]any{"drop": []any{"ALL"}}}
	if !accepted(toolbox) {
		t.Fatal("restricted toolbox rejected")
	}
	container["securityContext"].(map[string]any)["runAsUser"] = int64(0)
	if accepted(toolbox) {
		t.Fatal("root toolbox accepted")
	}
}
