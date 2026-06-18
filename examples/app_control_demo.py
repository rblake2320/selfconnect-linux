"""
Phase 5 end-to-end demo: X11 app control + AT-SPI readback.

Proves the full round-trip:
  1. Launch a dedicated gnome-terminal with a known unique title
  2. Focus it via X11 (by window ID, verified by title)
  3. Type a unique sentinel command via XTEST (type_text)
  4. Read the terminal output back via AT-SPI accessibility tree
  5. Verify the sentinel appears in the terminal buffer

Note: XTEST injects into the focused window. This demo launches its own
terminal (not the one running Claude Code) to avoid cross-contamination.

Run:
    python examples/app_control_demo.py
"""
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from self_connect_linux.at_spi import (
    at_spi_available,
    get_application_widgets,
    get_text,
)
from self_connect_linux.x11_input import (
    x11_available,
    list_windows,
    focus_window,
    type_text,
)

SENTINEL = f"SELFCONNECT_PHASE5_{uuid.uuid4().hex[:8].upper()}"
TITLE = f"SelfConnect-AppControl-{uuid.uuid4().hex[:6]}"


def poll_until(fn, timeout: float, interval: float = 0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(interval)
    return None


def find_window_by_title(title: str):
    for w in list_windows():
        if w.title and title in w.title and w.width > 100:
            return w
    return None


def main() -> bool:
    if not x11_available():
        print("SKIP: no X11 display available")
        return True
    if not at_spi_available():
        print("SKIP: AT-SPI not available (needs /usr/bin/python3 + python3-gi)")
        return True

    display = os.environ.get("DISPLAY", ":1")
    env = {**os.environ, "DISPLAY": display}
    print(f"Display: {display}")
    print(f"Sentinel: '{SENTINEL}'")
    print(f"Window title: '{TITLE}'")

    # ── 1. Launch a fresh gnome-terminal with a unique title ───────────────────
    print("\n[1] Launching dedicated gnome-terminal...")
    proc = subprocess.Popen(
        ["gnome-terminal", f"--title={TITLE}", "--", "/bin/bash", "--norc", "--noprofile"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # ── 2. Wait for window to appear in X11 tree ──────────────────────────────
    print("[2] Waiting for window to appear in X11 tree...")
    win = poll_until(lambda: find_window_by_title(TITLE), timeout=10.0, interval=0.3)
    if not win:
        proc.terminate()
        print("FAIL: terminal window never appeared in X11 tree")
        return False
    print(f"    Window found: {win}")

    # Bash needs a moment to start and render its prompt
    time.sleep(0.8)

    # ── 3. Focus the new terminal window ──────────────────────────────────────
    print("[3] Focusing terminal...")
    focus_window(win.window_id)
    time.sleep(0.5)  # wait for WM raise and VTE to be ready for input

    # ── 4. Type the sentinel command via XTEST ─────────────────────────────────
    cmd = f"echo {SENTINEL}\n"
    print(f"[4] Typing: {cmd.strip()!r}")
    type_text(win.window_id, cmd)
    time.sleep(0.8)  # wait for shell to run echo + VTE to render output

    # ── 5. Read terminal buffer via AT-SPI ─────────────────────────────────────
    print("[5] Reading terminal buffer via AT-SPI...")

    def read_terminal():
        for w in get_application_widgets("gnome-terminal", max_depth=8):
            if w.get("role") == "terminal" and SENTINEL in (w.get("text") or ""):
                return w.get("text", "")
        return ""

    terminal_text = poll_until(read_terminal, timeout=5.0, interval=0.2)

    proc.terminate()

    # ── 6. Verdict ────────────────────────────────────────────────────────────
    found = bool(terminal_text)
    snippet = ""
    if found:
        idx = terminal_text.index(SENTINEL)
        snippet = terminal_text[max(0, idx - 5):idx + len(SENTINEL) + 10].strip()

    print(f"\n{'='*58}")
    print("PHASE 5 APP CONTROL — RESULTS:")
    print(f"{'='*58}")
    print(f"  Command typed:      echo {SENTINEL}")
    print(f"  Sentinel in buffer: {found}")
    if found:
        print(f"  Buffer snippet:     '{snippet}'")
    print(f"  {'PASS — X11 type_text + AT-SPI readback verified' if found else 'FAIL — sentinel not found in terminal output'}")
    return found


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
