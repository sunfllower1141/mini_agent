#!/usr/bin/env python3
"""
browser_ops.py — browser interaction tools for mini_agent.

Tools:
    open_url            — open the user's default browser to a URL
    browser_navigate    — navigate a headless Playwright page
    browser_snapshot    — capture the accessibility tree (LLM-friendly structured view)
    browser_click       — click an element by role + name
    browser_type        — type text into an input field
    browser_screenshot  — capture a full-page PNG screenshot

Playwright is loaded lazily so the module imports even when
playwright is not installed.  Tools that need Playwright return
a clear error message if it's missing.
"""

from __future__ import annotations

import os
import threading
import webbrowser as _webbrowser

from core.safety import ReadSafetyGate, WriteSafetyGate
from tools import _register, _summarize, ToolResult

# ---------------------------------------------------------------------------
# Playwright lazy singleton — THREAD-AWARE
#
# The tool dispatch runs every tool call in a *new* daemon thread.
# Playwright's sync API (greenlet-based) ties its internal event-loop
# greenlet to the thread that created it.  Accessing the singleton from
# a different thread causes "cannot switch to a different thread (which
# happens to have exited)".  We track the creation thread and recreate
# the browser whenever the calling thread changes.
# ---------------------------------------------------------------------------

_PLAYWRIGHT = None
_BROWSER = None
_PAGE = None
_PLAYWRIGHT_INSTANCE = None
_BROWSER_THREAD_ID: int | None = None  # thread that owns the current browser


def _get_playwright():
    """Import Playwright and return the module (lazy, cached)."""
    global _PLAYWRIGHT
    if _PLAYWRIGHT is None:
        try:
            from playwright.sync_api import sync_playwright
            _PLAYWRIGHT = sync_playwright
        except ImportError:
            raise ImportError(
                "Playwright is not installed. Install it with:\n"
                "  pip install playwright && playwright install chromium"
            )
    return _PLAYWRIGHT


# Substrings that indicate the browser/event-loop thread has died and
# the global singleton must be hard-reset (skipping normal close).
_BROWSER_DEAD_SIGNATURES: tuple[str, ...] = (
    "different thread",
    "has exited",
    "Target page, context or browser has been closed",
    "Browser closed",
)


def _page_alive(page) -> bool:
    """Check if a Playwright page object is still responsive."""
    try:
        page.evaluate("1")  # trivial JS eval to test connectivity
        return True
    except Exception as exc:
        msg = str(exc)
        if any(sig in msg for sig in _BROWSER_DEAD_SIGNATURES):
            return False
        return False


def _force_reset_browser_globals() -> None:
    """Hard-reset browser global state without calling close() on stale objects."""
    global _BROWSER, _PAGE, _PLAYWRIGHT_INSTANCE, _BROWSER_THREAD_ID
    _BROWSER = None
    _PAGE = None
    _PLAYWRIGHT_INSTANCE = None
    _BROWSER_THREAD_ID = None


def _get_page():
    """Return a browser page, recreating if the calling thread changed.

    Because tool dispatch runs each call in a new daemon thread,
    Playwright's greenlet-backed sync API must be created fresh for
    each thread.  This function detects thread changes and recreates
    the browser automatically.
    """
    global _BROWSER, _PAGE, _PLAYWRIGHT_INSTANCE, _BROWSER_THREAD_ID

    current_tid = threading.get_ident()
    if _PAGE is not None and _BROWSER_THREAD_ID == current_tid:
        if _page_alive(_PAGE):
            return _PAGE
        # Alive check failed — browser crashed within same thread
        _force_reset_browser_globals()
    elif _PAGE is not None:
        # Different thread — stale singleton, force reset
        _force_reset_browser_globals()

    # Create fresh browser on this thread
    for attempt in range(2):
        try:
            pw = _get_playwright()
            _PLAYWRIGHT_INSTANCE = pw().__enter__()
            _BROWSER = _PLAYWRIGHT_INSTANCE.chromium.launch(headless=True)
            _PAGE = _BROWSER.new_page()
            _BROWSER_THREAD_ID = current_tid
            return _PAGE
        except Exception as exc:
            msg = str(exc)
            if attempt == 1 or not any(
                sig in msg for sig in _BROWSER_DEAD_SIGNATURES
            ):
                raise
            _force_reset_browser_globals()

    raise RuntimeError("Failed to create browser page after retries")


