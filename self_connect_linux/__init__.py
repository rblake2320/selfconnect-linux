"""
self_connect_linux — Linux-native SelfConnect agent control layer for DGX Spark.

Phase 0/1: PTY agent lane, AF_UNIX broker skeleton, /proc identity.

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
from . import tmux_agent

__version__ = "0.1.0"
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
    # Optional tmux adapter (check tmux_agent.is_available() before use)
    "tmux_agent",
]
