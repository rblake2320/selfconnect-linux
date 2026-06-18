"""
Phase 7 — Container/namespace isolation and cgroup v2 resource control.

On DGX Spark, production AI workloads run inside Docker containers managed
by the NVIDIA Container Runtime.  This module provides:

  ContainerInfo   — snapshot of a running container (pid, cgroup, GPU assignment)
  CgroupInfo      — cgroup v2 resource limits for any process
  list_containers()         → all visible Docker containers
  gpu_containers()          → containers with GPU device access
  cgroup_info(pid)          → cpu/memory/pid limits for a process
  container_identity(name)  → LinuxTargetIdentity for a container's init PID

Capability gate: call container_available() before use.

All Docker queries use the docker CLI via subprocess — no Python Docker SDK
dependency required.  cgroup reads go directly to /sys/fs/cgroup (v2 unified).
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .identity import capture_identity, LinuxTargetIdentity


@dataclass
class ContainerInfo:
    """Point-in-time snapshot of a Docker container."""
    container_id: str          # full 64-char container ID
    short_id: str              # first 12 chars
    name: str                  # container name (without leading /)
    image: str                 # image name:tag
    state: str                 # "running", "exited", "paused", etc.
    init_pid: int | None       # PID of container's init process (host namespace)
    gpu_devices: list[str]     # GPU capability strings / device IDs
    cgroup_path: str | None    # /sys/fs/cgroup/... absolute path

    @property
    def is_running(self) -> bool:
        return self.state == "running"

    @property
    def has_gpu(self) -> bool:
        return len(self.gpu_devices) > 0

    def __repr__(self) -> str:
        gpu = f", gpus={self.gpu_devices}" if self.gpu_devices else ""
        return f"ContainerInfo(name={self.name!r}, id={self.short_id}, state={self.state}{gpu})"


@dataclass
class CgroupInfo:
    """Resource limits from cgroup v2 for a process."""
    pid: int
    cgroup_path: str | None      # relative path under /sys/fs/cgroup/
    cpu_max: str                 # raw "quota period" string from cpu.max
    cpu_quota_pct: float | None  # None = unlimited
    memory_max_bytes: int | None # None = unlimited
    memory_current_bytes: int | None
    pids_max: str                # "max" or an integer string

    def __repr__(self) -> str:
        cpu = f"{self.cpu_quota_pct:.0f}%" if self.cpu_quota_pct is not None else "unlimited"
        mem = (f"{self.memory_max_bytes // (1024 * 1024)}MiB"
               if self.memory_max_bytes else "unlimited")
        return f"CgroupInfo(pid={self.pid}, cpu={cpu}, memory={mem})"


# ---------------------------------------------------------------------------
# Capability gate
# ---------------------------------------------------------------------------

def container_available() -> bool:
    """True if the Docker daemon is accessible via the docker CLI."""
    try:
        r = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _docker_inspect(name_or_id: str) -> dict | None:
    try:
        r = subprocess.run(
            ["docker", "inspect", name_or_id],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
        return data[0] if data else None
    except Exception:
        return None


def _cgroup_dir_for_container(long_id: str) -> str | None:
    # Ubuntu 24.04 + systemd: /sys/fs/cgroup/system.slice/docker-<id>.scope
    candidate = Path(f"/sys/fs/cgroup/system.slice/docker-{long_id}.scope")
    if candidate.is_dir():
        return str(candidate)
    # kubepods or custom cgroup parent
    for p in Path("/sys/fs/cgroup").rglob(f"*{long_id[:12]}*"):
        if p.is_dir():
            return str(p)
    return None


def _read_cgroup_file(cgroup_dir: str, name: str) -> str | None:
    try:
        return Path(cgroup_dir, name).read_text().strip()
    except Exception:
        return None


def _parse_cpu_quota(cpu_max: str) -> float | None:
    """Parse "quota period" into percentage; "max ..." → None (unlimited)."""
    if not cpu_max or cpu_max.startswith("max"):
        return None
    try:
        quota, period = cpu_max.split()
        return (int(quota) / int(period)) * 100.0
    except Exception:
        return None


def _parse_bytes(raw: str | None) -> int | None:
    if not raw or raw == "max":
        return None
    try:
        return int(raw)
    except Exception:
        return None


def _gpu_devices_from_inspect(inspect: dict) -> list[str]:
    device_requests = (inspect.get("HostConfig") or {}).get("DeviceRequests") or []
    for dr in device_requests:
        caps = dr.get("Capabilities") or [[]]
        if "gpu" in (caps[0] if caps else []):
            ids = dr.get("DeviceIDs") or []
            if ids:
                return ids
            count = dr.get("Count", 0)
            return ["all"] if count == -1 else ([f"{count}x"] if count > 0 else [])
    return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_containers(running_only: bool = True) -> list[ContainerInfo]:
    """
    Return Docker containers visible to this user.

    *running_only* — True (default) returns only running containers.
    """
    cmd = ["docker", "ps", "--format", "{{json .}}"]
    if not running_only:
        cmd.append("-a")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return []
        rows = [json.loads(line) for line in r.stdout.strip().splitlines() if line]
    except Exception:
        return []

    result = []
    for row in rows:
        short_id = row.get("ID", "")
        if not short_id:
            continue
        info = _docker_inspect(short_id)
        if info is None:
            continue
        long_id = info.get("Id", short_id)
        pid_val = (info.get("State") or {}).get("Pid")
        init_pid = int(pid_val) if pid_val else None
        cgroup_dir = _cgroup_dir_for_container(long_id)
        gpu_devices = _gpu_devices_from_inspect(info)
        name = row.get("Names", "").lstrip("/")
        result.append(ContainerInfo(
            container_id=long_id,
            short_id=long_id[:12],
            name=name,
            image=row.get("Image", ""),
            state=row.get("State", ""),
            init_pid=init_pid,
            gpu_devices=gpu_devices,
            cgroup_path=cgroup_dir,
        ))
    return result


def gpu_containers(running_only: bool = True) -> list[ContainerInfo]:
    """Return only containers with GPU device access."""
    return [c for c in list_containers(running_only=running_only) if c.has_gpu]


def cgroup_info(pid: int) -> CgroupInfo:
    """
    Read cgroup v2 resource limits for a process.

    Resolves the cgroup path from /proc/<pid>/cgroup, then reads
    cpu.max, memory.max, memory.current, and pids.max from /sys/fs/cgroup.
    """
    cgroup_rel: str | None = None
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            for line in f:
                parts = line.strip().split(":", 2)
                if len(parts) == 3:
                    cgroup_rel = parts[2].lstrip("/")
                    break
    except Exception:
        pass

    cgroup_dir: str | None = None
    if cgroup_rel:
        candidate = Path("/sys/fs/cgroup") / cgroup_rel
        if candidate.is_dir():
            cgroup_dir = str(candidate)

    cpu_raw = (_read_cgroup_file(cgroup_dir, "cpu.max") if cgroup_dir else None) or "max 100000"
    mem_max_raw = _read_cgroup_file(cgroup_dir, "memory.max") if cgroup_dir else None
    mem_cur_raw = _read_cgroup_file(cgroup_dir, "memory.current") if cgroup_dir else None
    pids_raw = (_read_cgroup_file(cgroup_dir, "pids.max") if cgroup_dir else None) or "max"

    return CgroupInfo(
        pid=pid,
        cgroup_path=cgroup_rel or None,
        cpu_max=cpu_raw,
        cpu_quota_pct=_parse_cpu_quota(cpu_raw),
        memory_max_bytes=_parse_bytes(mem_max_raw),
        memory_current_bytes=_parse_bytes(mem_cur_raw),
        pids_max=pids_raw,
    )


def container_identity(name_or_id: str) -> LinuxTargetIdentity:
    """
    Return a LinuxTargetIdentity anchored to a container's init process.

    This lets verify_identity() confirm a container hasn't been replaced
    (image swap, PID reuse) between capture and use.

    Raises RuntimeError if the container is not found or not running.
    """
    info = _docker_inspect(name_or_id)
    if info is None:
        raise RuntimeError(f"Container not found: {name_or_id!r}")
    pid_val = (info.get("State") or {}).get("Pid")
    if not pid_val or int(pid_val) == 0:
        raise RuntimeError(f"Container {name_or_id!r} is not running (pid=0)")
    return capture_identity(int(pid_val), backend="container")
