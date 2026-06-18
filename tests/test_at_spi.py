"""Tests for Phase 5 AT-SPI accessibility tree access (at_spi.py)."""
import sys

import pytest

from self_connect_linux.at_spi import at_spi_available

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="Linux only"),
    pytest.mark.skipif(not at_spi_available(), reason="AT-SPI not available"),
]


def test_at_spi_available():
    assert at_spi_available() is True


def test_list_applications_returns_list():
    from self_connect_linux.at_spi import list_applications
    apps = list_applications()
    assert isinstance(apps, list)
    assert len(apps) > 0


def test_list_applications_contains_gnome_shell():
    from self_connect_linux.at_spi import list_applications
    apps = list_applications()
    assert any("gnome" in a.lower() for a in apps), \
        f"Expected gnome app in {apps}"


def test_find_application_by_substring():
    from self_connect_linux.at_spi import find_application
    result = find_application("gnome-shell")
    assert result is not None
    assert "gnome" in result.lower()


def test_find_application_returns_none_for_unknown():
    from self_connect_linux.at_spi import find_application
    assert find_application("xyzzy_nonexistent_app_99999") is None


def test_get_application_widgets_returns_list():
    from self_connect_linux.at_spi import find_application, get_application_widgets
    app = find_application("gnome-terminal-server")
    if app is None:
        pytest.skip("gnome-terminal not running")
    widgets = get_application_widgets(app)
    assert isinstance(widgets, list)
    assert len(widgets) > 0


def test_widget_info_has_required_keys():
    from self_connect_linux.at_spi import find_application, get_application_widgets
    app = find_application("gnome-shell")
    if app is None:
        pytest.skip("gnome-shell not running")
    widgets = get_application_widgets(app, max_depth=2)
    assert len(widgets) > 0
    for w in widgets:
        assert "name" in w
        assert "role" in w
        assert "text" in w


def test_get_text_returns_string():
    from self_connect_linux.at_spi import find_application, get_text
    app = find_application("gnome-shell")
    if app is None:
        pytest.skip("gnome-shell not running")
    result = get_text(app)
    assert isinstance(result, str)


def test_get_focused_text_returns_string():
    from self_connect_linux.at_spi import get_focused_text
    result = get_focused_text()
    assert isinstance(result, str)
