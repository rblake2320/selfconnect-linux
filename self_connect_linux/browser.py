"""
Phase 8 — Real internet browser control via SelfConnect primitives.

SelfConnect spawns the browser as a PTY subprocess (Phase 1), captures its
/proc identity (identity.py), then connects to the Chrome DevTools Protocol
(CDP) via a raw TCP WebSocket built from Python stdlib only.

No Playwright. No Selenium. No external browser framework.
The browser is just another agent process — it gets a lease-like identity
snapshot at spawn time and every action produces an ActionReceipt.

Architecture:
  spawn_pty_agent(chromium --remote-debugging-port=N)
      → capture_identity(browser_pid)          # /proc attestation
      → raw TCP WebSocket to CDP port          # stdlib socket only
      → JSON-RPC CDP commands                  # navigate, click, eval, etc.
      → ActionReceipt per mutation             # audit trail

Capability gate: call browser_available() before use.

Typical usage:
    with BrowserSession() as b:
        b.goto("https://example.com")
        print(b.title())
        print(b.get_text("h1"))
        b.fill("input[name=q]", "NVIDIA GB10")
        b.press("Enter")
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

from .identity import LinuxTargetIdentity, capture_identity
from .receipts import ActionReceipt, make_receipt

# ---------------------------------------------------------------------------
# Browser binary discovery
# ---------------------------------------------------------------------------

def _find_chromium() -> str | None:
    playwright_cache = Path(os.environ.get(
        "MS_PLAYWRIGHT_BROWSERS_PATH",
        Path.home() / ".cache" / "ms-playwright"
    ))
    if playwright_cache.is_dir():
        for p in sorted(playwright_cache.glob("chromium-*/chrome-linux/chrome"), reverse=True):
            if p.is_file() and os.access(p, os.X_OK):
                return str(p)
    for name in ("google-chrome", "chromium-browser", "chromium"):
        for prefix in ("/usr/bin", "/usr/local/bin", "/snap/bin"):
            c = Path(prefix, name)
            if c.is_file() and os.access(c, os.X_OK):
                return str(c)
    return None


def browser_available() -> bool:
    """True if a Chromium binary is present and responds to --version."""
    exe = _find_chromium()
    if not exe:
        return False
    try:
        result = subprocess.run(
            [exe, "--version"],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Minimal stdlib WebSocket (no external dependency)
# ---------------------------------------------------------------------------

class _CdpSocket:
    """
    Minimal WebSocket client over a raw TCP socket — no external libraries.
    Implements RFC 6455 framing sufficient for CDP JSON-RPC.
    """

    def __init__(self, host: str, port: int, path: str) -> None:
        self._sock = socket.create_connection((host, port), timeout=15)
        self._sock.settimeout(30)
        self._do_handshake(host, port, path)
        self._recv_buf = b""

    def _do_handshake(self, host: str, port: int, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        self._sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("CDP WebSocket handshake: connection closed")
            resp += chunk
        if b"101" not in resp:
            raise ConnectionError(f"CDP WebSocket upgrade failed: {resp[:200]}")

    def send(self, msg: str) -> None:
        data = msg.encode()
        length = len(data)
        mask = os.urandom(4)
        if length < 126:
            header = bytes([0x81, 0x80 | length]) + mask
        elif length < 65536:
            header = bytes([0x81, 0xFE, (length >> 8) & 0xFF, length & 0xFF]) + mask
        else:
            header = bytes([0x81, 0x7F]) + struct.pack(">Q", length) + mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self._sock.sendall(header + masked)

    def recv(self) -> str:
        while True:
            buf = self._recv_buf
            if len(buf) < 2:
                buf += self._sock.recv(65536)
                self._recv_buf = buf
                continue
            length = buf[1] & 0x7F
            offset = 2
            if length == 126:
                if len(buf) < 4:
                    self._recv_buf += self._sock.recv(65536)
                    continue
                length = struct.unpack(">H", buf[2:4])[0]
                offset = 4
            elif length == 127:
                if len(buf) < 10:
                    self._recv_buf += self._sock.recv(65536)
                    continue
                length = struct.unpack(">Q", buf[2:10])[0]
                offset = 10
            total = offset + length
            while len(buf) < total:
                buf += self._sock.recv(65536)
            self._recv_buf = buf[total:]
            return buf[offset:total].decode()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# BrowserSession
# ---------------------------------------------------------------------------

class BrowserSession:
    """
    A headless Chromium tab driven by SelfConnect — PTY spawn + raw CDP.

    The browser process is treated like any other agent:
      - Spawned as a subprocess with its PID captured
      - /proc identity snapshot taken at open() time
      - Every navigation/mutation returns an ActionReceipt
      - No Playwright, no Selenium, no external browser framework

    Usage:
        with BrowserSession() as b:
            b.goto("https://example.com")
            print(b.title())
            print(b.get_text("h1"))
    """

    def __init__(
        self,
        cdp_port: int = 19222,
        timeout: float = 30.0,
        extra_args: list[str] | None = None,
    ) -> None:
        self._cdp_port = cdp_port
        self._timeout = timeout
        self._extra_args = extra_args or []
        self._proc: subprocess.Popen | None = None
        self._tmpdir: str | None = None
        self._ws: _CdpSocket | None = None
        self._cdp_id = 0
        self.identity: LinuxTargetIdentity | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> "BrowserSession":
        exe = _find_chromium()
        if exe is None:
            raise RuntimeError("No Chromium binary found — check browser_available()")

        self._tmpdir = tempfile.mkdtemp(prefix="sc-browser-")
        # --ozone-platform=headless: Chromium's native headless display abstraction.
        # No X11/xcb dependency; GPU is NOT disabled (unlike --disable-gpu which was
        # an earlier workaround — see CHANGELOG v0.9.1 and git commits 59d0701/ba25cec).
        # Source: Ubuntu bug #1959416, Chromium Ozone docs.
        self._proc = subprocess.Popen(
            [
                exe,
                "--headless=new",
                "--ozone-platform=headless",
                f"--remote-debugging-port={self._cdp_port}",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                f"--user-data-dir={self._tmpdir}",
            ] + self._extra_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Capture /proc identity — browser is an attested agent process
        self.identity = capture_identity(self._proc.pid)

        # Wait for CDP to come up
        ws_path = self._wait_for_cdp()
        self._ws = _CdpSocket("localhost", self._cdp_port, ws_path)
        self._cdp("Page.enable")
        return self

    def _wait_for_cdp(self) -> str:
        deadline = time.time() + self._timeout
        while time.time() < deadline:
            try:
                raw = urllib.request.urlopen(
                    f"http://localhost:{self._cdp_port}/json/list", timeout=2
                ).read()
                tabs = json.loads(raw)
                # Pick the first "page" type target — some Chromium builds (snap,
                # system extensions) prepend chrome-extension background targets
                # that are not real browsing tabs.
                page = next(
                    (t for t in tabs
                     if t.get("type") == "page" and "webSocketDebuggerUrl" in t),
                    None,
                )
                if page is None and tabs and "webSocketDebuggerUrl" in tabs[0]:
                    page = tabs[0]  # fallback: use first tab if no "page" type
                if page:
                    url = page["webSocketDebuggerUrl"]
                    return url.replace(f"ws://localhost:{self._cdp_port}", "")
            except Exception:
                pass
            time.sleep(0.2)
        raise TimeoutError(f"Browser CDP did not start within {self._timeout}s")

    def close(self) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None
        self.identity = None

    def __enter__(self) -> "BrowserSession":
        return self.open()

    def __exit__(self, *_) -> None:
        self.close()

    def _require_open(self) -> _CdpSocket:
        if self._ws is None:
            raise RuntimeError("BrowserSession is not open")
        return self._ws

    # ------------------------------------------------------------------
    # Raw CDP
    # ------------------------------------------------------------------

    def _cdp(self, method: str, params: dict | None = None) -> dict:
        ws = self._require_open()
        self._cdp_id += 1
        ws.send(json.dumps({"id": self._cdp_id, "method": method, "params": params or {}}))
        target_id = self._cdp_id
        while True:
            data = json.loads(ws.recv())
            if data.get("id") == target_id:
                return data

    def _eval(self, expression: str) -> Any:
        r = self._cdp("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        result = r.get("result", {}).get("result", {})
        if result.get("subtype") == "null" or result.get("type") == "undefined":
            return None
        return result.get("value")

    def _wait_load(self, timeout: float = 10.0) -> None:
        ws = self._require_open()
        deadline = time.time() + timeout
        ws._sock.settimeout(1.0)
        while time.time() < deadline:
            try:
                raw = ws.recv()
                ev = json.loads(raw)
                if ev.get("method") in ("Page.loadEventFired", "Page.domContentEventFired"):
                    break
            except (TimeoutError, OSError):
                pass
        ws._sock.settimeout(self._timeout)

    # ------------------------------------------------------------------
    # Read-only accessors
    # ------------------------------------------------------------------

    @property
    def url(self) -> str:
        return self._eval("window.location.href") or ""

    def title(self) -> str:
        return self._eval("document.title") or ""

    def content(self) -> str:
        return self._eval("document.documentElement.outerHTML") or ""

    def get_text(self, selector: str | None = None) -> str:
        if selector is None:
            return self._eval("document.body.innerText") or ""
        js = f"document.querySelector({json.dumps(selector)})?.innerText ?? ''"
        return self._eval(js) or ""

    def get_attribute(self, selector: str, name: str) -> str | None:
        js = f"document.querySelector({json.dumps(selector)})?.getAttribute({json.dumps(name)})"
        return self._eval(js)

    def query_all(self, selector: str) -> list[str]:
        js = (
            f"Array.from(document.querySelectorAll({json.dumps(selector)}))"
            f".map(e => e.innerText)"
        )
        return self._eval(js) or []

    def screenshot(self) -> bytes:
        r = self._cdp("Page.captureScreenshot", {"format": "png"})
        data = r.get("result", {}).get("data", "")
        return base64.b64decode(data)

    def evaluate(self, expression: str) -> Any:
        return self._eval(expression)

    # ------------------------------------------------------------------
    # Mutating actions — each returns an ActionReceipt
    # ------------------------------------------------------------------

    def goto(self, url: str) -> ActionReceipt:
        try:
            self._cdp("Page.navigate", {"url": url})
            self._wait_load()
            return make_receipt(
                action="browser",
                payload=f"goto {url}",
                backend="browser_cdp",
                pid=self._proc.pid if self._proc else os.getpid(),
                success=True,
                metadata={"url": self.url, "title": self.title()},
            )
        except Exception as exc:
            return make_receipt(
                action="browser",
                payload=f"goto {url}",
                backend="browser_cdp",
                pid=self._proc.pid if self._proc else os.getpid(),
                success=False,
                metadata={"error": str(exc)},
            )

    def click(self, selector: str) -> ActionReceipt:
        try:
            self._eval(f"document.querySelector({json.dumps(selector)}).click()")
            time.sleep(0.1)
            return make_receipt(
                action="browser",
                payload=f"click {selector}",
                backend="browser_cdp",
                pid=self._proc.pid if self._proc else os.getpid(),
                success=True,
                metadata={"url": self.url},
            )
        except Exception as exc:
            return make_receipt(
                action="browser",
                payload=f"click {selector}",
                backend="browser_cdp",
                pid=self._proc.pid if self._proc else os.getpid(),
                success=False,
                metadata={"error": str(exc)},
            )

    def fill(self, selector: str, value: str) -> ActionReceipt:
        try:
            js = (
                f"(function(){{"
                f"  var el = document.querySelector({json.dumps(selector)});"
                f"  el.focus(); el.value = {json.dumps(value)};"
                f"  el.dispatchEvent(new Event('input', {{bubbles:true}}));"
                f"  el.dispatchEvent(new Event('change', {{bubbles:true}}));"
                f"}})()"
            )
            self._eval(js)
            return make_receipt(
                action="browser",
                payload=f"fill {selector}",
                backend="browser_cdp",
                pid=self._proc.pid if self._proc else os.getpid(),
                success=True,
                metadata={"url": self.url, "length": len(value)},
            )
        except Exception as exc:
            return make_receipt(
                action="browser",
                payload=f"fill {selector}",
                backend="browser_cdp",
                pid=self._proc.pid if self._proc else os.getpid(),
                success=False,
                metadata={"error": str(exc)},
            )

    def press(self, key: str) -> ActionReceipt:
        try:
            self._cdp("Input.dispatchKeyEvent", {"type": "keyDown", "key": key})
            self._cdp("Input.dispatchKeyEvent", {"type": "keyUp",   "key": key})
            time.sleep(0.1)
            return make_receipt(
                action="browser",
                payload=f"press {key}",
                backend="browser_cdp",
                pid=self._proc.pid if self._proc else os.getpid(),
                success=True,
                metadata={"url": self.url, "key": key},
            )
        except Exception as exc:
            return make_receipt(
                action="browser",
                payload=f"press {key}",
                backend="browser_cdp",
                pid=self._proc.pid if self._proc else os.getpid(),
                success=False,
                metadata={"error": str(exc)},
            )

    def type_text(self, text: str) -> ActionReceipt:
        try:
            for ch in text:
                self._cdp("Input.dispatchKeyEvent", {"type": "char", "text": ch})
            return make_receipt(
                action="browser",
                payload=f"type_text len={len(text)}",
                backend="browser_cdp",
                pid=self._proc.pid if self._proc else os.getpid(),
                success=True,
                metadata={"url": self.url, "length": len(text)},
            )
        except Exception as exc:
            return make_receipt(
                action="browser",
                payload="type_text",
                backend="browser_cdp",
                pid=self._proc.pid if self._proc else os.getpid(),
                success=False,
                metadata={"error": str(exc)},
            )

    def wait_for(self, selector: str, timeout: float | None = None) -> ActionReceipt:
        t = timeout or self._timeout
        deadline = time.time() + t
        try:
            while time.time() < deadline:
                result = self._eval(
                    f"document.querySelector({json.dumps(selector)}) !== null"
                )
                if result:
                    return make_receipt(
                        action="browser",
                        payload=f"wait_for {selector}",
                        backend="browser_cdp",
                        pid=self._proc.pid if self._proc else os.getpid(),
                        success=True,
                        metadata={"url": self.url},
                    )
                time.sleep(0.2)
            raise TimeoutError(f"wait_for({selector!r}) timed out after {t}s")
        except Exception as exc:
            return make_receipt(
                action="browser",
                payload=f"wait_for {selector}",
                backend="browser_cdp",
                pid=self._proc.pid if self._proc else os.getpid(),
                success=False,
                metadata={"error": str(exc)},
            )

    def __repr__(self) -> str:
        if self._proc is None:
            return "BrowserSession(closed)"
        pid = self._proc.pid
        return f"BrowserSession(pid={pid}, port={self._cdp_port})"
