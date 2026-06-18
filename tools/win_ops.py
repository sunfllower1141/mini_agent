#!/usr/bin/env python3
"""
win_ops.py -- Windows desktop API integrations for the desktop agent.

Tools:
    desktop_apps        -- List running applications (name, PID, window title)
    desktop_launch      -- Launch an app by name or path
    desktop_quit        -- Quit an app by name or PID
    desktop_focus       -- Bring an app window to the foreground
    desktop_clipboard   -- Read or write the system clipboard
    desktop_windows     -- List all visible windows across applications
    desktop_system_info -- CPU, memory, disk, battery, uptime
    desktop_key         -- Press a key combination (e.g. "ctrl+c", "alt+tab")
    desktop_open        -- Open a file, folder, or URL in the default app
    desktop_reveal      -- Reveal a file in Explorer
    desktop_notify      -- Post a system notification

Uses subprocess (built-in Windows commands + PowerShell) for maximum
compatibility with zero mandatory dependencies.  Optional packages
(pyperclip, pygetwindow, psutil) improve quality when available.
"""

from __future__ import annotations

import os
import platform
import subprocess
import time

from core.safety import ReadSafetyGate, WriteSafetyGate
from tools import _register, _summarize, ToolResult

PLATFORM = platform.system()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_ps(script: str, timeout: float = 10.0) -> tuple[bool, str]:
    """Run a PowerShell script snippet. Returns (ok, output_stripped)."""
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        if result.returncode != 0:
            return False, stderr.strip() or stdout.strip()
        return True, stdout.strip()
    except FileNotFoundError:
        return False, "powershell.exe not found"
    except subprocess.TimeoutExpired:
        return False, "PowerShell command timed out"
    except Exception as exc:
        return False, str(exc)


def _run_cmd(cmd: list[str], timeout: float = 10.0) -> tuple[bool, str, str]:
    """Run a command, return (ok, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        return result.returncode == 0, stdout.strip(), stderr.strip()
    except FileNotFoundError:
        return False, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return False, "", "timed out"
    except Exception as exc:
        return False, "", str(exc)


# ===========================================================================
# desktop_clipboard -- read or write the system clipboard
# ===========================================================================


def _win_clipboard(action: str, text: str = "") -> ToolResult:
    """Read or write the system clipboard on Windows.

    Uses pyperclip if installed, falls back to PowerShell.
    """
    try:
        import pyperclip
        if action == "read":
            content = pyperclip.paste()
            return ToolResult(success=True, content=f"Clipboard content:\n{content}")
        else:
            pyperclip.copy(text)
            return ToolResult(
                success=True,
                content=f"Copied to clipboard ({len(text)} chars).",
            )
    except ImportError:
        pass

    # Fallback: PowerShell
    if action == "read":
        ok, output = _run_ps("Get-Clipboard", timeout=5.0)
        if ok:
            return ToolResult(success=True, content=f"Clipboard content:\n{output}")
        return ToolResult(success=False, content=f"Clipboard read failed: {output}")
    else:
        # Escape special chars for PowerShell
        escaped = text.replace("'", "''")
        ok, output = _run_ps(f"Set-Clipboard -Value '{escaped}'", timeout=5.0)
        if ok:
            return ToolResult(
                success=True,
                content=f"Copied to clipboard ({len(text)} chars).",
            )
        return ToolResult(success=False, content=f"Clipboard write failed: {output}")


# ===========================================================================
# desktop_open -- open a file, folder, or URL in the default app
# ===========================================================================


def _win_open(target: str) -> ToolResult:
    """Open a file, folder, or URL in the default application on Windows."""
    try:
        # os.startfile is the canonical Windows way to open files/folders
        os.startfile(target)
        return ToolResult(success=True, content=f"Opened: {target}")
    except FileNotFoundError:
        return ToolResult(
            success=False,
            content=f"File not found: {target}",
            hint="Check the path. Use absolute paths for best results.",
        )
    except OSError as exc:
        # Fallback: try 'start' command (handles URLs too)
        try:
            subprocess.run(
                ["cmd.exe", "/C", "start", "", target],
                capture_output=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return ToolResult(success=True, content=f"Opened: {target}")
        except Exception as exc2:
            return ToolResult(success=False, content=f"Open failed: {exc} / {exc2}")


# ===========================================================================
# desktop_reveal -- reveal a file in Explorer
# ===========================================================================


def _win_reveal(path: str) -> ToolResult:
    """Reveal a file in Windows Explorer (selects it)."""
    if not os.path.exists(path):
        return ToolResult(
            success=False,
            content=f"Path not found: {path}",
            hint="Use an absolute path to an existing file or folder.",
        )

    abs_path = os.path.abspath(path)
    try:
        subprocess.run(
            ["explorer", "/select,", abs_path],
            capture_output=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return ToolResult(success=True, content=f"Revealed in Explorer: {abs_path}")
    except Exception as exc:
        return ToolResult(success=False, content=f"Explorer reveal failed: {exc}")


# ===========================================================================
# desktop_apps -- list running applications
# ===========================================================================


def _win_list_apps() -> ToolResult:
    """List running applications on Windows.

    Uses PowerShell Get-Process for best filtering (only processes with
    a visible main window), falls back to tasklist with window title filter.
    """
    # Try PowerShell first (best filtering) via temp script to avoid escaping issues
    import tempfile
    ps_script = """Get-Process | Where-Object { $_.MainWindowTitle -ne '' } |
    ForEach-Object {
        '{0}|{1}|{2}' -f $_.Id, $_.ProcessName, $_.MainWindowTitle
    } | Sort-Object