def _close_browser():
    """Close the shared browser and clean up globals. Used by tests."""
    global _BROWSER, _PAGE, _PLAYWRIGHT_INSTANCE
    if _BROWSER is not None:
        _BROWSER.close()
        _BROWSER = None
    if _PLAYWRIGHT_INSTANCE is not None:
        try:
            _PLAYWRIGHT_INSTANCE.__exit__(None, None, None)
        except Exception:
            pass
        _PLAYWRIGHT_INSTANCE = None
    _PAGE = None
    # Stop the asyncio event loop Playwright spun up
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.stop()
        loop.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# open_url — trivial, no deps beyond stdlib webbrowser
# ---------------------------------------------------------------------------

@_register("open_url")
def _open_url(args: dict, _wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:
    """Open the user's default browser to the given URL.

    Opens in a new tab.  Returns immediately — does not wait for the
    page to load or report any browser state.  For programmatic
    browser interaction, use the browser_* tools instead.
    """
    url = args.get("url", "")
    if not url:
        return ToolResult(success=False, content="Missing required parameter: 'url'.")

    if not (url.startswith("http://") or url.startswith("https://")):
        return ToolResult(success=False,
                          content="URL must start with http:// or https://")

    try:
        opened = _webbrowser.open(url, new=2)  # new=2 → new tab if possible
        if opened:
            return ToolResult(success=True,
                              content=f"Opened {url} in default browser.")
        else:
            return ToolResult(success=False,
                              content=f"Browser failed to open {url} — "
                                      "no suitable browser found.")
    except Exception as exc:
        return ToolResult(success=False, content=f"Failed to open {url}: {exc}")


@_summarize("open_url")
def _open_url_summary(args: dict) -> str:
    url = args.get("url", "?")
    preview = url[:60] + ("…" if len(url) > 60 else "")
    return f"open_url({preview})"


# ---------------------------------------------------------------------------
# browser_navigate — headless Playwright
# ---------------------------------------------------------------------------

@_register("browser_navigate")
def _browser_navigate(args: dict, _wg: WriteSafetyGate,
                      _rg: ReadSafetyGate) -> ToolResult:
    """Navigate a headless browser to a URL.

    Returns the page title and final URL after redirects.
    """
    url = args.get("url", "")
    if not url:
        return ToolResult(success=False, content="Missing required parameter: 'url'.")

    if not (url.startswith("http://") or url.startswith("https://")):
        return ToolResult(success=False,
                          content="URL must start with http:// or https://")

    try:
        page = _get_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        title = page.title()
        final_url = page.url
        return ToolResult(
            success=True,
            content=f"Navigated to {final_url}\nTitle: {title}"
        )
    except Exception as exc:
        return ToolResult(success=False,
                          content=f"Failed to navigate to {url}: {exc}")


@_summarize("browser_navigate")
def _browser_navigate_summary(args: dict) -> str:
    url = args.get("url", "?")
    return f"browser_navigate({url[:60]})"


# ---------------------------------------------------------------------------
# browser_snapshot — accessibility tree (LLM-friendly)
# ---------------------------------------------------------------------------

@_register("browser_snapshot")
def _browser_snapshot(args: dict, _wg: WriteSafetyGate,
                      _rg: ReadSafetyGate) -> ToolResult:
    """Capture a structured view of the current page's interactive elements.

    Returns a text listing of buttons, links, inputs, and other
    interactive elements — their roles, accessible names, and states.
    This is much more compact and LLM-friendly than raw HTML or a screenshot.

    The LLM can use the snapshot to decide which element to click,
    type into, or inspect further.
    """
    try:
        page = _get_page()
        elements = _extract_interactive_elements(page)
        if not elements:
            # Fall back to page text
            body_text = page.inner_text("body").strip()
            if body_text:
                return ToolResult(success=True,
                                  content=f"(page text)\n{body_text[:4000]}")
            return ToolResult(success=True,
                              content="(empty page — no interactive elements)")
        text = _format_interactive_elements(elements)
        if len(text) > 8000:
            text = text[:8000] + "\n… (truncated)"
        return ToolResult(success=True, content=text)
    except Exception as exc:
        return ToolResult(success=False,
                          content=f"Failed to capture page snapshot: {exc}")


def _extract_interactive_elements(page) -> list[dict]:
    """Extract interactive elements from the page via JS evaluation."""
    return page.evaluate("""() => {
        const interactive = [
            'button', 'a', 'input', 'select', 'textarea',
            'details', 'summary',
            '[role="button"]', '[role="link"]', '[role="textbox"]',
            '[role="searchbox"]', '[role="checkbox"]', '[role="radio"]',
            '[role="combobox"]', '[role="listbox"]', '[role="menuitem"]',
            '[role="tab"]', '[role="switch"]', '[role="slider"]',
            '[role="spinbutton"]', '[role="option"]', '[role="treeitem"]',
            '[onclick]', '[tabindex]', '[contenteditable="true"]'
        ];
        const selector = interactive.join(',');

        function resolveName(el) {
            // 1. aria-label (explicit)
            let name = el.getAttribute('aria-label') || '';
            if (name) return name;

            // 2. placeholder
            name = el.getAttribute('placeholder') || '';
            if (name) return name;

            // 3. associated <label for="..."> for form elements with an id
            if (el.id) {
                const label = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                if (label) {
                    name = label.textContent.trim().slice(0, 80);
                    if (name) return name;
                }
            }

            // 4. wrapping <label> parent
            const parentLabel = el.closest('label');
            if (parentLabel) {
                // Get label text excluding the element's own text
                const clone = parentLabel.cloneNode(true);
                const child = clone.querySelector(el.tagName);
                if (child) child.remove();
                name = clone.textContent.trim().slice(0, 80);
                if (name) return name;
            }

            // 5. fallback to element's own text content
            return el.textContent.trim().slice(0, 80) || '';
        }

        const seen = new Set();
        return Array.from(document.querySelectorAll(selector))
            .filter(el => {
                const rect = el.getBoundingClientRect();
                const visible = rect.width > 0 && rect.height > 0;
                if (!visible) return false;
                const name = resolveName(el);
                const key = el.tagName + '|' + name;
                if (seen.has(key)) return false;
                seen.add(key);
                return true;
            })
            .map(el => ({
                tag: el.tagName.toLowerCase(),
                role: el.getAttribute('role') || el.tagName.toLowerCase(),
                name: resolveName(el),
                type: el.getAttribute('type') || '',
                disabled: !!el.disabled,
                checked: el.getAttribute('aria-checked') || '',
                href: el.getAttribute('href') || '',
            }));
    }""")


def _format_interactive_elements(elements: list[dict]) -> str:
    """Format a list of interactive element dicts as plain text."""
    lines: list[str] = []
    for i, el in enumerate(elements):
        role = el.get("role", "unknown")
        name = el.get("name", "")
        tag = el.get("tag", "")
        typ = el.get("type", "")
        href = el.get("href", "")
        checked = el.get("checked", "")
        disabled = el.get("disabled", False)

        parts = [f"[{i}]", role]
        if name:
            parts.append(f'"{name}"')
        if typ and typ != "text":
            parts.append(f"type={typ}")
        if href:
            parts.append(f"→ {href}")
        if checked:
            parts.append(f"[{checked}]")
        if disabled:
            parts.append("[disabled]")

        lines.append("  " + " ".join(parts))

    return "\n".join(lines)


@_summarize("browser_snapshot")
def _browser_snapshot_summary(args: dict) -> str:
    return "browser_snapshot()"


# ---------------------------------------------------------------------------
# browser_click — click by role + name
# ---------------------------------------------------------------------------

@_register("browser_click")
def _browser_click(args: dict, _wg: WriteSafetyGate,
                   _rg: ReadSafetyGate) -> ToolResult:
    """Click an element identified by its accessibility role and name.

    Args:
        role: ARIA role (e.g. 'button', 'link', 'textbox', 'checkbox')
        name: Accessible name (visible text or aria-label)

    Uses the accessibility tree to locate the element, so the page
    should be navigated and snapshotted first.
    """
    role = args.get("role", "")
    name = args.get("name", "")

    if not role:
        return ToolResult(success=False, content="Missing required parameter: 'role'.")
    if not name:
        return ToolResult(success=False, content="Missing required parameter: 'name'.")

    try:
        page = _get_page()
        # Playwright's get_by_role uses the accessibility tree internally
        locator = page.get_by_role(role, name=name)
        locator.click(timeout=10000)
        return ToolResult(
            success=True,
            content=f"Clicked {role} \"{name}\"."
        )
    except Exception as exc:
        return ToolResult(success=False,
                          content=f"Failed to click {role} \"{name}\": {exc}")


@_summarize("browser_click")
def _browser_click_summary(args: dict) -> str:
    role = args.get("role", "?")
    name = args.get("name", "?")
    return f"browser_click({role}, {name[:40]})"


# ---------------------------------------------------------------------------
# browser_type — type text into an input
# ---------------------------------------------------------------------------

@_register("browser_type")
def _browser_type(args: dict, _wg: WriteSafetyGate,
                  _rg: ReadSafetyGate) -> ToolResult:
    """Type text into an input element identified by role and name.

    Args:
        role: ARIA role (typically 'textbox' or 'searchbox')
        name: Accessible name (label text, placeholder, or aria-label)
        text: Text to type
    """
    role = args.get("role", "textbox")
    name = args.get("name", "")
    text = args.get("text", "")

    if not name:
        return ToolResult(success=False, content="Missing required parameter: 'name'.")
    if not text:
        return ToolResult(success=False, content="Missing required parameter: 'text'.")

    try:
        page = _get_page()
        locator = page.get_by_role(role, name=name)
        locator.fill(text, timeout=10000)
        return ToolResult(
            success=True,
            content=f"Typed \"{text}\" into {role} \"{name}\"."
        )
    except Exception as exc:
        return ToolResult(success=False,
                          content=f"Failed to type into {role} \"{name}\": {exc}")


@_summarize("browser_type")
def _browser_type_summary(args: dict) -> str:
    name = args.get("name", "?")
    text = args.get("text", "?")
    return f"browser_type({name[:30]}, {text[:30]})"


# ---------------------------------------------------------------------------
# browser_screenshot — PNG capture
# ---------------------------------------------------------------------------

@_register("browser_screenshot")
def _browser_screenshot(args: dict, wg: WriteSafetyGate,
                        _rg: ReadSafetyGate) -> ToolResult:
    """Capture a full-page PNG screenshot of the current browser page.

    Saves to the workspace so it can be inspected with read_image.
    Default filename: browser_screenshot.png

    Args:
        path: Optional path within workspace (default: browser_screenshot.png)
        full_page: Capture the full scrollable page (default: true)
    """
    path = args.get("path", "browser_screenshot.png")
    full_page = args.get("full_page", True)

    # Safety check — must be within workspace
    if wg is not None:
        safety_result = wg.check(path)
        if not safety_result.allowed:
            return ToolResult(success=False,
                              content=f"Screenshot blocked by safety layer: "
                                      f"{safety_result.reason}")
    else:
        from core.safety import WriteSafetyGate
        safety_result = WriteSafetyGate(os.getcwd()).check(path)
        if not safety_result.allowed:
            return ToolResult(success=False,
                              content=f"Screenshot blocked by safety layer: "
                                      f"{safety_result.reason}")

    resolved = safety_result.resolved_path
    try:
        page = _get_page()
        page.screenshot(path=resolved, full_page=full_page)
        return ToolResult(
            success=True,
            content=f"Screenshot saved to {path} "
                    f"({'full page' if full_page else 'viewport'})."
        )
    except Exception as exc:
        return ToolResult(success=False,
                          content=f"Failed to capture screenshot: {exc}")


@_summarize("browser_screenshot")
def _browser_screenshot_summary(args: dict) -> str:
    path = args.get("path", "browser_screenshot.png")
    return f"browser_screenshot({path})"
