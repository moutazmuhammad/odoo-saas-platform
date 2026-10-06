package controller

import (
	"context"
	"os"
	"testing"

	apiextensions "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	"k8s.io/apiextensions-apiserver/pkg/apiserver/schema"
	"k8s.io/apiextensions-apiserver/pkg/apiserver/schema/cel"
	"k8s.io/apiextensions-apiserver/pkg/apiserver/schema/defaulting"
	"k8s.io/apimachinery/pkg/util/validation/field"
	"sigs.k8s.io/yaml"
)

// Exercise the generated CRD's actual CEL rules without an API server.
func TestSharedStorageSchemaLayoutImmutability(t *testing.T) {
	data, err := os.ReadFile("../../config/crd/bases/saas.odoo.example.com_odooinstances.yaml")
	if err != nil {
		t.Fatal(err)
	}
	var crd apiextensionsv1.CustomResourceDefinition
	if err := yaml.Unmarshal(data, &crd); err != nil {
		t.Fatal(err)
	}
	var converted apiextensions.JSONSchemaProps
	if err := apiextensionsv1.Convert_v1_JSONSchemaProps_To_apiextensions_JSONSchemaProps(crd.Spec.Versions[0].Schema.OpenAPIV3Schema, &converted, nil); err != nil {
		t.Fatal(err)
	}
	structural, err := schema.NewStructural(&converted)
	if err != nil {
		t.Fatal(err)
	}
	specSchema := structural.Properties["spec"]
	if _, present := specSchema.Properties["autoscaling"]; present {
		t.Fatal("removed autoscaling option is still in the CRD")
	}
	validator := cel.NewValidator(structural, true, 1000000)
	object := func(shared interface{}, mode string) map[string]interface{} {
		storage := map[string]interface{}{"filestore": map[string]interface{}{"size": "10Gi"}}
		if shared != nil {
			storage["sharedWithDatabase"] = shared
		}
		obj := map[string]interface{}{"apiVersion": "saas.odoo.example.com/v1alpha1", "kind": "OdooInstance", "metadata": map[string]interface{}{"name": "schema-test"}, "spec": map[string]interface{}{"storage": storage, "database": map[string]interface{}{"mode": mode}}}
		defaulting.Default(obj, structural)
		return obj
	}
	withReplicas := func(obj map[string]interface{}, replicas int64) map[string]interface{} {
		obj["spec"].(map[string]interface{})["replicas"] = replicas
		return obj
	}
	edited := func(obj map[string]interface{}) map[string]interface{} {
		obj["spec"].(map[string]interface{})["suspended"] = true
		return obj
	}
	for _, tc := range []struct {
		name              string
		current, previous map[string]interface{}
		allowed           bool
	}{
		{"new shared", object(true, "Managed"), nil, true},
		{"horizontal scaling", withReplicas(object(true, "Managed"), 2), object(true, "Managed"), false},
		// A legacy replicas > 1 must not block unrelated edits (the
		// transition rule ratchets it), may drop to 1, and may not grow.
		{"legacy replicas unrelated edit", edited(withReplicas(object(true, "Managed"), 2)), withReplicas(object(true, "Managed"), 2), true},
		{"legacy replicas to one", withReplicas(object(true, "Managed"), 1), withReplicas(object(true, "Managed"), 2), true},
		{"legacy replicas grow", withReplicas(object(true, "Managed"), 3), withReplicas(object(true, "Managed"), 2), false},
		{"unchanged shared", object(true, "Managed"), object(true, "Managed"), true},
		{"legacy default", object(nil, "Managed"), nil, true},
		{"unchanged legacy", object(nil, "Managed"), object(nil, "Managed"), true},
		{"shared to legacy", object(false, "Managed"), object(true, "Managed"), false},
		{"legacy to shared", object(true, "Managed"), object(nil, "Managed"), false},
		{"external", object(true, "External"), nil, false},
		{"CNPG", object(true, "CloudNativePG"), nil, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			errs, _ := validator.Validate(context.Background(), field.NewPath("resource"), structural, tc.current, tc.previous, 10000000)
			if (len(errs) == 0) != tc.allowed {
				t.Fatalf("allowed=%v, errors=%v", tc.allowed, errs)
			}
		})
	}
}
