"""
Phase 3 — memfd/eventfd zero-copy IPC bus.

Provides two primitives:

  MemfdChannel — a single shared memory region backed by memfd_create(2).
    Writer seals the region; reader maps it read-only.  For large payloads
    (token streams, embeddings, images) that cross process boundaries on the
    same DGX Spark host without any copy.

  EventfdChannel — a lightweight eventfd(2) counter used to signal between
    processes without busy-waiting.  Used as the rendezvous signal after a
    MemfdChannel write.

FD transport — both primitives rely on passing file descriptors over an
AF_UNIX SOCK_DGRAM socket via SCM_RIGHTS ancillary data.  The sender keeps
the memfd/eventfd; the receiver gets a duplicate that maps the same kernel
object.

Capability guard — call `shm_available()` first.  Returns False on kernels
that predate memfd_create (< Linux 3.17) or eventfd (< Linux 2.6.22).  On
spark-3cdf (Linux 6.17) both are available.

Example (single process):
    ch = MemfdChannel.create(size=4 * 1024 * 1024)
    ch.write(b"large embedding...")
    data = ch.read()
    ch.close()

Example (two processes via FD passing):
    # Producer
    ch = MemfdChannel.create(size=1024 * 1024)
    sig = EventfdChannel.create()
    sender_sock, _ = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    send_fds(sender_sock, [ch.fd, sig.fd])
    ch.write(b"payload")
    sig.signal()

    # Consumer (receives FDs from the other end of socketpair)
    fds = recv_fds(receiver_sock, 2)
    ch = MemfdChannel(fds[0], size=1024 * 1024, owner=False)
    sig = EventfdChannel(fds[1], owner=False)
    sig.wait()
    data = ch.read()
"""
from __future__ import annotations

import array
import ctypes
import ctypes.util
import mmap
import os
import socket
import struct
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# libc syscall helpers
# ---------------------------------------------------------------------------

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

_SYS_memfd_create_aarch64 = 279   # aarch64
_SYS_memfd_create_x86_64  = 319   # x86_64
_SYS_eventfd2_aarch64     = 19    # aarch64 (eventfd2)
_SYS_eventfd2_x86_64      = 290   # x86_64

import platform as _platform
_machine = _platform.machine().lower()
if "aarch64" in _machine or "arm64" in _machine:
    _SYS_memfd_create = _SYS_memfd_create_aarch64
    _SYS_eventfd2     = _SYS_eventfd2_aarch64
else:
    _SYS_memfd_create = _SYS_memfd_create_x86_64
    _SYS_eventfd2     = _SYS_eventfd2_x86_64

_MFD_CLOEXEC      = 0x0001
_MFD_ALLOW_SEALING = 0x0002
_F_ADD_SEALS      = 1033
_F_GET_SEALS      = 1034
_F_SEAL_SHRINK    = 0x0002
_F_SEAL_GROW      = 0x0004
_F_SEAL_WRITE     = 0x0008

_EFD_CLOEXEC      = 0x80000   # O_CLOEXEC for eventfd2
_EFD_SEMAPHORE    = 0x1


def _memfd_create(name: str, flags: int = _MFD_CLOEXEC | _MFD_ALLOW_SEALING) -> int:
    name_b = name.encode() + b"\x00"
    fd = _libc.syscall(_SYS_memfd_create, name_b, flags)
    if fd < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno), name)
    return fd


def _eventfd2(initval: int, flags: int = _EFD_CLOEXEC) -> int:
    fd = _libc.syscall(_SYS_eventfd2, ctypes.c_uint(initval), flags)
    if fd < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    return fd


def _seal_fd(fd: int) -> None:
    seals = _F_SEAL_SHRINK | _F_SEAL_GROW | _F_SEAL_WRITE
    ret = _libc.fcntl(fd, _F_ADD_SEALS, ctypes.c_int(seals))
    if ret < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))


# ---------------------------------------------------------------------------
# FD passing over AF_UNIX (SCM_RIGHTS)
# ---------------------------------------------------------------------------

def send_fds(sock: socket.socket, fds: list[int], msg: bytes = b"\x00") -> None:
    """Send file descriptors over an AF_UNIX socket via SCM_RIGHTS."""
    fds_arr = array.array("i", fds)
    cmsg = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds_arr)]
    sock.sendmsg([msg], cmsg)


def recv_fds(sock: socket.socket, maxfds: int, bufsize: int = 256) -> list[int]:
    """Receive file descriptors from an AF_UNIX socket via SCM_RIGHTS."""
    fds_arr = array.array("i")
    cmsg_space = socket.CMSG_SPACE(maxfds * fds_arr.itemsize)
    msg, ancdata, _flags, _addr = sock.recvmsg(bufsize, cmsg_space)
    fds = []
    for cmsg_level, cmsg_type, cmsg_data in ancdata:
        if cmsg_level == socket.SOL_SOCKET and cmsg_type == socket.SCM_RIGHTS:
            fds_arr.frombytes(cmsg_data[:len(cmsg_data) - (len(cmsg_data) % fds_arr.itemsize)])
            fds.extend(fds_arr)
    return fds


