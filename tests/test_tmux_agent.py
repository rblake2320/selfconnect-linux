"""
Tests for the optional tmux adapter.

All tests skip cleanly when tmux is not installed.
The core PTY lane does not require tmux — these tests only verify the
optional adapter layer.
"""
import sys
import uuid
import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Linux only")

tmux_skip = pytest.mark.skipif(
    not __import__("shutil").which("tmux"),
    reason="tmux not installed",
)


def test_is_available_returns_bool():
    from self_connect_linux.tmux_agent import is_available
    result = is_available()
    assert isinstance(result, bool)


def test_list_sessions_without_tmux_returns_empty():
    """list_sessions() never raises even when tmux is absent."""
    from self_connect_linux import tmux_agent
    if not tmux_agent.is_available():
        assert tmux_agent.list_sessions() == []


@tmux_skip
def test_new_session_and_kill():
    from self_connect_linux import tmux_agent
    name = f"sc-test-{uuid.uuid4().hex[:6]}"
    try:
        receipt = tmux_agent.new_session(name)
        assert receipt.success is True
        assert name in tmux_agent.list_sessions()
    finally:
        tmux_agent.kill_session(name)
    assert name not in tmux_agent.list_sessions()


@tmux_skip
def test_send_keys_and_capture():
    from self_connect_linux import tmux_agent
    name = f"sc-test-{uuid.uuid4().hex[:6]}"
    try:
        tmux_agent.new_session(name, cmd="/bin/bash")
        import time; time.sleep(0.3)
        tmux_agent.send_keys(name, "echo TMUX_CAPTURE_TEST", enter=True)
        time.sleep(0.5)
        text, receipt = tmux_agent.capture_pane(name)
        assert receipt.success is True
        assert "TMUX_CAPTURE_TEST" in text
    finally:
        tmux_agent.kill_session(name)


def test_send_keys_raises_without_tmux():
    """send_keys() must raise RuntimeError when tmux is missing."""
    from self_connect_linux import tmux_agent
    if tmux_agent.is_available():
        pytest.skip("tmux is installed — cannot test absence")
    with pytest.raises(RuntimeError, match="tmux not found"):
        tmux_agent.send_keys("no-session", "hello")


def test_capture_pane_raises_without_tmux():
    from self_connect_linux import tmux_agent
    if tmux_agent.is_available():
        pytest.skip("tmux is installed — cannot test absence")
    with pytest.raises(RuntimeError, match="tmux not found"):
        tmux_agent.capture_pane("no-session")
