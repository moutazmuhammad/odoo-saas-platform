#!/usr/bin/env bash
# C3.7 crash-safety run, on the SaaS TEST server as root:
#
#   run.sh <control-plane dir> [<harness dir>]
#
# Installs the addons into a throwaway database, queues a paid deploy, a paid
# plan change, a suspend and a cancel, runs them on a separate harness worker
# (worker.py, faked cluster effects), SIGKILLs that worker while all four are
# mid-operation, lets the reaper requeue them, restarts the worker and checks
# that every operation completed exactly once (verify.py). The database is
# dropped afterwards unless KEEP=1.
set -euo pipefail

SRC=$(realpath "${1:?usage: run.sh <control-plane dir> [<harness dir>]}")
HERE=$(realpath "${2:-$(dirname "$0")}")
DB=${DB:-saas_tmp_c37}
WORK=${WORK:-/tmp/saas-crash}
PY=/opt/saas/venv/bin/python
ODOO=/opt/saas/odoo18/odoo-bin
CONF=/etc/odoo/saas.conf
ADDONS=/opt/saas/odoo18/addons,$SRC
[[ $DB == saas_tmp_* ]] || { echo "DB must be saas_tmp_*" >&2; exit 2; }
export LC_ALL=C.UTF-8

odoo_args=(-c "$CONF" --addons-path="$ADDONS" -d "$DB" --db-filter="^$DB\$")
psql_db() { sudo -u postgres psql -d "$DB" -tAc "$1"; }
odoo_shell() {
    sudo -u odoo env EFFECT_LOG="$WORK/effects.log" \
        "$PY" "$ODOO" shell "${odoo_args[@]}" --no-http --logfile="$WORK/shell.log" < "$1"
}
# Anchored on the interpreter so the sudo wrapper never matches.
worker_re() { echo "^$PY $HERE/worker.py .*-d $DB"; }
worker_pid() { pgrep -f "$(worker_re)" | head -1; }
start_worker() {
    sudo -u odoo env PYTHONPATH="$(dirname "$ODOO")" \
        "$PY" "$HERE/worker.py" "${odoo_args[@]}" --logfile="$WORK/$1.log" \
        --block-dir "$WORK" --effect-log "$WORK/effects.log" &
    for _ in $(seq 60); do [[ -n $(worker_pid) ]] && break; sleep 1; done
    [[ -n $(worker_pid) ]] || { echo "worker did not start" >&2; exit 1; }
}
wait_for() {  # wait_for <seconds> <description> <command...>
    local limit=$1 what=$2; shift 2
    for _ in $(seq "$limit"); do "$@" && return 0; sleep 1; done
    echo "TIMEOUT waiting for $what" >&2; return 1
}
four_blocked() { [[ $(grep -c ' blocked ' "$WORK/effects.log" 2>/dev/null) -ge 4 ]]; }
queue_drained() { [[ $(psql_db "select count(*) from saas_job where state in ('pending','running')") == 0 ]]; }
cleanup() {
    pkill -9 -f "$(worker_re)" 2>/dev/null || true
    [[ ${KEEP:-0} == 1 ]] || sudo -u postgres dropdb --if-exists "$DB"
}
trap cleanup EXIT

sudo -u postgres dropdb --if-exists "$DB"
rm -rf "$WORK" && mkdir -p "$WORK" && chown odoo "$WORK"

echo "== install addons into $DB"
sudo -u odoo "$PY" "$ODOO" "${odoo_args[@]}" -i saas_core,saas_billing,saas_website,saas_iam \
    --stop-after-init --http-port=18999 --gevent-port=18998 --workers=0 --max-cron-threads=0 \
    --logfile="$WORK/install.log"

echo "== queue the four operations"
for effect in deploy resize stop destroy; do sudo -u odoo touch "$WORK/block-$effect"; done
odoo_shell "$HERE/setup.py"

echo "== run them; SIGKILL the worker once all four are mid-operation"
start_worker worker1
wait_for 300 "four blocked operations" four_blocked
grep ' blocked ' "$WORK/effects.log"
pid=$(worker_pid)
kill -9 "$pid"
wait_for 30 "worker exit" bash -c "! kill -0 $pid 2>/dev/null"
echo "killed worker $pid; state after the kill:"
psql_db "select method, state, attempts from saas_job order by id"
psql_db "select subdomain, state, pending_operation from saas_instance where subdomain like 'crash-%' order by id"

echo "== restart: the worker has been dead past the heartbeat window; reap, restart"
rm -f "$WORK"/block-*
psql_db "update saas_job set last_heartbeat = (now() at time zone 'utc') - interval '1 hour' where state = 'running'"
echo "env['saas.job']._cron_reap_jobs(); env.cr.commit()" > "$WORK/reap.py"
odoo_shell "$WORK/reap.py"
start_worker worker2
wait_for 300 "the queue to drain" queue_drained
kill -TERM "$(worker_pid)"
wait_for 60 "worker stop" bash -c "[[ -z \$(pgrep -f '$(worker_re)') ]]"
psql_db "select method, state, attempts from saas_job order by id"

echo "== verify"
odoo_shell "$HERE/verify.py" | tee "$WORK/verify.out"
grep -q '^RESULT PASS' "$WORK/verify.out"
