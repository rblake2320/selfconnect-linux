"""Linux capability detection — runs at import time, no side effects."""
import os
import shutil
import sys


def has_pty() -> bool:
    return hasattr(os, "openpty") and os.path.exists("/dev/ptmx")


def has_tmux() -> bool:
    return shutil.which("tmux") is not None


def has_memfd() -> bool:
    return hasattr(os, "memfd_create")


def has_eventfd() -> bool:
    return hasattr(os, "eventfd")


def has_x11() -> bool:
    return bool(os.environ.get("DISPLAY"))


def has_wayland() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY"))


def has_dbus() -> bool:
    return bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS"))


def has_docker() -> bool:
    return shutil.which("docker") is not None


def has_nvidia_ctk() -> bool:
    return shutil.which("nvidia-ctk") is not None


def has_cuda() -> bool:
    """True if a CUDA GPU is present and cupy can address it."""
    try:
        import cupy  # noqa: F401
        import cupy.cuda.runtime as rt
        return rt.getDeviceCount() > 0
    except Exception:
        return False


def gpu_uuids() -> list[str]:
    """Return UUIDs of all CUDA-visible GPUs, or [] if none/unavailable."""
    try:
        result = shutil.which("nvidia-smi")
        if not result:
            return []
        import subprocess
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=gpu_uuid", "--format=csv,noheader"],
            text=True, timeout=5,
        )
        return [u.strip() for u in out.strip().splitlines() if u.strip()]
    except Exception:
        return []


def capabilities() -> dict:
    return {
        "pty": has_pty(),
        "tmux": has_tmux(),
        "memfd_create": has_memfd(),
        "eventfd": has_eventfd(),
        "x11": has_x11(),
        "wayland": has_wayland(),
        "dbus": has_dbus(),
        "docker": has_docker(),
        "nvidia_ctk": has_nvidia_ctk(),
        "cuda": has_cuda(),
    }
