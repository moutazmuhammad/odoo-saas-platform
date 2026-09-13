# Infrastructure driver layer.
#
# The Control Plane talks to compute backends ONLY through ComputeDriver, so
# the backend (Docker-over-SSH today, Kubernetes later) can change without
# touching business logic. See docs/architecture/DRIVER-BOUNDARY.md.
#
# NOTE: this package is intentionally NOT imported from models/__init__.py yet.
# Phase 1 wires it in incrementally (one call site per commit). Until then it is
# importable for unit tests but changes no runtime behavior.

from . import base
