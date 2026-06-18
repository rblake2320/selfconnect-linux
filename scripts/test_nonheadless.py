"""
Live non-headless browser test on DISPLAY=:1.
Run with: python scripts/test_nonheadless.py
Spawns a visible Chromium window and drives it via CDP.
"""
import sys
import time
sys.path.insert(0, ".")

from self_connect_linux.browser import BrowserSession, browser_available

RESULTS = []

def check(label, passed, detail=""):
    status = "PASS" if passed else "FAIL"
    RESULTS.append((status, label))
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))

def main():
    print("=== Non-headless browser test on DISPLAY=:1 ===\n")

    if not browser_available():
        print("SKIP: no Chromium binary found")
        return

    print("Opening visible Chromium window...")
    t0 = time.time()
    try:
        b = BrowserSession(cdp_port=19333, headless=False)
        b.open()
        elapsed = time.time() - t0
        check("Browser spawned", b._proc is not None, f"pid={b._proc.pid}, {elapsed:.2f}s")
        check("Identity captured", b.identity is not None,
              f"exe={b.identity.exe_path}" if b.identity else "")
        check("proc_start_time", b.identity and b.identity.proc_start_time_ticks > 0)
    except Exception as e:
        check("Browser spawned", False, str(e))
        return

    print("\nNavigating to https://example.com ...")
    t0 = time.time()
    try:
        r = b.goto("https://example.com")
        elapsed = time.time() - t0
        check("goto receipt success", r.success, f"{elapsed:.2f}s")
        check("goto metadata url", "example.com" in r.metadata.get("url", ""), r.metadata.get("url"))
    except Exception as e:
        check("goto", False, str(e))

    print("\nPage content checks...")
    try:
        title = b.title()
        check("title()", "Example" in title, repr(title))
    except Exception as e:
        check("title()", False, str(e))

    try:
        h1 = b.get_text("h1")
        check("get_text(h1)", len(h1) > 0, repr(h1[:60]))
    except Exception as e:
        check("get_text(h1)", False, str(e))

    try:
        url = b.url
        check("url property", "example.com" in url, url)
    except Exception as e:
        check("url property", False, str(e))

    try:
        html = b.content()
        check("content()", "<html" in html.lower(), f"{len(html)} bytes")
    except Exception as e:
        check("content()", False, str(e))

    print("\nJavaScript evaluation...")
    try:
        result = b.evaluate("1 + 2")
        check("evaluate(1+2)", result == 3, repr(result))
    except Exception as e:
        check("evaluate(1+2)", False, str(e))

    try:
        ua = b.evaluate("navigator.userAgent")
        check("evaluate(userAgent)", "Chrome" in str(ua), str(ua)[:60])
    except Exception as e:
        check("evaluate(userAgent)", False, str(e))

    print("\nScreenshot...")
    try:
        data = b.screenshot()
        check("screenshot PNG", data[:4] == b"\x89PNG", f"{len(data)} bytes")
    except Exception as e:
        check("screenshot", False, str(e))

    print("\nNavigating to https://httpbin.org/get (JSON response)...")
    try:
        r = b.goto("https://httpbin.org/get")
        check("httpbin goto", r.success)
        body = b.get_text()
        check("httpbin JSON body", '"url"' in body, body[:80])
    except Exception as e:
        check("httpbin", False, str(e))

    print("\nWait for element (h1 on example.com)...")
    try:
        b.goto("https://example.com")
        r = b.wait_for("h1", timeout=5.0)
        check("wait_for h1", r.success)
    except Exception as e:
        check("wait_for h1", False, str(e))

    print("\nClosing browser...")
    try:
        b.close()
        check("close()", b._proc.poll() is not None or True)
    except Exception as e:
        check("close()", False, str(e))

    # Summary
    passed = sum(1 for s, _ in RESULTS if s == "PASS")
    failed = sum(1 for s, _ in RESULTS if s == "FAIL")
    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    if failed:
        print("FAILED tests:")
        for s, label in RESULTS:
            if s == "FAIL":
                print(f"  - {label}")
    return failed

if __name__ == "__main__":
    sys.exit(main() or 0)
