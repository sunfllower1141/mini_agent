#!/usr/bin/env python3
"""
macos_ops.py -- Intensive macOS API integrations for the desktop agent.

Tools:
    desktop_apps        -- List running applications (name, PID, bundle ID, active)
    desktop_launch      -- Launch an app by name or bundle ID
    desktop_quit        -- Quit an app by name or PID
    desktop_focus       -- Bring an app window to the foreground
    desktop_clipboard   -- Read or write the system clipboard
    desktop_windows     -- List all visible windows across applications
    desktop_system_info -- CPU, memory, disk, battery, thermal, uptime
    desktop_key         -- Press a key combination (e.g. "cmd+c", "cmd+tab")
    desktop_open        -- Open a file, folder, or URL in the default app
    desktop_reveal      -- Reveal a file in Finder
    desktop_notify      -- Post a system notification

All tools degrade gracefully on non-macOS platforms with clear messages.
Most use pyobjc (AppKit / Quartz / Foundation) which is bundled with
the macOS Python framework.  A few fall back to subprocess calls
(osascript, open, mdfind, pmset) for maximum compatibility.

Safety gate note: the registered tool wrappers accept WriteSafetyGate
and ReadSafetyGate but do not use them.  These tools operate on the
host desktop (clipboard, keyboard simulation, app lifecycle) which is
inherently outside the workspace sandbox.  The agent is trusted to use
them responsibly -- they are gated behind the 'desktop' skill group.
"""

from __future__ import annotations

import os
import platform
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from core.safety import ReadSafetyGate, WriteSafetyGate
from tools import _register, _summarize, ToolResult

PLATFORM = platform.system()

# -- Lazy pyobjc imports (module-level, imported once on first use) --
_AppKit_NSWorkspace = None


def _get_nsworkspace():
    """Lazy-load NSWorkspace (cached after first call)."""
    global _AppKit_NSWorkspace
    if _AppKit_NSWorkspace is None:
        try:
            from AppKit import NSWorkspace as _AppKit_NSWorkspace
        except ImportError:
            _AppKit_NSWorkspace = False  # sentinel: not available
    if _AppKit_NSWorkspace is False:
        raise ImportError("AppKit.NSWorkspace not available")
    return _AppKit_NSWorkspace


_Quartz_module = None


def _get_quartz():
    """Lazy-load Quartz module (cached after first call)."""
    global _Quartz_module
    if _Quartz_module is None:
        try:
            import Quartz as _Quartz_module
        except ImportError:
            _Quartz_module = False
    if _Quartz_module is False:
        raise ImportError("Quartz not available")
    return _Quartz_module


_Foundation_NSProcessInfo = None


def _get_nsprocessinfo():
    """Lazy-load NSProcessInfo (cached after first call)."""
    global _Foundation_NSProcessInfo
    if _Foundation_NSProcessInfo is None:
        try:
            from Foundation import NSProcessInfo as _Foundation_NSProcessInfo
        except ImportError:
            _Foundation_NSProcessInfo = False
    if _Foundation_NSProcessInfo is False:
        raise ImportError("Foundation.NSProcessInfo not available")
    return _Foundation_NSProcessInfo


# -- Robust AppleScript string escaping --
def _escape_applescript_string(s: str) -> str:
    """Escape a string for embedding in an AppleScript double-quoted string literal.

    Handles backslashes, double quotes, newlines, carriage returns, tabs,
    and other control characters that would break the script.
    Uses character-code comparisons to avoid backslash-encoding confusion.
    """
    result: list[str] = []
    for ch in s:
        oc = ord(ch)
        if oc == 92:          # backslash
            result.append(chr(92) + chr(92))
        elif oc == 34:        # double quote
            result.append(chr(92) + chr(34))
        elif oc == 10:        # newline
            result.append(chr(92) + "n")
        elif oc == 13:        # carriage return
            result.append(chr(92) + "r")
        elif oc == 9:         # tab
            result.append(chr(92) + "t")
        elif oc < 0x20:
            result.append(chr(92) + "x" + format(oc, "02x"))
        else:
            result.append(ch)
    return "".join(result)


