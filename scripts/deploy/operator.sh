#!/usr/bin/env bash
set -Eeuo pipefail
: "${KUBECONFIG:?}" "${OPERATOR_VALUES_FILE:?}"
namespace=${OPERATOR_NAMESPACE:-odoo-system}
release=${OPERATOR_RELEASE:-odoo-operator}
chart=compute/charts/odoo-operator
args=()
if [[ -n ${OPERATOR_IMAGE:-} ]]; then
  args+=(--set-string "image.repository=${OPERATOR_IMAGE%:*}" --set-string "image.tag=${OPERATOR_IMAGE##*:}")
fi
if [[ -n ${BACKUP_IMAGE:-} ]]; then
  args+=(--set-string "images.backupTool=$BACKUP_IMAGE" --set-string "backup.toolImage=$BACKUP_IMAGE" --set-string "restore.toolImage=$BACKUP_IMAGE")
fi
helm lint "$chart" -f "$OPERATOR_VALUES_FILE" "${args[@]}"
# Helm does not upgrade CRDs in crds/. Do this explicitly, without taking
# ownership from another field manager using --force-conflicts.
kubectl apply --server-side --field-manager=saas-cicd -f "$chart/crds/"
helm upgrade --install "$release" "$chart" \
  --namespace "$namespace" --create-namespace \
  --reset-then-reuse-values -f "$OPERATOR_VALUES_FILE" "${args[@]}" \
  --atomic --wait --timeout 10m --history-max 20
kubectl -n "$namespace" rollout status "deployment/$release" --timeout=300s
