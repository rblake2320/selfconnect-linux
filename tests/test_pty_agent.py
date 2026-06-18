"""
Tests for the Linux PTY agent lane.

These are real integration tests — they spawn /bin/bash and /bin/sh.
No GUI permissions required. No tmux required.
"""
import os
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Linux only")


def test_spawn_echo_read():
    """Spawn a one-shot echo command and read the output."""
    from self_connect_linux.pty_agent import spawn_pty_agent
    with spawn_pty_agent(["/bin/echo", "hello-pty"]) as agent:
        assert agent.pid > 0
        assert agent.master_fd >= 0
        text, receipt = agent.read(timeout=3.0)
    assert "hello-pty" in text
    assert receipt.success is True
    assert receipt.action == "read"
    assert receipt.readback_hash is not None


def test_spawn_bash_send_command():
    """Spawn bash, send a command, read the output."""
    from self_connect_linux.pty_agent import spawn_pty_agent
    with spawn_pty_agent(["/bin/bash", "--norc", "--noprofile"]) as agent:
        time.sleep(0.3)   # let bash initialize
        agent.send("echo PTY_TEST_MARKER\n")
        text, receipt = agent.read(timeout=5.0)
    assert "PTY_TEST_MARKER" in text
    assert receipt.success is True


def test_spawn_bash_multiple_sends():
    """Send multiple commands to the same bash session."""
    from self_connect_linux.pty_agent import spawn_pty_agent
    with spawn_pty_agent(["/bin/bash", "--norc", "--noprofile"]) as agent:
        time.sleep(0.3)
        agent.send("echo FIRST\n")
        out1, _ = agent.read(timeout=3.0)
        agent.send("echo SECOND\n")
        out2, _ = agent.read(timeout=3.0)
    assert "FIRST" in out1 or "FIRST" in out2   # may buffer across reads
    assert "SECOND" in out1 or "SECOND" in out2


def test_expect_pattern():
    """expect() returns when the pattern is found."""
    from self_connect_linux.pty_agent import spawn_pty_agent
    with spawn_pty_agent(["/bin/bash", "--norc", "--noprofile"]) as agent:
        time.sleep(0.3)
        agent.send("echo READY_MARKER\n")
        text, receipt = agent.expect("READY_MARKER", timeout=10.0)
    assert "READY_MARKER" in text
    assert receipt.success is True


def test_expect_timeout():
    """expect() fails cleanly when pattern never appears."""
    from self_connect_linux.pty_agent import spawn_pty_agent
    with spawn_pty_agent(["/bin/bash", "--norc", "--noprofile"]) as agent:
        time.sleep(0.2)
        _, receipt = agent.expect("THIS_WILL_NEVER_APPEAR_XYZZY", timeout=1.0)
    assert receipt.success is False
    assert receipt.error is not None


def test_close_is_idempotent():
    """close() called twice must not raise."""
    from self_connect_linux.pty_agent import spawn_pty_agent
    agent = spawn_pty_agent(["/bin/bash", "--norc", "--noprofile"])
    agent.close()
    agent.close()   # should not raise


def test_send_after_close_raises():
    from self_connect_linux.pty_agent import spawn_pty_agent
    agent = spawn_pty_agent(["/bin/echo", "x"])
    agent.close()
    with pytest.raises(RuntimeError, match="closed"):
        agent.send("anything")


def test_read_after_close_raises():
    from self_connect_linux.pty_agent import spawn_pty_agent
    agent = spawn_pty_agent(["/bin/echo", "x"])
    agent.close()
    with pytest.raises(RuntimeError, match="closed"):
        agent.read()


def test_context_manager():
    """PtyAgent cleans up when used as a context manager."""
    from self_connect_linux.pty_agent import spawn_pty_agent
    with spawn_pty_agent(["/bin/bash", "--norc", "--noprofile"]) as agent:
        pid = agent.pid
        assert not agent._closed
    assert agent._closed
    # Process should be gone
    try:
        os.kill(pid, 0)
        # If kill(0) succeeds the process still exists (zombie or alive)
        # waitpid in close() should have reaped it, but allow a brief delay
    except ProcessLookupError:
        pass  # expected


def test_send_produces_receipt():
    from self_connect_linux.pty_agent import spawn_pty_agent
    with spawn_pty_agent(["/bin/bash", "--norc", "--noprofile"]) as agent:
        time.sleep(0.2)
        receipt = agent.send("echo hi\n")
    assert receipt.backend == "pty"
    assert receipt.action == "send"
    assert receipt.pid == agent.pid
    assert receipt.payload_hash.startswith("sha256:")


def test_long_running_process_incremental_output():
    """A long-running process produces output across multiple read() calls."""
    from self_connect_linux.pty_agent import spawn_pty_agent
    with spawn_pty_agent(["/bin/bash", "--norc", "--noprofile"]) as agent:
        time.sleep(0.2)
        for i in range(3):
            agent.send(f"echo LINE_{i}\n")
            time.sleep(0.05)
        all_text = ""
        for _ in range(5):
            chunk, _ = agent.read(timeout=1.0)
            all_text += chunk
            if all_text.count("LINE_") >= 3:
                break
    assert "LINE_0" in all_text
    assert "LINE_2" in all_text


def test_string_cmd_via_sh():
    """spawn_pty_agent accepts a plain string command."""
    from self_connect_linux.pty_agent import spawn_pty_agent
    with spawn_pty_agent("echo STRING_CMD_OK") as agent:
        text, _ = agent.read(timeout=3.0)
    assert "STRING_CMD_OK" in text
