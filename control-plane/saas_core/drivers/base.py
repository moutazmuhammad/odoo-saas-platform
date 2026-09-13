"""ComputeDriver — the stable seam between the Control Plane and a compute backend.

Business logic must depend only on this interface, never on Docker/SSH directly.
The single v1 implementation is ``SshDockerDriver`` (Docker over SSH); a future
``KubernetesDriver`` implements the same interface and is a new file only.

Design rules:
- The interface is Odoo-free: ``ComputeSpec`` / ``ComputeHandle`` are plain
  dataclasses, so drivers can be unit-tested without a database.
- Stateful operations (PostgreSQL provisioning, backup/restore) do NOT live here
  — they belong to DataService. Ingress (nginx) is a separate seam too.
  See docs/architecture/DRIVER-BOUNDARY.md §3.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ComputeSpec:
    """Everything a driver needs to CREATE a tenant's compute, backend-agnostic.

    Built by the Control Plane from a saas.instance; carries no ORM record.
    """
    container_name: str          # logical name / compose project (e.g. odoo_<sub>)
    image: str                   # base image ref, e.g. 'odoo:18' (see saas.odoo.version)
    instance_path: str           # host dir holding docker-compose.yml + odoo.conf + addons
    http_port: int               # host port -> container xmlrpc
    longpolling_port: int        # host port -> container longpolling/websocket
    db_name: str
    db_host: str
    env: dict = field(default_factory=dict)   # extra environment / template context


@dataclass(frozen=True)
class ComputeHandle:
    """Opaque reference to an existing tenant's compute, returned by create().

    Enough for a driver to locate and act on the workload. For SshDockerDriver
    that is (server, instance_path, container_name); a KubernetesDriver would
    map the same fields onto namespace/deployment.
    """
    server_id: int               # saas.server record id (where it runs)
    container_name: str
    instance_path: str
    host: str = ""               # host/IP where the workload is reachable (for endpoint())
    http_port: int = 0           # host port mapped to the container's HTTP port


@dataclass(frozen=True)
class ExecResult:
    rc: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


@dataclass(frozen=True)
class HealthStatus:
    running: bool
    detail: str = ""             # raw 'docker inspect'/'stats' summary, for logs/UI
    status: str = ""             # raw State.Status: running/exited/dead/restarting/not_found
    restart_count: int = 0       # State.RestartCount — distinguishes a one-off stop from a crash-loop


class ComputeDriver(abc.ABC):
    """Lifecycle + introspection for a single tenant's compute workload.

    Implementations are stateless services constructed per operation; they take
    the target server (and its SSH connection factory) as needed. All methods
    operate on one workload identified by ``spec``/``handle``.
    """

    # --- lifecycle ---------------------------------------------------------
    @abc.abstractmethod
    def create(self, spec: ComputeSpec) -> ComputeHandle:
        """Write configs and bring the workload up for the first time."""

    @abc.abstractmethod
    def destroy(self, handle: ComputeHandle) -> None:
        """Stop and remove the workload (compose down + cleanup)."""

    @abc.abstractmethod
    def start(self, handle: ComputeHandle) -> None:
        """Start an existing, stopped workload."""

    @abc.abstractmethod
    def stop(self, handle: ComputeHandle) -> None:
        """Stop a running workload (without removing it)."""

    @abc.abstractmethod
    def restart(self, handle: ComputeHandle) -> None:
        """Restart the workload (default impl: stop then start)."""

    # --- introspection / interaction --------------------------------------
    @abc.abstractmethod
    def exec(self, handle: ComputeHandle, command: str,
             *, user: Optional[str] = None, timeout: Optional[int] = None) -> ExecResult:
        """Run a command inside the workload's container."""

    @abc.abstractmethod
    def logs(self, handle: ComputeHandle, *, tail: Optional[int] = None) -> str:
        """Return recent container logs."""

    @abc.abstractmethod
    def endpoint(self, handle: ComputeHandle) -> tuple[str, int]:
        """Return (host, http_port) where ingress should route traffic."""

    @abc.abstractmethod
    def health(self, handle: ComputeHandle) -> HealthStatus:
        """Return whether the workload is up (+ a short detail string)."""

    # --- shared default ----------------------------------------------------
    def restart_default(self, handle: ComputeHandle) -> None:
        """Reusable stop+start, for drivers without a native restart."""
        self.stop(handle)
        self.start(handle)
