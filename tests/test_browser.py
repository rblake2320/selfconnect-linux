"""
Tests for Phase 8 browser control (browser.py).

Uses a local HTTP server so tests work on CI without internet access.
The server serves a small static HTML page on localhost.
"""
import http.server
import sys
import threading

import pytest

from self_connect_linux.browser import browser_available

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="Linux only"),
    pytest.mark.skipif(not browser_available(), reason="No Chromium binary available"),
]

_BROWSER_PORT = 19444
_HTTP_PORT = 18765

_HTML = b"""<!DOCTYPE html>
<html>
<head><title>SelfConnect Test Page</title></head>
<body>
  <h1>SelfConnect Browser Test</h1>
  <p id="p1">Hello from the local test server.</p>
  <form id="f1" action="#" method="get">
    <input id="q" name="q" type="text" />
    <button type="submit">Go</button>
  </form>
  <div id="result">waiting</div>
</body>
</html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(_HTML)))
        self.end_headers()
        self.wfile.write(_HTML)

    def log_message(self, *_):
        pass  # silence server log output


@pytest.fixture(scope="module")
def local_server():
    server = http.server.HTTPServer(("localhost", _HTTP_PORT), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://localhost:{_HTTP_PORT}/"
    server.shutdown()


@pytest.fixture(scope="module")
def browser(local_server):
    from self_connect_linux.browser import BrowserSession
    with BrowserSession(cdp_port=_BROWSER_PORT) as b:
        b.goto(local_server)
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


def test_goto_returns_receipt(browser, local_server):
    from self_connect_linux import ActionReceipt
    r = browser.goto(local_server)
    assert isinstance(r, ActionReceipt)
    assert r.success is True
    assert r.backend == "browser_cdp"
    assert r.action == "browser"


def test_goto_metadata_has_url(browser, local_server):
    r = browser.goto(local_server)
    assert "url" in r.metadata
    assert "localhost" in r.metadata["url"]


def test_title_after_goto(browser, local_server):
    browser.goto(local_server)
    t = browser.title()
    assert isinstance(t, str)
    assert "SelfConnect" in t


def test_get_text_selector(browser, local_server):
    browser.goto(local_server)
    h1 = browser.get_text("h1")
    assert isinstance(h1, str)
    assert "SelfConnect" in h1


def test_get_text_full_body(browser, local_server):
    browser.goto(local_server)
    body = browser.get_text()
    assert isinstance(body, str)
    assert len(body) > 10


def test_url_property(browser, local_server):
    browser.goto(local_server)
    assert "localhost" in browser.url


def test_evaluate_js(browser):
    result = browser.evaluate("1 + 2")
    assert result == 3


def test_evaluate_dom(browser, local_server):
    browser.goto(local_server)
    result = browser.evaluate("document.querySelectorAll('p').length")
    assert isinstance(result, int)
    assert result >= 1


def test_screenshot_returns_png(browser, local_server):
    browser.goto(local_server)
    data = browser.screenshot()
    assert isinstance(data, bytes)
    assert data[:4] == b"\x89PNG"


def test_get_attribute(browser, local_server):
    browser.goto(local_server)
    val = browser.get_attribute("input#q", "name")
    assert val == "q"


def test_query_all(browser, local_server):
    browser.goto(local_server)
    items = browser.query_all("p")
    assert isinstance(items, list)
    assert len(items) >= 1


def test_receipt_has_payload_hash(browser, local_server):
    r = browser.goto(local_server)
    assert r.payload_hash.startswith("sha256:")
    assert len(r.payload_hash) > 20


def test_browser_repr(browser):
    r = repr(browser)
    assert "BrowserSession" in r
    assert str(browser._proc.pid) in r


def test_wait_for_existing_element(browser, local_server):
    browser.goto(local_server)
    r = browser.wait_for("h1")
    assert r.success is True


def test_wait_for_missing_element_fails(browser, local_server):
    browser.goto(local_server)
    r = browser.wait_for("div#does-not-exist-xyzzy", timeout=1.0)
    assert r.success is False


def test_fill_input(browser, local_server):
    browser.goto(local_server)
    r = browser.fill("input#q", "hello selfconnect")
    assert r.success is True
    val = browser.evaluate("document.getElementById('q').value")
    assert val == "hello selfconnect"


def test_press_key(browser, local_server):
    browser.goto(local_server)
    browser.fill("input#q", "test")
    r = browser.press("Tab")
    assert r.success is True


def test_click_button(browser, local_server):
    browser.goto(local_server)
    r = browser.click("button")
    assert r.success is True


def test_content_returns_html(browser, local_server):
    browser.goto(local_server)
    html = browser.content()
    assert "<html" in html.lower()
    assert "SelfConnect" in html
