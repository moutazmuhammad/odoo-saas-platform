package resources

import (
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"io/fs"
	"path"
	"sort"
	"strings"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

// platformAddons are Odoo addons the operator ships into tenant pods
// itself (no custom image): a ConfigMap per tenant, mounted at
// PlatformAddonsPath. "all:" keeps the __init__.py/__manifest__.py files
// that go:embed would otherwise skip.
//
//go:embed all:platformaddons
var platformAddons embed.FS

// PlatformAddonsPath is where the platform addons are mounted in the
// Odoo container.
const PlatformAddonsPath = "/opt/saas-platform-addons"

// DatabaseManagerModule is the server-wide module behind
// spec.databaseManager.
const DatabaseManagerModule = "saas_tenant_dbm"

// PlatformAddonsConfigMapName is the per-tenant ConfigMap holding the
// platform addons.
func PlatformAddonsConfigMapName(instance *saasv1alpha1.OdooInstance) string {
	return "odoo-platform-addons"
}

// platformAddonFiles maps ConfigMap keys ("<addon>.<file>") to paths
// relative to PlatformAddonsPath ("<addon>/<file>"), and returns the
// file contents by key.
func platformAddonFiles() (map[string]string, map[string]string) {
	paths := map[string]string{}
	data := map[string]string{}
	_ = fs.WalkDir(platformAddons, "platformaddons", func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() || strings.HasSuffix(p, ".pyc") {
			return err
		}
		rel := strings.TrimPrefix(p, "platformaddons/")
		content, readErr := platformAddons.ReadFile(p)
		if readErr != nil {
			return readErr
		}
		key := strings.ReplaceAll(rel, "/", ".")
		paths[key] = rel
		data[key] = string(content)
		return nil
	})
	return paths, data
}

// PlatformAddonsConfigMap builds the ConfigMap with the platform addons.
func PlatformAddonsConfigMap(instance *saasv1alpha1.OdooInstance) *corev1.ConfigMap {
	_, data := platformAddonFiles()
	return &corev1.ConfigMap{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ConfigMap"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      PlatformAddonsConfigMapName(instance),
			Namespace: TenantNamespace(instance),
			Labels:    CommonLabels(instance),
		},
		Data: data,
	}
}

// platformAddonsVolume mounts the ConfigMap keys back into their addon
// directories.
func platformAddonsVolume(instance *saasv1alpha1.OdooInstance) corev1.Volume {
	paths, _ := platformAddonFiles()
	keys := make([]string, 0, len(paths))
	for k := range paths {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	items := make([]corev1.KeyToPath, 0, len(keys))
	for _, k := range keys {
		items = append(items, corev1.KeyToPath{Key: k, Path: paths[k]})
	}
	return corev1.Volume{
		Name: "platform-addons",
		VolumeSource: corev1.VolumeSource{
			ConfigMap: &corev1.ConfigMapVolumeSource{
				LocalObjectReference: corev1.LocalObjectReference{Name: PlatformAddonsConfigMapName(instance)},
				Items:                items,
			},
		},
	}
}

// platformAddonsHash changes whenever an embedded addon file changes, so
// the pods roll onto the new code.
func platformAddonsHash() string {
	paths, data := platformAddonFiles()
	keys := make([]string, 0, len(paths))
	for k := range paths {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	h := sha256.New()
	for _, k := range keys {
		h.Write([]byte(path.Clean(paths[k])))
		h.Write([]byte{0})
		h.Write([]byte(data[k]))
		h.Write([]byte{0})
	}
	return hex.EncodeToString(h.Sum(nil))[:16]
}
