"""Tests for Phase 6 NCCL coordination layer (nccl.py)."""
import sys
import threading
import time

import pytest

from self_connect_linux.nccl import nccl_available

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="Linux only"),
    pytest.mark.skipif(not nccl_available(), reason="NCCL not available"),
]


def test_nccl_available():
    assert nccl_available() is True


def test_nccl_versions():
    from self_connect_linux.nccl import get_build_version, get_runtime_version
    bv = get_build_version()
    rv = get_runtime_version()
    assert bv is None or isinstance(bv, int)
    assert rv is None or isinstance(rv, int)
    if rv is not None:
        assert rv > 0


def test_generate_unique_id_returns_128_bytes():
    from self_connect_linux.nccl import generate_unique_id
    uid = generate_unique_id()
    assert isinstance(uid, bytes)
    assert len(uid) == 128


def test_generate_unique_id_is_unique():
    from self_connect_linux.nccl import generate_unique_id
    uid1 = generate_unique_id()
    uid2 = generate_unique_id()
    assert uid1 != uid2


def test_uid_roundtrip_base64():
    from self_connect_linux.nccl import generate_unique_id, _uid_to_b64, _uid_from_b64
    uid = generate_unique_id()
    b64 = _uid_to_b64(uid)
    assert isinstance(b64, str)
    assert _uid_from_b64(b64) == uid


def test_ncclcomm_loopback_world_size_1():
    """NcclComm with world_size=1 is valid loopback mode."""
    import cupy
    from self_connect_linux.nccl import NcclComm, generate_unique_id
    uid = generate_unique_id()
    with NcclComm.init(uid, rank=0, world_size=1, device=0) as comm:
        assert comm.rank == 0
        assert comm.world_size == 1
        assert comm.device == 0
        assert not comm._closed


def test_ncclcomm_close_is_idempotent():
    from self_connect_linux.nccl import NcclComm, generate_unique_id
    uid = generate_unique_id()
    comm = NcclComm.init(uid, rank=0, world_size=1, device=0)
    comm.close()
    assert comm._closed
    comm.close()  # second close must not raise


def test_ncclcomm_allreduce_after_close_raises():
    import cupy
    from self_connect_linux.nccl import NcclComm, generate_unique_id
    uid = generate_unique_id()
    comm = NcclComm.init(uid, rank=0, world_size=1, device=0)
    comm.close()
    arr = cupy.array([1.0, 2.0])
    with pytest.raises(RuntimeError, match="closed"):
        comm.allreduce(arr, arr)


def test_ncclcomm_allreduce_loopback():
    """allreduce on a single-rank comm is a no-op — array unchanged."""
    import cupy
    from self_connect_linux.nccl import NcclComm, generate_unique_id
    uid = generate_unique_id()
    with NcclComm.init(uid, rank=0, world_size=1, device=0) as comm:
        arr = cupy.array([1.0, 2.0, 3.0], dtype=cupy.float32)
        out = cupy.zeros_like(arr)
        comm.allreduce(arr, out, op="sum")
        cupy.cuda.Device(0).synchronize()
        result = out.get()
        assert list(result) == pytest.approx([1.0, 2.0, 3.0])


def test_ncclcomm_invalid_op_raises():
    import cupy
    from self_connect_linux.nccl import NcclComm, generate_unique_id
    uid = generate_unique_id()
    with NcclComm.init(uid, rank=0, world_size=1, device=0) as comm:
        arr = cupy.array([1.0], dtype=cupy.float32)
        with pytest.raises(ValueError, match="Unknown NCCL op"):
            comm.allreduce(arr, arr, op="xyzzy")


def test_ncclcomm_repr():
    from self_connect_linux.nccl import NcclComm, generate_unique_id
    uid = generate_unique_id()
    with NcclComm.init(uid, rank=0, world_size=1, device=0) as comm:
        r = repr(comm)
        assert "NcclComm" in r
        assert "rank=0" in r


def test_nccl_rank_negotiate_world_size_1():
    """Rank negotiate with world_size=1 — root just returns the ID immediately."""
    from self_connect_linux.nccl import nccl_rank_negotiate, generate_unique_id
    import tempfile, os
    from self_connect_linux import BrokerServer, BrokerClient

    import shutil
    tmpdir = tempfile.mkdtemp()
    sock = os.path.join(tmpdir, "broker.sock")
    try:
        with BrokerServer(socket_path=sock) as _broker:
            time.sleep(0.05)
            with BrokerClient(f"test-job:rank0", socket_path=sock) as c:
                uid = nccl_rank_negotiate(c, "test-job", rank=0, world_size=1)
                assert isinstance(uid, bytes)
                assert len(uid) == 128
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_nccl_rank_negotiate_two_ranks():
    """Root sends UniqueId to rank 1 via broker; both get the same bytes."""
    import tempfile, os, shutil
    from self_connect_linux import BrokerServer, BrokerClient
    from self_connect_linux.nccl import nccl_rank_negotiate

    tmpdir = tempfile.mkdtemp()
    sock = os.path.join(tmpdir, "broker.sock")
    results: list[bytes | None] = [None, None]
    errors: list[Exception | None] = [None, None]

    def run_rank(rank: int):
        try:
            with BrokerClient(f"negotiate-test:rank{rank}", socket_path=sock) as c:
                uid = nccl_rank_negotiate(c, "negotiate-test", rank=rank, world_size=2, timeout=10.0)
                results[rank] = uid
        except Exception as exc:
            errors[rank] = exc

    try:
        with BrokerServer(socket_path=sock) as _broker:
            time.sleep(0.05)
            t0 = threading.Thread(target=run_rank, args=(0,))
            t1 = threading.Thread(target=run_rank, args=(1,))
            t1.start()
            time.sleep(0.1)  # let rank 1 register first
            t0.start()
            t0.join(timeout=15)
            t1.join(timeout=15)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    assert errors[0] is None, f"rank 0 error: {errors[0]}"
    assert errors[1] is None, f"rank 1 error: {errors[1]}"
    assert results[0] is not None
    assert results[1] is not None
    assert results[0] == results[1], "Both ranks must receive the same UniqueId"
    assert len(results[0]) == 128


def test_nccl_rank_negotiate_invalid_rank_raises():
    from self_connect_linux.nccl import nccl_rank_negotiate
    import tempfile, os, shutil
    from self_connect_linux import BrokerServer, BrokerClient

    tmpdir = tempfile.mkdtemp()
    sock = os.path.join(tmpdir, "broker.sock")
    try:
        with BrokerServer(socket_path=sock):
            time.sleep(0.05)
            with BrokerClient("bad:rank0", socket_path=sock) as c:
                with pytest.raises(ValueError, match="out of range"):
                    nccl_rank_negotiate(c, "bad", rank=5, world_size=2)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