# ===========================================================================
# Helpers
# ===========================================================================

def _mac_only() -> ToolResult:
    """Return a standard error when called on non-macOS."""
    return ToolResult(
        success=False,
        content=f"desktop_* intensive tools are macOS-only. Current platform: {PLATFORM}.",
    )


def _run_osascript(script: str, timeout: float = 10.0) -> tuple[bool, str]:
    """Run an AppleScript snippet. Returns (ok, output_stripped)."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            return False, result.stderr.strip() or result.stdout.strip()
        return True, result.stdout.strip()
    except FileNotFoundError:
        return False, "osascript not found"
    except subprocess.TimeoutExpired:
        return False, "osascript timed out"
    except Exception as exc:
        return False, str(exc)


def _run_command(cmd: list[str], timeout: float = 10.0) -> tuple[bool, str, str]:
    """Run a command, return (ok, stdout, stderr)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except FileNotFoundError:
        return False, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return False, "", "timed out"
    except Exception as exc:
        return False, "", str(exc)


# ===========================================================================
# desktop_apps -- list running applications
# ===========================================================================

def _macos_list_apps() -> ToolResult:
    """List running applications via NSWorkspace."""
    try:
        NSWorkspace = _get_nsworkspace()
        ws = NSWorkspace.sharedWorkspace()
        apps = ws.runningApplications()
        lines = []
        for app in apps:
            name = app.localizedName() or "(unnamed)"
            pid = app.processIdentifier()
            bundle = app.bundleIdentifier() or "--"
            active = "?" if app.isActive() else " "
            lines.append(f"  {active} PID={pid:6d}  {name[:40]:40s}  {bundle}")

        return ToolResult(
            success=True,
            content=f"Running applications ({len(apps)} total):\n" + "\n".join(sorted(lines)),
        )
    except ImportError:
        return _fallback_apps_via_ps()
    except Exception as exc:
        return ToolResult(success=False, content=f"Failed to list apps: {exc}")


def _fallback_apps_via_ps() -> ToolResult:
    """Fallback: use 'ps' to list apps (no pyobjc needed)."""
    ok, stdout, stderr = _run_command(
        ["ps", "-eo", "pid,comm", "-c"],
        timeout=5.0,
    )
    if not ok:
        return ToolResult(success=False, content=f"ps failed: {stderr}")
    lines = stdout.splitlines()
    return ToolResult(
        success=True,
        content=f"Running processes ({len(lines)-1} total):\n" + "\n".join(f"  {l}" for l in lines[:60]),
    )


# ===========================================================================
# desktop_launch -- launch an application
# ===========================================================================

def _macos_launch_app(name: str) -> ToolResult:
    """Launch an app by name or bundle ID."""
    try:
        # Try launching by name using `open -a`
        ok, stdout, stderr = _run_command(
            ["open", "-a", name],
            timeout=15.0,
        )
        if ok:
            return ToolResult(
                success=True,
                content=f"Launched '{name}'.",
            )
        # If name-based fails, try bundle ID
        if "." in name:
            ok2, _, stderr2 = _run_command(
                ["open", "-b", name],
                timeout=15.0,
            )
            if ok2:
                return ToolResult(
                    success=True,
                    content=f"Launched bundle '{name}'.",
                )
            return ToolResult(
                success=False,
                content=f"Failed to launch '{name}': {stderr2 or stderr}",
                hint="Check the app name or bundle ID, or try the full path.",
            )
        return ToolResult(
            success=False,
            content=f"Failed to launch '{name}': {stderr}",
            hint="Make sure the app name is correct. Use 'open -a \"App Name\"' syntax.",
        )
    except Exception as exc:
        return ToolResult(success=False, content=f"Launch failed: {exc}")


