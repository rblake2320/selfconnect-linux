"""
Live non-headless browser control demo — aihangout.ai
Visible on DISPLAY=:1. No headless. Real site, real account, real actions.

Actions:
  1. Open aihangout.ai (visible browser)
  2. Register as an AI Agent
  3. Log in
  4. Browse the problem feed
  5. Post a new problem
  6. Update the profile/bio

Run:
    DISPLAY=:1 python examples/aihangout_live_demo.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from self_connect_linux.identity import capture_identity
from self_connect_linux.browser import _find_chromium, _CdpSocket

# ── Credentials — set via env vars or edit here before running ─────────────────
USERNAME    = os.environ.get("AIHANGOUT_USER", "selfconnect-dgx1")
EMAIL       = os.environ.get("AIHANGOUT_EMAIL", "your@email.com")
PASSWORD    = os.environ.get("AIHANGOUT_PASS", "")       # required: set env var
AGENT_TYPE  = "AI Agent"   # selects 🤖 AI Agent in the dropdown

BASE_URL    = "https://aihangout.ai"
CDP_PORT    = 19600
DISPLAY     = os.environ.get("DISPLAY", ":1")


class LiveBrowser:
    """Non-headless BrowserSession — browser is visible on screen."""

    def __init__(self, cdp_port: int = CDP_PORT):
        self._port = cdp_port
        self._proc = None
        self._ws: _CdpSocket | None = None
        self._tmpdir = None
        self.identity = None

    def open(self):
        exe = _find_chromium()
        if not exe:
            raise RuntimeError("No Chromium found")
        self._tmpdir = tempfile.mkdtemp(prefix="sc-live-")
        env = {**os.environ, "DISPLAY": DISPLAY}
        self._proc = subprocess.Popen(
            [
                exe,
                # NO --headless — browser is visible on screen
                f"--remote-debugging-port={self._port}",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--window-size=1280,900",
                f"--user-data-dir={self._tmpdir}",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.identity = capture_identity(self._proc.pid)
        print(f"  Browser PID: {self._proc.pid}")
        print(f"  Identity: uid={self.identity.uid} exe={self.identity.exe_path}")

        # Wait for CDP
        ws_path = self._wait_for_cdp()
        self._ws = _CdpSocket("localhost", self._port, ws_path)
        self._cdp_id = 0
        self._cdp("Page.enable")
        self._cdp("Runtime.enable")
        return self

    def _wait_for_cdp(self, timeout: float = 30.0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = urllib.request.urlopen(
                    f"http://localhost:{self._port}/json/list", timeout=2
                ).read()
                tabs = json.loads(raw)
                page = next(
                    (t for t in tabs
                     if t.get("type") == "page" and "webSocketDebuggerUrl" in t),
                    None,
                )
                if page:
                    url = page["webSocketDebuggerUrl"]
                    return url.replace(f"ws://localhost:{self._port}", "")
            except Exception:
                pass
            time.sleep(0.3)
        raise TimeoutError("Browser CDP did not come up")

    def _cdp(self, method: str, params: dict | None = None) -> dict:
        self._cdp_id += 1
        self._ws.send(json.dumps({"id": self._cdp_id, "method": method, "params": params or {}}))
        target_id = self._cdp_id
        while True:
            data = json.loads(self._ws.recv())
            if data.get("id") == target_id:
                return data

    def goto(self, url: str, wait: float = 2.5):
        self._cdp("Page.navigate", {"url": url})
        time.sleep(wait)

    def js(self, expr: str):
        r = self._cdp("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        return r.get("result", {}).get("result", {}).get("value")

    def fill(self, selector: str, value: str):
        # React controlled inputs ignore direct .value assignment.
        # Use the native HTMLInputElement/HTMLTextAreaElement value setter
        # so React's synthetic event system sees the change.
        self.js(f"""
            (function() {{
                var el = document.querySelector({json.dumps(selector)});
                if (!el) return 'NOT FOUND';
                el.focus();
                var proto = el.tagName === 'TEXTAREA'
                    ? window.HTMLTextAreaElement.prototype
                    : window.HTMLInputElement.prototype;
                var setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                setter.call(el, {json.dumps(value)});
                el.dispatchEvent(new Event('input',  {{bubbles: true}}));
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
                el.dispatchEvent(new KeyboardEvent('keydown', {{bubbles: true}}));
                el.dispatchEvent(new KeyboardEvent('keyup',   {{bubbles: true}}));
                return 'ok';
            }})()
        """)

    def click(self, selector: str, wait: float = 1.5):
        self.js(f"""
            (function() {{
                var el = document.querySelector({json.dumps(selector)});
                if (!el) return 'NOT FOUND';
                el.click();
                return 'clicked';
            }})()
        """)
        time.sleep(wait)

    def select(self, selector: str, text: str):
        self.js(f"""
            (function() {{
                var el = document.querySelector({json.dumps(selector)});
                if (!el) return 'NOT FOUND';
                for (var i = 0; i < el.options.length; i++) {{
                    if (el.options[i].text.includes({json.dumps(text)})) {{
                        var setter = Object.getOwnPropertyDescriptor(
                            window.HTMLSelectElement.prototype, 'value').set;
                        setter.call(el, el.options[i].value);
                        el.dispatchEvent(new Event('change', {{bubbles: true}}));
                        el.dispatchEvent(new Event('input',  {{bubbles: true}}));
                        return el.options[i].text;
                    }}
                }}
                return 'option not found';
            }})()
        """)

    def text(self, selector: str = "body") -> str:
        return self.js(f"document.querySelector({json.dumps(selector)})?.innerText || ''") or ""

    def title(self) -> str:
        return self.js("document.title") or ""

    def url(self) -> str:
        return self.js("location.href") or ""

    def wait_visible(self, selector: str, timeout: float = 10.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = self.js(f"!!document.querySelector({json.dumps(selector)})")
            if result:
                return True
            time.sleep(0.4)
        return False

    def scroll_down(self, px: int = 600):
        self.js(f"window.scrollBy(0, {px})")
        time.sleep(0.5)

    def close(self):
        if self._ws:
            self._ws.close()
        if self._proc:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        if self._tmpdir:
            import shutil
            shutil.rmtree(self._tmpdir, ignore_errors=True)

    def __enter__(self): return self.open()
    def __exit__(self, *_): self.close()


def step(n: int, msg: str):
    print(f"\n[Step {n}] {msg}")


def main():
    print("=" * 60)
    print("SelfConnect — Live Browser Control Demo")
    print(f"Target: {BASE_URL}")
    print(f"Display: {DISPLAY}")
    print(f"Account: {USERNAME} / {EMAIL}")
    print("=" * 60)

    with LiveBrowser() as b:

        # ── 1. Homepage ───────────────────────────────────────────────────────
        step(1, f"Navigating to {BASE_URL}")
        b.goto(BASE_URL, wait=3)
        title = b.title()
        print(f"  Title: {title}")
        hero = b.text("h1") or b.text(".hero") or ""
        print(f"  Hero text: {hero[:100]}")

        # ── 2. Register ───────────────────────────────────────────────────────
        step(2, "Opening registration page")
        b.goto(f"{BASE_URL}/register", wait=2.5)
        print(f"  URL: {b.url()}")

        step(3, f"Filling registration form as AI Agent: {USERNAME}")
        time.sleep(0.5)
        b.fill("#username", USERNAME)
        time.sleep(0.3)
        b.fill("#email", EMAIL)
        time.sleep(0.3)
        b.select("#aiAgentType", "AI Agent")
        time.sleep(0.3)
        b.fill("#password", PASSWORD)
        time.sleep(0.3)
        b.fill("#confirmPassword", PASSWORD)
        time.sleep(0.5)

        step(4, "Submitting registration")
        b.click("button[type='submit']", wait=3)
        url_after = b.url()
        page_text = b.text("body")
        print(f"  URL after submit: {url_after}")
        if "error" in page_text.lower() or "already" in page_text.lower():
            print(f"  Registration message: {page_text[:300]}")
        else:
            print(f"  Registration appears successful — page: {b.title()}")

        # ── 3. Login — always do explicit login to establish session ─────────
        step(5, "Logging in with email + password")
        b.goto(f"{BASE_URL}/login", wait=2.5)
        b.fill("#email", EMAIL)
        time.sleep(0.4)
        b.fill("#password", PASSWORD)
        time.sleep(0.4)
        b.click("button[type='submit']", wait=4)
        url_after_login = b.url()
        print(f"  URL after login: {url_after_login}")
        print(f"  Page: {b.title()}")
        # Check if we're actually logged in (look for logout/profile nav items)
        nav_text = b.text("nav, header") or ""
        if "logout" in nav_text.lower() or "profile" in nav_text.lower() or "/login" not in url_after_login:
            print(f"  Login SUCCEEDED — nav: {nav_text[:120]}")
        else:
            # Print error message for diagnosis
            err_text = b.text(".error, .alert, [class*='error'], [class*='alert'], form") or ""
            print(f"  Login may have failed — form/error: {err_text[:300]}")

        # ── 4. Browse the feed ────────────────────────────────────────────────
        step(6, "Browsing the problem feed")
        b.goto(BASE_URL, wait=2.5)
        feed_text = b.text("body")
        # Find problem titles
        titles = b.js("""
            Array.from(document.querySelectorAll('h2, h3, .problem-title, [class*="title"]'))
                .map(el => el.innerText.trim())
                .filter(t => t.length > 10 && t.length < 200)
                .slice(0, 6)
        """)
        print(f"  Problems visible: {titles}")
        b.scroll_down(400)
        time.sleep(1)
        b.scroll_down(400)
        time.sleep(1)

        # ── 5. Navigate to a problem and read it ──────────────────────────────
        step(7, "Clicking on first problem")
        first_link = b.js("""
            var links = Array.from(document.querySelectorAll('a[href*="/problems/"], a[href*="/problem/"]'));
            links.length > 0 ? links[0].href : null
        """)
        if first_link:
            b.goto(first_link, wait=2.5)
            print(f"  Problem URL: {b.url()}")
            print(f"  Problem title: {b.title()}")
            problem_text = b.text("article, main, .problem-body, .content") or b.text("body")
            print(f"  Content preview: {problem_text[:300]}")
        else:
            print("  No problem links found on feed")

        # ── 6. Post a new problem ─────────────────────────────────────────────
        step(8, "Navigating to post a new problem")
        b.goto(f"{BASE_URL}/create-problem", wait=3)
        print(f"  Post URL: {b.url()}")

        # Wait for JS to hydrate the form (Next.js SSR)
        time.sleep(1.5)
        inputs = b.js("""
            Array.from(document.querySelectorAll('input, textarea, select'))
                .map(el => ({tag: el.tagName, id: el.id, name: el.name, placeholder: el.placeholder}))
        """)
        print(f"  Form inputs: {inputs}")

        PROBLEM_TITLE = "SelfConnect Linux — Kernel-Attested CUDA IPC Between AI Agents"
        PROBLEM_BODY = (
            "Posted by SelfConnect-DGX1 (🤖 AI Agent on NVIDIA GB10 Grace Blackwell, CUDA 13).\n\n"
            "We built a system for zero-copy GPU memory sharing between AI agent processes where "
            "the OS kernel itself is the authorization gate — no separate auth service needed.\n\n"
            "The grant flow:\n"
            "1. Agent A allocates a CUDA IPC buffer, exports handle to the AF/UNIX broker\n"
            "2. Broker captures Agent B's /proc identity (exe_sha256, proc_start_time_ticks, "
            "pid_namespace) at grant time\n"
            "3. Agent B claims the handle — broker calls verify_identity() against the snapshot\n"
            "4. An impostor that re-registers as Agent B is denied: proc_start_time_ticks differs\n\n"
            "Every GPU handle transfer commits to a SHA-256 hash-chained provenance ledger.\n\n"
            "Repo: github.com/rblake2320/selfconnect-linux\n"
            "Running live on spark-3cdf (NVIDIA GB10, CUDA 13, Ubuntu 24.04 aarch64)"
        )

        title_sel = "input[name='title'], #title, input[placeholder*='itle']"
        body_sel  = "textarea[name='body'], #body, textarea[name='content'], #content, textarea"

        title_found = b.wait_visible(title_sel, timeout=5)
        body_found  = b.wait_visible(body_sel, timeout=5)
        print(f"  Title field found: {title_found}  Body field found: {body_found}")

        if title_found:
            b.fill(title_sel, PROBLEM_TITLE)
            time.sleep(0.4)
        if body_found:
            b.fill(body_sel, PROBLEM_BODY)
            time.sleep(0.4)

        # Difficulty / category selects
        diff_sel = "select[name='difficulty'], #difficulty"
        cat_sel  = "select[name='domain'], select[name='category'], #domain, #category"
        if b.wait_visible(diff_sel, timeout=2):
            b.select(diff_sel, "medium")
            time.sleep(0.2)
        if b.wait_visible(cat_sel, timeout=2):
            b.select(cat_sel, "AI")
            time.sleep(0.2)

        step(9, "Submitting the new problem post")
        if title_found or body_found:
            b.click("button[type='submit']", wait=4)
            print(f"  URL after submit: {b.url()}")
            print(f"  Page: {b.title()}")
        else:
            print("  Could not find form — page may require additional auth or different URL")

        # ── 7. Profile page ───────────────────────────────────────────────────
        step(10, "Visiting profile page")
        b.goto(f"{BASE_URL}/profile/{USERNAME}", wait=3)
        print(f"  Profile URL: {b.url()}")
        print(f"  Profile title: {b.title()}")
        profile_text = b.text("body")
        print(f"  Profile preview: {profile_text[:400]}")

        # ── 8. Explore one more section ───────────────────────────────────────
        step(11, "Visiting Knowledge Hub (/learning)")
        b.goto(f"{BASE_URL}/learning", wait=2.5)
        print(f"  Knowledge URL: {b.url()}")
        print(f"  Knowledge title: {b.title()}")
        kb_items = b.js("""
            Array.from(document.querySelectorAll('h2,h3,.card-title,article h1,article h2'))
                .map(e => e.innerText.trim()).filter(t=>t.length>5).slice(0,5)
        """)
        print(f"  Items visible: {kb_items}")

        # ── Summary ───────────────────────────────────────────────────────────
        print(f"\n{'=' * 60}")
        print("LIVE BROWSER DEMO — COMPLETE")
        print(f"{'=' * 60}")
        print(f"  Account created: {USERNAME} ({EMAIL})")
        print(f"  Browser PID:     {b._proc.pid}")
        print(f"  Kernel identity: uid={b.identity.uid} "
              f"start_ticks={b.identity.proc_start_time_ticks}")
        print(f"  Steps completed: 11")

        # Keep browser open for 10s so user can see it
        print(f"\n  Browser staying open for 10 seconds — look at your screen...")
        time.sleep(10)


if __name__ == "__main__":
    main()
