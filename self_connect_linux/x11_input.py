"""
Phase 5 — X11 input injection and window management.

Uses python-xlib (available in Miniconda) + XTEST extension to:
  - Enumerate top-level windows and find by title or WM_CLASS
  - Focus and raise windows
  - Inject synthetic keyboard events via XTEST (no write to /dev/input)
  - Capture window geometry

Does NOT require root or uinput. Works on any X11 session (DISPLAY set).
For Wayland: use uinput or portal APIs — this module is X11-only.

Capability gate: call x11_available() before any other function.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional

# Lazy import — only bind when actually used
_display = None


def x11_available() -> bool:
    """True if an X11 display is accessible and python-xlib is installed."""
    if not os.environ.get("DISPLAY"):
        return False
    try:
        from Xlib import display as _xdisplay
        d = _xdisplay.Display()
        d.close()
        return True
    except Exception:
        return False


def _get_display():
    global _display
    if _display is None:
        from Xlib import display as _xdisplay
        _display = _xdisplay.Display()
    return _display


def _close_display():
    global _display
    if _display is not None:
        try:
            _display.close()
        except Exception:
            pass
        _display = None


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class WindowInfo:
    window_id: int
    title: str
    wm_class: tuple[str, str] | None
    x: int
    y: int
    width: int
    height: int

    def __repr__(self) -> str:
        cls = f"{self.wm_class[0]}/{self.wm_class[1]}" if self.wm_class else "?"
        return f"WindowInfo(id={self.window_id:#x}, title={self.title!r}, class={cls}, {self.width}x{self.height})"


# ---------------------------------------------------------------------------
# Window discovery
# ---------------------------------------------------------------------------

def list_windows() -> list[WindowInfo]:
    """Return all top-level windows visible to this X session."""
    d = _get_display()
    root = d.screen().root
    results = []
    _collect_windows(d, root, results)
    return results


def _collect_windows(d, window, results: list) -> None:
    try:
        title = _get_title(window)
        cls = _get_wm_class(window)
        if title or cls:
            try:
                geom = window.get_geometry()
                results.append(WindowInfo(
                    window_id=window.id,
                    title=title or "",
                    wm_class=cls,
                    x=geom.x, y=geom.y,
                    width=geom.width, height=geom.height,
                ))
            except Exception:
                pass
    except Exception:
        pass
    try:
        for child in window.query_tree().children:
            _collect_windows(d, child, results)
    except Exception:
        pass


def _get_title(window) -> str:
    try:
        name = window.get_wm_name()
        if isinstance(name, bytes):
            return name.decode(errors="replace")
        return name or ""
    except Exception:
        return ""


def _get_wm_class(window) -> tuple[str, str] | None:
    try:
        cls = window.get_wm_class()
        if cls and len(cls) >= 2:
            return (str(cls[0]), str(cls[1]))
        return None
    except Exception:
        return None


def find_window(
    title: str | None = None,
    wm_class: str | None = None,
) -> WindowInfo | None:
    """
    Find the first window matching *title* (substring, case-insensitive)
    or *wm_class* (substring, case-insensitive).  Returns None if not found.
    """
    for win in list_windows():
        if title and title.lower() in win.title.lower():
            return win
        if wm_class and win.wm_class:
            cls_str = " ".join(win.wm_class).lower()
            if wm_class.lower() in cls_str:
                return win
    return None


def find_windows(
    title: str | None = None,
    wm_class: str | None = None,
) -> list[WindowInfo]:
    """Return all windows matching the filter."""
    results = []
    for win in list_windows():
        if title and title.lower() in win.title.lower():
            results.append(win)
        elif wm_class and win.wm_class:
            cls_str = " ".join(win.wm_class).lower()
            if wm_class.lower() in cls_str:
                results.append(win)
    return results


# ---------------------------------------------------------------------------
# Focus and raise
# ---------------------------------------------------------------------------

def focus_window(window_id: int) -> None:
    """Raise and focus a window by its X window ID."""
    d = _get_display()
    from Xlib import X
    win = d.create_resource_object("window", window_id)
    win.raise_window()
    win.set_input_focus(X.RevertToParent, X.CurrentTime)
    d.flush()
    time.sleep(0.05)  # let WM process the raise


# ---------------------------------------------------------------------------
# Keyboard injection via XTEST
# ---------------------------------------------------------------------------

# Keysym lookup table for common printable ASCII and specials.
# python-xlib's keysymdef covers the rest.
_SPECIAL_KEYS: dict[str, int] = {
    "Return":    0xff0d,
    "Escape":    0xff1b,
    "Tab":       0xff09,
    "BackSpace": 0xff08,
    "Delete":    0xffff,
    "Home":      0xff50,
    "End":       0xff57,
    "Left":      0xff51,
    "Right":     0xff53,
    "Up":        0xff52,
    "Down":      0xff54,
    "F1":        0xffbe,
    "F2":        0xffbf,
    "F3":        0xffc0,
    "F4":        0xffc1,
    "F5":        0xffc2,
    "F6":        0xffc3,
    "F7":        0xffc4,
    "F8":        0xffc5,
    "F9":        0xffc6,
    "F10":       0xffc7,
    "F11":       0xffc8,
    "F12":       0xffc9,
    "ctrl+c":    None,  # handled specially
    "ctrl+d":    None,
    "ctrl+l":    None,
}

_MOD_CTRL  = 0xffe3  # XK_Control_L
_MOD_SHIFT = 0xffe1  # XK_Shift_L


def _keysym_to_keycode(d, keysym: int) -> int:
    return d.keysym_to_keycode(keysym)


def _xtest_key(d, keycode: int, press: bool) -> None:
    from Xlib.ext import xtest
    from Xlib import X
    xtest.fake_input(d, X.KeyPress if press else X.KeyRelease, keycode)


def send_key(window_id: int, key: str) -> None:
    """
    Inject a single key event into the window with the given ID.

    *key* may be a printable character ("a", "A", "!") or a special key name
    ("Return", "Escape", "Tab", "BackSpace", "F1"…"F12",
    "ctrl+c", "ctrl+d", "ctrl+l").
    """
    d = _get_display()
    from Xlib import X
    from Xlib.ext import xtest

    # Handle ctrl+X shortcuts
    if key.startswith("ctrl+") and len(key) == 6:
        char = key[-1]
        ctrl_kc = _keysym_to_keycode(d, _MOD_CTRL)
        char_kc = _keysym_to_keycode(d, ord(char))
        xtest.fake_input(d, X.KeyPress,   ctrl_kc)
        xtest.fake_input(d, X.KeyPress,   char_kc)
        xtest.fake_input(d, X.KeyRelease, char_kc)
        xtest.fake_input(d, X.KeyRelease, ctrl_kc)
        d.flush()
        return

    # Special named keys
    if key in _SPECIAL_KEYS:
        keysym = _SPECIAL_KEYS[key]
        if keysym is None:
            return
        kc = _keysym_to_keycode(d, keysym)
        xtest.fake_input(d, X.KeyPress,   kc)
        xtest.fake_input(d, X.KeyRelease, kc)
        d.flush()
        return

    # Single printable character
    if len(key) == 1:
        keysym = ord(key)
        kc = _keysym_to_keycode(d, keysym)
        if kc == 0:
            return
        # Shift required for uppercase and symbols
        needs_shift = key.isupper() or key in '~!@#$%^&*()_+{}|:"<>?'
        if needs_shift:
            shift_kc = _keysym_to_keycode(d, _MOD_SHIFT)
            xtest.fake_input(d, X.KeyPress,   shift_kc)
        xtest.fake_input(d, X.KeyPress,   kc)
        xtest.fake_input(d, X.KeyRelease, kc)
        if needs_shift:
            xtest.fake_input(d, X.KeyRelease, shift_kc)
        d.flush()
        return

    raise ValueError(f"Unknown key: {key!r}")


def type_text(window_id: int, text: str, delay_ms: float = 10.0) -> None:
    """
    Type a string into a window by injecting individual key events.

    *delay_ms* is the inter-keystroke delay in milliseconds.  For fast
    automated typing 10ms is reliable; for slower human-visible typing use 50+.

    Handles printable ASCII and newline ('\\n' → Return).
    """
    d = _get_display()
    focus_window(window_id)
    delay = delay_ms / 1000.0
    for ch in text:
        if ch == "\n":
            send_key(window_id, "Return")
        elif ch == "\t":
            send_key(window_id, "Tab")
        else:
            send_key(window_id, ch)
        if delay > 0:
            time.sleep(delay)
    d.flush()
