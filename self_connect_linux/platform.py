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
    }
