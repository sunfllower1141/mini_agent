#!/usr/bin/env python3
"""
desktop_ops.py — Desktop UI automation via OS accessibility APIs.

Cross-platform desktop interaction for agentic tools:
- macOS:     AXUIElement (Accessibility API) via atomacos or JXA/osascript
- Windows:   UI Automation (COM) via uiautomation library
- Linux:     AT-SPI2 via dogtail or similar

Architecture
------------
Two-tier routing:
  Tier 1 — MCP bridge: connects to a desktop MCP server if configured
           (e.g. macos-use, win32-mcp-server, native-devtools-mcp)
  Tier 2 — Native Python: uses platform bindings directly as fallback

Tools:
    desktop_snapshot  — capture the accessibility tree of the frontmost window
    desktop_click     — click a UI element by role + name
    desktop_type      — type text into a focused field
    desktop_find      — find UI elements matching a text/role query
    desktop_screenshot — capture a screenshot of the current screen (native,
                        no browser required)
"""

from __future__ import annotations

import platform
import subprocess
import time

from core.safety import ReadSafetyGate, WriteSafetyGate
from tools import _register, _summarize, ToolResult

PLATFORM = platform.system()  # "Darwin", "Windows", "Linux"

# ---------------------------------------------------------------------------
# Prime atomacos on the main thread (macOS only).
#
# PyObjC / atomacos require the first Accessibility API call to happen on
# the main thread.  If the first call happens on a background thread (as
# happens when execute_tool wraps every tool in a daemon thread), the call
# hangs indefinitely.  This module-level init makes one lightweight call so
# that all subsequent background-thread calls succeed.
# ---------------------------------------------------------------------------
if PLATFORM == "Darwin":
    try:
        from atomacos import NativeUIElement
        NativeUIElement.getFrontmostApp()
    except Exception:
        pass  # atomacos not installed or accessibility permission not granted


# ---------------------------------------------------------------------------
# Auto-detection of available desktop providers
# ---------------------------------------------------------------------------

def _detect_providers() -> dict[str, bool]:
    """Detect which desktop automation providers are available on this system."""
    available: dict[str, bool] = {}

    if PLATFORM == "Darwin":
        # Tier 1: MCP servers
        available["macos-use"] = _command_exists("macos-use")
        available["npx_native_devtools"] = _command_exists("npx")
        # Tier 2: Native Python
        available["atomacos"] = _module_available("atomacos")
        available["pyobjc"] = _module_available("Quartz")  # pyobjc-framework-Quartz
        # Always available: osascript (AppleScript/JXA)
        available["osascript"] = _command_exists("osascript")

    elif PLATFORM == "Windows":
        available["win32_mcp"] = _module_available("win32_mcp_server")
        available["npx_native_devtools"] = _command_exists("npx")
        available["uiautomation"] = _module_available("uiautomation")
        available["pywinauto"] = _module_available("pywinauto")
        available["powershell"] = _command_exists("powershell")

    elif PLATFORM == "Linux":
        available["npx_native_devtools"] = _command_exists("npx")
        available["dogtail"] = _module_available("dogtail")
        available["pyatspi"] = _module_available("pyatspi")

    return available


