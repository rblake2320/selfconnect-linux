"""
self_connect_linux — Linux-native SelfConnect agent control layer for DGX Spark.

Phase 0/1: PTY agent lane, AF_UNIX broker skeleton, /proc identity.
Phase 2: AF_UNIX broker with SO_PEERCRED lease issuance + agent messaging.
Phase 3: memfd/eventfd zero-copy IPC bus.
Phase 4: CUDA IPC — zero-copy GPU buffer sharing across processes + provenance ledger.
Phase 5: X11 input injection + AT-SPI accessibility tree.
Phase 6: NCCL coordination — rank negotiation, UniqueId exchange, allreduce/broadcast.
Phase 7: Container/namespace isolation, cgroup v2 resource control.

This package does not import any Win32 module and is safe to import on
Linux/DGX (Ubuntu 24.04, aarch64, NVIDIA GB10).

On Windows: use self_connect.py instead.
"""
import sys

if sys.platform == "win32":
    raise ImportError(
        "self_connect_linux is not supported on Windows. Use self_connect.py instead."
    )

from .identity import (
    LinuxTargetIdentity,
    LinuxTargetMismatch,
    capture_identity,
    verify_identity,
)
from .platform import capabilities
from .pty_agent import PtyAgent, spawn_pty_agent
from .receipts import ActionReceipt, make_receipt, receipt_to_json
from .broker import BrokerServer, BrokerClient, default_socket_path, LEASE_TTL_SECONDS
from .shm import MemfdChannel, EventfdChannel, send_fds, recv_fds, shm_available
from .cuda_ipc import (
    CudaIpcBuffer,
    CudaError,
    cuda_ipc_available,
    device_count,
    handle_to_b64,
    handle_from_b64,
)
from .x11_input import (
    x11_available,
    list_windows,
    find_window,
    find_windows,
    focus_window,
    send_key,
    type_text,
    WindowInfo,
)
from .at_spi import (
    at_spi_available,
    list_applications,
    find_application,
    get_application_widgets,
    get_text as at_spi_get_text,
    activate as at_spi_activate,
    get_focused_text,
)
from .provenance import (
    ChainedReceipt,
    ProvenanceLedger,
    make_chained_receipt,
    ChainBroken,
    GENESIS_HASH,
)
from .container import (
    ContainerInfo,
    CgroupInfo,
    container_available,
    list_containers,
    gpu_containers,
    cgroup_info,
    container_identity,
)
from .nccl import (
    nccl_available,
    generate_unique_id,
    get_build_version as nccl_build_version,
    get_runtime_version as nccl_runtime_version,
    nccl_rank_negotiate,
    NcclComm,
)
from .browser import (
    browser_available,
    BrowserSession,
)
from . import tmux_agent
from . import at_spi
from . import provenance
from . import container
from . import nccl
from . import browser

__version__ = "0.9.0"
__all__ = [
    # Identity
    "LinuxTargetIdentity",
    "LinuxTargetMismatch",
    "capture_identity",
    "verify_identity",
    # PTY lane — primary agent control primitive
    "PtyAgent",
    "spawn_pty_agent",
    # Receipts
    "ActionReceipt",
    "make_receipt",
    "receipt_to_json",
    # Platform detection
    "capabilities",
    # Phase 2 — AF_UNIX broker (SO_PEERCRED leases + agent messaging)
    "BrokerServer",
    "BrokerClient",
    "default_socket_path",
    "LEASE_TTL_SECONDS",
    # Phase 3 — memfd/eventfd zero-copy IPC bus
    "MemfdChannel",
    "EventfdChannel",
    "send_fds",
    "recv_fds",
    "shm_available",
    # Phase 4 — CUDA IPC (zero-copy GPU buffer sharing, gated on cuda_ipc_available())
    "CudaIpcBuffer",
    "CudaError",
    "cuda_ipc_available",
    "device_count",
    "handle_to_b64",
    "handle_from_b64",
    # Phase 5 — X11 input (gated on x11_available())
    "x11_available",
    "list_windows",
    "find_window",
    "find_windows",
    "focus_window",
    "send_key",
    "type_text",
    "WindowInfo",
    # Phase 5 — AT-SPI accessibility tree (gated on at_spi_available())
    "at_spi_available",
    "list_applications",
    "find_application",
    "get_application_widgets",
    "at_spi_get_text",
    "at_spi_activate",
    "get_focused_text",
    "at_spi",
    # Phase 4 provenance — hash-chained GPU transfer audit trail
    "ChainedReceipt",
    "ProvenanceLedger",
    "make_chained_receipt",
    "ChainBroken",
    "GENESIS_HASH",
    "provenance",
    # Phase 6 — NCCL coordination (gated on nccl_available())
    "nccl_available",
    "generate_unique_id",
    "nccl_build_version",
    "nccl_runtime_version",
    "nccl_rank_negotiate",
    "NcclComm",
    "nccl",
    # Phase 7 — Container/namespace isolation (gated on container_available())
    "ContainerInfo",
    "CgroupInfo",
    "container_available",
    "list_containers",
    "gpu_containers",
    "cgroup_info",
    "container_identity",
    "container",
    # Phase 8 — Browser control (gated on browser_available())
    "browser_available",
    "BrowserSession",
    "browser",
    # Optional tmux adapter (check tmux_agent.is_available() before use)
    "tmux_agent",
]
