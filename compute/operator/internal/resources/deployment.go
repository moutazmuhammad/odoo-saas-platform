package resources

import (
	"fmt"
	"strconv"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

const RoleWeb = "web"

// OdooCronDeploymentName identifies the retired cron deployment for cleanup.
func OdooCronDeploymentName(instance *saasv1alpha1.OdooInstance) string {
	return "odoo-cron"
}

// OdooDeployment builds one pod with separate web and smaller cron containers.
func OdooDeployment(instance *saasv1alpha1.OdooInstance) *appsv1.Deployment {
	name := OdooDeploymentName(instance)
	workers := instance.Spec.Workers.Count
	cronThreads := int32(0)
	exposesHTTP := true
	roleLabelVal := RoleWeb

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
			Replicas: ptr.To(int32(1)),
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
		// Odoo's own defaults (--limit-time-real=120s, --limit-time-cpu=60s)
		// kill a request — and its DB connection — mid-flight, so longs
		// pop-up reports/imports/batch ops always died with a dropped
		// connection. Give long-but-legitimate tenant jobs a 30-minute
		// ceiling instead of the surprise-killing defaults.
		"--limit-time-real=1800",
		"--limit-time-cpu=1800",
	}
	args = append(args, odooMemoryLimitArgs(instance, workers)...)
	odooMounts := []corev1.VolumeMount{
		{Name: "etc-odoo", MountPath: "/etc/odoo", ReadOnly: true},
		odooDataMount(instance, "/var/lib/odoo", false),
		{Name: "tmp", MountPath: "/tmp"},
	}
	var extraVolumes []corev1.Volume
	// On the command line rather than in odoo.conf, so the one-shot
	// init/update Jobs (which share odoo.conf) don't need the mount.
	// Always loaded — the platform addon gates Odoo's raw
	// /web/database/* endpoints, which must never be left open just
	// because an instance doesn't opt into the database manager.
	args = append(args,
		"--load=base,web,"+DatabaseManagerModule,
		"--addons-path="+strings.Join(append([]string{PlatformAddonsPath}, instance.Spec.AddonsPaths...), ","),
	)
	odooMounts = append(odooMounts, corev1.VolumeMount{Name: "platform-addons", MountPath: PlatformAddonsPath, ReadOnly: true})
	extraVolumes = append(extraVolumes, platformAddonsVolume(instance))
	var sidecars []corev1.Container
	if instance.Spec.Shell && exposesHTTP {
		sidecars = append(sidecars, shellContainer(instance))
		extraVolumes = append(extraVolumes, corev1.Volume{
			Name: "shell-tmp", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
		})
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

	pod := corev1.PodSpec{
		Affinity:                      filestoreCoLocation(instance, selector, true),
		ServiceAccountName:            OdooServiceAccountName(instance),
		AutomountServiceAccountToken:  ptr.To(false),
		TerminationGracePeriodSeconds: ptr.To(int64(60)),
		ImagePullSecrets:              pullSecrets,
		SecurityContext: &corev1.PodSecurityContext{
			RunAsNonRoot: ptr.To(true),
			SeccompProfile: &corev1.SeccompProfile{
				Type: corev1.SeccompProfileTypeRuntimeDefault,
			},
			FSGroup:             ptr.To(int64(odooImageGID)),
			FSGroupChangePolicy: ptr.To(corev1.FSGroupChangeOnRootMismatch),
		},
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
		Containers: append([]corev1.Container{
			{
				Name:            "odoo",
				Image:           fmt.Sprintf("%s:%s", instance.Spec.Image.Repository, instance.Spec.Image.Tag),
				ImagePullPolicy: instance.Spec.Image.PullPolicy,
				Args:            args,
				Ports:           ports,
				Resources:       instance.Spec.Resources,
				LivenessProbe:   odooLivenessProbe(exposesHTTP),
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
				VolumeMounts:    odooMounts,
				SecurityContext: containerSecurityContext(),
			},
		}, sidecars...),
		Volumes: append([]corev1.Volume{
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
		}, extraVolumes...),
	}
	if instance.Spec.Workers.MaxCronThreads > 0 {
		cron := pod.Containers[0].DeepCopy()
		cron.Name = "cron"
		cron.Resources = CronResources(instance)
		cron.Ports = nil
		cron.Lifecycle = nil
		// No readiness probe: the cron container serves no traffic, and a
		// failing cron must never mark the pod NotReady and drop web requests.
		cron.ReadinessProbe = nil
		cron.LivenessProbe = odooLivenessProbe(false)
		cron.StartupProbe = odooStartupProbe(false)
		cron.Args = nil
		for _, arg := range pod.Containers[0].Args {
			if strings.HasPrefix(arg, "--workers=") || strings.HasPrefix(arg, "--max-cron-threads=") || strings.HasPrefix(arg, "--limit-memory-") {
				continue
			}
			cron.Args = append(cron.Args, arg)
		}
		cron.Args = append(cron.Args, "--no-http", "--workers=0", fmt.Sprintf("--max-cron-threads=%d", instance.Spec.Workers.MaxCronThreads))
		pod.Containers = append(pod.Containers, *cron)
	}
	return pod

}

// shellContainer is the customer terminal's container (spec.shell): the
// Odoo image and filestore, but no odoo.conf and no secret env, so the
// master and database passwords stay out of reach. It idles until the
// control plane execs a shell into it, and exits promptly on SIGTERM.
func shellContainer(instance *saasv1alpha1.OdooInstance) corev1.Container {
	return corev1.Container{
		Name:            ShellContainerName,
		Image:           fmt.Sprintf("%s:%s", instance.Spec.Image.Repository, instance.Spec.Image.Tag),
		ImagePullPolicy: instance.Spec.Image.PullPolicy,
		Command:         []string{"sh", "-c", "trap 'exit 0' TERM; while :; do sleep 3600 & wait $!; done"},
		Env:             []corev1.EnvVar{{Name: "HOME", Value: "/var/lib/odoo"}},
		Resources: corev1.ResourceRequirements{
			Requests: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("10m"),
				corev1.ResourceMemory: resource.MustParse("32Mi"),
			},
			Limits: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("500m"),
				corev1.ResourceMemory: resource.MustParse("256Mi"),
			},
		},
		VolumeMounts: []corev1.VolumeMount{
			odooDataMount(instance, "/var/lib/odoo", false),
			{Name: "shell-tmp", MountPath: "/tmp"},
		},
		SecurityContext: containerSecurityContext(),
	}
}

