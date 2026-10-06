package resources

import (
	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
)

// CronResources isolates scheduled jobs from HTTP workers in the same pod.
// Keep this sizing rule aligned with saas.plan._package cost/quota accounting.
func CronResources(instance *saasv1alpha1.OdooInstance) corev1.ResourceRequirements {
	cpu := instance.Spec.Resources.Limits[corev1.ResourceCPU]
	memory := instance.Spec.Resources.Limits[corev1.ResourceMemory]
	cpuM, memoryMi := cpu.MilliValue(), memory.Value()/(1024*1024)
	if cpuM <= 0 {
		cpuM = 1000
	}
	if memoryMi <= 0 {
		memoryMi = 2048
	}
	cpuM = max(int64(1), min(cpuM/4, int64(500)))
	memoryMi = max(int64(1), min(memoryMi/4, int64(512)))
	return corev1.ResourceRequirements{
		Limits: corev1.ResourceList{
			corev1.ResourceCPU:    *resource.NewMilliQuantity(cpuM, resource.DecimalSI),
			corev1.ResourceMemory: *resource.NewQuantity(memoryMi*1024*1024, resource.BinarySI),
		},
		Requests: corev1.ResourceList{
			corev1.ResourceCPU:    *resource.NewMilliQuantity(min(cpuM, max(int64(10), cpuM/4)), resource.DecimalSI),
			corev1.ResourceMemory: *resource.NewQuantity(min(memoryMi, max(int64(32), memoryMi/4))*1024*1024, resource.BinarySI),
		},
	}
}
