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

// OdooUpdateJob runs `odoo -u <modules> --stop-after-init` with the new
// spec.image and spec.addonsPaths (via OdooUpdateConfigMap) against the
// tenant database, before any serving pod is switched to that image. It is
// not retried: a failed migration needs a fix and a new token, not a loop.
func OdooUpdateJob(instance *saasv1alpha1.OdooInstance) *batchv1.Job {
	args := []string{
		"-c", "/etc/odoo/odoo.conf",
		"-d", OdooDatabaseName(instance),
		"-u", strings.Join(instance.Spec.Update.Modules, ","),
		"--workers=0",
		"--max-cron-threads=0",
		"--no-http",
		"--stop-after-init",
	}
	return odooOneShotJob(instance, OdooUpdateJobName(instance), UpdateJobComponent, "update-modules",
		OdooUpdateConfigMapName(instance), args, corev1.RestartPolicyNever, ptr.To(int32(0)),
		ptr.To(int64(updateJobDeadlineSeconds)))
}
