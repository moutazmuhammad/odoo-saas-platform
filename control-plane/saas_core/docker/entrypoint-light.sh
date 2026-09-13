#!/bin/bash
set -e

# Odoo source is mounted at /opt/odoo (read-only)

# Verify the source is mounted
if [ ! -d "/opt/odoo/odoo" ]; then
    echo "================================================================"
    echo "ERROR: Odoo source not found at /opt/odoo"
    echo ""
    echo "Mount the Odoo source directory as a volume:"
    echo "  -v /opt/odoo-source/18.0:/opt/odoo:ro"
    echo "================================================================"
    exit 1
fi

# Find the correct entry point
if [ -f "/opt/odoo/odoo-bin" ]; then
    ODOO_BIN="/opt/odoo/odoo-bin"
elif [ -f "/opt/odoo/odoo.py" ]; then
    ODOO_BIN="/opt/odoo/odoo.py"
elif [ -f "/opt/odoo/openerp-server" ]; then
    ODOO_BIN="/opt/odoo/openerp-server"
else
    ODOO_BIN=""
fi

# Read database host from odoo.conf if it exists
# This avoids hardcoding 'db' as default
CONF_FILE="/etc/odoo/odoo.conf"
if [ -f "$CONF_FILE" ]; then
    DB_HOST=$(grep -E "^db_host\s*=" "$CONF_FILE" | sed 's/.*=\s*//' | tr -d '[:space:]')
    DB_PORT=$(grep -E "^db_port\s*=" "$CONF_FILE" | sed 's/.*=\s*//' | tr -d '[:space:]')
fi
DB_HOST=${DB_HOST:-${PGHOST:-localhost}}
DB_PORT=${DB_PORT:-${PGPORT:-5432}}

# Wait for PostgreSQL
if [ -n "$DB_HOST" ] && [ "$DB_HOST" != "false" ] && [ "$DB_HOST" != "False" ]; then
    for i in $(seq 1 30); do
        if python3 -c "import socket; s=socket.socket(); s.settimeout(2); s.connect(('$DB_HOST', $DB_PORT)); s.close()" 2>/dev/null; then
            break
        fi
        if [ "$i" = "30" ]; then
            echo "WARNING: Could not connect to PostgreSQL at $DB_HOST:$DB_PORT after 30 attempts"
        else
            echo "Waiting for PostgreSQL at $DB_HOST:$DB_PORT... ($i/30)"
        fi
        sleep 1
    done
fi

if [ "$1" = "odoo" ] || [ "$1" = "odoo-bin" ] || [ -z "$1" ]; then
    shift 2>/dev/null || true
    if [ -n "$ODOO_BIN" ]; then
        exec python3 "$ODOO_BIN" -c "$CONF_FILE" "$@"
    else
        exec python3 -c "import odoo; odoo.cli.main()" "$@"
    fi
fi

exec "$@"
