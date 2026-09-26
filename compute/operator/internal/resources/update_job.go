package resources

import (
	"crypto/sha256"
	"encoding/hex"
	"strings"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/utils/ptr"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

// UpdateJobComponent labels every module-update Job (and its pods).
const UpdateJobComponent = "update"

// updateJobDeadlineSeconds bounds a single `odoo -u` run.
const updateJobDeadlineSeconds = 3600

// OdooUpdateJobName is unique per spec.update.token, so each update gets
// its own (immutable) Job and a failed one is never silently re-run.
func OdooUpdateJobName(instance *saasv1alpha1.OdooInstance) string {
	token := ""
	if instance.Spec.Update != nil {
		token = instance.Spec.Update.Token
	}
	sum := sha256.Sum256([]byte(token))
	return "odoo-update-" + hex.EncodeToString(sum[:])[:10]
}

// updateScript upgrades each database given as a positional argument in
// turn and stops at the first failure: Odoo refuses -u with several
// databases in one -d. Module names are CRD-validated ([a-z0-9_]+);
// database names are passed as arguments, never interpolated.
const updateScript = `set -e
for db in "$@"; do
  echo "== upgrading database $db"
  odoo -c /etc/odoo/odoo.conf -d "$db" -u "$SAAS_MODULES" --workers=0 --max-cron-threads=0 --no-http --stop-after-init
done`

// OdooUpdateJob runs `odoo -u <modules> --stop-after-init` with the new
// spec.image and spec.addonsPaths (via OdooUpdateConfigMap) against the
// tenant database and then each of spec.update.databases, before any
// serving pod is switched to that image; every database must succeed. It
// is not retried: a failed migration needs a fix and a new token, not a
// loop.
func OdooUpdateJob(instance *saasv1alpha1.OdooInstance) *batchv1.Job {
	command := []string{"sh", "-c", updateScript, "update-modules"}
	args := updateDatabases(instance)
	job := odooOneShotJob(instance, OdooUpdateJobName(instance), UpdateJobComponent, "update-modules",
		OdooUpdateConfigMapName(instance), command, args, corev1.RestartPolicyNever, ptr.To(int32(0)),
		ptr.To(int64(updateJobDeadlineSeconds)))
	container := &job.Spec.Template.Spec.Containers[0]
	container.Env = append(container.Env, corev1.EnvVar{
		Name: "SAAS_MODULES", Value: strings.Join(instance.Spec.Update.Modules, ","),
	})
	return job
}

// updateDatabases is the instance's own database followed by
// spec.update.databases, without duplicates.
func updateDatabases(instance *saasv1alpha1.OdooInstance) []string {
	dbs := []string{OdooDatabaseName(instance)}
	seen := map[string]bool{dbs[0]: true}
	for _, db := range instance.Spec.Update.Databases {
		if db != "" && !seen[db] {
			seen[db] = true
			dbs = append(dbs, db)
		}
	}
	return dbs
}
