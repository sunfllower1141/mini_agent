#!/usr/bin/env python3
"""Tests for tools/browser_ops.py.

Covers all 6 browser tools:
    open_url           — unit tests with mocked webbrowser.open
    browser_navigate   — integration with real Playwright (localhost server)
    browser_snapshot   — integration with real Playwright
    browser_click      — integration with real Playwright
    browser_type       — integration with real Playwright
    browser_screenshot — integration with real Playwright

Also tests error paths: missing args, bad URLs, Playwright-not-installed.
"""

from __future__ import annotations

import http.server
import os
import socketserver
import tempfile
import threading
from pathlib import Path
from unittest import mock

import pytest

import tools.browser_ops as bo
from tools import ToolResult


# ---------------------------------------------------------------------------
# Helpers — local HTTP server for integration tests
# ---------------------------------------------------------------------------

HTML_PAGE = """<!DOCTYPE html>
<html>
<head><title>Test Page</title></head>
<body>
  <button aria-label="Login">Sign In</button>
  <input type="text" placeholder="Search" aria-label="Search">
  <a href="/page2">Go to page 2</a>
  <div role="checkbox" aria-label="Accept terms" aria-checked="false">Accept</div>
</body>
</html>
"""

HTML_PAGE2 = """<!DOCTYPE html>
<html>
<head><title>Page Two</title></head>
<body><p>You arrived at page 2.</p></body>
</html>
"""


def _start_test_server() -> tuple[str, threading.Thread]:
    """Start a local HTTP server on a free port, return (base_url, thread)."""
    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/" or self.path == "/index.html":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(HTML_PAGE.encode())
            elif self.path == "/page2":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(HTML_PAGE2.encode())
            else:
                super().do_GET()

    # Find a free port
    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as s:
        port = s.server_address[1]

    server = socketserver.TCPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{port}", thread


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_browser_state():
    """Reset the lazy singleton globals between tests."""
    bo._close_browser()
    yield
    bo._close_browser()


@pytest.fixture(scope="module")
def test_server():
    """Module-scoped test HTTP server."""
    url, thread = _start_test_server()
    yield url
    # daemon thread will die with process


# ---------------------------------------------------------------------------
# open_url tests (stdlib only, no Playwright needed)
# ---------------------------------------------------------------------------

class TestOpenUrl:
    """Unit tests for open_url — uses mock for webbrowser.open."""

    def test_missing_url(self):
        result = bo._open_url({}, None, None)
        assert result.success is False
        assert "Missing required parameter" in result.content

    def test_bad_scheme(self):
        result = bo._open_url({"url": "ftp://example.com"}, None, None)
        assert result.success is False
        assert "http://" in result.content

    def test_opens_browser(self):
        with mock.patch("webbrowser.open", return_value=True) as m:
            result = bo._open_url({"url": "https://example.com"}, None, None)
        assert result.success is True
        assert "Opened" in result.content
        m.assert_called_once_with("https://example.com", new=2)

    def test_browser_fails_to_open(self):
        with mock.patch("webbrowser.open", return_value=False) as m:
            result = bo._open_url({"url": "https://example.com"}, None, None)
        assert result.success is False
        assert "no suitable browser" in result.content

    def test_browser_raises_exception(self):
        with mock.patch("webbrowser.open", side_effect=RuntimeError("boom")):
            result = bo._open_url({"url": "https://example.com"}, None, None)
        assert result.success is False
        assert "boom" in result.content

    def test_summary(self):
        summary = bo._open_url_summary({"url": "https://example.com/path"})
        assert "open_url" in summary
        assert "example.com" in summary


# ---------------------------------------------------------------------------
# browser_navigate tests (integration — real Playwright)
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skip(reason="Integration test — run tests/browser_validation.py instead")


