"""
Optional tmux adapter.

tmux is NOT required for PTY agent control. This module is a convenience layer
that adds persistent sessions, pane capture, and human-visible attach/detach
on top of the raw PTY lane.

Behavior:
  - is_available() returns False when tmux is not installed.
  - All functions raise RuntimeError if called without tmux present.
  - Tests that import this module must skip cleanly when is_available() is False.
"""
import shutil
import subprocess

from .receipts import ActionReceipt, make_receipt


def is_available() -> bool:
    return shutil.which("tmux") is not None


def _require() -> None:
    if not is_available():
        raise RuntimeError("tmux not found — install tmux or use pty_agent directly")


def new_session(name: str, cmd: str = "/bin/bash") -> ActionReceipt:
    """Create a detached tmux session named `name` running `cmd`."""
    _require()
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, cmd],
        check=True,
    )
    return make_receipt(
        backend="tmux", pid=None, action="new_session",
        payload=f"{name}:{cmd}", success=True,
    )


def send_keys(
    session: str,
    keys: str,
    window: str = "",
    pane: str = "",
    enter: bool = False,
) -> ActionReceipt:
    """Send keystrokes to a tmux target (session[:window[.pane]])."""
    _require()
    target = _target(session, window, pane)
    args = ["tmux", "send-keys", "-t", target, keys]
    if enter:
        args.append("Enter")
    subprocess.run(args, check=True)
    return make_receipt(
        backend="tmux", pid=None, action="send_keys",
        payload=keys, success=True, metadata={"target": target},
    )


def capture_pane(
    session: str,
    window: str = "",
    pane: str = "",
) -> tuple[str, ActionReceipt]:
    """Capture current text content of a tmux pane."""
    _require()
    target = _target(session, window, pane)
    result = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", target],
        capture_output=True, text=True, check=True,
    )
    text = result.stdout
    receipt = make_receipt(
        backend="tmux", pid=None, action="capture_pane",
        payload="", readback=text, success=True, metadata={"target": target},
    )
    return text, receipt


def kill_session(name: str) -> ActionReceipt:
    _require()
    subprocess.run(["tmux", "kill-session", "-t", name], check=True)
    return make_receipt(
        backend="tmux", pid=None, action="kill_session",
        payload=name, success=True,
    )


def list_sessions() -> list[str]:
    """Return session names, or empty list when tmux is not installed."""
    if not is_available():
        return []
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True, text=True,
    )
    return [s.strip() for s in result.stdout.splitlines() if s.strip()]


def _target(session: str, window: str, pane: str) -> str:
    t = session
    if window:
        t += f":{window}"
    if pane:
        t += f".{pane}"
    return t
