"""Tests for Phase 5 X11 input and window management (x11_input.py)."""
import sys

import pytest

from self_connect_linux.x11_input import x11_available

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="Linux only"),
    pytest.mark.skipif(not x11_available(), reason="No X11 display available"),
]


def test_x11_available():
    assert x11_available() is True


def test_list_windows_returns_list():
    from self_connect_linux.x11_input import list_windows
    wins = list_windows()
    assert isinstance(wins, list)
    # On spark-3cdf GNOME session there are always windows
    assert len(wins) > 0


def test_window_info_fields():
    from self_connect_linux.x11_input import WindowInfo, list_windows
    wins = list_windows()
    w = wins[0]
    assert isinstance(w, WindowInfo)
    assert isinstance(w.window_id, int)
    assert w.window_id > 0
    assert isinstance(w.title, str)
    assert isinstance(w.width, int)
    assert isinstance(w.height, int)


def test_find_window_by_class():
    from self_connect_linux.x11_input import find_window
    # gnome-terminal is always open on spark-3cdf during dev
    win = find_window(wm_class="gnome-terminal")
    assert win is not None, "gnome-terminal should be running on spark-3cdf"
    assert win.window_id > 0
    assert win.wm_class is not None


def test_find_window_returns_none_for_unknown():
    from self_connect_linux.x11_input import find_window
    assert find_window(title="XYZZY_UNLIKELY_WINDOW_TITLE_12345") is None
    assert find_window(wm_class="xyzzy_unlikely_wm_class_99999") is None


def test_find_windows_returns_all_matches():
    from self_connect_linux.x11_input import find_windows
    wins = find_windows(wm_class="gnome-terminal")
    assert isinstance(wins, list)
    # Should find at least one since we're in a terminal
    assert len(wins) >= 1


def test_send_key_return_no_crash():
    """Inject a Return key — just verifies no exception is raised."""
    from self_connect_linux.x11_input import find_window, send_key
    win = find_window(wm_class="gnome-terminal")
    if win is None:
        pytest.skip("No gnome-terminal window found")
    send_key(win.window_id, "Return")


def test_send_special_keys_no_crash():
    """Verify all special key names resolve without error."""
    from self_connect_linux.x11_input import _SPECIAL_KEYS, find_window, send_key
    win = find_window(wm_class="gnome-terminal")
    if win is None:
        pytest.skip("No gnome-terminal window found")
    for key, keysym in _SPECIAL_KEYS.items():
        if keysym is not None and not key.startswith("ctrl+"):
            send_key(win.window_id, key)


def test_focus_window_no_crash():
    from self_connect_linux.x11_input import find_window, focus_window
    win = find_window(wm_class="gnome-terminal")
    if win is None:
        pytest.skip("No gnome-terminal window found")
    focus_window(win.window_id)  # must not raise


def test_window_repr():
    from self_connect_linux.x11_input import list_windows
    wins = list_windows()
    if not wins:
        pytest.skip("No windows")
    r = repr(wins[0])
    assert "WindowInfo" in r
    assert "id=" in r
