#!/bin/sh
# Fetch at most two repositories concurrently within the same build pod.
# Credentials come from the Secret and never appear in command output.
set -eu
mkdir -p /workspace/addons /workspace/meta
chmod 0777 /workspace /workspace/meta
fetch_repo() (
  index="$1"
  eval "url=\${REPO_URL_$index}"
  eval "ref=\${REPO_REF_$index}"
  eval "branch=\${REPO_BRANCH_$index}"
  eval "dir=\${REPO_DIR_$index}"
  dest="/workspace/addons/$dir"
  git init -q "$dest"
  cd "$dest"
  echo "fetching $dir @ ${ref:-$branch}"
  target=FETCH_HEAD
  if ! git -c http.sslVerify=true fetch -q --depth 1 "$url" "${ref:-$branch}" 2>/dev/null; then
    # Some git hosts refuse fetch-by-SHA: take the branch history instead and
    # check out the pinned commit from it (verified below).
    depth="--depth=1"
    if [ "${#ref}" -eq 40 ]; then
      depth=""
      target="$ref"
    fi
    if ! git -c http.sslVerify=true fetch -q $depth "$url" "$branch" 2>/dev/null; then
      echo "Repository fetch failed: $dir" >&2
      exit 1
    fi
  fi
  git -c advice.detachedHead=false checkout -q "$target" 2>/dev/null || {
    echo "Requested commit could not be fetched: $dir" >&2
    exit 1
  }
  sha=$(git rev-parse HEAD)
  # A pinned commit must never silently become a different branch head.
  if [ "${#ref}" -eq 40 ] && [ "$sha" != "$ref" ]; then
    echo "Requested commit could not be fetched: $dir" >&2
    exit 1
  fi
  echo "$index $sha" > "/workspace/meta/sha-$index"
  rm -rf .git
)
i=0
while [ "$i" -lt "${REPO_COUNT:-0}" ]; do
  pids=""
  count=0
  while [ "$count" -lt 2 ] && [ "$i" -lt "$REPO_COUNT" ]; do
    fetch_repo "$i" &
    pids="$pids $!"
    count=$((count + 1))
    i=$((i + 1))
  done
  failed=0
  for pid in $pids; do
    wait "$pid" || failed=1
  done
  [ "$failed" -eq 0 ] || exit 1
done
: > /workspace/meta/shas
i=0
while [ "$i" -lt "${REPO_COUNT:-0}" ]; do
  cat "/workspace/meta/sha-$i" >> /workspace/meta/shas
  rm "/workspace/meta/sha-$i"
  i=$((i + 1))
done
