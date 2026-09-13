# DataService — backend-agnostic stateful operations (backup/restore/clone)
# expressed as two primitives: snapshot() and materialize().
#
# v1 DELEGATES to the proven restic + restore logic in saas_instance_backup /
# saas_instance; it does not reimplement it. Not imported from models/__init__.py
# (used via saas.instance._data_service()); changes no runtime behavior on its own.
# See docs/architecture/DRIVER-BOUNDARY.md §3.

from . import service
