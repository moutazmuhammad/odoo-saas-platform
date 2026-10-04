#!/bin/sh
# Build Job, step 3 (rootless BuildKit): build /files/Dockerfile against the
# prepared /workspace context, push IMAGE_REF, then print the result markers
# the control plane parses from this container's log.
set -eu
output="type=image,name=${IMAGE_REF},push=true"
if [ "${REGISTRY_INSECURE:-}" = "1" ]; then
  output="${output},registry.insecure=true"
fi
set --
if [ -n "${CACHE_IMAGE_REF:-}" ]; then
  set -- --import-cache "type=registry,ref=${CACHE_IMAGE_REF}"
fi
buildctl-daemonless.sh build "$@" \
  --frontend dockerfile.v0 \
  --local context=/workspace \
  --local dockerfile=/files \
  --opt filename=Dockerfile \
  --output "$output" \
  --export-cache type=inline \
  --metadata-file /workspace/meta/build.json
digest=$(grep -o '"containerimage.digest": *"[^"]*"' /workspace/meta/build.json | sed 's/.*"\(sha256:[^"]*\)"/\1/')
echo "SAAS_BUILD_DIGEST ${digest}"
echo "SAAS_BUILD_RESULT $(cat /workspace/meta/result.json)"
