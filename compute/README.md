# Odoo SaaS Platform — Kubernetes Control Plane

A Kubernetes-native, declarative provisioning platform for running
multi-tenant Odoo SaaS instances. A SaaS control-plane API creates,
updates, and deletes one Kubernetes custom resource — `OdooInstance` — and
a controller reconciles it into a fully isolated tenant: namespace,
PostgreSQL, filestore, the Odoo workload, routing, TLS, backups, and
security policy. The API never creates a Deployment, Service, PVC, Secret,
or Job directly.

See **[docs/architecture.md](docs/architecture.md)** for the full design
rationale, diagrams, and every trade-off behind these decisions — it also
documents several real bugs found and fixed by testing this operator
against a live cluster while building it (a CRD schema bug, a subtle
Kubernetes-defaulting/Go-`omitempty` interaction, a missing scheme
registration, a pod security bug specific to the official Odoo image, a
missing database-init step, and a `WaitForFirstConsumer` storage
deadlock), rather than a from-scratch design that was only ever compiled.

## Repository layout

```text
.
├── operator/                  Go module: the OdooInstance CRD + controller
│   ├── api/v1alpha1/          CRD Go types, defaults, deepcopy
│   ├── cmd/                   Manager entrypoint (cmd/main.go)
│   ├── internal/resources/    Pure desired-state builders (unit-testable, no cluster)
│   ├── internal/controller/   Reconciliation loop (unit + envtest-tested)
│   ├── config/                Generated CRD/RBAC + a kustomize-based install path
│   ├── Dockerfile             Distroless, non-root operator image
│   └── Makefile                build / test / lint / docker-build / install / deploy / ...
├── charts/
│   ├── odoo-operator/         Install the operator itself (CRD + RBAC + Deployment)
│   └── odoo-instance/         Optional GitOps wrapper templating one OdooInstance
├── examples/                  Sample OdooInstance manifests (basic, production TLS,
│                              external database, multi-replica)
└── docs/architecture.md       Full architecture, diagrams, and design rationale
```

## Quick start

Prerequisites: a Kubernetes cluster, Helm 3, and (optionally) the Gateway
API CRDs + an implementation such as Traefik or Envoy Gateway installed —
or set `networking.provider=ingress` if you'd rather use a classic Ingress
controller.

```bash
# 1. Build and push the operator image (or use a published one).
cd operator
make docker-build IMG=registry.example.com/odoo-saas/operator:v0.1.0
make docker-push  IMG=registry.example.com/odoo-saas/operator:v0.1.0

# 2. Install the operator (CRD + RBAC + Deployment) via Helm.
helm upgrade --install odoo-operator ../charts/odoo-operator \
  --namespace odoo-system --create-namespace \
  --set image.repository=registry.example.com/odoo-saas/operator \
  --set image.tag=v0.1.0

# 3. Provision a tenant.
kubectl apply -f ../examples/odoo-instance-acme.yaml
kubectl get odooinstance customer-acme -w
```

```text
NAME             PHASE    VERSION   URL                                 READY
customer-acme    Ready    18.0      https://customer-acme.example.com   True
```

```bash
kubectl describe odooinstance customer-acme
```

## Provisioning / update / delete flow

```bash
kubectl apply -f examples/odoo-instance-acme.yaml       # create
kubectl get odooinstance customer-acme -o yaml           # status.phase, .conditions, .url
kubectl patch odooinstance customer-acme --type=merge \
  -p '{"spec":{"workers":{"count":4}}}'                  # update: triggers a rolling restart
kubectl delete odooinstance customer-acme                # delete: finalizer runs a final
                                                           # backup (if enabled), then tears
                                                           # down the tenant namespace
```

## Upgrading the operator

```bash
helm upgrade odoo-operator charts/odoo-operator \
  --namespace odoo-system \
  --set image.tag=v0.2.0
```

CRD schema changes (new optional fields) can be applied the same way;
Helm's `crds/` directory is not automatically upgraded by `helm upgrade`
on existing installs (a Helm limitation, not specific to this chart) — run
`kubectl apply -f charts/odoo-operator/crds/` alongside the chart upgrade
if a release changed the CRD.

## Uninstalling

```bash
helm uninstall odoo-operator --namespace odoo-system
# CRDs and any remaining OdooInstance objects (and everything the operator
# provisioned for them) are NOT removed by uninstalling the chart. Delete
# tenants first if you want their data gone:
kubectl get odooinstance -A
kubectl delete odooinstance --all
# Only then, if you want the CRD itself gone too:
kubectl delete -f charts/odoo-operator/crds/
```

## Local development

```bash
cd operator
make manifests generate fmt vet   # regenerate CRD/RBAC/deepcopy after editing api/v1alpha1
make test                         # unit tests + envtest-based controller tests
make run                          # run the manager locally against your current kubeconfig
```

`make test` downloads real `kube-apiserver`/`etcd` binaries via
`setup-envtest` and runs the controller against them — not a mock. See
`docs/architecture.md` for what envtest can and cannot exercise (no
kubelet/scheduler, so Pod/Deployment status has to be simulated in those
tests) versus what was additionally validated against a full live cluster.

## Testing philosophy

- `internal/resources` — plain Go unit tests, no cluster needed
  (`go test ./internal/resources/...`).
- `internal/controller` — `envtest`-backed: a real `kube-apiserver` +
  `etcd`, driving the actual `Reconcile` function.
- Beyond both of those, this operator was run against a real cluster
  (create → provision → serve traffic → delete) as part of building it,
  which is how the bugs listed in `docs/architecture.md`'s opening
  paragraph were found.
