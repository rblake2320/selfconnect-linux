"""Tests for Phase 7 container/namespace isolation (container.py)."""
import os
import sys

import pytest

from self_connect_linux.container import container_available

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="Linux only"),
    pytest.mark.skipif(not container_available(), reason="Docker not accessible"),
]


def test_container_available():
    assert container_available() is True


def test_list_containers_returns_list():
    from self_connect_linux.container import list_containers
    containers = list_containers()
    assert isinstance(containers, list)


def test_list_containers_has_running_on_dgx():
    from self_connect_linux.container import list_containers
    containers = list_containers(running_only=True)
    if not containers:
        pytest.skip("No running containers on this host")
    assert len(containers) > 0


def test_container_info_fields():
    from self_connect_linux.container import ContainerInfo, list_containers
    containers = list_containers()
    if not containers:
        pytest.skip("No containers running")
    c = containers[0]
    assert isinstance(c, ContainerInfo)
    assert isinstance(c.container_id, str)
    assert len(c.container_id) == 64
    assert isinstance(c.short_id, str)
    assert len(c.short_id) == 12
    assert c.short_id == c.container_id[:12]
    assert isinstance(c.name, str)
    assert isinstance(c.state, str)
    assert isinstance(c.gpu_devices, list)


def test_container_info_is_running_property():
    from self_connect_linux.container import list_containers
    containers = list_containers(running_only=True)
    if not containers:
        pytest.skip("No running containers")
    assert all(c.is_running for c in containers)


def test_container_info_repr():
    from self_connect_linux.container import list_containers
    containers = list_containers()
    if not containers:
        pytest.skip("No containers")
    r = repr(containers[0])
    assert "ContainerInfo" in r
    assert "state=" in r


def test_gpu_containers_returns_list():
    from self_connect_linux.container import gpu_containers
    gpus = gpu_containers()
    assert isinstance(gpus, list)
    if not gpus:
        pytest.skip("No GPU containers running on this host")


def test_gpu_containers_all_have_gpu():
    from self_connect_linux.container import gpu_containers
    for c in gpu_containers():
        assert c.has_gpu is True
        assert len(c.gpu_devices) > 0


def test_cgroup_info_own_process():
    from self_connect_linux.container import CgroupInfo, cgroup_info
    info = cgroup_info(os.getpid())
    assert isinstance(info, CgroupInfo)
    assert info.pid == os.getpid()
    assert isinstance(info.cpu_max, str)
    assert isinstance(info.pids_max, str)
    # Our own process is not cgroup-limited
    assert info.cpu_quota_pct is None or info.cpu_quota_pct > 0


def test_cgroup_info_fields_are_typed():
    from self_connect_linux.container import cgroup_info
    info = cgroup_info(os.getpid())
    # cpu_quota_pct is None (unlimited) or a positive float
    if info.cpu_quota_pct is not None:
        assert isinstance(info.cpu_quota_pct, float)
        assert info.cpu_quota_pct > 0
    # memory fields are None or non-negative ints
    if info.memory_max_bytes is not None:
        assert isinstance(info.memory_max_bytes, int)
        assert info.memory_max_bytes > 0
    if info.memory_current_bytes is not None:
        assert isinstance(info.memory_current_bytes, int)
        assert info.memory_current_bytes >= 0


def test_cgroup_info_repr():
    from self_connect_linux.container import cgroup_info
    info = cgroup_info(os.getpid())
    r = repr(info)
    assert "CgroupInfo" in r
    assert "pid=" in r


def test_cgroup_info_container_process():
    """Container's init process should have cgroup limits set."""
    from self_connect_linux.container import cgroup_info, list_containers
    containers = [c for c in list_containers() if c.init_pid]
    if not containers:
        pytest.skip("No containers with readable init_pid")
    c = containers[0]
    info = cgroup_info(c.init_pid)
    assert info.pid == c.init_pid
    # Container should have a cgroup path
    assert info.cgroup_path is not None, f"Container {c.name} has no cgroup path"


def test_container_identity_running():
    from self_connect_linux.container import container_identity, list_containers
    from self_connect_linux.identity import LinuxTargetIdentity
    containers = [c for c in list_containers() if c.is_running and c.init_pid]
    if not containers:
        pytest.skip("No running containers with readable init_pid")
    ident = container_identity(containers[0].name)
    assert isinstance(ident, LinuxTargetIdentity)
    assert ident.backend == "container"
    assert ident.pid == containers[0].init_pid
    assert ident.proc_start_time_ticks is not None


def test_container_identity_not_found_raises():
    from self_connect_linux.container import container_identity
    with pytest.raises(RuntimeError, match="not found"):
        container_identity("xyzzy_nonexistent_container_99999")


def test_list_containers_all_includes_stopped():
    from self_connect_linux.container import list_containers
    running = list_containers(running_only=True)
    all_c = list_containers(running_only=False)
    # all >= running
    assert len(all_c) >= len(running)
