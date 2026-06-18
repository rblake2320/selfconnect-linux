"""Tests for /proc-based Linux identity capture."""
import os
import sys
import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Linux only")


def test_capture_identity_self():
    """Capture identity for the current Python process — always available."""
    from self_connect_linux.identity import capture_identity
    pid = os.getpid()
    ident = capture_identity(pid)
    assert ident.pid == pid
    assert ident.platform == "linux"
    assert ident.proc_start_time_ticks is not None
    assert ident.proc_start_time_ticks > 0
    assert ident.exe_path is not None
    assert "python" in ident.exe_path.lower()


def test_capture_identity_has_cgroup():
    from self_connect_linux.identity import capture_identity
    ident = capture_identity(os.getpid())
    # cgroup should be readable on DGX/Ubuntu
    assert ident.cgroup_path is not None


def test_capture_identity_has_namespaces():
    from self_connect_linux.identity import capture_identity
    ident = capture_identity(os.getpid())
    assert ident.pid_namespace is not None
    assert ident.mount_namespace is not None
    assert ident.net_namespace is not None


def test_capture_identity_has_uid_gid():
    from self_connect_linux.identity import capture_identity
    ident = capture_identity(os.getpid())
    assert ident.uid == os.getuid()
    assert ident.gid == os.getgid()


def test_capture_identity_exe_sha256():
    from self_connect_linux.identity import capture_identity
    ident = capture_identity(os.getpid())
    assert ident.exe_sha256 is not None
    assert ident.exe_sha256.startswith("sha256:")


def test_verify_identity_passes_self():
    """Capturing the same PID twice should pass verification."""
    from self_connect_linux.identity import capture_identity, verify_identity
    pid = os.getpid()
    a = capture_identity(pid)
    b = capture_identity(pid)
    verify_identity(a, b)  # should not raise


def test_verify_identity_fails_on_pid_mismatch():
    from self_connect_linux.identity import LinuxTargetIdentity, LinuxTargetMismatch, verify_identity
    a = LinuxTargetIdentity(pid=100)
    b = LinuxTargetIdentity(pid=999)
    with pytest.raises(LinuxTargetMismatch):
        verify_identity(a, b)


def test_verify_identity_ignores_none_optional_fields():
    """Optional fields that are None in expected are skipped — don't check what we don't know."""
    from self_connect_linux.identity import LinuxTargetIdentity, verify_identity, LinuxTargetMismatch
    # exe_path=None means "don't check" — observed value irrelevant
    a = LinuxTargetIdentity(pid=100, proc_start_time_ticks=999, exe_path=None)
    b = LinuxTargetIdentity(pid=100, proc_start_time_ticks=999, exe_path="/usr/bin/python3")
    verify_identity(a, b)  # should not raise


def test_verify_identity_required_none_fails_closed():
    """proc_start_time_ticks=None in expected must raise — unverifiable anti-spoofing field."""
    from self_connect_linux.identity import LinuxTargetIdentity, verify_identity, LinuxTargetMismatch
    a = LinuxTargetIdentity(pid=100, proc_start_time_ticks=None)
    b = LinuxTargetIdentity(pid=100, proc_start_time_ticks=12345)
    with pytest.raises(LinuxTargetMismatch, match="proc_start_time_ticks"):
        verify_identity(a, b)


def test_identity_is_frozen():
    """LinuxTargetIdentity must be immutable (frozen dataclass)."""
    from self_connect_linux.identity import LinuxTargetIdentity
    ident = LinuxTargetIdentity(pid=1)
    with pytest.raises((AttributeError, TypeError)):
        ident.pid = 2  # type: ignore[misc]


def test_verify_identity_each_field_mismatch():
    """Each field in _VERIFY_FIELDS is independently enforced — changing any one raises."""
    from self_connect_linux.identity import (
        capture_identity, verify_identity, LinuxTargetMismatch, _VERIFY_FIELDS,
    )
    import dataclasses

    base = capture_identity(os.getpid())

    # Fields to test: only those that are not None in the base (captured) identity.
    # We also skip proc_start_time_ticks from the loop since it is a _REQUIRED_FIELD
    # and None values have different semantics; it is already covered by existing tests.
    # We test each field by setting it to a clearly-wrong value.
    nonsense_values = {
        "pid": 999999999,
        "uid": 999999999,
        "exe_sha256": "sha256:" + "f" * 64,
        "proc_start_time_ticks": 999999999999,
        "cgroup_path": "/totally/fake/cgroup",
        "pid_namespace": "9999999999",
    }

    for field_name, bad_value in nonsense_values.items():
        # Skip if the field is None in base (verify skips None-expected fields)
        if getattr(base, field_name) is None:
            continue
        modified = dataclasses.replace(base, **{field_name: bad_value})
        with pytest.raises(LinuxTargetMismatch):
            verify_identity(modified, base)


def test_capture_identity_nonexistent_pid_returns_partial():
    """Capturing a non-existent PID gracefully returns a partial identity (all None fields)."""
    from self_connect_linux.identity import capture_identity
    # PID 99999999 almost certainly does not exist
    ident = capture_identity(99999999)
    # These /proc fields will be None for a non-existent PID
    assert ident.pid == 99999999
    assert ident.exe_path is None
    assert ident.exe_sha256 is None
    assert ident.proc_start_time_ticks is None


def test_proc_start_time_bad_pid_returns_none():
    from self_connect_linux.identity import _proc_start_time
    assert _proc_start_time(99999999) is None


def test_exe_path_bad_pid_returns_none():
    from self_connect_linux.identity import _exe_path
    assert _exe_path(99999999) is None


def test_cgroup_bad_pid_returns_none():
    from self_connect_linux.identity import _cgroup
    assert _cgroup(99999999) is None


def test_namespace_bad_pid_returns_none():
    from self_connect_linux.identity import _namespace_id
    assert _namespace_id(99999999, "pid") is None


def test_gpu_uuid_returns_str_or_none():
    from self_connect_linux.identity import _gpu_uuid
    result = _gpu_uuid()
    assert result is None or isinstance(result, str)


def test_cuda_context_id_returns_str_or_none():
    from self_connect_linux.identity import _cuda_context_id
    result = _cuda_context_id()
    assert result is None or isinstance(result, str)
