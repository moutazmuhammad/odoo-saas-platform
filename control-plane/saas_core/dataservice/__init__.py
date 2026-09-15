# DataService — backend-agnostic stateful operations (backup/restore/clone)
# expressed as two primitives: snapshot() and materialize().
#
# v1 DELEGATES to the proven restic + restore logic in saas_instance_backup /
# saas_instance; it does not reimplement it. Not imported from models/__init__.py
# (used via saas.instance._data_service()); changes no runtime behavior on its own.
# See /ROADMAP.md §8 (ADR-2) and §4.5/§5 Phase 5 for what's still incomplete
# (materialize() across targets, neutralize()).

from . import service
