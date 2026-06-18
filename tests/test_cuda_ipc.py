"""Tests for Phase 4 CUDA IPC (cuda_ipc.py).

All tests are gated on cuda_ipc_available() so the suite still passes on
machines without a GPU.  On spark-3cdf (NVIDIA GB10, CUDA 13.0) all tests run.
"""
import base64
import json
import subprocess
import sys

import pytest

from self_connect_linux.cuda_ipc import (
    CudaIpcBuffer,
    CudaError,
    cuda_ipc_available,
    device_count,
    handle_from_b64,
    handle_to_b64,
    _IPC_HANDLE_SIZE,
)

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="Linux only"),
    pytest.mark.skipif(not cuda_ipc_available(), reason="CUDA not available"),
]

_SIZE = 64 * 1024   # 64 KiB — large enough for real data, small enough to be fast


# ---------------------------------------------------------------------------
# Capability
# ---------------------------------------------------------------------------

def test_cuda_ipc_available():
    assert cuda_ipc_available() is True


def test_device_count_nonzero():
    assert device_count() >= 1


# ---------------------------------------------------------------------------
# CudaIpcBuffer — alloc / close
# ---------------------------------------------------------------------------

def test_alloc_and_close():
    buf = CudaIpcBuffer.alloc(size=_SIZE)
    assert buf.size == _SIZE
    assert buf.device == 0
    assert buf._owner is True
    buf.close()


def test_close_is_idempotent():
    buf = CudaIpcBuffer.alloc(size=4096)
    buf.close()
    buf.close()  # must not raise


def test_context_manager():
    with CudaIpcBuffer.alloc(size=4096) as buf:
        assert not buf._closed
    assert buf._closed


# ---------------------------------------------------------------------------
# write / read (host ↔ device memcpy)
# ---------------------------------------------------------------------------

def test_write_and_read_roundtrip():
    payload = b"Hello from GPU Phase 4!"
    with CudaIpcBuffer.alloc(size=_SIZE) as buf:
        written = buf.write(payload)
        assert written == len(payload)
        data = buf.read(length=len(payload))
    assert data == payload


def test_write_full_buffer():
    payload = b"X" * _SIZE
    with CudaIpcBuffer.alloc(size=_SIZE) as buf:
        buf.write(payload)
        data = buf.read()
    assert data == payload


def test_write_and_read_with_offset():
    with CudaIpcBuffer.alloc(size=256) as buf:
        buf.write(b"OFFSET_DATA", offset=64)
        data = buf.read(length=11, offset=64)
    assert data == b"OFFSET_DATA"


def test_write_overflow_raises():
    with CudaIpcBuffer.alloc(size=16) as buf:
        with pytest.raises(ValueError):
            buf.write(b"X" * 32)


def test_read_overflow_raises():
    with CudaIpcBuffer.alloc(size=16) as buf:
        with pytest.raises(ValueError):
            buf.read(length=32)


def test_write_after_close_raises():
    buf = CudaIpcBuffer.alloc(size=64)
    buf.close()
    with pytest.raises(RuntimeError):
        buf.write(b"nope")


def test_read_after_close_raises():
    buf = CudaIpcBuffer.alloc(size=64)
    buf.close()
    with pytest.raises(RuntimeError):
        buf.read()


# ---------------------------------------------------------------------------
# IPC handle export
# ---------------------------------------------------------------------------

def test_export_handle_is_64_bytes():
    with CudaIpcBuffer.alloc(size=_SIZE) as buf:
        handle = buf.export_handle()
    assert isinstance(handle, bytes)
    assert len(handle) == _IPC_HANDLE_SIZE


def test_export_handle_is_nonzero():
    with CudaIpcBuffer.alloc(size=_SIZE) as buf:
        handle = buf.export_handle()
    assert any(b != 0 for b in handle)


def test_non_owner_cannot_export():
    # Construct a non-owner wrapper directly — cudaIpcOpenMemHandle is cross-process only,
    # so we test the Python guard without triggering an in-process IPC open.
    with CudaIpcBuffer.alloc(size=_SIZE) as owner:
        non_owner = CudaIpcBuffer(ptr=owner._ptr, size=_SIZE, device=0, owner=False)
        with pytest.raises(PermissionError):
            non_owner.export_handle()


# ---------------------------------------------------------------------------
# Handle serialisation (b64)
# ---------------------------------------------------------------------------

def test_handle_to_b64_roundtrip():
    with CudaIpcBuffer.alloc(size=_SIZE) as buf:
        handle_bytes = buf.export_handle()

    b64 = handle_to_b64(handle_bytes)
    assert isinstance(b64, str)
    recovered = handle_from_b64(b64)
    assert recovered == handle_bytes


def test_handle_to_b64_wrong_length_raises():
    with pytest.raises(ValueError):
        handle_to_b64(b"too short")


def test_handle_from_b64_wrong_length_raises():
    bad = base64.b64encode(b"not 64 bytes").decode()
    with pytest.raises(ValueError):
        handle_from_b64(bad)


# ---------------------------------------------------------------------------
# Cross-process IPC via subprocess (the real use case)
# Note: cudaIpcOpenMemHandle is explicitly cross-process only — same-process
# open returns error 201 (cudaErrorPeerAccessUnsupported on Grace Blackwell).
# ---------------------------------------------------------------------------

def test_cross_process_cuda_ipc():
    """
    Producer allocates GPU memory, writes a sentinel, exports handle.
    Consumer subprocess imports handle, reads data.
    """
    sentinel = b"CUDA_IPC_CROSS_PROCESS_OK"

    with CudaIpcBuffer.alloc(size=_SIZE) as buf:
        buf.write(sentinel)
        handle_b64 = handle_to_b64(buf.export_handle())

        child_script = f"""
import sys
sys.path.insert(0, "{_repo_root()}")
from self_connect_linux.cuda_ipc import CudaIpcBuffer, handle_from_b64
handle_bytes = handle_from_b64("{handle_b64}")
with CudaIpcBuffer.from_handle(handle_bytes, size={_SIZE}) as remote:
    data = remote.read(length={len(sentinel)})
print(data.decode())
"""
        result = subprocess.run(
            [sys.executable, "-c", child_script],
            capture_output=True,
            text=True,
            timeout=20,
        )

    assert result.returncode == 0, f"child stderr: {result.stderr}"
    assert result.stdout.strip() == sentinel.decode()


def test_cross_process_broker_json_transport():
    """
    Simulate the broker transport path: handle serialised as JSON field.
    """
    sentinel = b"BROKER_JSON_TRANSPORT_OK"

    with CudaIpcBuffer.alloc(size=_SIZE) as buf:
        buf.write(sentinel)
        msg = json.dumps({
            "type": "cuda_ipc_handle",
            "handle": handle_to_b64(buf.export_handle()),
            "size": _SIZE,
        })

        child_script = f"""
import sys, json
sys.path.insert(0, "{_repo_root()}")
from self_connect_linux.cuda_ipc import CudaIpcBuffer, handle_from_b64
info = json.loads({msg!r})
handle_bytes = handle_from_b64(info["handle"])
with CudaIpcBuffer.from_handle(handle_bytes, size=info["size"]) as remote:
    data = remote.read(length={len(sentinel)})
print(data.decode())
"""
        result = subprocess.run(
            [sys.executable, "-c", child_script],
            capture_output=True,
            text=True,
            timeout=20,
        )

    assert result.returncode == 0, f"child stderr: {result.stderr}"
    assert result.stdout.strip() == sentinel.decode()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _repo_root() -> str:
    import pathlib
    return str(pathlib.Path(__file__).parent.parent)
