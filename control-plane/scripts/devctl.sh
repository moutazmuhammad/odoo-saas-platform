#!/usr/bin/env bash
# Local dev control for the SaaS control-plane (Odoo 18 + userspace PostgreSQL).
# Usage: scripts/devctl.sh {up|down|status|logs|otp|seed|shell|reset|test|crons-off|crons-on|cron}
#
# Paths are auto-derived from the repo location and overridable via env vars,
# so this works on any checkout without editing. Layout expected (siblings of
# the repo, all overridable):
#   <base>/odoo18        Odoo 18 source        (SAAS_ODOO_SRC)
#   <base>/odoo18-venv   Python venv           (SAAS_VENV)
#   <base>/saas-odoo     odoo.conf + data/log  (SAAS_RUNTIME)
#   <base>/saas-pg       userspace PG cluster  (SAAS_PGROOT)
# where <base> defaults to the repo's parent directory (SAAS_DEV_BASE).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="${SAAS_DEV_BASE:-$(dirname "$REPO")}"
VENV="${SAAS_VENV:-$BASE/odoo18-venv}"
ODOO="${SAAS_ODOO_SRC:-$BASE/odoo18}"
RUNTIME="${SAAS_RUNTIME:-$BASE/saas-odoo}"
CONF="${SAAS_CONF:-$RUNTIME/odoo.conf}"
LOGDIR="$RUNTIME/log"
PGROOT="${SAAS_PGROOT:-$BASE/saas-pg}"
PGDATA="$PGROOT/data"
PGBIN="${SAAS_PGBIN:-/usr/lib/postgresql/14/bin}"
PGPORT="${SAAS_PGPORT:-5455}"
DB="${SAAS_DB:-saas_dev}"

pg_up() {
  if ! (echo > /dev/tcp/127.0.0.1/$PGPORT) 2>/dev/null; then
    "$PGBIN/pg_ctl" -D "$PGDATA" -l "$PGROOT/server.log" \
      -o "-p $PGPORT -k $PGROOT/sock -c listen_addresses=127.0.0.1" start
    sleep 2
  else echo "postgres already up (:$PGPORT)"; fi
}
pg_down() {
  (echo > /dev/tcp/127.0.0.1/$PGPORT) 2>/dev/null && \
    "$PGBIN/pg_ctl" -D "$PGDATA" stop || echo "postgres not running"
}
PIDFILE=$LOGDIR/odoo.pid
odoo_up() {
  if (echo > /dev/tcp/127.0.0.1/8069) 2>/dev/null; then echo "odoo already up (:8069)"; return; fi
  cd "$ODOO"
  nohup "$VENV/bin/python" odoo-bin -c "$CONF" > "$LOGDIR/server.out" 2>&1 &
  echo $! > "$PIDFILE"
  echo "odoo starting pid $! -> http://127.0.0.1:8069"
}
odoo_down() {
  # Kill only the server we started (pidfile), never a test run sharing the conf.
  if [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null; then
    rm -f "$PIDFILE"; echo "odoo stopped"
  else echo "odoo not running (no pidfile)"; fi
}

case "${1:-}" in
  up)     pg_up; odoo_up; sleep 6; echo "--- http://127.0.0.1:8069 ---" ;;
  down)   odoo_down; pg_down ;;
  status)
    (echo > /dev/tcp/127.0.0.1/$PGPORT) 2>/dev/null && echo "postgres :$PGPORT UP" || echo "postgres DOWN"
    (echo > /dev/tcp/127.0.0.1/8069) 2>/dev/null && echo "odoo :8069 UP" || echo "odoo DOWN" ;;
  logs)   tail -f "$LOGDIR/odoo.log" ;;
  otp)    # print the most recent registration code from the log, so you can
          # complete sign-up in the browser (codes are delivered out-of-band; in
          # local dev the phone OTP lands in the log ending with the 6-digit code).
    line=$(grep -hE "phone OTP for|code for" "$LOGDIR/odoo.log" 2>/dev/null | tail -1)
    if [ -n "$line" ]; then echo "$line" | grep -oE "[0-9]{6}" | tail -1
    else echo "no recent code — start a registration first, then run: devctl otp"; fi ;;
  seed)   pg_up; cd "$ODOO"; "$VENV/bin/python" odoo-bin shell -c "$CONF" -d "$DB" --no-http --log-level=warn < "$REPO/scripts/seed_dev.py" ;;
  test)   # run the saas test suite in a DEDICATED test DB. Must override the
          # conf's dbfilter (=^saas_dev$) and http port, or HttpCase requests
          # route to saas_dev and every HTTP-layer test fails with not-found.
    pg_up; cd "$ODOO"
    "$PGBIN/psql" -h 127.0.0.1 -p $PGPORT -U odoo -d postgres -c "DROP DATABASE IF EXISTS saas_test;" >/dev/null 2>&1
    "$VENV/bin/python" odoo-bin -c "$CONF" -d saas_test -i saas_core,saas_website \
      --test-enable --test-tags=/saas_core,/saas_website --without-demo=False \
      --db-filter='^saas_test$' --http-port=8093 --http-interface=127.0.0.1 \
      --log-level=test --logfile="$LOGDIR/test.log" --stop-after-init
    echo "--- result ---"; grep -oE "[0-9]+ failed, [0-9]+ error\(s\) of [0-9]+ tests" "$LOGDIR/test.log" | tail -1 ;;
  shell)  cd "$ODOO"; "$VENV/bin/python" odoo-bin shell -c "$CONF" -d "$DB" --no-http ;;
  crons-off)  # seed-only stability: disable all SaaS: crons (server must be down
              # so we don't fight a running cron worker's row lock).
    odoo_down; sleep 1; pg_up
    "$PGBIN/psql" -h 127.0.0.1 -p $PGPORT -U odoo -d "$DB" -c \
      "UPDATE ir_cron SET active=false WHERE cron_name LIKE 'SaaS:%';" ;;
  crons-on)   odoo_down; sleep 1; pg_up
    "$PGBIN/psql" -h 127.0.0.1 -p $PGPORT -U odoo -d "$DB" -c \
      "UPDATE ir_cron SET active=true WHERE cron_name LIKE 'SaaS:%';" ;;
  cron)   # run a single cron on demand by name fragment: devctl cron "Trial Expiry"
    frag="${2:?usage: devctl cron \"<cron name fragment>\"}"
    cd "$ODOO"; printf '%s\n' \
      "c=env['ir.cron'].sudo().search([('cron_name','ilike','$frag')])" \
      "print('running:', c.mapped('cron_name'))" \
      "[j.sudo().method_direct_trigger() for j in c]" \
      "env.cr.commit(); print('done')" \
      | "$VENV/bin/python" odoo-bin shell -c "$CONF" -d "$DB" --no-http --log-level=warn ;;
  reset)  # DANGER: drop + reinit the DB, then reseed
    odoo_down; pg_up
    "$PGBIN/psql" -h 127.0.0.1 -p $PGPORT -U odoo -d postgres -c "DROP DATABASE IF EXISTS $DB;"
    cd "$ODOO"; "$VENV/bin/python" odoo-bin -c "$CONF" -d "$DB" -i saas_core,saas_website,payment_demo --without-demo=False --stop-after-init
    "$VENV/bin/python" odoo-bin shell -c "$CONF" -d "$DB" --no-http --log-level=warn < "$REPO/scripts/seed_dev.py" ;;
  *) echo "usage: $0 {up|down|status|logs|seed|shell|reset}"; exit 1 ;;
esac
