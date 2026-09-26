package resources

import (
	"fmt"
	"strings"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

// odooConfTemplate is rendered into the tenant namespace as a ConfigMap. It
// intentionally contains no secret values: placeholders (double-underscore
// tokens) are substituted by an init container at pod startup from
// environment variables sourced via secretKeyRef, so the plaintext admin
// password and database password never pass through the Kubernetes API as
// ConfigMap data and never appear in `kubectl get configmap -o yaml`.
//
// The placeholder tokens are plain identifiers (no shell/sed metacharacters)
// and GeneratePassword only emits alphanumeric characters, so substituting
// them with `sed` in the init container (see deployment.go) is safe without
// further escaping.
// Note: `workers` and `max_cron_threads` are deliberately NOT baked into
// this template. They differ between the "web" and "cron" Deployment roles
// (see deployment.go and docs/architecture.md, "Odoo Worker Model") and are
// instead passed as CLI flags on each Deployment's container command,
// which Odoo applies as overrides on top of this config file.
const odooConfTemplate = `[options]
admin_passwd = __ADMIN_PASSWORD__
db_host = __DB_HOST__
db_port = __DB_PORT__
db_user = __DB_USER__
db_password = __DB_PASSWORD__
db_name = __DB_NAME__
dbfilter = ^__DB_NAME__$
list_db = False
proxy_mode = True
without_demo = all
data_dir = /var/lib/odoo
`

// odooConf renders the odoo.conf template for instance: the fixed settings,
// spec.databaseFilter in place of the default dbfilter when set, plus, when
// spec.addonsPaths is set, an addons_path line. Odoo always adds
// its own built-in addons directory on top of addons_path, so only the
// extra (tenant) directories are listed.
func odooConf(instance *saasv1alpha1.OdooInstance) string {
	conf := odooConfTemplate
	if instance.Spec.DatabaseFilter != "" {
		conf = strings.Replace(conf, "dbfilter = ^__DB_NAME__$",
			"dbfilter = "+instance.Spec.DatabaseFilter, 1)
	}
	if len(instance.Spec.AddonsPaths) > 0 {
		conf += "addons_path = " + strings.Join(instance.Spec.AddonsPaths, ",") + "\n"
	}
	return conf
}

// OdooConfigMap builds the ConfigMap holding the odoo.conf template
// (non-secret settings only) the Odoo pods start from.
func OdooConfigMap(instance *saasv1alpha1.OdooInstance) *corev1.ConfigMap {
	return odooConfigMap(instance, OdooConfigMapName(instance))
}

// OdooUpdateConfigMap is the same config rendered for the pending
// spec.update's update Job, kept separate so the running pods (which may
// still be on the previous image and addons paths) never read it.
func OdooUpdateConfigMap(instance *saasv1alpha1.OdooInstance) *corev1.ConfigMap {
	return odooConfigMap(instance, OdooUpdateConfigMapName(instance))
}

func odooConfigMap(instance *saasv1alpha1.OdooInstance, name string) *corev1.ConfigMap {
	return &corev1.ConfigMap{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ConfigMap"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: TenantNamespace(instance),
			Labels:    CommonLabels(instance),
		},
		Data: map[string]string{
			"odoo.conf.tmpl": odooConf(instance),
		},
	}
}

// renderInitContainerScript is the shell script that substitutes secret
// values into the odoo.conf template at container start. It is identical
// for every instance; kept as a function (not a const) so it stays next to
// the template it depends on.
func renderInitContainerScript() string {
	tokens := []string{"ADMIN_PASSWORD", "DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME"}
	var sedExprs []string
	for _, t := range tokens {
		sedExprs = append(sedExprs, fmt.Sprintf(`s/__%s__/$%s/g`, t, t))
	}
	return fmt.Sprintf(
		"set -e; cp /template/odoo.conf.tmpl /etc/odoo/odoo.conf; sed -i \"%s\" /etc/odoo/odoo.conf",
		strings.Join(sedExprs, "; "),
	)
}
