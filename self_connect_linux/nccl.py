"""
Phase 6 — NCCL coordination layer for multi-process GPU operations.

NCCL (NVIDIA Collective Communications Library) performs efficient all-reduce,
broadcast, and scatter/gather operations across GPU processes.  On multi-GPU
DGX Spark, NCCL uses NVLink; on single-GPU configurations it operates in
loopback mode (world_size=1).

This module handles the *coordination* layer: how distributed agents discover
each other, negotiate ranks, and exchange the NCCL UniqueId that bootstraps a
communicator.  The broker (Phase 2) is the transport for this negotiation.

Typical usage:
    # On root rank — generates UniqueId and broadcasts via broker
    uid = nccl_rank_negotiate(broker_client, "job-1", rank=0, world_size=N)
    comm = NcclComm.init(uid, rank=0, world_size=N, device=0)
    # ... allreduce / broadcast ...
    comm.close()

    # On non-root ranks — waits for root's UniqueId via broker
    uid = nccl_rank_negotiate(broker_client, "job-1", rank=k, world_size=N)
    comm = NcclComm.init(uid, rank=k, world_size=N, device=k)

Capability gate: call nccl_available() before any NCCL operation.
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any


def nccl_available() -> bool:
    """True if NCCL is accessible via cupy.cuda.nccl."""
    try:
        import cupy.cuda.nccl as _nccl
        return bool(_nccl.available)
    except Exception:
        return False


def get_build_version() -> int | None:
    """Return the NCCL version cupy was compiled against, or None."""
    try:
        import cupy.cuda.nccl as _nccl
        return _nccl.get_build_version()
    except Exception:
        return None


def get_runtime_version() -> int | None:
    """Return the NCCL runtime version, or None."""
    try:
        import cupy.cuda.nccl as _nccl
        return _nccl.get_version()
    except Exception:
        return None


def generate_unique_id() -> bytes:
    """
    Generate a 128-byte NCCL UniqueId.

    Only the root rank (rank 0) calls this directly.  All ranks receive it
    via nccl_rank_negotiate(), which coordinates the exchange through the broker.
    """
    import cupy.cuda.nccl as _nccl
    return _nccl.get_unique_id()


def _uid_to_b64(uid: bytes) -> str:
    return base64.b64encode(uid).decode()


def _uid_from_b64(s: str) -> bytes:
    return base64.b64decode(s)


def nccl_rank_negotiate(
    broker_client: Any,
    session_name: str,
    rank: int,
    world_size: int,
    root: int = 0,
    timeout: float = 30.0,
) -> bytes:
    """
    Coordinate NCCL UniqueId distribution across all ranks via the broker.

    The root rank generates the UniqueId and sends it to every other rank's
    mailbox.  Non-root ranks poll their mailbox until the ID arrives.

    *broker_client* — a connected BrokerClient registered as
                      f"{session_name}:rank{rank}"
    *session_name*  — shared job identifier (same string on all ranks)
    *rank*          — this process's rank (0 .. world_size-1)
    *world_size*    — total number of ranks
    *root*          — which rank generates the ID (default 0)
    *timeout*       — seconds before non-root raises TimeoutError

    Returns the 128-byte NCCL UniqueId.
    """
    if world_size < 1:
        raise ValueError(f"world_size must be >= 1, got {world_size}")
    if not (0 <= rank < world_size):
        raise ValueError(f"rank {rank} out of range for world_size {world_size}")

    if rank == root:
        uid = generate_unique_id()
        uid_b64 = _uid_to_b64(uid)
        payload = json.dumps({
            "type": "nccl_uid",
            "session": session_name,
            "uid": uid_b64,
            "world_size": world_size,
        })
        for r in range(world_size):
            if r != root:
                broker_client.send(f"{session_name}:rank{r}", payload)
        return uid

    else:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = broker_client.recv()
            if msg:
                try:
                    data = json.loads(msg["payload"])
                    if data.get("type") == "nccl_uid" and data.get("session") == session_name:
                        return _uid_from_b64(data["uid"])
                except (json.JSONDecodeError, KeyError):
                    pass
            time.sleep(0.05)
        raise TimeoutError(
            f"nccl_rank_negotiate: rank {rank} did not receive UniqueId "
            f"for session {session_name!r} within {timeout}s"
        )


class NcclComm:
    """
    NCCL communicator wrapping cupy.cuda.nccl.NcclCommunicator.

    Each process in a distributed job creates one NcclComm with the same
    unique_id (from nccl_rank_negotiate) but a different rank.

    Single-GPU loopback (world_size=1) is valid for testing:
        with NcclComm.init(generate_unique_id(), rank=0, world_size=1) as comm:
            arr = cupy.array([1.0, 2.0, 3.0])
            comm.allreduce(arr, arr)  # no-op at size 1

    Multi-GPU (world_size>1 requires one CUDA device per rank):
        uid = nccl_rank_negotiate(client, "job-1", rank=my_rank, world_size=N)
        comm = NcclComm.init(uid, rank=my_rank, world_size=N, device=my_rank)
    """

    def __init__(self, _comm: Any, rank: int, world_size: int, device: int) -> None:
        self._comm = _comm
        self.rank = rank
        self.world_size = world_size
        self.device = device
        self._closed = False

    @classmethod
    def init(
        cls,
        unique_id: bytes,
        rank: int,
        world_size: int,
        device: int = 0,
    ) -> "NcclComm":
        """
        Initialize an NCCL communicator.

        *unique_id*  — 128-byte ID from generate_unique_id() / nccl_rank_negotiate()
        *rank*       — this process's rank (0..world_size-1)
        *world_size* — total communicator size
        *device*     — CUDA device index for this rank
        """
        import cupy
        import cupy.cuda.nccl as _nccl
        with cupy.cuda.Device(device):
            comm = _nccl.NcclCommunicator(world_size, unique_id, rank)
        return cls(comm, rank=rank, world_size=world_size, device=device)

    @staticmethod
    def _nccl_dtype(dtype: Any) -> int:
        """Map a numpy/cupy dtype to an NCCL datatype integer constant."""
        import cupy.cuda.nccl as _nccl
        import numpy as np
        dtype = np.dtype(dtype)
        _map = {
            np.dtype("float32"):  _nccl.NCCL_FLOAT32,
            np.dtype("float64"):  _nccl.NCCL_FLOAT64,
            np.dtype("float16"):  _nccl.NCCL_FLOAT16,
            np.dtype("int8"):     _nccl.NCCL_INT8,
            np.dtype("int32"):    _nccl.NCCL_INT32,
            np.dtype("int64"):    _nccl.NCCL_INT64,
            np.dtype("uint8"):    _nccl.NCCL_UINT8,
            np.dtype("uint32"):   _nccl.NCCL_UINT32,
            np.dtype("uint64"):   _nccl.NCCL_UINT64,
        }
        dt = _map.get(dtype)
        if dt is None:
            raise TypeError(f"Unsupported dtype for NCCL: {dtype} — supported: {list(_map)}")
        return dt

    def allreduce(
        self,
        sendbuf: Any,
        recvbuf: Any,
        op: str = "sum",
        stream: Any = None,
    ) -> None:
        """
        All-reduce across all ranks.

        *sendbuf* / *recvbuf* — cupy arrays (same dtype and shape)
        *op* — "sum" | "prod" | "max" | "min" | "avg"
        *stream* — cupy CUDA stream (None = default stream)
        """
        import cupy
        import cupy.cuda.nccl as _nccl
        if self._closed:
            raise RuntimeError("NcclComm is closed")
        _op_map = {
            "sum": _nccl.NCCL_SUM,
            "prod": _nccl.NCCL_PROD,
            "max": _nccl.NCCL_MAX,
            "min": _nccl.NCCL_MIN,
            "avg": _nccl.NCCL_AVG,
        }
        op_code = _op_map.get(op.lower())
        if op_code is None:
            raise ValueError(f"Unknown NCCL op: {op!r} — valid: {sorted(_op_map)}")
        s = stream.ptr if stream is not None else cupy.cuda.Stream.null.ptr
        self._comm.allReduce(
            sendbuf.data.ptr, recvbuf.data.ptr,
            sendbuf.size, self._nccl_dtype(sendbuf.dtype), op_code, s,
        )

    def broadcast(self, buf: Any, root: int = 0, stream: Any = None) -> None:
        """Broadcast *buf* from *root* to all ranks in-place."""
        import cupy
        if self._closed:
            raise RuntimeError("NcclComm is closed")
        s = stream.ptr if stream is not None else cupy.cuda.Stream.null.ptr
        self._comm.broadcast(buf.data.ptr, buf.data.ptr, buf.size, self._nccl_dtype(buf.dtype), root, s)

    def close(self) -> None:
        if not self._closed:
            try:
                self._comm.destroy()
            except Exception:
                pass
            self._closed = True

    def __enter__(self) -> "NcclComm":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def __repr__(self) -> str:
        status = "closed" if self._closed else f"rank={self.rank}/{self.world_size}"
        return f"NcclComm({status}, device={self.device})"
