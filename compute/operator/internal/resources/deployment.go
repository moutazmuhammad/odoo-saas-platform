package resources

import (
	"fmt"
	"strconv"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

// OdooRole distinguishes the two Deployments an instance can have. See
// docs/architecture.md ("Odoo Worker Model") for why cron execution is
// split into its own Deployment instead of being pinned to a Deployment
// replica ordinal (which Deployments, unlike StatefulSets, do not
// guarantee).
type OdooRole string

const (
	// RoleWeb serves HTTP traffic. Always created. Runs cron itself only
	// when the instance has a single replica (the common case).
	RoleWeb OdooRole = "web"
	// RoleCron runs only scheduled jobs, at exactly one replica, and is
	// only created when spec.Replicas > 1 (multi-replica web tier).
	RoleCron OdooRole = "cron"
)

// OdooCronDeploymentName is the name of the dedicated cron Deployment.
func OdooCronDeploymentName(instance *saasv1alpha1.OdooInstance) string {
	return "odoo-cron"
}

// NeedsCronDeployment reports whether this instance requires a separate
// cron Deployment (i.e. it runs more than one web replica).
func NeedsCronDeployment(instance *saasv1alpha1.OdooInstance) bool {
	return ptr.Deref(instance.Spec.Replicas, 1) > 1
}

// OdooDeployment builds the Deployment for the given role.
func OdooDeployment(instance *saasv1alpha1.OdooInstance, role OdooRole) *appsv1.Deployment {
	var (
		name         string
		replicas     int32
		workers      int32
		cronThreads  int32
		exposesHTTP  = true
		roleLabelVal = string(role)
	)

	switch role {
	case RoleWeb:
		name = OdooDeploymentName(instance)
		replicas = ptr.Deref(instance.Spec.Replicas, 1)
		workers = instance.Spec.Workers.Count
		if NeedsCronDeployment(instance) {
			cronThreads = 0 // cron runs exclusively in the dedicated cron Deployment
		} else {
			cronThreads = instance.Spec.Workers.MaxCronThreads
		}
	case RoleCron:
		name = OdooCronDeploymentName(instance)
		replicas = 1
		workers = 0
		cronThreads = instance.Spec.Workers.MaxCronThreads
		exposesHTTP = false
	}

	labels := mergeLabels(CommonLabels(instance), map[string]string{"saas.odoo.example.com/role": roleLabelVal})
	selector := mergeLabels(SelectorLabels(instance), map[string]string{"saas.odoo.example.com/role": roleLabelVal})

	podSpec := odooPodSpec(instance, selector, workers, cronThreads, exposesHTTP)

	return &appsv1.Deployment{
		TypeMeta: metav1.TypeMeta{APIVersion: "apps/v1", Kind: "Deployment"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: TenantNamespace(instance),
			Labels:    labels,
		},
		Spec: appsv1.DeploymentSpec{
			Replicas: ptr.To(replicas),
			Selector: &metav1.LabelSelector{MatchLabels: selector},
			Strategy: appsv1.DeploymentStrategy{
				// RollingUpdate with MaxUnavailable=0 avoids serving traffic
				// from zero ready pods during an image/config update; safe
				// even at replicas=1 because MaxSurge=1 brings up the new
				// pod before the old one terminates.
				Type: appsv1.RollingUpdateDeploymentStrategyType,
				RollingUpdate: &appsv1.RollingUpdateDeployment{
					MaxUnavailable: ptr.To(intstr.FromInt32(0)),
					MaxSurge:       ptr.To(intstr.FromInt32(1)),
				},
			},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels:      labels,
					Annotations: podTemplateAnnotations(instance),
				},
				Spec: podSpec,
			},
		},
	}
}