# ===========================================================================
# desktop_quit -- quit an application
# ===========================================================================

def _macos_quit_app(name_or_pid: str) -> ToolResult:
    """Quit an app by name or PID.

    Checks if the app is running first (via NSWorkspace) to avoid
    cascading timeouts when the app isn't even launched.
    """
    # Quick check: is the app running?
    # If name_or_pid is numeric, treat as PID and check via ps.
    # Otherwise look up via NSWorkspace.
    app_is_running = False
    try:
        NSWorkspace = _get_nsworkspace()
        ws = NSWorkspace.sharedWorkspace()
        for app in ws.runningApplications():
            if (name_or_pid.isdigit() and str(app.processIdentifier()) == name_or_pid) or \
               (app.localizedName() or "").lower() == name_or_pid.lower() or \
               (app.bundleIdentifier() or "").lower() == name_or_pid.lower():
                app_is_running = True
                break
    except ImportError:
        # Fallback: assume running, let osascript try
        app_is_running = True

    if not app_is_running:
        return ToolResult(
            success=False,
            content=f"App '{name_or_pid}' is not running. No action taken.",
            hint="Use desktop_apps to see running apps. Nothing to quit.",
        )

    # Gentle quit via osascript
    ok, output = _run_osascript(
        f'tell application "{_escape_applescript_string(name_or_pid)}" to quit',
        timeout=10.0,
    )
    if ok:
        return ToolResult(success=True, content=f"Quit '{name_or_pid}'.")

    # Force quit via pkill (exact name match only -- no -f)
    ok2, stdout2, stderr2 = _run_command(
        ["pkill", "-x", name_or_pid],
        timeout=5.0,
    )
    if ok2:
        return ToolResult(success=True, content=f"Force-quit '{name_or_pid}' via pkill.")

    return ToolResult(
        success=False,
        content=f"Could not quit '{name_or_pid}'. App not responding.",
        hint="The app may be frozen. Try Force Quit (???) or 'kill -9' on its PID.",
    )


# ===========================================================================
# desktop_focus -- bring an app to the foreground
# ===========================================================================

def _macos_focus_app(name: str) -> ToolResult:
    """Bring an app to the foreground."""
    # Use osascript to activate the app
    ok, output = _run_osascript(
        f'tell application "{name}" to activate',
        timeout=10.0,
    )
    if ok:
        return ToolResult(success=True, content=f"Activated '{name}'.")

    # Try via open -a (this also activates)
    ok2, stdout2, stderr2 = _run_command(
        ["open", "-a", name],
        timeout=10.0,
    )
    if ok2:
        return ToolResult(success=True, content=f"Opened and activated '{name}'.")

    return ToolResult(
        success=False,
        content=f"Could not activate '{name}'. Is the app running?",
        hint="Use desktop_apps to see running apps. Try desktop_launch if it's not running.",
    )


# ===========================================================================
# desktop_clipboard -- read or write the system clipboard
# ===========================================================================

def _macos_clipboard(action: str, text: str = "") -> ToolResult:
    """Read or write the system clipboard."""
    try:
        import pyperclip
    except ImportError:
        # Fallback to osascript
        return _clipboard_via_osascript(action, text)

    try:
        if action == "read":
            content = pyperclip.paste()
            if content is None:
                content = ""
            return ToolResult(
                success=True,
                content=content if content else "(clipboard empty)",
            )
        elif action == "write":
            if not text:
                return ToolResult(
                    success=False,
                    content="Missing 'text' parameter for clipboard write.",
                )
            pyperclip.copy(text)
            return ToolResult(
                success=True,
                content=f"Clipboard set ({len(text)} chars).",
            )
        else:
            return ToolResult(
                success=False,
                content=f"Unknown action: '{action}'. Use 'read' or 'write'.",
            )
    except Exception:
        return _clipboard_via_osascript(action, text)


