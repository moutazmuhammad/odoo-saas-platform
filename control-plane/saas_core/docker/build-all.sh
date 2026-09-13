#!/bin/bash
# Build lightweight Odoo images for all supported versions
# Run from this directory: ./build-all.sh

set -e

VERSIONS="14.0 15.0 16.0 17.0 18.0 19.0"
# Add 19.0 when the official image is available:
# VERSIONS="$VERSIONS 19.0"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

for VERSION in $VERSIONS; do
    echo "============================================"
    echo "Building odoo-light:$VERSION"
    echo "============================================"

    docker build \
        --build-arg ODOO_VERSION="$VERSION" \
        -t "odoo-light:$VERSION" \
        -f "$SCRIPT_DIR/Dockerfile.odoo-light" \
        "$SCRIPT_DIR"

    echo "Done: odoo-light:$VERSION"
    echo ""
done

echo "============================================"
echo "All images built successfully!"
echo ""
echo "Image sizes:"
docker images --format "table {{.Repository}}:{{.Tag}}\t{{.Size}}" | grep odoo-light
echo ""
echo "Next steps:"
echo "1. Clone Odoo source on each server:"
echo "   mkdir -p /opt/odoo-source/18.0"
echo "   cd /opt/odoo-source/18.0"
echo "   git clone --depth 1 --branch 18.0 https://github.com/odoo/odoo.git ."
echo ""
echo "2. Push images to your registry (if using remote servers):"
echo "   docker tag odoo-light:18.0 your-registry/odoo-light:18.0"
echo "   docker push your-registry/odoo-light:18.0"
