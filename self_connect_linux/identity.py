"""Linux process identity from /proc — the Linux equivalent of HWND + SID guard."""
import hashlib
import os
import re
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class LinuxTargetIdentity:
    """
    Point-in-time identity snapshot for a Linux target process.
    Fail-closed: any non-None field mismatch on re-verify aborts the action.
    """
    platform: Literal["linux"] = "linux"
    backend: str = ""

    # Process identity
    pid: int | None = None
    uid: int | None = None
    gid: int | None = None
    proc_start_time_ticks: int | None = None
    exe_path: str | None = None
    exe_inode: str | None = None
    exe_sha256: str | None = None
    cmdline_hash: str | None = None

    # Linux isolation identity
    cgroup_path: str | None = None
    systemd_unit: str | None = None
    user_slice: str | None = None
    pid_namespace: str | None = None
    mount_namespace: str | None = None
    net_namespace: str | None = None
    apparmor_label: str | None = None
    selinux_label: str | None = None

    # GUI identity
    display_server: str | None = None
    x_window_id: int | None = None
    atspi_bus_name: str | None = None
    atspi_object_path: str | None = None
    wayland_portal_session: str | None = None
    pipewire_stream_serial: int | None = None

    # Terminal identity
    tty_path: str | None = None
    pty_master_fd_id: int | None = None
    tmux_session: str | None = None
    tmux_window: str | None = None
    tmux_pane: str | None = None

    # Container / GPU identity
    container_id: str | None = None
    image_digest: str | None = None
    gpu_uuid: str | None = None
    cuda_context_id: str | None = None

    generation: int = 0


# ── /proc readers ─────────────────────────────────────────────────────────────

def _proc_start_time(pid: int) -> int | None:
    """
    Field 22 of /proc/PID/stat is starttime in clock ticks since boot.
    Parse after the closing paren to avoid issues with spaces in comm names.
    """
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
        rparen = stat.rfind(")")
        fields = stat[rparen + 2:].split()
        # After ")": field3=state[0] ... field22=starttime[19]
        return int(fields[19])
    except Exception:
        return None


def _exe_path(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except Exception:
        return None


def _exe_inode(exe: str) -> str | None:
    try:
        return str(os.stat(exe).st_ino)
    except Exception:
        return None


def _exe_sha256(exe: str | None) -> str | None:
    if not exe:
        return None
    try:
        h = hashlib.sha256()
        with open(exe, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return "sha256:" + h.hexdigest()
    except Exception:
        return None


def _cmdline_hash(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            data = f.read()
        return "sha256:" + hashlib.sha256(data).hexdigest()[:16]
    except Exception:
        return None


def _status_field(pid: int, field: str) -> int | None:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith(field + ":"):
                    return int(line.split()[1])
    except Exception:
        return None


def _cgroup(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            for line in f:
                parts = line.strip().split(":", 2)
                if len(parts) == 3 and parts[2]:
                    return parts[2]
    except Exception:
        return None


def _namespace_id(pid: int, ns: str) -> str | None:
    try:
        target = os.readlink(f"/proc/{pid}/ns/{ns}")
        m = re.search(r"\[(\d+)\]", target)
        return m.group(1) if m else target
    except Exception:
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def _gpu_uuid() -> str | None:
    """Read the UUID of CUDA GPU 0 from nvidia-smi. Returns None on failure.

    Security note: GPU UUID is a hardware property of the device, not of the
    process. All processes on the same host share the same GPU UUID. Its value
    in identity verification is: (1) confirms the agent is bound to the same
    physical GPU as the broker, and (2) would differ on a different machine,
    blocking cross-host spoofing via network tunnels. On multi-GPU hosts,
    GPU 0 is used; a future enhancement could track per-process device binding
    via /proc/<pid>/fdinfo for stronger per-process GPU attestation.
    """
    try:
        import subprocess
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=gpu_uuid", "--format=csv,noheader", "--id=0"],
            text=True, timeout=5,
        )
        return out.strip() or None
    except Exception:
        return None


def _cuda_context_id() -> str | None:
    """
    Return a stable identifier for the calling process's CUDA context:
    the device ordinal joined with the process ID, giving a unique context key.
    Returns None if CUDA is unavailable.
    """
    try:
        import cupy.cuda.runtime as rt
        dev = rt.getDevice()
        return f"gpu{dev}:pid{os.getpid()}"
    except Exception:
        return None


def capture_identity(pid: int, backend: str = "pty") -> LinuxTargetIdentity:
    """Build a /proc-based identity snapshot for a PID. Read-only, no side effects."""
    exe = _exe_path(pid)
    gpu = _gpu_uuid()  # GPU UUID is a system property; valid for any PID on same host
    ctx = _cuda_context_id() if pid == os.getpid() else None
    return LinuxTargetIdentity(
        platform="linux",
        backend=backend,
        pid=pid,
        uid=_status_field(pid, "Uid"),
        gid=_status_field(pid, "Gid"),
        proc_start_time_ticks=_proc_start_time(pid),
        exe_path=exe,
        exe_inode=_exe_inode(exe) if exe else None,
        exe_sha256=_exe_sha256(exe),
        cmdline_hash=_cmdline_hash(pid),
        cgroup_path=_cgroup(pid),
        pid_namespace=_namespace_id(pid, "pid"),
        mount_namespace=_namespace_id(pid, "mnt"),
        net_namespace=_namespace_id(pid, "net"),
        gpu_uuid=gpu,
        cuda_context_id=ctx,
    )


_VERIFY_FIELDS = [
    "pid", "uid", "gid", "proc_start_time_ticks",
    "exe_path", "exe_inode", "exe_sha256",
    "cgroup_path", "pid_namespace", "mount_namespace", "net_namespace",
    "container_id", "x_window_id", "atspi_object_path",
    "pipewire_stream_serial", "tmux_pane", "tty_path",
    "gpu_uuid",
]

# Fields that are core anti-spoofing guards — if None in expected, verification
# cannot proceed safely and must fail closed rather than skip the check.
_REQUIRED_FIELDS = frozenset({"proc_start_time_ticks"})


class LinuxTargetMismatch(RuntimeError):
    pass


def verify_identity(expected: LinuxTargetIdentity, observed: LinuxTargetIdentity) -> None:
    """
    Fail closed: raise LinuxTargetMismatch if any non-None expected field
    doesn't match the observed snapshot.

    For fields in _REQUIRED_FIELDS (core anti-PID-reuse guards), a None value
    in expected is itself a failure — unknown expected state is not safe.
    """
    for f in _REQUIRED_FIELDS:
        if getattr(expected, f) is None:
            raise LinuxTargetMismatch(
                f"Cannot verify identity: {f!r} is None in expected snapshot "
                f"— was the process unreadable at capture time?"
            )
    failures = {
        f: {"expected": getattr(expected, f), "observed": getattr(observed, f)}
        for f in _VERIFY_FIELDS
        if getattr(expected, f) is not None
        and getattr(expected, f) != getattr(observed, f)
    }
    if failures:
        raise LinuxTargetMismatch(f"Refusing wrong/stale Linux target: {failures}")