def _command_exists(cmd: str) -> bool:
    """Check if a command is on PATH."""
    try:
        result = subprocess.run(
            ["which" if PLATFORM != "Windows" else "where", cmd],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _module_available(name: str) -> bool:
    """Check if a Python module can be imported."""
    try:
        __import__(name)
        return True
    except ImportError:
        return False


# Cache provider detection (run once)
_PROVIDERS: dict[str, bool] | None = None


def _get_providers() -> dict[str, bool]:
    global _PROVIDERS
    if _PROVIDERS is None:
        _PROVIDERS = _detect_providers()
    return _PROVIDERS


def _provider_list() -> str:
    """Return a human-readable list of available/available providers."""
    providers = _get_providers()
    available = [k for k, v in providers.items() if v]
    unavailable = [k for k, v in providers.items() if not v]
    lines = []
    if available:
        lines.append(f"Available: {', '.join(available)}")
    if unavailable:
        lines.append(f"Not installed: {', '.join(unavailable)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Install instructions (shown when nothing is available)
# ---------------------------------------------------------------------------

_INSTALL_INSTRUCTIONS = {
    "Darwin": """
No desktop automation provider found on macOS.

Install one of these (easiest first):

  # Option 1: Native Python (lightweight)
  pip install atomacos

  # Option 2: MCP server (cross-platform, full-featured)
  npm install -g native-devtools-mcp

  # Option 3: Native MCP server (Swift, fastest)
  brew install macos-use

After installing, you must grant Accessibility permission:
  System Settings → Privacy & Security → Accessibility → enable Terminal
""".strip(),

    "Windows": """
No desktop automation provider found on Windows.

Install one of these (easiest first):

  # Option 1: Native Python (recommended)
  pip install uiautomation

  # Option 2: MCP server (cross-platform, full-featured)
  pip install win32-mcp-server

  # Option 3: via Node.js
  npm install -g native-devtools-mcp
""".strip(),

    "Linux": """
No desktop automation provider found on Linux.

Install one of these:

  # Option 1: AT-SPI2
  sudo apt install python3-pyatspi
  pip install pyatspi

  # Option 2: via Node.js
  npm install -g native-devtools-mcp
""".strip(),
}


def _install_instructions() -> str:
    return _INSTALL_INSTRUCTIONS.get(PLATFORM, _INSTALL_INSTRUCTIONS["Linux"])


# ---------------------------------------------------------------------------
# Tier 1: MCP bridge (routes through configured MCP servers)
# ---------------------------------------------------------------------------

def _get_mcp_desktop_server() -> str | None:
    """Find the name of a connected desktop MCP server, or None."""
    try:
        from tools.mcp_client import get_mcp_manager
        manager = get_mcp_manager()
        manager._ensure_started()

        # Look for servers whose name suggests desktop capability
        desktop_keywords = ["desktop", "macos", "win32", "native-devtools", "uia"]
        for name, conn in manager._connections.items():
            if conn.is_connected:
                lower = name.lower()
                if any(kw in lower for kw in desktop_keywords):
                    return name
    except Exception:
        pass
    return None


def _mcp_call(server: str, tool: str, arguments: dict) -> ToolResult:
    """Call a tool on an MCP server. Returns ToolResult."""
    try:
        from tools.mcp_client import get_mcp_manager
        manager = get_mcp_manager()
        return manager.call(server, tool, arguments)
    except Exception as exc:
        return ToolResult(
            success=False,
            content=f"MCP call failed: {exc}",
        )


# ---------------------------------------------------------------------------
# Tier 2: Native Python bindings
# ---------------------------------------------------------------------------

# -- macOS: atomacos (v3 API) -----------------------------------------------

# Per-operation timeout for atomacos calls (seconds).
# Each AX attribute access is a synchronous XPC call that can hang
# if the target app is unresponsive.  We cap the total walk at
# _SNAPSHOT_DEADLINE seconds to avoid the 120 s agent-level timeout.
_ATOMACOS_OP_TIMEOUT = 3.0   # per getattr / children call
_SNAPSHOT_DEADLINE = 25.0    # total wall-clock budget for the snapshot
_MAX_ELEMENTS = 200           # hard cap on collected elements


def _atomacos_getattr(element, attr: str, default=None):
    """``getattr`` with a per-call timeout to prevent hangs."""
    import threading
    result = default
    exc = None
    done = threading.Event()

    def _get():
        nonlocal result, exc
        try:
            result = getattr(element, attr, default)
        except Exception as e:
            exc = e
        finally:
            done.set()

    t = threading.Thread(target=_get, daemon=True)
    t.start()
    if not done.wait(timeout=_ATOMACOS_OP_TIMEOUT):
        # Timed out — abandon thread, return default
        return default
    if exc is not None:
        raise exc
    return result


def _macos_atomacos_snapshot() -> ToolResult:
    """Capture the accessibility tree using atomacos."""
    try:
        from atomacos import NativeUIElement

        deadline = time.monotonic() + _SNAPSHOT_DEADLINE

        # Get the frontmost application
        front_app = NativeUIElement.getFrontmostApp()
        if front_app is None:
            return ToolResult(
                success=False,
                content="No frontmost application found. Is Accessibility permission granted?",
            )

        app_name = str(_atomacos_getattr(front_app, 'AXTitle') or front_app)

        # Get windows
        try:
            windows = front_app.windows()
        except Exception:
            windows = []

        if not windows:
            return ToolResult(success=True, content=_format_app_no_window(front_app))

        main_window = windows[0]
        # Reset the global element counter for this snapshot
        _walk_atomacos_tree._total_elements = []
        elements = _walk_atomacos_tree(main_window, 0, deadline=deadline)

        content = f"Frontmost app: {app_name}\n\n{_format_element_list(elements)}"
        return ToolResult(success=True, content=_truncate(content, 8000))

    except ImportError:
        return ToolResult(
            success=False,
            content="atomacos is not installed. Run: pip install atomacos\n"
                    "Then grant Accessibility permission in System Settings.",
        )
    except Exception as exc:
        return ToolResult(success=False, content=f"atomacos snapshot failed: {exc}")


def _walk_atomacos_tree(element, depth: int = 0, max_depth: int = 4,
                        deadline: float | None = None) -> list[dict]:
    """Walk the accessibility tree, collecting interactive elements.

    *deadline* is a ``time.monotonic()`` timestamp after which we stop
    recursing (prevents hanging on unresponsive apps).
    """
    elements: list[dict] = []

    # Bail out early: depth limit, deadline expired, or element cap
    if depth > max_depth:
        return elements
    if deadline is not None and time.monotonic() > deadline:
        return elements
    if len(_walk_atomacos_tree._total_elements) >= _MAX_ELEMENTS:
        return elements

    try:
        role = str(_atomacos_getattr(element, 'AXRole', 'unknown'))
    except Exception:
        role = 'unknown'

    try:
        name = str(_atomacos_getattr(element, 'AXTitle', '') or
                   _atomacos_getattr(element, 'AXDescription', '') or
                   _atomacos_getattr(element, 'AXValue', '') or '')
    except Exception:
        name = ''

    # Skip non-interactive roles
    skip_roles = {'AXGroup', 'AXLayoutArea', 'AXLayoutItem', 'AXUnknown'}
    if role not in skip_roles:
        elements.append({
            'role': role,
            'name': name[:100],
            'depth': depth,
        })

    # Track total elements collected across recursive calls
    try:
        total_list = _walk_atomacos_tree._total_elements
    except AttributeError:
        total_list = []
        _walk_atomacos_tree._total_elements = total_list
    total_list.extend(elements)

    if len(total_list) >= _MAX_ELEMENTS:
        return elements

    try:
        children = _atomacos_getattr(element, 'AXChildren', None)
        if children and isinstance(children, list):
            for child in children:
                if deadline is not None and time.monotonic() > deadline:
                    break
                if len(total_list) >= _MAX_ELEMENTS:
                    break
                elements.extend(
                    _walk_atomacos_tree(child, depth + 1, max_depth, deadline)
                )
    except Exception:
        pass

    return elements


def _format_app_no_window(app) -> str:
    """Format output when an app has no windows (e.g. menu bar only)."""
    app_name = str(getattr(app, 'AXTitle', app) or app)
    try:
        menu_items = []
        menu_bar = getattr(app, 'AXMenuBar', None)
        if menu_bar and isinstance(menu_bar, list):
            for item in menu_bar[:30]:
                try:
                    menu_items.append(str(getattr(item, 'AXTitle', item)))
                except Exception:
                    pass
        if menu_items:
            return f"Frontmost app: {app_name}\nMenu bar items: {', '.join(menu_items)}"
    except Exception:
        pass
    return f"Frontmost app: {app_name} (no windows open)"


def _format_element_list(elements: list[dict]) -> str:
    """Format an element list as human-readable text."""
    if not elements:
        return "(no interactive elements found)"

    lines = []
    for i, el in enumerate(elements):
        indent = "  " * el.get('depth', 0)
        role = el.get('role', '?')
        name = el.get('name', '')
        if name:
            lines.append(f"{indent}[{i}] {role}: \"{name}\"")
        else:
            lines.append(f"{indent}[{i}] {role}")
    return "\n".join(lines)


def _macos_atomacos_click(role: str, name: str) -> ToolResult:
    """Click an element by role and name using atomacos."""
    try:
        from atomacos import NativeUIElement
        import atomacos.mouse as mouse

        deadline = time.monotonic() + _SNAPSHOT_DEADLINE

        front_app = NativeUIElement.getFrontmostApp()
        if front_app is None:
            return ToolResult(success=False, content="No frontmost application found.")

        # Use recursive find to locate the element
        element = _find_atomacos_element(front_app, role, name, deadline=deadline)
        if element is None:
            # Also search each window
            try:
                for window in front_app.windows():
                    if time.monotonic() > deadline:
                        break
                    element = _find_atomacos_element(window, role, name, deadline=deadline)
                    if element:
                        break
            except Exception:
                pass

        if element is None:
            return ToolResult(
                success=False,
                content=f"No {role} named \"{name}\" found in frontmost app.",
                hint="Try desktop_snapshot first to see available elements.",
            )

        # Click the element at its center
        try:
            pos = _atomacos_getattr(element, 'AXPosition')
            size = _atomacos_getattr(element, 'AXSize')
            center_x = pos[0] + size[0] / 2
            center_y = pos[1] + size[1] / 2
            mouse.click(center_x, center_y)
            app_name = str(_atomacos_getattr(front_app, 'AXTitle') or front_app)
            return ToolResult(
                success=True,
                content=f"Clicked {role} \"{name}\" in {app_name}.",
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                content=f"Found element but click failed: {exc}",
            )

    except ImportError:
        return ToolResult(
            success=False,
            content="atomacos is not installed. Run: pip install atomacos",
        )
    except Exception as exc:
        return ToolResult(success=False, content=f"atomacos click failed: {exc}")


def _find_atomacos_element(element, role: str, name: str, max_depth: int = 8,
                           deadline: float | None = None):
    """Recursively find an element by role and name.

    *deadline* is a ``time.monotonic()`` timestamp after which we stop
    searching (prevents hanging on unresponsive apps).
    """
    if deadline is not None and time.monotonic() > deadline:
        return None
    if max_depth <= 0:
        return None

    try:
        el_role = str(_atomacos_getattr(element, 'AXRole', ''))
    except Exception:
        return None

    try:
        el_name = str(_atomacos_getattr(element, 'AXTitle', '') or
                      _atomacos_getattr(element, 'AXDescription', '') or '')
    except Exception:
        el_name = ''

    if el_role.lower() == role.lower() and name.lower() in el_name.lower():
        return element

    try:
        children = _atomacos_getattr(element, 'AXChildren', None)
        if children and isinstance(children, list):
            for child in children:
                if deadline is not None and time.monotonic() > deadline:
                    return None
                result = _find_atomacos_element(child, role, name, max_depth - 1, deadline)
                if result is not None:
                    return result
    except Exception:
        pass

    return None


def _macos_atomacos_type(text: str) -> ToolResult:
    """Type text into the focused element using atomacos."""
    try:
        import atomacos.keyboard as keyboard

        # Use typewrite for text input
        keyboard.typewrite(text)
        return ToolResult(success=True, content="Typed text into focused element.")

    except ImportError:
        return ToolResult(
            success=False,
            content="atomacos is not installed. Run: pip install atomacos",
        )
    except Exception:
        # Fall back to CGEvent
        return _macos_cgevent_type(text)


# -- macOS: CGEvent (keyboard injection) -----------------------------------

def _macos_cgevent_type(text: str) -> ToolResult:
    """Type text via Core Graphics events (requires pyobjc-framework-Quartz)."""
    try:
        from Quartz import (
            CGEventCreateKeyboardEvent,
            CGEventPost,
            kCGHIDEventTap,
            kCGEventKeyDown,
        )
        import time

        for char in text:
            # CGEventCreateKeyboardEvent takes a keycode, not a character.
            # Mapping characters to keycodes is complex; use osascript as fallback.
            pass
        # Fall through to osascript
    except ImportError:
        pass

    return _macos_osascript_type(text)


# -- macOS: osascript (always available) -----------------------------------

def _macos_osascript_type(text: str) -> ToolResult:
    """Type text via AppleScript keystroke (always works on macOS)."""
    try:
        escaped = text.replace('\\', '\\\\').replace('"', '\\"')
        subprocess.run(
            ["osascript", "-e", f'tell application "System Events" to keystroke "{escaped}"'],
            capture_output=True, text=True, timeout=5,
        )
        return ToolResult(success=True, content="Typed text via System Events.")
    except Exception as exc:
        return ToolResult(success=False, content=f"osascript keystroke failed: {exc}")


# -- Windows: uiautomation -------------------------------------------------

def _win_uia_snapshot() -> ToolResult:
    """Capture the UI tree using Windows UI Automation."""
    try:
        import uiautomation as auto

        # Get the foreground window
        window = auto.GetForegroundControl()
        if window is None:
            return ToolResult(success=False, content="No foreground window found.")

        elements = _walk_uia_tree(window)
        title = window.Name or "(unnamed)"
        content = f"Foreground window: {title}\n\n{_format_element_list(elements)}"
        return ToolResult(success=True, content=_truncate(content, 8000))

    except ImportError:
        return ToolResult(
            success=False,
            content="uiautomation is not installed. Run: pip install uiautomation",
        )
    except Exception as exc:
        return ToolResult(success=False, content=f"UIA snapshot failed: {exc}")


def _walk_uia_tree(element, depth: int = 0, max_depth: int = 5) -> list[dict]:
    """Walk the Windows UI Automation tree."""
    elements: list[dict] = []
    if depth > max_depth or element is None:
        return elements

    try:
        control_type = str(element.ControlTypeName or 'unknown')
        name = str(element.Name or '')
    except Exception:
        control_type = 'unknown'
        name = ''

    # Skip generic container types
    skip_types = {'GroupControl', 'PaneControl', 'WindowControl'}
    if control_type not in skip_types or depth <= 1:
        elements.append({
            'role': control_type,
            'name': name[:100],
            'depth': depth,
        })

    try:
        children = element.GetChildren()
        for child in children:
            elements.extend(_walk_uia_tree(child, depth + 1, max_depth))
    except Exception:
        pass

    return elements


def _win_uia_click(role: str, name: str) -> ToolResult:
    """Click an element by role and name using Windows UI Automation."""
    try:
        import uiautomation as auto

        window = auto.GetForegroundControl()
        if window is None:
            return ToolResult(success=False, content="No foreground window found.")

        element = _find_uia_element(window, role, name)
        if element is None:
            return ToolResult(
                success=False,
                content=f"No {role} named \"{name}\" found in foreground window.",
                hint="Try desktop_snapshot first to see available elements.",
            )

        element.Click()
        return ToolResult(success=True, content=f"Clicked {role} \"{name}\".")

    except ImportError:
        return ToolResult(
            success=False,
            content="uiautomation is not installed. Run: pip install uiautomation",
        )
    except Exception as exc:
        return ToolResult(success=False, content=f"UIA click failed: {exc}")


def _find_uia_element(element, role: str, name: str, max_depth: int = 6):
    """Recursively find a UIA element by control type and name."""
    try:
        el_type = str(element.ControlTypeName or '')
        el_name = str(element.Name or '')
    except Exception:
        return None

    if role.lower() in el_type.lower() and name.lower() in el_name.lower():
        return element

    if max_depth <= 0:
        return None

    try:
        for child in element.GetChildren():
            result = _find_uia_element(child, role, name, max_depth - 1)
            if result is not None:
                return result
    except Exception:
        pass

    return None


def _win_uia_type(text: str) -> ToolResult:
    """Type text into the focused element using Windows UI Automation."""
    try:
        import uiautomation as auto

        focused = auto.GetFocusedControl()
        if focused is None:
            return ToolResult(
                success=False,
                content="No focused element. Click into a text field first.",
            )

        # Try setting the value pattern
        value_pattern = focused.GetValuePattern()
        if value_pattern:
            value_pattern.SetValue(text)
            return ToolResult(success=True, content="Typed text into focused element.")

        # Fallback: SendKeys
        focused.SendKeys(text)
        return ToolResult(success=True, content="Typed text via SendKeys.")

    except ImportError:
        return ToolResult(
            success=False,
            content="uiautomation is not installed. Run: pip install uiautomation",
        )
    except Exception as exc:
        return ToolResult(success=False, content=f"UIA type failed: {exc}")


# -- Screenshot (cross-platform via mss or platform-native) ----------------

def _native_screenshot() -> ToolResult:
    """Take a screenshot of the current screen using mss (lightweight)."""
    try:
        import mss
        import mss.tools
        from pathlib import Path
        import tempfile

        with mss.MSS() as sct:
            monitor = sct.monitors[1]  # primary monitor
            screenshot = sct.grab(monitor)
            # Save to a temp file
            out_dir = Path(tempfile.gettempdir()) / "mini_agent_screenshots"
            out_dir.mkdir(exist_ok=True)
            out_path = out_dir / f"screenshot_{_timestamp()}.png"
            mss.tools.to_png(screenshot.rgb, screenshot.size, output=str(out_path))

        return ToolResult(
            success=True,
            content=f"Screenshot saved to: {out_path}\n"
                    f"Resolution: {screenshot.width}x{screenshot.height}\n"
                    f"Use read_image to view this screenshot.",
        )

    except ImportError:
        return ToolResult(
            success=False,
            content="mss is not installed. Run: pip install mss\n"
                    "mss is a lightweight, cross-platform screenshot library.",
        )
    except Exception as exc:
        return ToolResult(success=False, content=f"Screenshot failed: {exc}")


def _timestamp() -> str:
    """Return a compact timestamp for filenames."""
    import time
    return time.strftime("%Y%m%d_%H%M%S")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"\n… (truncated, {len(text)} chars total)"


# ---------------------------------------------------------------------------
# Main dispatch: choose provider and execute
# ---------------------------------------------------------------------------

def _get_best_provider() -> str | None:
    """Return the name of the best available provider."""
    providers = _get_providers()

    # Tier 1: MCP servers (best UX, most capable)
    mcp_server = _get_mcp_desktop_server()
    if mcp_server:
        return "mcp"

    # Tier 2: Native Python bindings
    if PLATFORM == "Darwin":
        if providers.get("atomacos"):
            return "atomacos"
        if providers.get("pyobjc"):
            return "pyobjc"

    elif PLATFORM == "Windows":
        if providers.get("uiautomation"):
            return "uiautomation"
        if providers.get("pywinauto"):
            return "pywinauto"

    # Tier 3: Always-available fallbacks
    if PLATFORM == "Darwin" and providers.get("osascript"):
        return "osascript"  # limited but always available

    return None


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

@_register("desktop_snapshot")
def _desktop_snapshot(args: dict, _wg: WriteSafetyGate,
                      _rg: ReadSafetyGate) -> ToolResult:
    """Capture the accessibility tree of the frontmost window.

    Returns a structured text representation of interactive elements
    (roles, names, states) — much more compact and LLM-friendly than
    a screenshot. Use this to understand what's on screen before
    clicking or typing.

    On macOS: requires Accessibility permission (System Settings →
    Privacy & Security → Accessibility → enable Terminal).

    On Windows: works out of the box with uiautomation installed.
    """
    provider = _get_best_provider()

    if provider == "mcp":
        server = _get_mcp_desktop_server()
        if server:
            # Try common MCP tool names
            for tool_name in ("get_accessibility_tree", "snapshot", "get_ui_tree",
                              "capture_snapshot", "get_snapshot"):
                result = _mcp_call(server, tool_name, {})
                if result.success:
                    return result
            return ToolResult(
                success=False,
                content=f"MCP server '{server}' connected but no snapshot tool found. "
                        f"Use mcp_discover to see available tools.",
            )

    if provider == "atomacos":
        return _macos_atomacos_snapshot()

    if provider == "uiautomation":
        return _win_uia_snapshot()

    # No provider available
    providers_text = _provider_list()
    return ToolResult(
        success=False,
        content=f"No desktop automation provider available.\n\n{providers_text}\n\n{_install_instructions()}",
    )


@_register("desktop_click")
def _desktop_click(args: dict, _wg: WriteSafetyGate,
                   _rg: ReadSafetyGate) -> ToolResult:
    """Click a UI element identified by its role and name.

    Args:
        role: element role (e.g. 'button', 'textfield', 'checkbox',
              'menuItem', 'tab', 'link')
        name: accessible name (visible text or label)

    Use desktop_snapshot first to see available elements.
    On macOS: requires Accessibility permission.
    """
    role = args.get("role", "")
    name = args.get("name", "")

    if not role:
        return ToolResult(success=False, content="Missing required parameter: 'role'.")
    if not name:
        return ToolResult(success=False, content="Missing required parameter: 'name'.")

    provider = _get_best_provider()

    if provider == "mcp":
        server = _get_mcp_desktop_server()
        if server:
            for tool_name in ("click", "click_element", "press", "select"):
                result = _mcp_call(server, tool_name, {"role": role, "name": name})
                if result.success:
                    return result
            return ToolResult(
                success=False,
                content=f"MCP server '{server}' connected but no click tool found.",
            )

    if provider == "atomacos":
        return _macos_atomacos_click(role, name)

    if provider == "uiautomation":
        return _win_uia_click(role, name)

    return ToolResult(
        success=False,
        content=f"No desktop automation provider available.\n\n{_provider_list()}\n\n{_install_instructions()}",
    )


@_register("desktop_type")
def _desktop_type(args: dict, _wg: WriteSafetyGate,
                  _rg: ReadSafetyGate) -> ToolResult:
    """Type text into the currently focused input field.

    Args:
        text: text to type

    Click into the target field first (using desktop_click or manually),
    then call this to type.
    """
    text = args.get("text", "")

    if not text:
        return ToolResult(success=False, content="Missing required parameter: 'text'.")

    provider = _get_best_provider()

    if provider == "mcp":
        server = _get_mcp_desktop_server()
        if server:
            for tool_name in ("type", "type_text", "insert_text", "send_keys"):
                result = _mcp_call(server, tool_name, {"text": text})
                if result.success:
                    return result
            return ToolResult(
                success=False,
                content=f"MCP server '{server}' connected but no type tool found.",
            )

    if provider == "atomacos":
        return _macos_atomacos_type(text)

    if provider == "uiautomation":
        return _win_uia_type(text)

    if provider == "osascript":
        return _macos_osascript_type(text)

    return ToolResult(
        success=False,
        content=f"No desktop automation provider available.\n\n{_provider_list()}\n\n{_install_instructions()}",
    )


@_register("desktop_find")
def _desktop_find(args: dict, _wg: WriteSafetyGate,
                  _rg: ReadSafetyGate) -> ToolResult:
    """Find UI elements matching a text or role query.

    Useful for locating elements across all open windows.
    Args:
        query: text to search for in element names/labels
        role: optional role filter (e.g. 'button', 'window', 'menu')
    """
    query = args.get("query", "")
    role_filter = args.get("role", "")

    if not query:
        return ToolResult(success=False, content="Missing required parameter: 'query'.")

    provider = _get_best_provider()

    if provider == "mcp":
        server = _get_mcp_desktop_server()
        if server:
            for tool_name in ("find", "find_element", "search_ui", "query_elements"):
                result = _mcp_call(
                    server, tool_name,
                    {"query": query, "role": role_filter},
                )
                if result.success:
                    return result
            return ToolResult(
                success=False,
                content=f"MCP server '{server}' connected but no find tool found.",
            )

    # Native find: re-use snapshot and filter
    if provider in ("atomacos", "uiautomation"):
        snapshot_result = (
            _macos_atomacos_snapshot() if provider == "atomacos"
            else _win_uia_snapshot()
        )
        if not snapshot_result.success:
            return snapshot_result

        # Filter snapshot results client-side
        lines = snapshot_result.content.split("\n")
        matched = []
        query_lower = query.lower()
        for line in lines:
            if query_lower in line.lower():
                if role_filter and role_filter.lower() not in line.lower():
                    continue
                matched.append(line)

        if not matched:
            return ToolResult(
                success=True,
                content=f"No elements matching \"{query}\" found. "
                        f"Try desktop_snapshot to see all elements.",
            )
        return ToolResult(
            success=True,
            content=f"Found {len(matched)} matching elements:\n" + "\n".join(matched[:50]),
        )

    if provider == "osascript":
        # Limited: can only search via AppleScript System Events
        return ToolResult(
            success=False,
            content="desktop_find requires atomacos (pip install atomacos) or an MCP server. "
                    "The osascript fallback does not support search.",
        )

    return ToolResult(
        success=False,
        content=f"No desktop automation provider available.\n\n{_provider_list()}\n\n{_install_instructions()}",
    )


@_register("desktop_screenshot")
def _desktop_screenshot(args: dict, _wg: WriteSafetyGate,
                        _rg: ReadSafetyGate) -> ToolResult:
    """Capture a screenshot of the current screen (not browser).

    Unlike browser_screenshot, this captures the native desktop —
    any open application, the menubar, dock, taskbar, etc.

    Saves to a temp directory. Use read_image to view it,
    or use an image-capable model to analyze it.

    Requires: pip install mss (lightweight, no native deps)
    """
    return _native_screenshot()


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------

@_summarize("desktop_snapshot")
def _desktop_snapshot_summary(args: dict) -> str:
    return "desktop_snapshot()"


@_summarize("desktop_click")
def _desktop_click_summary(args: dict) -> str:
    role = args.get("role", "?")
    name = args.get("name", "?")
    return f"desktop_click({role}, {name[:30]})"


@_summarize("desktop_type")
def _desktop_type_summary(args: dict) -> str:
    text = args.get("text", "")
    return f"desktop_type({text[:30]}...)"


@_summarize("desktop_find")
def _desktop_find_summary(args: dict) -> str:
    query = args.get("query", "?")
    return f"desktop_find({query[:30]})"


@_summarize("desktop_screenshot")
def _desktop_screenshot_summary(args: dict) -> str:
    return "desktop_screenshot()"
