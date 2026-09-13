#!/bin/bash
# Clone Odoo source code for all versions on a server
# Run on each Docker host: ./setup-source.sh
#
# Source is stored at /opt/odoo-source/<version>/
# Mounted read-only into containers

set -e

BASE_DIR="/opt/odoo-source"
VERSIONS="14.0 15.0 16.0 17.0 18.0 19.0"
# Add 19.0 when available:
# VERSIONS="$VERSIONS 19.0"

REPO="https://github.com/odoo/odoo.git"

for VERSION in $VERSIONS; do
    TARGET="$BASE_DIR/$VERSION"

    if [ -d "$TARGET/.git" ]; then
        echo "[$VERSION] Already cloned at $TARGET — pulling latest..."
        cd "$TARGET"
        git pull --ff-only
    else
        echo "[$VERSION] Cloning to $TARGET..."
        mkdir -p "$TARGET"
        git clone --depth 1 --branch "$VERSION" "$REPO" "$TARGET"
    fi

    echo "[$VERSION] Done."
    echo ""
done

echo "============================================"
echo "All versions ready at $BASE_DIR/"
echo ""
du -sh $BASE_DIR/*/
echo ""
echo "Total:"
du -sh $BASE_DIR/