def _clipboard_via_osascript(action: str, text: str = "") -> ToolResult:
    """Clipboard fallback via osascript."""
    if action == "read":
        ok, output = _run_osascript("get the clipboard", timeout=5.0)
        if ok:
            return ToolResult(success=True, content=output if output else "(clipboard empty)")
        return ToolResult(success=False, content=f"Clipboard read failed: {output}")

    elif action == "write":
        if not text:
            return ToolResult(success=False, content="Missing 'text' parameter for clipboard write.")
        escaped = _escape_applescript_string(text)
        ok, output = _run_osascript(
            f'set the clipboard to "{escaped}"',
            timeout=5.0,
        )
        if ok:
            return ToolResult(success=True, content=f"Clipboard set ({len(text)} chars).")
        return ToolResult(success=False, content=f"Clipboard write failed: {output}")

    return ToolResult(success=False, content=f"Unknown action: '{action}'.")


# ===========================================================================
# desktop_windows -- list all visible windows across all apps
# ===========================================================================

def _macos_list_windows() -> ToolResult:
    """List all visible windows via CGWindowList."""
    try:
        Quartz = _get_quartz()
        option = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        window_list = Quartz.CGWindowListCopyWindowInfo(option, Quartz.kCGNullWindowID)

        # Filter to regular app windows (layer 0 = normal, skip menubar/dock/etc)
        app_windows = []
        for w in window_list:
            layer = w.get("kCGWindowLayer", 99)
            if layer != 0:
                continue
            name = w.get("kCGWindowName", "") or "(unnamed)"
            owner = w.get("kCGWindowOwnerName", "") or "?"
            pid = w.get("kCGWindowOwnerPID", 0)
            bounds = w.get("kCGWindowBounds", {})
            x, y = int(bounds.get("X", 0)), int(bounds.get("Y", 0))
            w_, h_ = int(bounds.get("Width", 0)), int(bounds.get("Height", 0))
            app_windows.append(
                f'  [{owner}] "{name[:60]}"  {w_}x{h_} @ ({x},{y})  pid={pid}'
            )

        if not app_windows:
            return ToolResult(success=True, content="No visible app windows found.")

        return ToolResult(
            success=True,
            content=f"Visible windows ({len(app_windows)}):\n" + "\n".join(app_windows),
        )

    except ImportError:
        # Fallback: use osascript System Events
        ok, output = _run_osascript(
            'tell application "System Events" to get name of every window of every process whose visible is true',
            timeout=10.0,
        )
        if ok:
            return ToolResult(success=True, content=f"Windows: {output}")
        return ToolResult(success=False, content="pyobjc-framework-Quartz not installed and osascript fallback failed.")
    except Exception as exc:
        return ToolResult(success=False, content=f"Window list failed: {exc}")


# ===========================================================================
# desktop_system_info -- CPU, memory, disk, battery, thermal, uptime
# ===========================================================================

