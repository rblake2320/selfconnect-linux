"""
Linux-native PTY agent lane.

Primitive: os.openpty() gives a master/slave fd pair.
  - Slave fd → subprocess stdin/stdout/stderr (the agent's terminal)
  - Master fd → controller read/write (our side)

This is the Linux equivalent of Windows ConPTY + WriteConsoleInput + ReadConsoleOutput.
No tmux required. tmux_agent.py is an optional durability layer on top.
"""
import fcntl
import os
import re
import select
import signal
import struct
import subprocess
import termios
import time
import uuid
from dataclasses import dataclass, field

from .receipts import ActionReceipt, make_receipt


@dataclass
class PtyAgent:
    """
    A live agent process running under a PTY master/slave pair.
    Send text to the agent via send(); read its output via read() or expect().
    """
    agent_id: str
    cmd: list[str]
    pid: int
    master_fd: int
    _closed: bool = field(default=False, repr=False)
    _pending_echo: list[bytes] = field(default_factory=list, repr=False)

    def send(self, text: str) -> ActionReceipt:
        """Write text to the PTY master (delivered to the agent's stdin)."""
        if self._closed:
            raise RuntimeError(f"PtyAgent {self.agent_id!r} is already closed")
        encoded = text.encode()
        self._pending_echo.append(encoded)
        os.write(self.master_fd, encoded)
        return make_receipt(backend="pty", pid=self.pid, action="send", payload=text)

    def read(self, timeout: float = 5.0, max_bytes: int = 65536) -> tuple[str, ActionReceipt]:
        """
        Read available output from the PTY master.
        Strips echo of previously sent text from the readback.
        Returns (filtered_output, receipt).
        """
        if self._closed:
            raise RuntimeError(f"PtyAgent {self.agent_id!r} is already closed")
        raw = _read_pty(self.master_fd, timeout=timeout, max_bytes=max_bytes)
        filtered, was_filtered = _echo_filter(raw.encode(), self._pending_echo)
        if was_filtered:
            self._pending_echo.clear()
        text = filtered.decode("utf-8", errors="replace")
        receipt = make_receipt(
            backend="pty", pid=self.pid, action="read", payload="",
            readback=text, echo_filtered=was_filtered,
        )
        return text, receipt

    def expect(
        self,
        pattern: str,
        timeout: float = 30.0,
    ) -> tuple[str, ActionReceipt]:
        """
        Accumulate PTY output until `pattern` (regex) matches or `timeout` expires.
        Returns (all_accumulated_output, receipt).
        Receipt success=True only if the pattern was found within timeout.
        """
        if self._closed:
            raise RuntimeError(f"PtyAgent {self.agent_id!r} is already closed")
        compiled = re.compile(pattern)
        buf = b""
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            chunk = _read_raw_pty(self.master_fd, timeout=min(remaining, 0.1))
            if chunk:
                buf += chunk

            filtered, was_filtered = _echo_filter(buf, self._pending_echo)
            if was_filtered:
                self._pending_echo.clear()
                buf = filtered

            text = buf.decode("utf-8", errors="replace")
            if compiled.search(text):
                receipt = make_receipt(
                    backend="pty", pid=self.pid, action="expect",
                    payload=pattern, readback=text, echo_filtered=was_filtered,
                    success=True,
                )
                return text, receipt

        text = buf.decode("utf-8", errors="replace")
        receipt = make_receipt(
            backend="pty", pid=self.pid, action="expect",
            payload=pattern, readback=text, echo_filtered=False,
            success=False,
            error=f"Timeout after {timeout}s waiting for {pattern!r}",
        )
        return text, receipt

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        try:
            os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __repr__(self):
        return (
            f"PtyAgent(id={self.agent_id!r}, pid={self.pid}, "
            f"cmd={self.cmd!r}, closed={self._closed})"
        )


# ── Public factory ────────────────────────────────────────────────────────────

def spawn_pty_agent(
    cmd: list[str] | str,
    env: dict | None = None,
    cwd: str | None = None,
    cols: int = 220,
    rows: int = 50,
) -> PtyAgent:
    """
    Spawn `cmd` under a PTY master/slave pair.

    The slave fd becomes the subprocess's controlling terminal and all three
    stdio streams. The master fd is returned inside PtyAgent for controller I/O.

    Uses os.openpty() + subprocess.Popen. No tmux dependency.
    """
    if isinstance(cmd, str):
        cmd = ["/bin/sh", "-c", cmd]

    master_fd, slave_fd = os.openpty()
    _set_winsize(slave_fd, rows, cols)

    # Non-blocking master for select-based incremental reads
    _set_nonblocking(master_fd)

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            env=env,
            cwd=cwd,
            start_new_session=True,  # new session = new process group, no signal leak
        )
    except Exception:
        os.close(slave_fd)
        os.close(master_fd)
        raise
    os.close(slave_fd)  # controller only needs the master end

    return PtyAgent(
        agent_id=str(uuid.uuid4())[:8],
        cmd=cmd,
        pid=proc.pid,
        master_fd=master_fd,
    )


# ── Private helpers ───────────────────────────────────────────────────────────

def _set_winsize(fd: int, rows: int, cols: int) -> None:
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        pass


def _set_nonblocking(fd: int) -> None:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def _read_raw_pty(master_fd: int, timeout: float, max_bytes: int = 65536) -> bytes:
    """
    Read raw bytes from PTY master.
    Waits up to `timeout` for the first byte, then drains with 50ms silence detection.
    """
    chunks: list[bytes] = []
    total = 0
    deadline = time.monotonic() + timeout
    got_first = False

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        wait = min(remaining, 0.05 if got_first else remaining)
        r, _, _ = select.select([master_fd], [], [], wait)
        if not r:
            if got_first:
                break  # 50ms silence after data — done
            continue   # still waiting for first byte within timeout
        try:
            data = os.read(master_fd, min(4096, max_bytes - total))
        except (BlockingIOError, InterruptedError):
            if got_first:
                break
            continue
        except OSError:
            break
        if not data:
            break
        chunks.append(data)
        total += len(data)
        got_first = True
        if total >= max_bytes:
            break

    return b"".join(chunks)


def _read_pty(master_fd: int, timeout: float, max_bytes: int = 65536) -> str:
    """Convenience wrapper: read raw bytes and decode to str."""
    return _read_raw_pty(master_fd, timeout, max_bytes).decode("utf-8", errors="replace")


def _echo_filter(buf: bytes, pending: list[bytes]) -> tuple[bytes, bool]:
    """
    Strip the first pending echo from buf if found.
    Handles CRLF/LF normalization that terminals may introduce.
    Returns (filtered_buf, was_filtered).
    """
    if not pending:
        return buf, False

    # Normalize CRLF to LF for matching
    buf_norm = buf.replace(b"\r\n", b"\n").replace(b"\r", b"\n")

    for sent in pending:
        sent_norm = sent.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        if sent_norm in buf_norm:
            filtered = buf_norm.replace(sent_norm, b"", 1)
            return filtered, True

    return buf, False
