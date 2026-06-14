#!/usr/bin/env python3
"""End-to-end validation of ALL 6 browser tools using real Playwright.

Run this directly (not via pytest) to avoid asyncio conflicts:
    python3 tests/browser_validation.py
"""

from __future__ import annotations

import http.server
import os
import socketserver
import threading
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.browser_ops import (
    _open_url,
    _browser_navigate,
    _browser_snapshot,
    _browser_click,
    _browser_type,
    _browser_screenshot,
    _close_browser,
)


HTML = """<!DOCTYPE html>
<html><head><title>Validation Page</title></head>
<body>
  <h1>Hello World</h1>
  <button aria-label="Login">Sign In</button>
  <input type="text" placeholder="Search" aria-label="Search">
  <a href="/page2">Go to page 2</a>
  <div role="checkbox" aria-label="Accept" aria-checked="false">Accept</div>
</body>
</html>"""

HTML2 = """<!DOCTYPE html>
<html><head><title>Page Two</title></head>
<body><p>Second page reached.</p></body>
</html>"""


def start_server():
    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(HTML.encode())
            elif self.path == "/page2":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(HTML2.encode())

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as s:
        port = s.server_address[1]
    server = socketserver.TCPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{port}"


def test(label, result, expect_success=True):
    status = "PASS" if result.success == expect_success else "FAIL"
    marker = "[OK]" if result.success == expect_success else "[FAIL]"
    print(f"  {marker} {status}: {label}")
    if not result.success:
        print(f"       content: {result.content[:120]}")
    return result.success == expect_success


def main():
    url = start_server()
    all_pass = True
    print(f"Server at {url}\n")

    # 1. open_url (mock -- don't open real browser)
    print("--- open_url ---")
    from unittest.mock import patch
    with patch("webbrowser.open", return_value=True):
        all_pass &= test("Opens valid URL",
                         _open_url({"url": "https://example.com"}, None, None))
    all_pass &= test("Rejects missing url",
                     _open_url({}, None, None), expect_success=False)
    all_pass &= test("Rejects bad scheme",
                     _open_url({"url": "ftp://x.com"}, None, None), expect_success=False)
    print()

    # 2. browser_navigate
    print("--- browser_navigate ---")
    all_pass &= test("Navigates to homepage",
                     _browser_navigate({"url": url}, None, None))
    all_pass &= test("Navigates to page 2",
                     _browser_navigate({"url": f"{url}/page2"}, None, None))
    all_pass &= test("Rejects missing url",
                     _browser_navigate({}, None, None), expect_success=False)
    print()

    # 3. browser_snapshot
    print("--- browser_snapshot ---")
    _browser_navigate({"url": url}, None, None)
    snap = _browser_snapshot({}, None, None)
    all_pass &= test("Captures interactive elements", snap)
    print(f"       Snapshot content:\n{snap.content[:500]}")
    print()

    # 4. browser_click
    print("--- browser_click ---")
    # Re-navigate to reset page state
    _browser_navigate({"url": url}, None, None)
    all_pass &= test("Clicks button 'Login'",
                     _browser_click({"role": "button", "name": "Login"}, None, None))
    all_pass &= test("Rejects missing role",
                     _browser_click({"name": "X"}, None, None), expect_success=False)
    print()

    # 5. browser_type
    print("--- browser_type ---")
    _browser_navigate({"url": url}, None, None)
    all_pass &= test("Types into textbox 'Search'",
                     _browser_type(
                         {"role": "textbox", "name": "Search", "text": "hello world"},
                         None, None))
    all_pass &= test("Rejects missing name",
                     _browser_type({"text": "hi"}, None, None), expect_success=False)
    print()

    # 6. browser_screenshot
    print("--- browser_screenshot ---")
    _browser_navigate({"url": url}, None, None)
    shot_path = "browser_validation_screenshot.png"
    result = _browser_screenshot({"path": shot_path}, None, None)
    all_pass &= test("Saves screenshot", result)
    if result.success and os.path.exists(shot_path):
        size = os.path.getsize(shot_path)
        print(f"       File size: {size} bytes")
        all_pass &= (size > 0)
        if size > 0:
            print("  [OK] PASS: Screenshot > 0 bytes")
        else:
            print("  [FAIL] FAIL: Screenshot is empty")
    _close_browser()

    # Summary
    print()
    print("=" * 50)
    if all_pass:
        print("ALL TESTS PASSED [OK]")
    else:
        print("SOME TESTS FAILED [FAIL]")
    print("=" * 50)
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