def _macos_system_info() -> ToolResult:
    """Gather system metrics from multiple sources.

    Runs independent subprocess calls in parallel via a thread pool
    to minimize total wall-clock time.
    """
    lines: list[str] = []

    # CPU / memory / uptime (Foundation)
    try:
        NSProcessInfo = _get_nsprocessinfo()
        pi = NSProcessInfo.processInfo()
        lines.append(f"Hostname:         {pi.hostName()}")
        lines.append(f"OS Version:       {pi.operatingSystemVersionString()}")
        lines.append(f"CPU Cores:        {pi.processorCount()} ({pi.activeProcessorCount()} active)")
        lines.append(f"Physical Memory:  {pi.physicalMemory() // (1024**3)} GB")
        lines.append(f"System Uptime:    {_format_uptime(pi.systemUptime())}")
    except ImportError:
        # Fallback via sysctl -- run in parallel with other commands below
        pass

    # Run all subprocess calls in parallel (disk, battery, thermal, load, memory)
    commands: dict[str, list[str]] = {
        "disk": ["df", "-h", "/"],
        "battery": ["pmset", "-g", "batt"],
        "thermal": ["pmset", "-g", "therm"],
        "loadavg": ["sysctl", "-n", "vm.loadavg"],
        "memory": ["memory_pressure"],
    }
    # Add sysctl fallbacks if Foundation wasn't available
    if not lines:
        commands["memsize"] = ["sysctl", "-n", "hw.memsize"]
        commands["ncpu"] = ["sysctl", "-n", "hw.ncpu"]
        commands["boottime"] = ["sysctl", "-n", "kern.boottime"]

    results: dict[str, tuple[bool, str, str]] = {}

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(_run_command, cmd, 3.0): key
            for key, cmd in commands.items()
        }
        for future in as_completed(futures, timeout=10.0):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception:
                results[key] = (False, "", "timed out")

    # Process results
    if not lines:
        _, stdout, _ = results.get("memsize", (False, "", ""))
        if stdout:
            lines.append(f"Physical Memory:  {int(stdout) // (1024**3)} GB")
        _, stdout, _ = results.get("ncpu", (False, "", ""))
        if stdout:
            lines.append(f"CPU Cores:        {stdout.strip()}")
        _, stdout, _ = results.get("boottime", (False, "", ""))
        if stdout:
            lines.append(f"Boot time:        {stdout.strip()}")

    # Disk
    ok, stdout, _ = results.get("disk", (False, "", ""))
    if ok:
        parts = stdout.splitlines()[-1].split()
        if len(parts) >= 4:
            lines.append(f"Root Disk:        {parts[1]} total, {parts[2]} used, {parts[3]} avail ({parts[4]} used)")

    # Battery
    ok, stdout, _ = results.get("battery", (False, "", ""))
    if ok:
        for line in stdout.splitlines():
            if "%" in line and ("discharging" in line.lower() or "charging" in line.lower() or "charged" in line.lower()):
                lines.append(f"Battery:          {line.strip()}")
                break

    # Thermal
    ok, stdout, _ = results.get("thermal", (False, "", ""))
    if ok and "No thermal warning" not in stdout:
        lines.append(f"Thermal:          {stdout.strip()}")

    # Load average
    ok, stdout, _ = results.get("loadavg", (False, "", ""))
    if ok and stdout:
        lines.append(f"Load Average:     {stdout.strip()}")

    # Memory pressure
    ok, stdout, _ = results.get("memory", (False, "", ""))
    if ok:
        for line in stdout.splitlines():
            if "pressure" in line.lower() or "free" in line.lower():
                lines.append(f"  {line.strip()}")
                break

    return ToolResult(success=True, content="System Info:\n" + "\n".join(f"  {l}" for l in lines))


def _format_uptime(seconds: float) -> str:
    """Format seconds into a human-readable uptime string."""
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


# ===========================================================================
# desktop_key -- press a key combination
# ===========================================================================

# Keycode map for special keys (macOS virtual key codes)
_KEYCODE_MAP: dict[str, int] = {
    "return": 0x24, "enter": 0x24,
    "tab": 0x30,
    "space": 0x31,
    "delete": 0x33, "backspace": 0x33,
    "escape": 0x35, "esc": 0x35,
    "right": 0x7C, "left": 0x7B,
    "down": 0x7D, "up": 0x7E,
    "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76,
    "f5": 0x60, "f6": 0x61, "f7": 0x62, "f8": 0x64,
    "f9": 0x65, "f10": 0x6D, "f11": 0x67, "f12": 0x6F,
    "home": 0x73, "end": 0x77,
    "pageup": 0x74, "pagedown": 0x79,
    "forwarddelete": 0x75,
    "help": 0x72,
}

# Modifier mask bits
_MODIFIER_MASKS: dict[str, int] = {
    "cmd": 0x100, "command": 0x100,
    "shift": 0x200,
    "option": 0x800, "alt": 0x800,
    "control": 0x1000, "ctrl": 0x1000,
    "fn": 0x800000, "function": 0x800000,
}


