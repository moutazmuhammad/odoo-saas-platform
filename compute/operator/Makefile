# Image URL to use for building/pushing the operator image.
IMG ?= docker.io/moutazmuhammad/odoo-saas-operator:0.1.5
# Odoo version(s) the platform image pipeline has published, used only by
# the example manifests under examples/.
KUBEBUILDER_ASSETS ?= $(shell $(LOCALBIN)/setup-envtest use $(ENVTEST_K8S_VERSION) -p path)
ENVTEST_K8S_VERSION = 1.29.0

LOCALBIN ?= $(shell pwd)/bin
$(LOCALBIN):
	mkdir -p $(LOCALBIN)

CONTROLLER_GEN ?= $(LOCALBIN)/controller-gen
SETUP_ENVTEST ?= $(LOCALBIN)/setup-envtest
CONTROLLER_GEN_VERSION ?= latest

.PHONY: all
all: build

##@ Development

.PHONY: manifests
manifests: controller-gen ## Generate CRD YAML and RBAC ClusterRole from Go markers.
	$(CONTROLLER_GEN) crd paths="./api/..." output:crd:artifacts:config=config/crd/bases
	$(CONTROLLER_GEN) rbac:roleName=odoo-instance-operator paths="./..." output:rbac:artifacts:config=config/rbac
	cp config/crd/bases/*.yaml ../charts/odoo-operator/crds/
	@echo "NOTE: if config/rbac/role.yaml's rule list changed, hand-sync it into"
	@echo "      charts/odoo-operator/templates/clusterrole.yaml (kept as a real"
	@echo "      Helm template, not a generated copy, so it can carry release"
	@echo "      labels/naming — see that file's header comment)."

.PHONY: generate
generate: controller-gen ## Generate DeepCopy/DeepCopyObject methods.
	$(CONTROLLER_GEN) object:headerFile="" paths="./api/..."

.PHONY: fmt
fmt: ## Run gofmt against code.
	gofmt -l -w .

.PHONY: vet
vet: ## Run go vet against code.
	go vet ./...

.PHONY: lint
lint: fmt vet ## Run all static checks.

.PHONY: test
test: manifests generate fmt vet setup-envtest ## Run unit + envtest suites.
	KUBEBUILDER_ASSETS="$(shell $(SETUP_ENVTEST) use $(ENVTEST_K8S_VERSION) -p path)" go test ./... -race -coverprofile cover.out

.PHONY: test-unit
test-unit: ## Run only the fast, no-cluster-needed unit tests (internal/resources).
	go test ./internal/resources/... -v

##@ Build

.PHONY: build
build: manifests generate fmt vet ## Build the manager binary.
	CGO_ENABLED=0 go build -o bin/manager ./cmd

.PHONY: run
run: manifests generate fmt vet ## Run the manager locally against the current kubeconfig context.
	go run ./cmd

.PHONY: docker-build
docker-build: ## Build the operator image.
	docker build -t ${IMG} .

.PHONY: docker-push
docker-push: ## Push the operator image.
	docker push ${IMG}

##@ Deployment

.PHONY: install
install: manifests ## Install CRDs into the cluster pointed at by the current kubeconfig context.
	kubectl apply -f config/crd/bases

.PHONY: uninstall
uninstall: manifests ## Remove CRDs (and, by cascade, every OdooInstance) from the cluster.
	kubectl delete -f config/crd/bases --ignore-not-found

.PHONY: deploy
deploy: manifests ## Install the operator (CRDs + RBAC + Deployment) via its Helm chart.
	helm upgrade --install odoo-operator charts/odoo-operator \
		--namespace odoo-system --create-namespace \
		--set image.repository=$(shell echo ${IMG} | cut -d: -f1) \
		--set image.tag=$(shell echo ${IMG} | cut -d: -f2)

.PHONY: undeploy
undeploy: ## Uninstall the operator Helm release (leaves CRDs and tenant data in place; see docs/architecture.md "Uninstall").
	helm uninstall odoo-operator --namespace odoo-system

##@ Tooling

.PHONY: controller-gen
controller-gen: $(LOCALBIN) ## Install controller-gen locally if not already present.
	test -s $(CONTROLLER_GEN) || GOBIN=$(LOCALBIN) go install sigs.k8s.io/controller-tools/cmd/controller-gen@$(CONTROLLER_GEN_VERSION)

.PHONY: setup-envtest
setup-envtest: $(LOCALBIN) ## Install setup-envtest locally if not already present.
	test -s $(SETUP_ENVTEST) || GOBIN=$(LOCALBIN) go install sigs.k8s.io/controller-runtime/tools/setup-envtest@latest

##@ Help

.PHONY: help
help: ## Display this help.
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n"} /^[a-zA-Z_0-9-]+:.*?##/ { printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)
