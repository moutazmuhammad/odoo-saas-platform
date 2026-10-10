#!/usr/bin/env bash
# Install root-owned as /usr/local/sbin/saas-deploy; do not execute a script
# uploaded by the SSH user as root. The incoming archive is extracted as odoo.
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run via sudo.' >&2; exit 1; }
[[ $# -eq 1 && $1 =~ ^[0-9]+-[0-9]+-[a-f0-9]{40}$ ]] || { echo 'Expected RUN-ATTEMPT-SHA release ID.' >&2; exit 1; }
# shellcheck source=/dev/null
source /etc/saas-deploy.conf
: "${SAAS_DB:?}" "${SAAS_HEALTH_URL:?}"
[[ $SAAS_DB =~ ^[a-zA-Z0-9_]+$ ]] || exit 1
exec 9>/run/lock/saas-deploy.lock
flock -n 9 || { echo 'Another deployment is running.' >&2; exit 1; }
release_id=$1
root=/opt/saas
release=$root/releases/$release_id
archive=/var/lib/saas-deploy/incoming/$release_id.tar.gz
[[ -f $archive && ! -e $release ]] || { echo 'Archive missing or release already exists.' >&2; exit 1; }
install -d -o odoo -g odoo "$release/platform"
runuser -u odoo -- tar -xzf "$archive" -C "$release/platform" --no-same-owner --no-same-permissions
[[ -f $release/platform/control-plane/saas_core/__manifest__.py ]]
[[ -f $release/platform/control-plane/saas_website/static/spa/index.html ]]
runuser -u odoo -- python3.12 -m venv "$release/venv"
runuser -u odoo -- "$release/venv/bin/pip" install -r "$root/odoo18/requirements.txt"
runuser -u odoo -- "$release/venv/bin/pip" install -r "$release/platform/control-plane/requirements.txt"
backup=/var/backups/saas/$release_id
install -d -m 700 "$backup"
# Fail closed once maintenance begins: schema changes are not safely reverted
# just by pointing the code symlink at an old release.
trap 'echo "Deployment failed. Inspect services and logs. Backup: $backup. Prepared release: $release" >&2' ERR
systemctl stop saas-jobs
systemctl stop saas-odoo
runuser -u postgres -- pg_dump -Fc "$SAAS_DB" > "$backup/database.dump"
tar -czf "$backup/filestore.tar.gz" -C "$root/data" "filestore/$SAAS_DB"
cp -a /etc/odoo/saas.conf "$backup/saas.conf"
readlink -f "$root/platform" > "$backup/previous-platform"
readlink -f "$root/venv" > "$backup/previous-venv"
runuser -u odoo -- "$release/venv/bin/python" "$root/odoo18/odoo-bin" \
  -c /etc/odoo/saas.conf -d "$SAAS_DB" \
  --addons-path="$root/odoo18/addons,$release/platform/control-plane" \
  -u saas_core,saas_billing,saas_website,saas_iam --stop-after-init --no-http
# Initial native installations have real directories, later releases symlinks.
for item in platform venv; do
  if [[ ! -L $root/$item ]]; then
    mv "$root/$item" "$root/$item.pre-cicd-$release_id"
  fi
  ln -s "$release/$item" "$root/.$item.next"
  mv -Tf "$root/.$item.next" "$root/$item"
done
systemctl start saas-odoo saas-jobs
systemctl is-active --quiet saas-odoo saas-jobs
curl --fail --silent --show-error --retry 30 --retry-delay 5 \
  --retry-all-errors --max-time 10 "$SAAS_HEALTH_URL" >/dev/null
printf '%s\n' "$release_id" > "$root/current-release"
rm -f "$archive"
echo "Deployed $release_id; backup: $backup"