@pytest.mark.integration
class TestBrowserNavigate:
    """Integration tests using a local HTTP server + real Playwright."""

    def test_navigates_and_returns_title(self, test_server):
        result = bo._browser_navigate({"url": test_server}, None, None)
        assert result.success is True
        assert "Test Page" in result.content
        assert test_server in result.content

    def test_follows_redirect_and_shows_final_url(self, test_server):
        result = bo._browser_navigate({"url": f"{test_server}/page2"}, None, None)
        assert result.success is True
        assert "Page Two" in result.content

    def test_missing_url(self):
        result = bo._browser_navigate({}, None, None)
        assert result.success is False
        assert "Missing required parameter" in result.content

    def test_bad_scheme(self):
        result = bo._browser_navigate({"url": "ftp://bad"}, None, None)
        assert result.success is False
        assert "http://" in result.content

    def test_invalid_url_returns_error(self):
        result = bo._browser_navigate({"url": "http://does.not.exist.invalid"}, None, None)
        assert result.success is False

    def test_summary(self):
        summary = bo._browser_navigate_summary({"url": "https://example.com"})
        assert "browser_navigate" in summary


# ---------------------------------------------------------------------------
# browser_snapshot tests (integration — real Playwright)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestBrowserSnapshot:
    """Integration tests for accessibility tree capture."""

    def test_captures_interactive_elements(self, test_server):
        # Navigate first so there's a page to snapshot
        bo._browser_navigate({"url": test_server}, None, None)
        result = bo._browser_snapshot({}, None, None)
        assert result.success is True
        # Should contain button and input from our test page
        content = result.content.lower()
        assert "button" in content
        assert "login" in content or "sign in" in content

    def test_no_page_returns_error(self):
        bo._PAGE = None  # force no page
        result = bo._browser_snapshot({}, None, None)
        assert result.success is False

    def test_summary(self):
        summary = bo._browser_snapshot_summary({})
        assert summary == "browser_snapshot()"


# ---------------------------------------------------------------------------
# browser_click tests (integration — real Playwright)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestBrowserClick:
    """Integration tests for clicking elements."""

    def test_clicks_button_by_role_and_name(self, test_server):
        bo._browser_navigate({"url": test_server}, None, None)
        result = bo._browser_click({"role": "button", "name": "Login"}, None, None)
        assert result.success is True
        assert "Clicked" in result.content
        assert "Login" in result.content

    def test_clicks_link_and_navigates(self, test_server):
        bo._browser_navigate({"url": test_server}, None, None)
        result = bo._browser_click({"role": "link", "name": "Go to page 2"}, None, None)
        assert result.success is True
        # After clicking, we should be on page 2
        nav = bo._browser_snapshot({}, None, None)
        assert "page 2" in nav.content.lower() or "Page Two" in nav.content

    def test_missing_role(self):
        result = bo._browser_click({"name": "Login"}, None, None)
        assert result.success is False
        assert "role" in result.content

    def test_missing_name(self):
        result = bo._browser_click({"role": "button"}, None, None)
        assert result.success is False
        assert "name" in result.content

    def test_element_not_found(self, test_server):
        bo._browser_navigate({"url": test_server}, None, None)
        result = bo._browser_click(
            {"role": "button", "name": "NonexistentButton"}, None, None
        )
        assert result.success is False

    def test_summary(self):
        summary = bo._browser_click_summary({"role": "button", "name": "OK"})
        assert "browser_click" in summary
        assert "button" in summary
        assert "OK" in summary


# ---------------------------------------------------------------------------
# browser_type tests (integration — real Playwright)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestBrowserType:
    """Integration tests for typing into inputs."""

    def test_types_into_input(self, test_server):
        bo._browser_navigate({"url": test_server}, None, None)
        result = bo._browser_type(
            {"role": "textbox", "name": "Search", "text": "hello world"},
            None, None,
        )
        assert result.success is True
        assert "Typed" in result.content
        assert "hello world" in result.content

    def test_missing_name(self):
        result = bo._browser_type({"text": "hello"}, None, None)
        assert result.success is False
        assert "name" in result.content

    def test_missing_text(self):
        result = bo._browser_type({"name": "Search"}, None, None)
        assert result.success is False
        assert "text" in result.content

    def test_summary(self):
        summary = bo._browser_type_summary({"name": "Search", "text": "hello"})
        assert "browser_type" in summary


