#!/bin/bash
# Safe pip installer for Odoo containers.
# Installs to /var/lib/odoo/pip_packages (persisted via data volume).
#
# CRITICAL: a pip failure must NEVER stop the container from starting Odoo.
# We deliberately do NOT use `set -e` here — a non-zero pip exit would abort
# the script before the final exec, the container would exit, and Docker
# would restart it in an endless loop. Errors are handled explicitly and we
# always fall through to start Odoo.

REQ_FILE="/etc/odoo/requirements.txt"
PKG_DIR="/var/lib/odoo/pip_packages"
CHECKSUM_FILE="${PKG_DIR}/.requirements.md5"
ERR_FILE="${PKG_DIR}/.pip_error"

mkdir -p "$PKG_DIR" 2>/dev/null

start_odoo() { exec /entrypoint-light.sh odoo "$@"; }

# Nothing to install → start immediately.
if [ ! -s "$REQ_FILE" ]; then
    start_odoo "$@"
fi

CURRENT_MD5=$(md5sum "$REQ_FILE" 2>/dev/null | awk '{print $1}')
STORED_MD5=""
[ -f "$CHECKSUM_FILE" ] && STORED_MD5=$(cat "$CHECKSUM_FILE" 2>/dev/null)

if [ "$CURRENT_MD5" = "$STORED_MD5" ]; then
    echo "[pip-install] Packages up to date, skipping install."
else
    echo "[pip-install] Force-installing packages from requirements.txt..."
    # Capture output so a failure can be surfaced to the customer.
    PIP_OUT=$(pip3 install \
        --target="$PKG_DIR" \
        --upgrade \
        --force-reinstall \
        --no-warn-script-location \
        -r "$REQ_FILE" 2>&1)
    PIP_RC=$?
    echo "$PIP_OUT" | tail -25
    # Stamp the checksum either way, so a failing package can't make every
    # restart re-run the (slow) install. The dashboard "Apply" re-installs
    # on demand, and a real package change bumps the checksum to retry.
    echo "$CURRENT_MD5" > "$CHECKSUM_FILE" 2>/dev/null
    if [ "$PIP_RC" -eq 0 ]; then
        rm -f "$ERR_FILE" 2>/dev/null
        echo "[pip-install] Done."
    else
        echo "$PIP_OUT" | tail -40 > "$ERR_FILE" 2>/dev/null
        echo "[pip-install] WARNING: pip install failed (rc=$PIP_RC). Starting Odoo anyway."
    fi
fi

start_odoo "$@"
