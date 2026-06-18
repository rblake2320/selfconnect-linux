"""Tests for Linux capability detection."""
import sys
import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Linux only")


def test_capabilities_returns_dict():
    from self_connect_linux.platform import capabilities
    caps = capabilities()
    assert isinstance(caps, dict)
    expected_keys = {"pty", "tmux", "memfd_create", "eventfd", "x11", "wayland", "dbus"}
    assert expected_keys.issubset(caps.keys())


def test_has_pty_true_on_linux():
    from self_connect_linux.platform import has_pty
    assert has_pty() is True


def test_has_memfd_true_on_python313():
    from self_connect_linux.platform import has_memfd
    import os
    # Python 3.13 on Linux always has memfd_create
    if sys.version_info >= (3, 8):
        assert has_memfd() == hasattr(os, "memfd_create")


def test_all_capability_values_are_bool():
    from self_connect_linux.platform import capabilities
    for key, val in capabilities().items():
        assert isinstance(val, bool), f"{key} should be bool, got {type(val)}"


def test_has_cuda_returns_bool():
    from self_connect_linux.platform import has_cuda
    assert isinstance(has_cuda(), bool)


def test_gpu_uuids_returns_list():
    from self_connect_linux.platform import gpu_uuids
    assert isinstance(gpu_uuids(), list)


def test_capabilities_has_cuda_key():
    from self_connect_linux import capabilities
    assert "cuda" in capabilities()


def test_has_cuda_returns_bool():
    from self_connect_linux.platform import has_cuda
    assert isinstance(has_cuda(), bool)


def test_gpu_uuids_returns_list():
    from self_connect_linux.platform import gpu_uuids
    result = gpu_uuids()
    assert isinstance(result, list)
    for item in result:
        assert isinstance(item, str)


def test_capabilities_has_cuda_key():
    from self_connect_linux import capabilities
    caps = capabilities()
    assert "cuda" in caps
    assert isinstance(caps["cuda"], bool)


def test_capabilities_has_all_expected_keys():
    from self_connect_linux import capabilities
    caps = capabilities()
    for key in ("pty", "tmux", "memfd_create", "eventfd", "nvidia_ctk", "cuda"):
        assert key in caps, f"missing key: {key}"