func odooPodSpec(instance *saasv1alpha1.OdooInstance, selector map[string]string, workers, cronThreads int32, exposesHTTP bool) corev1.PodSpec {
	args := []string{
		"-c", "/etc/odoo/odoo.conf",
		fmt.Sprintf("--workers=%d", workers),
		fmt.Sprintf("--max-cron-threads=%d", cronThreads),
	}

	var ports []corev1.ContainerPort
	if exposesHTTP {
		ports = append(ports,
			corev1.ContainerPort{Name: "http", ContainerPort: OdooHTTPPort, Protocol: corev1.ProtocolTCP},
		)
		if workers > 0 {
			ports = append(ports,
				corev1.ContainerPort{Name: "longpolling", ContainerPort: OdooLongpollingPort, Protocol: corev1.ProtocolTCP},
			)
		}
	}

	probe := odooProbe(exposesHTTP)

	pullSecrets := make([]corev1.LocalObjectReference, 0, len(instance.Spec.Image.PullSecretRefs))
	pullSecrets = append(pullSecrets, instance.Spec.Image.PullSecretRefs...)

	var topologySpread []corev1.TopologySpreadConstraint
	if ptr.Deref(instance.Spec.Replicas, 1) > 1 {
		topologySpread = []corev1.TopologySpreadConstraint{
			{
				MaxSkew:           1,
				TopologyKey:       "kubernetes.io/hostname",
				WhenUnsatisfiable: corev1.ScheduleAnyway,
				LabelSelector:     &metav1.LabelSelector{MatchLabels: selector},
			},
		}
	}

	return corev1.PodSpec{
		ServiceAccountName:            OdooServiceAccountName(instance),
		AutomountServiceAccountToken:  ptr.To(false),
		TerminationGracePeriodSeconds: ptr.To(int64(60)),
		ImagePullSecrets:              pullSecrets,
		SecurityContext: &corev1.PodSecurityContext{
			RunAsNonRoot: ptr.To(true),
			SeccompProfile: &corev1.SeccompProfile{
				Type: corev1.SeccompProfileTypeRuntimeDefault,
			},
			FSGroup: ptr.To(int64(odooImageGID)),
		},
		TopologySpreadConstraints: topologySpread,
		InitContainers: []corev1.Container{
			{
				Name:            "render-config",
				Image:           fmt.Sprintf("%s:%s", instance.Spec.Image.Repository, instance.Spec.Image.Tag),
				ImagePullPolicy: instance.Spec.Image.PullPolicy,
				Command:         []string{"sh", "-c", renderInitContainerScript()},
				Env:             odooConfigEnvVars(instance),
				VolumeMounts: []corev1.VolumeMount{
					{Name: "config-template", MountPath: "/template", ReadOnly: true},
					{Name: "etc-odoo", MountPath: "/etc/odoo"},
				},
				SecurityContext: containerSecurityContext(),
			},
		},
		Containers: []corev1.Container{
			{
				Name:            "odoo",
				Image:           fmt.Sprintf("%s:%s", instance.Spec.Image.Repository, instance.Spec.Image.Tag),
				ImagePullPolicy: instance.Spec.Image.PullPolicy,
				Args:            args,
				Ports:           ports,
				Resources:       instance.Spec.Resources,
				LivenessProbe:   probe,
				ReadinessProbe:  probe,
				StartupProbe:    odooStartupProbe(exposesHTTP),
				// Zero-downtime rollouts: keep serving for a few seconds after
				// the pod is marked terminating, so Service endpoints and the
				// ingress controller stop routing to it before Odoo receives
				// SIGTERM. Without this, a rolling update (resize, restart,
				// image bump) drops the requests that land in that window.
				Lifecycle: &corev1.Lifecycle{
					PreStop: &corev1.LifecycleHandler{
						Exec: &corev1.ExecAction{Command: []string{"sleep", strconv.Itoa(preStopDelaySeconds)}},
					},
				},
				VolumeMounts: []corev1.VolumeMount{
					{Name: "etc-odoo", MountPath: "/etc/odoo", ReadOnly: true},
					{Name: "filestore", MountPath: "/var/lib/odoo"},
					{Name: "tmp", MountPath: "/tmp"},
				},
				SecurityContext: containerSecurityContext(),
			},
		},
		Volumes: []corev1.Volume{
			{
				Name: "config-template",
				VolumeSource: corev1.VolumeSource{
					ConfigMap: &corev1.ConfigMapVolumeSource{
						LocalObjectReference: corev1.LocalObjectReference{Name: OdooConfigMapName(instance)},
					},
				},
			},
			{
				Name:         "etc-odoo",
				VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
			},
			{
				Name: "filestore",
				VolumeSource: corev1.VolumeSource{
					PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
						ClaimName: FilestorePVCName(instance),
					},
				},
			},
			{
				Name:         "tmp",
				VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
			},
		},
	}
}