# ---------------------------------------------------------------------------
# browser_screenshot tests (integration — real Playwright)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestBrowserScreenshot:
    """Integration tests for screenshot capture."""

    def test_captures_screenshot(self, test_server, tmp_path):
        bo._browser_navigate({"url": test_server}, None, None)
        screenshot_path = str(tmp_path / "test.png")
        result = bo._browser_screenshot(
            {"path": screenshot_path}, None, None
        )
        assert result.success is True
        assert os.path.exists(screenshot_path)
        assert os.path.getsize(screenshot_path) > 0

    def test_default_path(self, test_server):
        bo._browser_navigate({"url": test_server}, None, None)
        result = bo._browser_screenshot({}, None, None)
        assert result.success is True
        # Default path is browser_screenshot.png in cwd
        # Clean up after test
        p = Path("browser_screenshot.png")
        if p.exists():
            p.unlink()

    def test_summary(self):
        summary = bo._browser_screenshot_summary({"path": "shot.png"})
        assert "browser_screenshot" in summary
        assert "shot.png" in summary


# ---------------------------------------------------------------------------
# Playwright-not-installed tests (mock the import to fail)
# ---------------------------------------------------------------------------

class TestPlaywrightNotInstalled:
    """Tests for graceful error when Playwright is missing."""

    def test_navigate_without_playwright(self):
        with mock.patch.object(bo, "_get_playwright", side_effect=ImportError("no pw")):
            result = bo._browser_navigate({"url": "https://example.com"}, None, None)
        assert result.success is False
        assert "no pw" in result.content

    def test_snapshot_without_playwright(self):
        with mock.patch.object(bo, "_get_page", side_effect=ImportError("no pw")):
            result = bo._browser_snapshot({}, None, None)
        assert result.success is False

    def test_click_without_playwright(self):
        with mock.patch.object(bo, "_get_page", side_effect=ImportError("no pw")):
            result = bo._browser_click(
                {"role": "button", "name": "X"}, None, None
            )
        assert result.success is False

    def test_type_without_playwright(self):
        with mock.patch.object(bo, "_get_page", side_effect=ImportError("no pw")):
            result = bo._browser_type(
                {"role": "textbox", "name": "X", "text": "hi"}, None, None
            )
        assert result.success is False

    def test_screenshot_without_playwright(self):
        with mock.patch.object(bo, "_get_page", side_effect=ImportError("no pw")):
            result = bo._browser_screenshot({"path": "x.png"}, None, None)
        assert result.success is False


# ---------------------------------------------------------------------------
# Interactive element formatting
# ---------------------------------------------------------------------------

class TestInteractiveElementFormatting:
    """Unit tests for _format_interactive_elements helper."""

    def test_basic_element(self):
        elements = [{"role": "button", "name": "Click me", "tag": "button",
                      "type": "", "href": "", "checked": "", "disabled": False}]
        result = bo._format_interactive_elements(elements)
        assert 'button' in result
        assert '"Click me"' in result
        assert '[0]' in result

    def test_element_with_href(self):
        elements = [{"role": "a", "name": "Home", "tag": "a",
                      "type": "", "href": "/home", "checked": "", "disabled": False}]
        result = bo._format_interactive_elements(elements)
        assert '/home' in result

    def test_element_with_checked_and_disabled(self):
        elements = [{"role": "checkbox", "name": "Accept", "tag": "div",
                      "type": "", "href": "", "checked": "true", "disabled": True}]
        result = bo._format_interactive_elements(elements)
        assert '[true]' in result
        assert '[disabled]' in result
        assert '"Accept"' in result

    def test_multiple_elements(self):
        elements = [
            {"role": "button", "name": "OK", "tag": "button",
             "type": "", "href": "", "checked": "", "disabled": False},
            {"role": "textbox", "name": "Search", "tag": "input",
             "type": "text", "href": "", "checked": "", "disabled": False},
        ]
        result = bo._format_interactive_elements(elements)
        lines = result.strip().split("\n")
        assert len(lines) == 2
        assert "OK" in result
        assert "Search" in result

    def test_element_with_type(self):
        elements = [{"role": "input", "name": "Email", "tag": "input",
                      "type": "email", "href": "", "checked": "", "disabled": False}]
        result = bo._format_interactive_elements(elements)
        assert 'type=email' in result