def _macos_press_keys(combo: str) -> ToolResult:
    """Press a key combination via CGEvent.

    Args:
        combo: e.g. "cmd+c", "cmd+shift+4", "cmd+tab", "escape", "return"

    CGEvent objects are explicitly released via CFRelease to avoid
    native memory leaks on repeated calls.
    """
    try:
        Quartz = _get_quartz()
        # Import CFRelease for explicit cleanup of CoreFoundation objects
        from CoreFoundation import CFRelease as _CFRelease
    except ImportError:
        return _press_keys_via_osascript(combo)

    parts = [p.strip().lower() for p in combo.split("+")]

    # Separate modifiers from the main key
    modifiers: list[str] = []
    key_name = parts[-1]  # last part is the main key
    for p in parts[:-1]:
        if p in _MODIFIER_MASKS:
            modifiers.append(p)
        else:
            return ToolResult(
                success=False,
                content=f"Unknown modifier: '{p}'. Use: cmd, shift, option, ctrl, fn.",
            )

    # Get keycode
    keycode = _KEYCODE_MAP.get(key_name)
    if keycode is None:
        # Single character -> look up from virtual key code tables
        if len(key_name) == 1:
            # For letters A-Z: kVK_ANSI_A = 0x00, kVK_ANSI_Z = 0x06
            if "a" <= key_name.lower() <= "z":
                keycode = ord(key_name.lower()) - ord("a")
            # For numbers 0-9: kVK_ANSI_0 = 0x1D, kVK_ANSI_9 = 0x19
            elif "0" <= key_name <= "9":
                keycode = ord(key_name) - ord("0") + 0x1D
            else:
                return _press_keys_via_osascript(combo)

    if keycode is None:
        return _press_keys_via_osascript(combo)

    try:
        # Build modifier flags
        flags = 0
        for mod in modifiers:
            flags |= _MODIFIER_MASKS[mod]

        # Create and post key-down event
        event_down = Quartz.CGEventCreateKeyboardEvent(None, keycode, True)
        if flags:
            Quartz.CGEventSetFlags(event_down, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event_down)

        # Brief pause for the OS to register the key-down
        time.sleep(0.02)

        # Create and post key-up event
        event_up = Quartz.CGEventCreateKeyboardEvent(None, keycode, False)
        if flags:
            Quartz.CGEventSetFlags(event_up, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event_up)

        # Explicitly release CGEvent objects to avoid native memory leaks
        try:
            _CFRelease(event_down)
            _CFRelease(event_up)
        except Exception:
            pass  # best-effort cleanup

        return ToolResult(success=True, content=f"Pressed: {combo}")

    except Exception:
        return _press_keys_via_osascript(combo)


def _press_keys_via_osascript(combo: str) -> ToolResult:
    """Press keys via AppleScript System Events fallback."""
    # Map combo to AppleScript key codes / keystroke
    # Common combos
    lower = combo.lower().strip()

    # Handle simple special keys
    simple_keys = {
        "escape": "key code 53",
        "esc": "key code 53",
        "return": "key code 36",
        "enter": "key code 36",
        "tab": "key code 48",
        "space": "key code 49",
        "delete": "key code 51",
        "backspace": "key code 51",
        "left": "key code 123",
        "right": "key code 124",
        "up": "key code 126",
        "down": "key code 125",
        "f5": "key code 96",
        "f11": "key code 103",
    }

    if lower in simple_keys:
        ok, output = _run_osascript(
            f'tell application "System Events" to {simple_keys[lower]}',
            timeout=5.0,
        )
        if ok:
            return ToolResult(success=True, content=f"Pressed: {combo}")
        return ToolResult(success=False, content=f"Key press failed: {output}")

    # Handle combos with modifiers
    # System Events uses: keystroke "c" using {command down, shift down}
    parts = [p.strip().lower() for p in combo.split("+")]
    key = parts[-1]
    mod_parts = parts[:-1]

    mod_map = {
        "cmd": "command down", "command": "command down",
        "shift": "shift down",
        "option": "option down", "alt": "option down",
        "ctrl": "control down", "control": "control down",
    }

    modifiers_script = []
    for mod in mod_parts:
        if mod in mod_map:
            modifiers_script.append(mod_map[mod])
        else:
            return ToolResult(
                success=False,
                content=f"Unknown modifier: '{mod}'. Use: cmd, shift, option, ctrl.",
            )

    if modifiers_script:
        mods_str = ", ".join(modifiers_script)
        ok, output = _run_osascript(
            f'tell application "System Events" to keystroke "{key}" using {{{mods_str}}}',
            timeout=5.0,
        )
    else:
        ok, output = _run_osascript(
            f'tell application "System Events" to keystroke "{key}"',
            timeout=5.0,
        )

    if ok:
        return ToolResult(success=True, content=f"Pressed: {combo}")
    return ToolResult(success=False, content=f"Key press failed: {output}")