// odooConfigEnvVars sources secret values only into the init container that
// renders odoo.conf; the main Odoo container never receives them as
// environment variables, limiting where plaintext credentials are exposed
// within the pod.
func odooConfigEnvVars(instance *saasv1alpha1.OdooInstance) []corev1.EnvVar {
	dbSecret := DatabaseSecretName(instance)
	adminSecret := AdminSecretName(instance)
	envFrom := func(secret, key string) *corev1.EnvVarSource {
		return &corev1.EnvVarSource{
			SecretKeyRef: &corev1.SecretKeySelector{
				LocalObjectReference: corev1.LocalObjectReference{Name: secret},
				Key:                  key,
			},
		}
	}
	return []corev1.EnvVar{
		{Name: "ADMIN_PASSWORD", ValueFrom: envFrom(adminSecret, "master-password")},
		{Name: "DB_HOST", ValueFrom: envFrom(dbSecret, "host")},
		{Name: "DB_PORT", ValueFrom: envFrom(dbSecret, "port")},
		{Name: "DB_USER", ValueFrom: envFrom(dbSecret, "username")},
		{Name: "DB_PASSWORD", ValueFrom: envFrom(dbSecret, "password")},
		{Name: "DB_NAME", ValueFrom: envFrom(dbSecret, "dbname")},
	}
}

// containerSecurityContext returns the hardened container securityContext
// shared by init and main containers: no privilege escalation, all Linux
// capabilities dropped, read-only root filesystem (every writable path is
// an explicit volume mount), and non-root execution. This matches the
// official Odoo image, which already runs as a non-root `odoo` user.
func containerSecurityContext() *corev1.SecurityContext {
	return &corev1.SecurityContext{
		AllowPrivilegeEscalation: ptr.To(false),
		ReadOnlyRootFilesystem:   ptr.To(true),
		RunAsNonRoot:             ptr.To(true),
		// The official Odoo image's Dockerfile declares `USER odoo` (a
		// name, not a numeric UID), so without an explicit numeric
		// RunAsUser here the kubelet cannot statically verify the image
		// runs as non-root and refuses to start the container at all
		// (observed directly: "container has runAsNonRoot and image has
		// non-numeric user (odoo), cannot verify user is non-root").
		// uid 100 / gid 101 is this image's actual `odoo` account,
		// confirmed by running `id` in the image; it matches the FSGroup
		// set on the pod's SecurityContext below so the filestore PVC is
		// group-writable by this user.
		//
		// This function is intentionally only used for Odoo's own
		// containers (init and main, both running Spec.Image). A
		// different platform-built image, such as the backup tool
		// (backup.go), must NOT reuse this function — it almost certainly
		// runs under a different account, and hardcoding this image's UID
		// onto an unrelated container would either break it outright or,
		// worse, silently run it as the wrong (possibly privileged) user.
		// See genericHardenedSecurityContext for that case.
		RunAsUser:  ptr.To(int64(odooImageUID)),
		RunAsGroup: ptr.To(int64(odooImageGID)),
		Capabilities: &corev1.Capabilities{
			Drop: []corev1.Capability{"ALL"},
		},
	}
}

// genericHardenedSecurityContext is used for platform-built containers
// (currently just the backup tool, backup.go) whose image this codebase
// does not control the exact UID/GID of. It still requires non-root
// execution, but — unlike containerSecurityContext — does not pin a
// specific numeric RunAsUser, which means the CONTRACT for any image used
// here is: its Dockerfile must declare a numeric `USER <uid>[:<gid>]`, not
// a named user, or the kubelet will refuse to start the container for the
// same reason documented on containerSecurityContext above.
func genericHardenedSecurityContext() *corev1.SecurityContext {
	return &corev1.SecurityContext{
		AllowPrivilegeEscalation: ptr.To(false),
		ReadOnlyRootFilesystem:   ptr.To(true),
		RunAsNonRoot:             ptr.To(true),
		Capabilities: &corev1.Capabilities{
			Drop: []corev1.Capability{"ALL"},
		},
	}
}

