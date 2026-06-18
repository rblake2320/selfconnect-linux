"""Tests for Phase 3 memfd/eventfd IPC bus (shm.py)."""
import os
import socket
import sys
import threading

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Linux only")

from self_connect_linux.shm import (
    MemfdChannel,
    EventfdChannel,
    send_fds,
    recv_fds,
    shm_available,
)


# ---------------------------------------------------------------------------
# Capability guard — skip entire module if kernel doesn't have memfd / eventfd
# ---------------------------------------------------------------------------

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="Linux only"),
    pytest.mark.skipif(not shm_available(), reason="memfd_create/eventfd not available"),
]


# ---------------------------------------------------------------------------
# MemfdChannel
# ---------------------------------------------------------------------------

def test_memfd_create_and_close():
    ch = MemfdChannel.create(size=4096)
    assert ch.fd >= 0
    assert ch.size == 4096
    ch.close()


def test_memfd_write_and_read():
    with MemfdChannel.create(size=1024) as ch:
        written = ch.write(b"hello memfd!")
        assert written == 12
        data = ch.read(length=12)
        assert data == b"hello memfd!"


def test_memfd_read_full_region():
    payload = b"X" * 256
    with MemfdChannel.create(size=256) as ch:
        ch.write(payload)
        data = ch.read()
        assert data == payload


def test_memfd_offset_write_and_read():
    with MemfdChannel.create(size=64) as ch:
        ch.write(b"ABC", offset=10)
        data = ch.read(length=3, offset=10)
        assert data == b"ABC"


def test_memfd_write_overflow_raises():
    with MemfdChannel.create(size=8) as ch:
        with pytest.raises(ValueError):
            ch.write(b"too much data here!")


def test_memfd_seal_prevents_write():
    with MemfdChannel.create(size=64) as ch:
        ch.write(b"initial")
        ch.seal()
        with pytest.raises(PermissionError):
            ch.write(b"blocked")


def test_memfd_sealed_region_still_readable():
    with MemfdChannel.create(size=64) as ch:
        ch.write(b"sealed data")
        ch.seal()
        data = ch.read(length=11)
        assert data == b"sealed data"


def test_memfd_non_owner_cannot_write():
    ch = MemfdChannel(fd=os.open("/dev/null", os.O_RDONLY), size=64, owner=False)
    with pytest.raises(PermissionError):
        ch.write(b"nope")
    ch.close()


def test_memfd_close_is_idempotent():
    ch = MemfdChannel.create(size=64)
    ch.close()
    ch.close()  # should not raise


# ---------------------------------------------------------------------------
# EventfdChannel
# ---------------------------------------------------------------------------

def test_eventfd_create_and_close():
    sig = EventfdChannel.create()
    assert sig.fd >= 0
    sig.close()


def test_eventfd_signal_and_wait():
    with EventfdChannel.create() as sig:
        sig.signal(1)
        val = sig.wait()
        assert val == 1


def test_eventfd_signal_count_accumulates():
    with EventfdChannel.create() as sig:
        sig.signal(3)
        sig.signal(5)
        val = sig.wait()
        assert val == 8


def test_eventfd_poll_returns_zero_when_empty():
    with EventfdChannel.create() as sig:
        val = sig.poll()
        assert val == 0


def test_eventfd_poll_nonblocking():
    with EventfdChannel.create() as sig:
        sig.signal(7)
        val = sig.poll()
        assert val == 7
        val2 = sig.poll()
        assert val2 == 0  # consumed by first poll


def test_eventfd_threaded_signal_wait():
    with EventfdChannel.create() as sig:
        results = []

        def reader():
            results.append(sig.wait())

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        import time; time.sleep(0.02)
        sig.signal(42)
        t.join(timeout=2.0)
        assert results == [42]


def test_eventfd_close_is_idempotent():
    sig = EventfdChannel.create()
    sig.close()
    sig.close()


# ---------------------------------------------------------------------------
# FD passing (send_fds / recv_fds)
# ---------------------------------------------------------------------------

def test_fd_passing_memfd():
    """Pass a MemfdChannel FD over a socketpair and read the content."""
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    with MemfdChannel.create(size=128) as ch:
        payload = b"fd_passing_test"
        ch.write(payload)

        send_fds(a, [ch.fd])
        received_fds = recv_fds(b, 1)
        assert len(received_fds) == 1

        remote_ch = MemfdChannel(fd=received_fds[0], size=128, owner=False)
        data = remote_ch.read(length=len(payload))
        assert data == payload
        remote_ch.close()

    a.close()
    b.close()


def test_fd_passing_eventfd():
    """Pass an EventfdChannel FD over a socketpair — signal crosses the boundary."""
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    with EventfdChannel.create() as sig:
        send_fds(a, [sig.fd])
        received_fds = recv_fds(b, 1)
        assert len(received_fds) == 1

        remote_sig = EventfdChannel(fd=received_fds[0], owner=False)
        sig.signal(99)
        val = remote_sig.wait()
        assert val == 99
        remote_sig.close()

    a.close()
    b.close()


def test_fd_passing_memfd_and_eventfd_together():
    """Simulate producer → consumer: write payload, signal; consumer waits, reads."""
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)

    with MemfdChannel.create(size=256) as ch:
        with EventfdChannel.create() as sig:
            ch.write(b"joint_fd_pass")
            send_fds(a, [ch.fd, sig.fd])
            received_fds = recv_fds(b, 2)
            assert len(received_fds) == 2

            r_ch = MemfdChannel(fd=received_fds[0], size=256, owner=False)
            r_sig = EventfdChannel(fd=received_fds[1], owner=False)

            sig.signal(1)
            assert r_sig.wait() == 1
            data = r_ch.read(length=13)
            assert data == b"joint_fd_pass"

            r_ch.close()
            r_sig.close()

    a.close()
    b.close()


# ---------------------------------------------------------------------------
# shm_available()
# ---------------------------------------------------------------------------

def test_shm_available_returns_true_on_dgx():
    assert shm_available() is True
