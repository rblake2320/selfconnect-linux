"""
Phase 4 — CUDA IPC: zero-copy GPU buffer sharing between processes on the
same DGX Spark host.

Wraps libcudart.so via ctypes — no pycuda or pip dependency required.

Primitives:

  CudaIpcBuffer.alloc(size, device=0)
      Allocate a GPU buffer on the specified device.
      write(data) / read(length) copy between host (CPU) and device (GPU).
      export_handle() returns a 64-byte opaque handle.

  CudaIpcBuffer.from_handle(handle_bytes, size, device=0)
      Import a handle (received from another process) and map the same
      GPU memory.  Call close() when done.

  handle_to_b64(handle_bytes) / handle_from_b64(s)
      Helpers for serialising handles through the JSON broker protocol.

  cuda_ipc_available() — True if libcudart.so loads and >=1 GPU is present.
  device_count()       — number of CUDA devices visible to this process.

Typical cross-process workflow:

  # Producer process
  buf = CudaIpcBuffer.alloc(size=4 * 1024 * 1024)
  buf.write(embedding_bytes)
  handle = handle_to_b64(buf.export_handle())
  broker_client.send("consumer", json.dumps({"handle": handle, "size": buf.size}))

  # Consumer process
  msg = broker_client.recv()
  info = json.loads(msg["payload"])
  remote = CudaIpcBuffer.from_handle(handle_from_b64(info["handle"]), info["size"])
  data = remote.read()
  remote.close()
  buf.close()

The handle can be transported over any channel: the AF_UNIX broker (Phase 2),
a MemfdChannel header (Phase 3), or any shared file.  No fd-passing is needed —
the handle is just 64 bytes of opaque bytes.

Tested on: NVIDIA GB10 Grace Blackwell, CUDA 13.0, Driver 580.126.09, Ubuntu 24.04
aarch64 (spark-3cdf).  Grace Blackwell uses unified memory: GPU allocations are
accessible from both CPU and GPU via NVLink-C2C fabric, but cudaMemcpy is still
required to move data through the CUDA programming model.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.util
import os
from typing import Literal

# ---------------------------------------------------------------------------
# libcudart loading
# ---------------------------------------------------------------------------

_CUDART_NAMES = [
    "libcudart.so",
    "libcudart.so.13",
    "libcudart.so.12",
    "/usr/local/cuda/lib64/libcudart.so",
    "/usr/local/cuda-13.0/targets/sbsa-linux/lib/libcudart.so",
]

def _load_cudart() -> ctypes.CDLL | None:
    name = ctypes.util.find_library("cudart")
    if name:
        try:
            return ctypes.CDLL(name)
        except OSError:
            pass
    for candidate in _CUDART_NAMES:
        try:
            return ctypes.CDLL(candidate)
        except OSError:
            continue
    return None

_lib: ctypes.CDLL | None = _load_cudart()

# ---------------------------------------------------------------------------
# CUDA constants
# ---------------------------------------------------------------------------

_cudaMemcpyHostToDevice   = ctypes.c_int(1)
_cudaMemcpyDeviceToHost   = ctypes.c_int(2)
_cudaMemcpyDeviceToDevice = ctypes.c_int(3)
_cudaIpcMemLazyEnablePeerAccess = ctypes.c_uint(1)
_IPC_HANDLE_SIZE = 64  # sizeof(cudaIpcMemHandle_t)

# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------

class CudaError(RuntimeError):
    def __init__(self, fn: str, code: int) -> None:
        self.code = code
        super().__init__(f"{fn} returned CUDA error {code}")


def _check(fn: str, code: int) -> None:
    if code != 0:
        raise CudaError(fn, code)


# ---------------------------------------------------------------------------
# Low-level wrappers
# ---------------------------------------------------------------------------

def _cuda_get_device_count() -> int:
    if _lib is None:
        return 0
    n = ctypes.c_int(0)
    code = _lib.cudaGetDeviceCount(ctypes.byref(n))
    if code != 0:
        return 0
    return n.value


def _cuda_set_device(device: int) -> None:
    _check("cudaSetDevice", _lib.cudaSetDevice(ctypes.c_int(device)))


def _cuda_malloc(size: int) -> ctypes.c_void_p:
    ptr = ctypes.c_void_p(0)
    _check("cudaMalloc", _lib.cudaMalloc(ctypes.byref(ptr), ctypes.c_size_t(size)))
    return ptr


def _cuda_free(ptr: ctypes.c_void_p) -> None:
    _lib.cudaFree(ptr)


def _cuda_memcpy_h2d(dst: ctypes.c_void_p, src: bytes, size: int) -> None:
    c_src = ctypes.create_string_buffer(src, size)
    _check("cudaMemcpy(H→D)", _lib.cudaMemcpy(dst, c_src, ctypes.c_size_t(size), _cudaMemcpyHostToDevice))


def _cuda_memcpy_d2h(src: ctypes.c_void_p, size: int) -> bytes:
    dst = ctypes.create_string_buffer(size)
    _check("cudaMemcpy(D→H)", _lib.cudaMemcpy(dst, src, ctypes.c_size_t(size), _cudaMemcpyDeviceToHost))
    return bytes(dst)


def _cuda_ipc_get_handle(ptr: ctypes.c_void_p) -> bytes:
    handle = ctypes.create_string_buffer(_IPC_HANDLE_SIZE)
    _check("cudaIpcGetMemHandle", _lib.cudaIpcGetMemHandle(handle, ptr))
    return bytes(handle)


def _cuda_ipc_open_handle(handle_bytes: bytes) -> ctypes.c_void_p:
    handle = ctypes.create_string_buffer(handle_bytes, _IPC_HANDLE_SIZE)
    ptr = ctypes.c_void_p(0)
    _check("cudaIpcOpenMemHandle",
           _lib.cudaIpcOpenMemHandle(ctypes.byref(ptr), handle, _cudaIpcMemLazyEnablePeerAccess))
    return ptr


def _cuda_ipc_close_handle(ptr: ctypes.c_void_p) -> None:
    _lib.cudaIpcCloseMemHandle(ptr)


def _offset_ptr(ptr: ctypes.c_void_p, offset: int) -> ctypes.c_void_p:
    return ctypes.c_void_p((ptr.value or 0) + offset)


# ---------------------------------------------------------------------------
# Public capability check
# ---------------------------------------------------------------------------

def cuda_ipc_available() -> bool:
    """True if libcudart.so is loadable and at least one CUDA device is present."""
    return _lib is not None and _cuda_get_device_count() > 0


def device_count() -> int:
    """Number of CUDA devices visible to this process."""
    return _cuda_get_device_count()


# ---------------------------------------------------------------------------
# CudaIpcBuffer
# ---------------------------------------------------------------------------

class CudaIpcBuffer:
    """
    A CUDA device buffer that can be shared across processes via IPC handles.

    Lifecycle:

      Producer:
        buf = CudaIpcBuffer.alloc(size=N, device=0)
        buf.write(data)
        handle = buf.export_handle()   # 64 bytes, send to consumer
        ...
        buf.close()

      Consumer (in a different process):
        buf = CudaIpcBuffer.from_handle(handle_bytes, size=N, device=0)
        data = buf.read()
        buf.close()
    """

    def __init__(
        self,
        ptr: ctypes.c_void_p,
        size: int,
        device: int,
        owner: bool,
    ) -> None:
        self._ptr = ptr
        self.size = size
        self.device = device
        self._owner = owner
        self._closed = False

    # --- construction -------------------------------------------------------

    @classmethod
    def alloc(cls, size: int, device: int = 0) -> "CudaIpcBuffer":
        """Allocate *size* bytes of device memory on *device*."""
        if _lib is None:
            raise RuntimeError("libcudart.so not found — CUDA not available")
        _cuda_set_device(device)
        ptr = _cuda_malloc(size)
        return cls(ptr=ptr, size=size, device=device, owner=True)

    @classmethod
    def from_handle(cls, handle_bytes: bytes, size: int, device: int = 0) -> "CudaIpcBuffer":
        """Import a 64-byte IPC handle and map the remote buffer into this process."""
        if _lib is None:
            raise RuntimeError("libcudart.so not found — CUDA not available")
        if len(handle_bytes) != _IPC_HANDLE_SIZE:
            raise ValueError(f"handle must be {_IPC_HANDLE_SIZE} bytes, got {len(handle_bytes)}")
        _cuda_set_device(device)
        ptr = _cuda_ipc_open_handle(handle_bytes)
        return cls(ptr=ptr, size=size, device=device, owner=False)

    # --- data transfer ------------------------------------------------------

    def write(self, data: bytes, offset: int = 0) -> int:
        """Copy *data* from host (CPU) to device (GPU).  Returns bytes written."""
        self._check_open()
        end = offset + len(data)
        if end > self.size:
            raise ValueError(
                f"write of {len(data)} bytes at offset {offset} exceeds buffer size {self.size}"
            )
        _cuda_memcpy_h2d(_offset_ptr(self._ptr, offset), data, len(data))
        return len(data)

    def read(self, length: int | None = None, offset: int = 0) -> bytes:
        """Copy *length* bytes from device (GPU) to host (CPU)."""
        self._check_open()
        n = (self.size - offset) if length is None else length
        if offset + n > self.size:
            raise ValueError(
                f"read of {n} bytes at offset {offset} exceeds buffer size {self.size}"
            )
        return _cuda_memcpy_d2h(_offset_ptr(self._ptr, offset), n)

    # --- handle export ------------------------------------------------------

    def export_handle(self) -> bytes:
        """Return 64-byte opaque IPC handle for this buffer.

        The handle can be sent to another process over any channel (broker JSON,
        MemfdChannel header, file, etc.) and imported with CudaIpcBuffer.from_handle().
        Only owner buffers (created via alloc()) can export a handle.
        """
        self._check_open()
        if not self._owner:
            raise PermissionError("imported (non-owner) buffers cannot export a handle")
        return _cuda_ipc_get_handle(self._ptr)

    # --- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owner:
            _cuda_free(self._ptr)
        else:
            _cuda_ipc_close_handle(self._ptr)

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("CudaIpcBuffer is closed")

    def __enter__(self) -> "CudaIpcBuffer":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"CudaIpcBuffer(size={self.size}, device={self.device}, "
            f"owner={self._owner}, closed={self._closed})"
        )


# ---------------------------------------------------------------------------
# Handle serialisation helpers (for JSON broker transport)
# ---------------------------------------------------------------------------

def handle_to_b64(handle_bytes: bytes) -> str:
    """Encode a 64-byte IPC handle as a base64 string for JSON transport."""
    if len(handle_bytes) != _IPC_HANDLE_SIZE:
        raise ValueError(f"expected {_IPC_HANDLE_SIZE} bytes, got {len(handle_bytes)}")
    return base64.b64encode(handle_bytes).decode()


def handle_from_b64(s: str) -> bytes:
    """Decode a base64 string back to a 64-byte IPC handle."""
    data = base64.b64decode(s)
    if len(data) != _IPC_HANDLE_SIZE:
        raise ValueError(f"decoded handle must be {_IPC_HANDLE_SIZE} bytes, got {len(data)}")
    return data