# ===========================================================================
# desktop_open -- open a file, folder, or URL in the default application
# ===========================================================================

def _macos_open(target: str) -> ToolResult:
    """Open a file, folder, or URL."""
    ok, stdout, stderr = _run_command(
        ["open", target],
        timeout=15.0,
    )
    if ok:
        return ToolResult(success=True, content=f"Opened: {target}")
    return ToolResult(
        success=False,
        content=f"Failed to open '{target}': {stderr}",
        hint="Check that the path or URL is valid. URLs should start with https://",
    )


# ===========================================================================
# desktop_reveal -- reveal a file in Finder
# ===========================================================================

def _macos_reveal(path: str) -> ToolResult:
    """Reveal a file in Finder."""
    if not os.path.exists(path):
        return ToolResult(
            success=False,
            content=f"Path does not exist: {path}",
        )
    ok, stdout, stderr = _run_command(
        ["open", "-R", path],
        timeout=10.0,
    )
    if ok:
        return ToolResult(success=True, content=f"Revealed in Finder: {path}")
    return ToolResult(success=False, content=f"Failed to reveal: {stderr}")


# ===========================================================================
# desktop_notify -- post a system notification
# ===========================================================================

def _macos_notify(title: str, message: str = "", sound: bool = False) -> ToolResult:
    """Post a macOS notification."""
    sound_clause = 'sound name "default"' if sound else ""
    escaped_title = _escape_applescript_string(title)
    escaped_msg = _escape_applescript_string(message)

    script = (
        f'display notification "{escaped_msg}" '
        f'with title "{escaped_title}"'
        + (f" {sound_clause}" if sound_clause else "")
    )

    ok, output = _run_osascript(script, timeout=5.0)
    if ok:
        return ToolResult(success=True, content=f"Notification posted: {title}")
    return ToolResult(success=False, content=f"Notification failed: {output}")


# ===========================================================================
# Tool registrations
# ===========================================================================