"""
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ps1", delete=False) as f:
            f.write(ps_script)
            tmp_path = f.name
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", tmp_path],
            capture_output=True, text=True, timeout=15.0,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        os.unlink(tmp_path)
        if result.returncode == 0 and result.stdout and result.stdout.strip():
            lines = result.stdout.strip().splitlines()
            apps = []
            for line in lines:
                parts = line.split("|", 2)
                if len(parts) >= 2:
                    pid = parts[0].strip()
                    name = parts[1].strip()
                    title = parts[2].strip() if len(parts) > 2 else ""
                    if title:
                        apps.append(f'  PID={pid:>6s}  {name[:35]:35s}  "{title[:60]}"')
                    else:
                        apps.append(f'  PID={pid:>6s}  {name[:35]:35s}')

            if apps:
                return ToolResult(
                    success=True,
                    content=f"Running applications ({len(apps)} with windows):\n" + "\n".join(apps[:80]),
                )
    except Exception:
        pass

    # Fallback: tasklist with window titles
    ok, stdout, stderr = _run_cmd(
        ["tasklist", "/FO", "CSV", "/NH", "/V",
         "/FI", "STATUS eq RUNNING"],
        timeout=10.0,
    )
    if not ok:
        ok, stdout, stderr = _run_cmd(
            ["tasklist", "/FO", "CSV", "/NH"],
            timeout=10.0,
        )
        if not ok:
            return ToolResult(success=False, content=f"tasklist failed: {stderr}")

    lines = stdout.splitlines()
    apps: list[str] = []

    for line in lines:
        parts = line.split('","')
        if len(parts) < 2:
            continue
        name = parts[0].strip('"')
        pid = parts[1].strip('"')

        # Get window title (field 9 in verbose output)
        title = ""
        if len(parts) >= 9:
            title = parts[8].strip('"')
            if title == "N/A":
                title = ""

        # Only show processes with a window title
        if title:
            apps.append(f'  PID={pid:>6s}  {name[:35]:35s}  "{title[:60]}"')

    # Deduplicate
    seen: set[str] = set()
    unique: list[str] = []
    for app in apps:
        key = app[:50]
        if key not in seen:
            seen.add(key)
            unique.append(app)

    return ToolResult(
        success=True,
        content=f"Running applications ({len(unique)} with windows):\n" + "\n".join(unique[:80]),
    )


# ===========================================================================
# desktop_launch -- launch an application
# ===========================================================================


def _win_launch_app(name: str) -> ToolResult:
    """Launch an application by name, path, or executable on Windows.

    Tries multiple strategies:
    1. Direct executable name (if it's on PATH or absolute)
    2. start command (Windows built-in app lookup)
    3. shell:AppsFolder search (UWP apps)
    """
    # If it looks like an absolute path, use it directly
    if os.path.isabs(name) and os.path.exists(name):
        try:
            os.startfile(name)
            return ToolResult(success=True, content=f"Launched: {name}")
        except Exception as exc:
            return ToolResult(success=False, content=f"Launch failed: {exc}")

    # If it has a .exe extension, try running it directly
    if name.lower().endswith(".exe"):
        try:
            subprocess.Popen(
                [name], close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            time.sleep(0.5)
            return ToolResult(success=True, content=f"Launched: {name}")
        except Exception as exc:
            return ToolResult(success=False, content=f"Launch failed: {exc}")

    # Use 'start' command (handles registered apps, URLs, etc.)
    ok, stdout, stderr = _run_cmd(
        ["cmd.exe", "/C", "start", "", name],
        timeout=10.0,
    )
    # start always returns 0 if cmd runs, even if app not found
    # Give it a moment and check if the app started
    time.sleep(0.5)

    if ok or not stderr:
        return ToolResult(success=True, content=f"Launched: {name}")

    # Last resort: try shell:AppsFolder via PowerShell
    ok, output = _run_ps(
        f"Start-Process 'shell:AppsFolder\\{name}' -ErrorAction SilentlyContinue",
        timeout=10.0,
    )
    if ok:
        return ToolResult(success=True, content=f"Launched UWP app: {name}")

    return ToolResult(
        success=False,
        content=f"Could not launch '{name}'.",
        hint="Try the full path to the .exe, or check the app name with desktop_apps.",
    )


# ===========================================================================
# desktop_quit -- quit an application
# ===========================================================================


def _win_quit_app(name_or_pid: str) -> ToolResult:
    """Quit an app by name or PID on Windows.

    Uses taskkill with graceful termination first, then force.
    """
    # Determine if it's a PID or a name
    is_pid = name_or_pid.isdigit()

    # First: gentle close (no /F)
    if is_pid:
        ok, stdout, stderr = _run_cmd(
            ["taskkill", "/PID", name_or_pid],
            timeout=10.0,
        )
    else:
        # Try exact name match first
        exe_name = name_or_pid if name_or_pid.lower().endswith(".exe") else f"{name_or_pid}.exe"
        ok, stdout, stderr = _run_cmd(
            ["taskkill", "/IM", exe_name],
            timeout=10.0,
        )
        if not ok:
            # Try without .exe
            ok, stdout, stderr = _run_cmd(
                ["taskkill", "/IM", name_or_pid],
                timeout=10.0,
            )

    if ok:
        return ToolResult(success=True, content=f"Quit: {name_or_pid}")

    # Force quit if gentle fails
    if is_pid:
        ok2, stdout2, stderr2 = _run_cmd(
            ["taskkill", "/F", "/PID", name_or_pid],
            timeout=10.0,
        )
    else:
        exe_name = name_or_pid if name_or_pid.lower().endswith(".exe") else f"{name_or_pid}.exe"
        ok2, stdout2, stderr2 = _run_cmd(
            ["taskkill", "/F", "/IM", exe_name],
            timeout=10.0,
        )

    if ok2:
        return ToolResult(success=True, content=f"Force quit: {name_or_pid}")
    else:
        return ToolResult(
            success=False,
            content=f"Could not quit '{name_or_pid}': {stderr2 or stderr}",
            hint="The app may not be running. Use desktop_apps to check.",
        )


# ===========================================================================
# desktop_focus -- bring an app window to the foreground
# ===========================================================================


def _win_focus_app(name: str) -> ToolResult:
    """Bring an application window to the foreground on Windows.

    Tries pygetwindow first (more reliable), falls back to PowerShell.
    """
    # Try pygetwindow (optional dependency)
    try:
        import pygetwindow as gw

        # Search for windows matching the name (case-insensitive, substring)
        matches = gw.getWindowsWithTitle(name)
        if not matches:
            # Try partial match
            all_windows = gw.getAllWindows()
            matches = [w for w in all_windows if name.lower() in w.title.lower()]

        if matches:
            # Prefer the one that isn't minimized
            for win in matches:
                if not win.isMinimized:
                    win.activate()
                    return ToolResult(success=True, content=f"Focused: {win.title}")
            # If all minimized, restore the first one
            matches[0].restore()
            matches[0].activate()
            return ToolResult(success=True, content=f"Focused: {matches[0].title}")

        return ToolResult(
            success=False,
            content=f"No window matching '{name}' found.",
            hint="Use desktop_windows to see open windows.",
        )
    except ImportError:
        pass

    # Fallback: PowerShell via temp script
    import tempfile
    ps_script = f"""Add-Type @'
using System;
using System.Runtime.InteropServices;
public class Win32Window {{
    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")]
    public static extern IntPtr FindWindow(string lpClassName, string lpWindowName);
    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
}}
'@
$proc = Get-Process | Where-Object {{ $_.ProcessName -like '*{name}*' -or $_.MainWindowTitle -like '*{name}*' }} | Select-Object -First 1
if ($proc -and $proc.MainWindowHandle -ne 0) {{
    [Win32Window]::ShowWindow($proc.MainWindowHandle, 9)
    [Win32Window]::SetForegroundWindow($proc.MainWindowHandle)
    Write-Output "Focused: $($proc.ProcessName)"
}} else {{
    Write-Error "No window found for '{name}'"
}}
"""
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ps1", delete=False) as f:
            f.write(ps_script)
            tmp_path = f.name
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", tmp_path],
            capture_output=True, text=True, timeout=10.0,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        os.unlink(tmp_path)
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        if result.returncode == 0:
            return ToolResult(success=True, content=stdout.strip())
        return ToolResult(
            success=False,
            content=f"Could not focus '{name}': {stderr.strip()}",
            hint="Use desktop_apps to verify the app is running.",
        )
    except Exception as exc:
        return ToolResult(
            success=False,
            content=f"Could not focus '{name}': {exc}",
            hint="Use desktop_apps to verify the app is running.",
        )


# ===========================================================================
# desktop_windows -- list all visible windows
# ===========================================================================


def _win_list_windows() -> ToolResult:
    """List all visible windows across all applications on Windows.

    Tries pygetwindow first, falls back to PowerShell.
    """
    try:
        import pygetwindow as gw

        all_windows = gw.getAllWindows()
        visible = [w for w in all_windows if w.title.strip() and w.visible]

        if not visible:
            return ToolResult(success=True, content="No visible windows found.")

        lines = []
        for w in visible[:100]:
            geom = f"{w.width}x{w.height}@{w.left},{w.top}"
            minim = " [min]" if w.isMinimized else ""
            lines.append(f'  "{w.title[:60]}"  {geom}{minim}')

        return ToolResult(
            success=True,
            content=f"Visible windows ({len(visible)}):\n" + "\n".join(lines),
        )
    except ImportError:
        pass

    # Fallback: PowerShell script via temp file (avoids escaping issues)
    import tempfile
    ps_script = """Get-Process | Where-Object { $_.MainWindowTitle -ne '' } |
    ForEach-Object {
        $title = if ($_.MainWindowTitle.Length -gt 60) { $_.MainWindowTitle.Substring(0,60) + '...' } else { $_.MainWindowTitle }
        "PID=$($_.Id)  $($_.ProcessName)  `"$title`""
    } | Sort-Object
"""
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ps1", delete=False) as f:
            f.write(ps_script)
            tmp_path = f.name
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", tmp_path],
            capture_output=True, text=True, timeout=15.0,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        os.unlink(tmp_path)
        if result.returncode == 0 and result.stdout and result.stdout.strip():
            lines = result.stdout.strip().splitlines()
            return ToolResult(
                success=True,
                content=f"Visible windows ({len(lines)}):\n" + "\n".join(f"  {l}" for l in lines[:100]),
            )
        return ToolResult(success=False, content=f"Could not list windows: {(result.stderr or '').strip()}")
    except Exception as exc:
        return ToolResult(success=False, content=f"Could not list windows: {exc}")


# ===========================================================================
# desktop_system_info -- CPU, memory, disk, battery, uptime
# ===========================================================================


def _win_system_info() -> ToolResult:
    """Gather system metrics on Windows.

    Uses psutil if available (best), falls back to wmic/systeminfo subprocesses.
    """
    lines: list[str] = []

    # Try psutil first (best quality)
    try:
        import psutil

        # CPU
        cpu_pct = psutil.cpu_percent(interval=0.5)
        cpu_count = psutil.cpu_count(logical=True)
        cpu_phys = psutil.cpu_count(logical=False)
        lines.append(f"CPU:              {cpu_phys}C/{cpu_count}T @ {cpu_pct}%")

        # Memory
        mem = psutil.virtual_memory()
        lines.append(f"Memory:           {mem.used // (1024**3)}.{mem.used % (1024**3) // (100*1024*1024):01d} GB / {mem.total // (1024**3)} GB ({mem.percent}%)")

        # Disk
        for part in psutil.disk_partitions():
            try:
                usage = psutil.disk_usage(part.mountpoint)
                if part.mountpoint == "C:\\" or usage.total > 50 * 1024**3:
                    lines.append(f"Disk ({part.mountpoint}):       {usage.used // (1024**3)} GB / {usage.total // (1024**3)} GB ({usage.percent}%)")
            except Exception:
                pass

        # Uptime
        boot_time = psutil.boot_time()
        uptime_seconds = time.time() - boot_time
        lines.append(f"Uptime:           {_fmt_uptime(uptime_seconds)}")

        # Battery
        if hasattr(psutil, 'sensors_battery'):
            battery = psutil.sensors_battery()
            if battery:
                status = "charging" if battery.power_plugged else "discharging"
                lines.append(f"Battery:          {battery.percent}% ({status})")

    except ImportError:
        pass

    # If psutil gave us nothing, use subprocess fallbacks
    if not lines:
        # wmic os: columns are Node,FreePhysicalMemory,LastBootUpTime,NumberOfProcesses,TotalVisibleMemorySize
        ok, stdout, _ = _run_cmd(
            ["wmic", "os", "get", "TotalVisibleMemorySize,FreePhysicalMemory,NumberOfProcesses,LastBootUpTime", "/format:csv"],
            timeout=15.0,
        )
        if ok:
            for line in stdout.splitlines():
                if "Node" in line or not line.strip():
                    continue
                parts = line.split(",")
                if len(parts) >= 5:
                    try:
                        total_kb = int(parts[4].strip() or 0)   # TotalVisibleMemorySize
                        free_kb = int(parts[1].strip() or 0)     # FreePhysicalMemory
                        used_gb = (total_kb - free_kb) // (1024 * 1024)
                        total_gb = total_kb // (1024 * 1024)
                        lines.append(f"Memory:           {used_gb} GB / {total_gb} GB")
                    except ValueError:
                        pass
                    lines.append(f"Processes:        {parts[3].strip()}")   # NumberOfProcesses
                    lines.append(f"Last Boot:        {parts[2].strip()[:14]}")  # LastBootUpTime

        # CPU info: columns are Node,Name,NumberOfCores,NumberOfLogicalProcessors
        ok, stdout, _ = _run_cmd(
            ["wmic", "cpu", "get", "Name,NumberOfCores,NumberOfLogicalProcessors", "/format:csv"],
            timeout=10.0,
        )
        if ok:
            for line in stdout.splitlines():
                if "Node" in line or not line.strip():
                    continue
                parts = line.split(",")
                if len(parts) >= 4:
                    lines.append(f"CPU:              {parts[1].strip()[:50]} ({parts[2].strip()}C/{parts[3].strip()}T)")

        # Disk: columns are Node,DeviceID,FreeSpace,Size
        ok, stdout, _ = _run_cmd(
            ["wmic", "logicaldisk", "where", "DriveType=3", "get", "DeviceID,Size,FreeSpace", "/format:csv"],
            timeout=10.0,
        )
        if ok:
            for line in stdout.splitlines():
                if "Node" in line or not line.strip():
                    continue
                parts = line.split(",")
                if len(parts) >= 4:
                    try:
                        device = parts[1].strip()
                        size = int(parts[3].strip()) // (1024**3)   # Size
                        free = int(parts[2].strip()) // (1024**3)   # FreeSpace
                        lines.append(f"Disk ({device}):       {size - free} GB / {size} GB")
                    except ValueError:
                        pass

    return ToolResult(
        success=True,
        content="System Info:\n" + "\n".join(f"  {l}" for l in lines) if lines else "Could not gather system info.",
    )


def _fmt_uptime(seconds: float) -> str:
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

# Windows virtual key codes for common special keys
_WIN_VK_MAP: dict[str, int] = {
    "return": 0x0D, "enter": 0x0D,
    "tab": 0x09,
    "space": 0x20,
    "delete": 0x2E, "backspace": 0x08,
    "escape": 0x1B, "esc": 0x1B,
    "right": 0x27, "left": 0x25,
    "down": 0x28, "up": 0x26,
    "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "insert": 0x2D,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
    "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
    "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "printscreen": 0x2C, "scrolllock": 0x91,
    "pause": 0x13,
    "numlock": 0x90,
    "capslock": 0x14,
    "apps": 0x5D, "menu": 0x5D,
    "volume_mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF,
    "media_next": 0xB0, "media_prev": 0xB1, "media_stop": 0xB2, "media_play": 0xB3,
}

# Windows modifier key mapping
_WIN_MOD_MAP: dict[str, str] = {
    "ctrl": "^", "control": "^",
    "alt": "%",
    "shift": "+",
    "win": "#", "windows": "#", "cmd": "#", "command": "#",
}


def _win_press_keys(combo: str) -> ToolResult:
    """Press a key combination on Windows.

    Uses uiautomation SendKeys if available (best), falls back to
    PowerShell SendKeys via a small C# snippet.

    Args:
        combo: e.g. "ctrl+c", "alt+tab", "win+r", "escape", "return"
    """
    parts = [p.strip().lower() for p in combo.split("+")]

    # Try uiautomation first (most reliable)
    try:
        import uiautomation as auto

        # Build SendKeys format: {Ctrl}c, {Alt}{Tab}, {Enter}, etc.
        # uiautomation uses {...} syntax for special keys
        keys_parts = []
        for p in parts:
            if p in _WIN_MOD_MAP:
                keys_parts.append(_WIN_MOD_MAP[p])
            elif len(p) == 1:
                # Regular character
                keys_parts.append(p)
            elif p in _WIN_VK_MAP:
                # Named special key: use {NAME}
                keys_parts.append(f"{{{p}}}")
            else:
                # Unknown: type as literal
                keys_parts.append(p)

        auto.SendKeys("".join(keys_parts))
        return ToolResult(success=True, content=f"Pressed: {combo}")
    except ImportError:
        pass

    # Fallback: PowerShell + WScript.Shell SendKeys
    # Convert to SendKeys syntax
    sendkeys_parts = []
    for p in parts:
        if p in _WIN_MOD_MAP:
            sendkeys_parts.append(_WIN_MOD_MAP[p])
        elif p in _WIN_VK_MAP:
            # Map common keys to SendKeys format
            sk_map = {
                "enter": "{ENTER}", "return": "{ENTER}",
                "tab": "{TAB}", "space": " ",
                "backspace": "{BS}", "delete": "{DEL}",
                "escape": "{ESC}", "esc": "{ESC}",
                "right": "{RIGHT}", "left": "{LEFT}",
                "down": "{DOWN}", "up": "{UP}",
                "home": "{HOME}", "end": "{END}",
                "pageup": "{PGUP}", "pagedown": "{PGDN}",
                "insert": "{INSERT}",
            }
            for i in range(1, 13):
                sk_map[f"f{i}"] = f"{{F{i}}}"
            sendkeys_parts.append(sk_map.get(p, f"{{{p}}}"))
        elif len(p) == 1:
            sendkeys_parts.append(p)
        else:
            sendkeys_parts.append(f"{{{p}}}")

    sendkeys_str = "".join(sendkeys_parts)
    import tempfile
    ps_script = f"Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait('{sendkeys_str}')"
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ps1", delete=False) as f:
            f.write(ps_script)
            tmp_path = f.name
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", tmp_path],
            capture_output=True, text=True, timeout=5.0,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        os.unlink(tmp_path)
        if result.returncode == 0:
            return ToolResult(success=True, content=f"Pressed: {combo}")
        return ToolResult(success=False, content=f"Key press failed: {(result.stderr or '').strip()}")
    except Exception as exc:
        return ToolResult(success=False, content=f"Key press failed: {exc}")


# ===========================================================================
# desktop_notify -- post a system notification
# ===========================================================================


def _win_notify(title: str, message: str = "", sound: bool = False) -> ToolResult:
    """Post a system notification on Windows via PowerShell."""
    import tempfile

    # Escape single quotes for PowerShell
    escaped_title = title.replace("'", "''")
    escaped_msg = message.replace("'", "''")

    ps_script = f"""[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null

$template = @'
<toast>
    <visual>
        <binding template="ToastText02">
            <text id="1">{escaped_title}</text>
            <text id="2">{escaped_msg}</text>
        </binding>
    </visual>
</toast>
'@

$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($template)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("mini_agent").Show($toast)
"""
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ps1", delete=False) as f:
            f.write(ps_script)
            tmp_path = f.name
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", tmp_path],
            capture_output=True, text=True, timeout=10.0,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        os.unlink(tmp_path)
        if result.returncode == 0:
            return ToolResult(success=True, content=f"Notification posted: {title}")
        return ToolResult(success=False, content=f"Notification failed: {(result.stderr or '').strip()}")
    except Exception as exc:
        return ToolResult(success=False, content=f"Notification failed: {exc}")