// odooImageUID and odooImageGID are the official Odoo image's `odoo`
// account, verified with `docker run --rm <image> id`. A custom platform
// image build MUST keep this same uid:gid (or this constant must be
// updated to match) since Kubernetes has no way to discover it from the
// image at admission time when the Dockerfile's USER directive is a name.
const (
	odooImageUID = 100
	odooImageGID = 101
)

// odooProbe returns the liveness/readiness probe. Odoo does not ship a
// dedicated health-check endpoint across all supported versions, so
// /web/login (always reachable once the HTTP worker pool is serving,
// requires no auth) is used as a pragmatic stand-in. Instances whose image
// bundles a custom health-check addon can override this by adding an addon
// that responds on /web/login faster/cheaper; changing the probe path
// itself would require a new API field (deliberately not added yet, to
// avoid over-engineering the v1alpha1 surface).
// AnnotationAddonsPaths records spec.addonsPaths on the pod template: a
// change rolls the pods (odoo.conf is only read at pod start), and the
// controller reads it back to know which addons paths the live pods run
// with while an update is pending (see LiveAddonsPaths).
const AnnotationAddonsPaths = "saas.odoo.example.com/addons-paths"

// AnnotationDatabaseFilter records spec.databaseFilter on the pod template
// so changing it rolls the pods onto the new odoo.conf.
const AnnotationDatabaseFilter = "saas.odoo.example.com/database-filter"

// podTemplateAnnotations is nil without addons paths or a database filter,
// so instances that don't use them keep an unchanged pod template (no
// spurious rollout).
func podTemplateAnnotations(instance *saasv1alpha1.OdooInstance) map[string]string {
	var ann map[string]string
	if len(instance.Spec.AddonsPaths) > 0 {
		ann = map[string]string{AnnotationAddonsPaths: strings.Join(instance.Spec.AddonsPaths, ",")}
	}
	if instance.Spec.DatabaseFilter != "" {
		if ann == nil {
			ann = map[string]string{}
		}
		ann[AnnotationDatabaseFilter] = instance.Spec.DatabaseFilter
	}
	return ann
}

// LiveAddonsPaths is the inverse of podTemplateAnnotations for a live
// Deployment's pod template annotations.
func LiveAddonsPaths(annotations map[string]string) []string {
	v := annotations[AnnotationAddonsPaths]
	if v == "" {
		return nil
	}
	return strings.Split(v, ",")
}

// preStopDelaySeconds must stay well below TerminationGracePeriodSeconds
// (60) so Odoo still gets time to shut down cleanly after the delay.
const preStopDelaySeconds = 15

func odooProbe(exposesHTTP bool) *corev1.Probe {
	if !exposesHTTP {
		return &corev1.Probe{
			ProbeHandler: corev1.ProbeHandler{
				Exec: &corev1.ExecAction{Command: []string{"pgrep", "-f", "odoo"}},
			},
			InitialDelaySeconds: 10,
			PeriodSeconds:       30,
			FailureThreshold:    3,
		}
	}
	return &corev1.Probe{
		ProbeHandler: corev1.ProbeHandler{
			HTTPGet: &corev1.HTTPGetAction{
				Path: "/web/login",
				Port: intstr.FromInt32(OdooHTTPPort),
			},
		},
		InitialDelaySeconds: 10,
		PeriodSeconds:       15,
		TimeoutSeconds:      5,
		FailureThreshold:    3,
	}
}

// odooStartupProbe allows generously for first-boot database
// initialization (Odoo creates/migrates schema on first connection to an
// empty database) before liveness/readiness probes start being enforced.
func odooStartupProbe(exposesHTTP bool) *corev1.Probe {
	p := odooProbe(exposesHTTP)
	p.PeriodSeconds = 10
	p.FailureThreshold = 30 // up to 5 minutes
	p.InitialDelaySeconds = 5
	return p
}
