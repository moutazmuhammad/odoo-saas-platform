# Infrastructure driver layer.
#
# The Control Plane talks to compute backends ONLY through ComputeDriver, so
# the backend (Docker-over-SSH today, Kubernetes not yet cut over) can change
# without touching business logic. See /ROADMAP.md §3.1/§8 (ADR-2).
#
# NOTE: this package is intentionally NOT imported from models/__init__.py.
# It's used lazily via saas.instance._compute_driver() so unit tests can
# import it without a database; this changes no runtime behavior on its own.

from . import base