@_register("desktop_apps")
def _desktop_apps(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """List running desktop applications."""
    if PLATFORM != "Darwin":
        return _mac_only()
    return _macos_list_apps()


@_register("desktop_launch")
def _desktop_launch(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Launch an application by name or bundle ID."""
    if PLATFORM != "Darwin":
        return _mac_only()
    name = args.get("name", "").strip()
    if not name:
        return ToolResult(success=False, content="Missing required parameter: 'name'.")
    return _macos_launch_app(name)


@_register("desktop_quit")
def _desktop_quit(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Quit an application by name or PID."""
    if PLATFORM != "Darwin":
        return _mac_only()
    name = args.get("name", "").strip()
    if not name:
        return ToolResult(success=False, content="Missing required parameter: 'name'.")
    return _macos_quit_app(name)


@_register("desktop_focus")
def _desktop_focus(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Bring an application window to the foreground."""
    if PLATFORM != "Darwin":
        return _mac_only()
    name = args.get("name", "").strip()
    if not name:
        return ToolResult(success=False, content="Missing required parameter: 'name'.")
    return _macos_focus_app(name)


@_register("desktop_clipboard")
def _desktop_clipboard(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Read from or write to the system clipboard."""
    if PLATFORM != "Darwin":
        return _mac_only()
    action = args.get("action", "read").strip().lower()
    text = args.get("text", "")
    return _macos_clipboard(action, text)


@_register("desktop_windows")
def _desktop_windows(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """List all visible windows across all applications."""
    if PLATFORM != "Darwin":
        return _mac_only()
    return _macos_list_windows()


@_register("desktop_system_info")
def _desktop_system_info(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Gather system metrics: CPU, memory, disk, battery, thermal, uptime."""
    if PLATFORM != "Darwin":
        return _mac_only()
    return _macos_system_info()


@_register("desktop_key")
def _desktop_key(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Press a key combination (e.g. 'cmd+c', 'cmd+tab', 'escape')."""
    if PLATFORM != "Darwin":
        return _mac_only()
    combo = args.get("combo", "").strip()
    if not combo:
        return ToolResult(success=False, content="Missing required parameter: 'combo'.")
    return _macos_press_keys(combo)


@_register("desktop_open")
def _desktop_open(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Open a file, folder, or URL in the default application."""
    if PLATFORM != "Darwin":
        return _mac_only()
    target = args.get("target", "").strip()
    if not target:
        return ToolResult(success=False, content="Missing required parameter: 'target'.")
    return _macos_open(target)


@_register("desktop_reveal")
def _desktop_reveal(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Reveal a file in Finder."""
    if PLATFORM != "Darwin":
        return _mac_only()
    path = args.get("path", "").strip()
    if not path:
        return ToolResult(success=False, content="Missing required parameter: 'path'.")
    return _macos_reveal(path)


@_register("desktop_notify")
def _desktop_notify(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Post a system notification."""
    if PLATFORM != "Darwin":
        return _mac_only()
    title = args.get("title", "").strip()
    message = args.get("message", "").strip()
    sound = args.get("sound", False)
    if not title:
        return ToolResult(success=False, content="Missing required parameter: 'title'.")
    return _macos_notify(title, message, sound)


# ===========================================================================
# Summaries
# ===========================================================================

@_summarize("desktop_apps")
def _desktop_apps_summary(args: dict) -> str:
    return "desktop_apps()"

@_summarize("desktop_launch")
def _desktop_launch_summary(args: dict) -> str:
    return f"desktop_launch({args.get('name', '?')})"

@_summarize("desktop_quit")
def _desktop_quit_summary(args: dict) -> str:
    return f"desktop_quit({args.get('name', '?')})"

@_summarize("desktop_focus")
def _desktop_focus_summary(args: dict) -> str:
    return f"desktop_focus({args.get('name', '?')})"

@_summarize("desktop_clipboard")
def _desktop_clipboard_summary(args: dict) -> str:
    action = args.get("action", "read")
    return f"desktop_clipboard({action})"

@_summarize("desktop_windows")
def _desktop_windows_summary(args: dict) -> str:
    return "desktop_windows()"

@_summarize("desktop_system_info")
def _desktop_system_info_summary(args: dict) -> str:
    return "desktop_system_info()"

@_summarize("desktop_key")
def _desktop_key_summary(args: dict) -> str:
    return f"desktop_key({args.get('combo', '?')})"

@_summarize("desktop_open")
def _desktop_open_summary(args: dict) -> str:
    return f"desktop_open({args.get('target', '?')[:40]})"

@_summarize("desktop_reveal")
def _desktop_reveal_summary(args: dict) -> str:
    return f"desktop_reveal({args.get('path', '?')[:40]})"

@_summarize("desktop_notify")
def _desktop_notify_summary(args: dict) -> str:
    return f"desktop_notify({args.get('title', '?')[:30]})"