// odooMemoryLimitArgs sets Odoo's per-process memory limits from the
// container's memory limit, so one runaway request gets its worker
// recycled (soft: after the request, hard: at once) long before the
// kernel OOM-kills the whole pod. Prefork mode (workers > 0) only; Odoo
// ignores these limits in threaded mode.
func odooMemoryLimitArgs(instance *saasv1alpha1.OdooInstance, workers int32) []string {
	mem, ok := instance.Spec.Resources.Limits[corev1.ResourceMemory]
	if workers <= 0 || !ok || mem.Value() <= 0 {
		return nil
	}
	limit := mem.Value()
	return []string{
		fmt.Sprintf("--limit-memory-soft=%d", limit*60/100),
		fmt.Sprintf("--limit-memory-hard=%d", limit*75/100),
	}
}

// filestoreCoLocation keeps pods that mount a ReadWriteOnce filestore on
// the node that already has it attached; otherwise a rolling update's surge
// pod (or an update Job) placed on another node can never attach the
// volume ("Multi-Attach error") and the rollout stalls. “required“ for
// the web Deployment: Kubernetes still places the first pod anywhere (a pod
// matching its own affinity term may start when no other pod matches).
// Preferred for one-shot Jobs, which must still run when no web pod exists
// (e.g. a stopped instance). Nil for ReadWriteMany.
func filestoreCoLocation(instance *saasv1alpha1.OdooInstance, webSelector map[string]string, required bool) *corev1.Affinity {
	if instance.Spec.Storage.SharedWithDatabase {
		return sharedDataCoLocation(instance)
	}
	if instance.Spec.Storage.Filestore.AccessMode == saasv1alpha1.FilestoreAccessModeRWX {
		return nil
	}
	term := corev1.PodAffinityTerm{
		LabelSelector: &metav1.LabelSelector{MatchLabels: webSelector},
		TopologyKey:   "kubernetes.io/hostname",
	}
	if required {
		return &corev1.Affinity{PodAffinity: &corev1.PodAffinity{
			RequiredDuringSchedulingIgnoredDuringExecution: []corev1.PodAffinityTerm{term},
		}}
	}
	return &corev1.Affinity{PodAffinity: &corev1.PodAffinity{
		PreferredDuringSchedulingIgnoredDuringExecution: []corev1.WeightedPodAffinityTerm{
			{Weight: 100, PodAffinityTerm: term},
		},
	}}
}

// WebSelectorLabels selects an instance's web pods.
func WebSelectorLabels(instance *saasv1alpha1.OdooInstance) map[string]string {
	return mergeLabels(SelectorLabels(instance), map[string]string{"saas.odoo.example.com/role": string(RoleWeb)})
}

// ShellContainerName is the spec.shell sidecar's container name.
const ShellContainerName = "shell"

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
		{Name: "DBM_KEY", ValueFrom: &corev1.EnvVarSource{
			SecretKeyRef: &corev1.SecretKeySelector{
				LocalObjectReference: corev1.LocalObjectReference{Name: adminSecret},
				Key:                  "dbmanager-key",
				Optional:             ptr.To(true),
			},
		}},
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

// AnnotationPlatformAddons records the platform addons' content hash so
// new addon code rolls the pods.
const AnnotationPlatformAddons = "saas.odoo.example.com/platform-addons"

// AnnotationPlatformAddons is always
// set — the platform addons are always mounted/loaded (they gate the
// raw /web/database/* endpoints regardless of the database manager
// spec), and their hash rolls pods whenever the embedded addon code
// changes.
func podTemplateAnnotations(instance *saasv1alpha1.OdooInstance) map[string]string {
	ann := map[string]string{AnnotationPlatformAddons: platformAddonsHash()}
	if len(instance.Spec.AddonsPaths) > 0 {
		ann[AnnotationAddonsPaths] = strings.Join(instance.Spec.AddonsPaths, ",")
	}
	if instance.Spec.DatabaseFilter != "" {
		ann[AnnotationDatabaseFilter] = instance.Spec.DatabaseFilter
	}
	if dbm := instance.Spec.DatabaseManager; dbm != nil {
		ann["saas.odoo.example.com/database-limit"] = strconv.FormatInt(int64(dbm.MaxDatabases), 10)
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
				Exec: &corev1.ExecAction{Command: []string{"python3", "-c", "import pathlib,sys; sys.exit(0 if b'odoo' in pathlib.Path('/proc/1/cmdline').read_bytes() else 1)"}},
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

// odooLivenessProbe allows a busy worker to finish within Odoo's request
// time limit before treating an unanswered HTTP probe as a stuck process.
// Readiness still removes an occupied pod from rotation promptly.
func odooLivenessProbe(exposesHTTP bool) *corev1.Probe {
	p := odooProbe(exposesHTTP)
	if exposesHTTP {
		p.FailureThreshold = livenessFailureThreshold
	}
	return p
}

// 121 x 15 seconds exceeds --limit-time-real=1800, including when every
// HTTP worker is occupied by a legitimate long request.
const livenessFailureThreshold = 121

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