# ---------------------------------------------------------------------------
# Capability check
# ---------------------------------------------------------------------------

def shm_available() -> bool:
    """True if memfd_create and eventfd are available on this kernel."""
    try:
        fd = _memfd_create("sc_probe")
        os.close(fd)
    except (OSError, AttributeError):
        return False
    try:
        fd = _eventfd2(0)
        os.close(fd)
    except (OSError, AttributeError):
        return False
    return True


# ---------------------------------------------------------------------------
# MemfdChannel
# ---------------------------------------------------------------------------

class MemfdChannel:
    """
    Shared memory channel backed by a single memfd_create(2) region.

    The fd is inheritable across fork() and can be sent to another process
    via send_fds().  After write() the region is sealed so the receiver can
    map it read-only with confidence that the content won't change.

    Owner (created via MemfdChannel.create()) holds write access until seal().
    Non-owner (received via recv_fds()) maps the already-sealed fd read-only.
    """

    def __init__(self, fd: int, size: int, owner: bool = True) -> None:
        self.fd = fd
        self.size = size
        self._owner = owner
        self._sealed = False
        self._closed = False

    @classmethod
    def create(cls, size: int, name: str = "sc_shm") -> "MemfdChannel":
        fd = _memfd_create(name)
        os.ftruncate(fd, size)
        return cls(fd=fd, size=size, owner=True)

    def write(self, data: bytes, offset: int = 0) -> int:
        if self._closed:
            raise RuntimeError("MemfdChannel is closed")
        if not self._owner:
            raise PermissionError("non-owner cannot write to MemfdChannel")
        if self._sealed:
            raise PermissionError("MemfdChannel is sealed — no further writes")
        if offset + len(data) > self.size:
            raise ValueError(
                f"data ({len(data)} bytes at offset {offset}) exceeds channel size ({self.size})"
            )
        with mmap.mmap(self.fd, self.size, access=mmap.ACCESS_WRITE) as m:
            m.seek(offset)
            m.write(data)
        return len(data)

    def seal(self) -> None:
        if not self._owner:
            raise PermissionError("only owner can seal MemfdChannel")
        _seal_fd(self.fd)
        self._sealed = True

    def read(self, length: int | None = None, offset: int = 0) -> bytes:
        if self._closed:
            raise RuntimeError("MemfdChannel is closed")
        read_len = (self.size - offset) if length is None else length
        with mmap.mmap(self.fd, self.size, access=mmap.ACCESS_READ) as m:
            m.seek(offset)
            return m.read(read_len)

    def close(self) -> None:
        if not self._closed:
            os.close(self.fd)
            self._closed = True

    def __enter__(self) -> "MemfdChannel":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"MemfdChannel(fd={self.fd}, size={self.size}, owner={self._owner}, sealed={self._sealed})"


# ---------------------------------------------------------------------------
# EventfdChannel
# ---------------------------------------------------------------------------

class EventfdChannel:
    """
    Lightweight signalling channel backed by eventfd(2).

    signal(n=1) adds n to the counter.
    wait()      blocks until the counter is non-zero, then resets it to 0.
    poll()      non-blocking check — returns current value (0 if nothing pending).

    The fd can be sent to another process via send_fds() to establish a
    cross-process wake-up channel.
    """

    def __init__(self, fd: int, owner: bool = True) -> None:
        self.fd = fd
        self._owner = owner
        self._closed = False

    @classmethod
    def create(cls, initval: int = 0) -> "EventfdChannel":
        fd = _eventfd2(initval)
        return cls(fd=fd, owner=True)

    def signal(self, n: int = 1) -> None:
        if self._closed:
            raise RuntimeError("EventfdChannel is closed")
        os.write(self.fd, struct.pack("<Q", n))

    def wait(self) -> int:
        if self._closed:
            raise RuntimeError("EventfdChannel is closed")
        data = os.read(self.fd, 8)
        return struct.unpack("<Q", data)[0]

    def poll(self) -> int:
        if self._closed:
            raise RuntimeError("EventfdChannel is closed")
        import fcntl
        flags = fcntl.fcntl(self.fd, fcntl.F_GETFL)
        fcntl.fcntl(self.fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        try:
            data = os.read(self.fd, 8)
            return struct.unpack("<Q", data)[0]
        except BlockingIOError:
            return 0
        finally:
            fcntl.fcntl(self.fd, fcntl.F_SETFL, flags)

    def close(self) -> None:
        if not self._closed:
            os.close(self.fd)
            self._closed = True

    def __enter__(self) -> "EventfdChannel":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"EventfdChannel(fd={self.fd}, owner={self._owner})"
