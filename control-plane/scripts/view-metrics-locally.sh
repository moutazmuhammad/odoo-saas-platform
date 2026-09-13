#!/usr/bin/env bash
# Launch the local SaaS portal so you can SEE the per-customer metrics dashboard
# in a real browser. Everything is pre-loaded in the `saas_dev` dev DB:
#   - a portal login:  rtcustomer@example.com  /  RtPortal123!
#   - the rt2 instance (id 138) with ~14 days of CPU/RAM/storage history
#
# Usage (run in YOUR terminal, not inside the assistant sandbox):
#   bash custom/saas/scripts/view-metrics-locally.sh
#
# Then open:
#   1) http://localhost:8069/web/login   → log in as rtcustomer@example.com / RtPortal123!
#   2) http://localhost:8069/my/instances/138   → scroll to "Performance history"
set -e

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"   # -> .../odoo18
cd "$ROOT"

echo "Starting local SaaS portal on http://localhost:8069 ..."
echo "Login:  rtcustomer@example.com  /  RtPortal123!"
echo "Then open:  http://localhost:8069/my/instances/138   (scroll to 'Performance history')"
echo

exec ./.env/bin/python odoo/odoo-bin -c odoo.conf -d saas_dev --http-port=8069
