"""
Non-headless browser tests — require DISPLAY and a Chromium binary.
Spawns a visible Chromium window and drives it via CDP.
Skipped automatically on CI (no X11 display).
"""
import http.server
import os
import sys
import threading

import pytest

from self_connect_linux.browser import browser_available

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="Linux only"),
    pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="No X11 display"),
    pytest.mark.skipif(not browser_available(), reason="No Chromium binary available"),
]

_BROWSER_PORT = 19555
_HTTP_PORT = 18766

_HTML = b"""<!DOCTYPE html>
<html>
<head><title>SelfConnect Non-Headless Test</title></head>
<body>
  <h1>Visible Window Test</h1>
  <p id="p1">SelfConnect drives this window via CDP.</p>
  <input id="q" type="text" />
  <button id="btn" onclick="document.getElementById('p1').textContent='clicked'">Click me</button>
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
        pass


@pytest.fixture(scope="module")
def local_server():
    server = http.server.HTTPServer(("localhost", _HTTP_PORT), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://localhost:{_HTTP_PORT}/"
    server.shutdown()


@pytest.fixture(scope="module")
def visible_browser(local_server):
    from self_connect_linux.browser import BrowserSession
    with BrowserSession(cdp_port=_BROWSER_PORT, headless=False) as b:
        b.goto(local_server)
        yield b


def test_visible_browser_spawns(visible_browser):
    assert visible_browser._proc is not None
    assert visible_browser._proc.pid > 0


def test_visible_browser_identity(visible_browser):
    assert visible_browser.identity is not None
    assert visible_browser.identity.pid == visible_browser._proc.pid
    assert visible_browser.identity.proc_start_time_ticks > 0


def test_visible_goto_receipt(visible_browser, local_server):
    from self_connect_linux import ActionReceipt
    r = visible_browser.goto(local_server)
    assert isinstance(r, ActionReceipt)
    assert r.success is True
    assert r.backend == "browser_cdp"


def test_visible_title(visible_browser, local_server):
    visible_browser.goto(local_server)
    assert "Non-Headless" in visible_browser.title()


def test_visible_get_text(visible_browser, local_server):
    visible_browser.goto(local_server)
    h1 = visible_browser.get_text("h1")
    assert "Visible Window" in h1


def test_visible_evaluate(visible_browser):
    result = visible_browser.evaluate("window.innerWidth > 0")
    assert result is True


def test_visible_screenshot(visible_browser, local_server):
    visible_browser.goto(local_server)
    data = visible_browser.screenshot()
    assert data[:4] == b"\x89PNG"
    assert len(data) > 1000


def test_visible_fill_input(visible_browser, local_server):
    visible_browser.goto(local_server)
    r = visible_browser.fill("input#q", "hello from selfconnect")
    assert r.success is True
    val = visible_browser.evaluate("document.getElementById('q').value")
    assert val == "hello from selfconnect"


def test_visible_click(visible_browser, local_server):
    visible_browser.goto(local_server)
    r = visible_browser.click("button#btn")
    assert r.success is True
    text = visible_browser.get_text("#p1")
    assert "clicked" in text


def test_visible_wait_for(visible_browser, local_server):
    visible_browser.goto(local_server)
    r = visible_browser.wait_for("h1")
    assert r.success is True


def test_visible_url(visible_browser, local_server):
    visible_browser.goto(local_server)
    assert "localhost" in visible_browser.url


def test_visible_window_geometry(visible_browser):
    width = visible_browser.evaluate("window.outerWidth")
    height = visible_browser.evaluate("window.outerHeight")
    assert isinstance(width, int) and width > 0
    assert isinstance(height, int) and height > 0


def test_visible_close_cleans_up():
    from self_connect_linux.browser import BrowserSession
    b = BrowserSession(cdp_port=_BROWSER_PORT + 1, headless=False)
    b.open()
    pid = b._proc.pid
    assert pid > 0
    b.close()
    assert b._proc is None
    assert b._ws is None
    assert b.identity is None
    # Process should be gone
    import os as _os
    try:
        _os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert not alive, f"PID {pid} still alive after close()"
