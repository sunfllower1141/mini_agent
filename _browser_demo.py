"""Demo: browser tools in action."""
from tools import _TOOL_DISPATCH

# ── open_url ────────────────────────────────────────────
# Uses webbrowser stdlib — no Playwright needed.
# Validates URL scheme and opens user's browser.
print("=== open_url (stdlib, no Playwright needed) ===")

r = _TOOL_DISPATCH['open_url']({}, None, None)
print(f"  Missing url:  success={r.success}")

r = _TOOL_DISPATCH['open_url']({'url': 'ftp://example.com'}, None, None)
print(f"  Bad scheme:   success={r.success}, content={r.content}")

# Valid call opens browser — not doing that here, just show the shape
print("  Valid call:   open_url({url: 'https://docs.python.org/3/'})")
print("    → opens system browser, returns immediately")
print()

# ── browser_navigate (needs Playwright) ─────────────────
print("=== browser_navigate (needs Playwright) ===")
r = _TOOL_DISPATCH['browser_navigate']({'url': 'https://example.com'}, None, None)
print(f"  success={r.success}, content={r.content}")
print()

# ── browser_snapshot (needs Playwright) ─────────────────
print("=== browser_snapshot (needs Playwright) ===")
r = _TOOL_DISPATCH['browser_snapshot']({}, None, None)
print(f"  success={r.success}, content={r.content}")
print()

# ── browser_click (needs Playwright) ────────────────────
print("=== browser_click (needs Playwright) ===")
r = _TOOL_DISPATCH['browser_click']({'role': 'button', 'name': 'Login'}, None, None)
print(f"  click button 'Login': success={r.success}, content={r.content}")
print()

# ── browser_type (needs Playwright) ────────────────────
print("=== browser_type (needs Playwright) ===")
r = _TOOL_DISPATCH['browser_type']({'name': 'Search', 'text': 'hello world'}, None, None)
print(f"  type into 'Search': success={r.success}, content={r.content}")
print()

# ── browser_screenshot (needs Playwright) ──────────────
print("=== browser_screenshot (needs Playwright) ===")
r = _TOOL_DISPATCH['browser_screenshot']({'path': 'page.png'}, None, None)
print(f"  screenshot: success={r.success}, content={r.content}")
