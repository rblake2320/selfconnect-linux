"""
Tests for Phase 8 browser control (browser.py).

These tests require a real internet connection and a Chromium binary.
They are skipped automatically when browser_available() returns False.
"""
import json
import os
import sys

import pytest

from self_connect_linux.browser import browser_available

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="Linux only"),
    pytest.mark.skipif(not browser_available(), reason="No Chromium binary available"),
]

# Use a fixed port per test to avoid conflicts
_PORT = 19444


@pytest.fixture(scope="module")
def browser():
    from self_connect_linux.browser import BrowserSession
    with BrowserSession(cdp_port=_PORT) as b:
        yield b


def test_browser_available():
    assert browser_available() is True


def test_browser_spawns_with_identity(browser):
    assert browser._proc is not None
    assert browser._proc.pid > 0
    assert browser.identity is not None
    assert browser.identity.pid == browser._proc.pid
    assert browser.identity.exe_path is not None


def test_browser_identity_has_proc_start_time(browser):
    assert browser.identity.proc_start_time_ticks is not None
    assert browser.identity.proc_start_time_ticks > 0


def test_goto_returns_receipt(browser):
    from self_connect_linux import ActionReceipt
    r = browser.goto("https://example.com")
    assert isinstance(r, ActionReceipt)
    assert r.success is True
    assert r.backend == "browser_cdp"
    assert r.action == "browser"


def test_goto_metadata_has_url(browser):
    r = browser.goto("https://example.com")
    assert "url" in r.metadata
    assert "example.com" in r.metadata["url"]


def test_title_after_goto(browser):
    browser.goto("https://example.com")
    t = browser.title()
    assert isinstance(t, str)
    assert len(t) > 0
    assert "Example" in t


def test_get_text_selector(browser):
    browser.goto("https://example.com")
    h1 = browser.get_text("h1")
    assert isinstance(h1, str)
    assert "Example" in h1


def test_get_text_full_body(browser):
    browser.goto("https://example.com")
    body = browser.get_text()
    assert isinstance(body, str)
    assert len(body) > 10


def test_url_property(browser):
    browser.goto("https://example.com")
    assert "example.com" in browser.url


def test_evaluate_js(browser):
    browser.goto("https://example.com")
    result = browser.evaluate("1 + 2")
    assert result == 3


def test_evaluate_dom(browser):
    browser.goto("https://example.com")
    result = browser.evaluate("document.querySelectorAll('p').length")
    assert isinstance(result, int)
    assert result >= 1


def test_screenshot_returns_png(browser):
    browser.goto("https://example.com")
    data = browser.screenshot()
    assert isinstance(data, bytes)
    assert data[:4] == b"\x89PNG"


def test_real_internet_github_api(browser):
    r = browser.goto("https://api.github.com/repos/rblake2320/selfconnect-linux")
    assert r.success
    pre = browser.get_text("pre")
    data = json.loads(pre)
    assert data["full_name"] == "rblake2320/selfconnect-linux"
    assert data["default_branch"] == "main"


def test_receipt_has_payload_hash(browser):
    r = browser.goto("https://example.com")
    assert r.payload_hash.startswith("sha256:")
    assert len(r.payload_hash) > 20


def test_browser_repr(browser):
    r = repr(browser)
    assert "BrowserSession" in r
    assert str(browser._proc.pid) in r


def test_wait_for_existing_element(browser):
    browser.goto("https://example.com")
    r = browser.wait_for("h1")
    assert r.success is True


def test_wait_for_missing_element_fails(browser):
    browser.goto("https://example.com")
    r = browser.wait_for("div#does-not-exist-xyzzy", timeout=1.0)
    assert r.success is False


def test_fill_and_press_enter(browser):
    # Use a page with a real form
    browser.goto("https://duckduckgo.com/html/")
    r_fill = browser.fill("input[name=q]", "NVIDIA GB10 Grace Blackwell")
    assert r_fill.success is True
    r_press = browser.press("Enter")
    assert r_press.success is True
