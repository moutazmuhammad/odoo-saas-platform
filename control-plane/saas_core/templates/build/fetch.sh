#!/bin/sh
# Build Job, step 1 (git image): fetch each tenant repository at the exact
# commit (or branch head) into /workspace/addons/<dir>, record the commit
# actually checked out. Clone URLs carry the access token and come from
# the build Secret (REPO_URL_<i>), never from the ConfigMap or the logs.
set -eu
mkdir -p /workspace/addons /workspace/meta
chmod 0777 /workspace /workspace/meta
: > /workspace/meta/shas
i=0
while [ "$i" -lt "${REPO_COUNT:-0}" ]; do
  eval "url=\${REPO_URL_$i}"
  eval "ref=\${REPO_REF_$i}"
  eval "branch=\${REPO_BRANCH_$i}"
  eval "dir=\${REPO_DIR_$i}"
  dest="/workspace/addons/$dir"
  git init -q "$dest"
  cd "$dest"
  echo "fetching $dir @ ${ref:-$branch}"
  if ! git -c http.sslVerify=true fetch -q --depth 1 "$url" "$ref" 2>/dev/null; then
    # Not every server allows fetching a bare commit; fall back to the branch.
    git -c http.sslVerify=true fetch -q --depth 1 "$url" "$branch"
  fi
  git -c advice.detachedHead=false checkout -q FETCH_HEAD
  echo "$i $(git rev-parse HEAD)" >> /workspace/meta/shas
  rm -rf .git
  cd /
  i=$((i + 1))
done
