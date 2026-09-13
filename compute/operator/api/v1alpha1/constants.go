package v1alpha1

const (
	// Finalizer is set on every OdooInstance so the controller can run
	// cleanup (final backup trigger, tenant namespace teardown ordering)
	// before Kubernetes garbage-collects the tenant namespace and its
	// contents.
	Finalizer = "saas.odoo.example.com/finalizer"

	// LabelInstanceName is set on every resource owned by an OdooInstance.
	LabelInstanceName = "saas.odoo.example.com/instance"
	// LabelManagedBy identifies resources managed by this operator.
	LabelManagedBy = "app.kubernetes.io/managed-by"
	// LabelComponent classifies a child resource's role, e.g. "odoo",
	// "database", "backup".
	LabelComponent = "app.kubernetes.io/component"

	// ManagedByValue is the value written into LabelManagedBy.
	ManagedByValue = "odoo-instance-operator"

	// NamespacePrefix prefixes the derived tenant namespace name.
	NamespacePrefix = "odoo-tenant-"
)
