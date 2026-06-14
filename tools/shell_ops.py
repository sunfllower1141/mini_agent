#!/usr/bin/env python3
"""
shell_ops.py -- shell, search, test, and git tools for mini_agent.

Tools: run_shell, task_status, search_files, run_tests, verify, git
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import threading
import uuid
import threading

from core.safety import ReadSafetyGate, WriteSafetyGate
from tools import _register, _summarize, ToolResult, _TASK_REGISTRY

_WINDOWS = platform.system() == "Windows"
_WINDOWS_POPEN_KWARGS = {"creationflags": subprocess.CREATE_NO_WINDOW} if _WINDOWS else {}

# ---------------------------------------------------------------------------
# Platform helpers for cross-platform shell execution
# ---------------------------------------------------------------------------

def _get_shell_command() -> list[str]:
    """Return the best available shell command on this platform.
    
    On Windows: prefers Git Bash, then PowerShell, fallback to cmd.exe.
    On Unix: returns /bin/sh.
    """
    if platform.system() == "Windows":
        # Prefer Git Bash if available
        bash_paths = [
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
            r"C:\Git\bin\bash.exe",
        ]
        for bp in bash_paths:
            if os.path.isfile(bp):
                return [bp]
        # Try shutil.which for bash on PATH
        bash_on_path = shutil.which("bash")
        if bash_on_path:
            return [bash_on_path]
        # Next, PowerShell
        ps_path = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if ps_path:
            return [ps_path, "-Command"]
        # Fallback to cmd.exe
        return [os.environ.get("COMSPEC", "cmd.exe"), "/C"]
    else:
        return ["/bin/sh"]


def _is_bash_available() -> bool:
    """Check if bash is available on this system."""
    if platform.system() == "Windows":
        bash_paths = [
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
            r"C:\Git\bin\bash.exe",
        ]
        for bp in bash_paths:
            if os.path.isfile(bp):
                return True
        return shutil.which("bash") is not None
    else:
        return True  # /bin/sh is always present


_PYTHON_CMD: list[str] = []
def _get_python_cmd() -> list[str]:
    """Return the best available python command as a list.

    On Windows: tries 'py -3', then 'python3', then 'python'.
    On Unix: tries 'python3', versioned 'python3.X', then 'python'.
    Prefers a Python that has pytest installed (for run_tests).
    Results are memoised in _PYTHON_CMD.
    """
    global _PYTHON_CMD
    if _PYTHON_CMD:
        return _PYTHON_CMD
    if platform.system() == "Windows":
        candidates = [["py", "-3"], ["python3"], ["python"]]
    else:
        # Build candidate list: python3, then versioned python3.X (newest first),
        # then bare python.  This covers the common case where python3 is a
        # different install than the versioned python3.12 that has pytest.
        candidates = [["python3"]]
        for minor in range(14, 7, -1):  # python3.14 down to python3.8
            candidates.append([f"python3.{minor}"])
        candidates.append(["python"])
    # Find all viable pythons, preferring one with pytest
    viable: list[list[str]] = []
    with_pytest: list[list[str]] = []
    for cmd in candidates:
        if shutil.which(cmd[0]):
            viable.append(cmd)
            try:
                result = subprocess.run(
                    cmd + ["-m", "pytest", "--version"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    with_pytest.append(cmd)
            except (OSError, subprocess.TimeoutExpired):
                pass
    # Prefer a python with pytest, then any viable python, then fallback
    preferred = with_pytest[:1] or viable[:1]
    if preferred:
        _PYTHON_CMD = preferred[0]
        return _PYTHON_CMD
    # Ultimate fallback
    _PYTHON_CMD = ["python"]
    return _PYTHON_CMD


def _persist_test_output(output: str) -> None:
    """Save test run output to the memory DB for later inspection."""
    from tools import _TOOL_CONTEXT
    db_path = _TOOL_CONTEXT.scratchpad_path
    if not db_path:
        return
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS test_output ("
            "id INTEGER PRIMARY KEY CHECK (id = 1),"
            "output TEXT NOT NULL DEFAULT ''"
            ")"
        )
        conn.execute("INSERT OR IGNORE INTO test_output (id, output) VALUES (1, '')")
        conn.execute(
            "INSERT OR REPLACE INTO test_output (id, output) VALUES (1, ?)",
            (output,),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


_STREAM_READER_MAX_LINES = 10000  # cap to prevent unbounded memory growth


def _stream_reader(stream, collector: list[str], forward: bool = False,
                   on_output: callable = None, prefix: str = "") -> None:
    """Read lines from *stream* into *collector*, optionally forwarding via *on_output*."""
    for line in iter(stream.readline, ""):
        line = line.rstrip("\n")
        if len(collector) >= _STREAM_READER_MAX_LINES:
            if len(collector) == _STREAM_READER_MAX_LINES:
                collector.append(f"... (truncated at {_STREAM_READER_MAX_LINES} lines)")
            continue
        collector.append(line)
        if forward and on_output:
            try:
                on_output(prefix + line)
            except Exception:
                pass
    stream.close()


def _parse_pytest_output(raw_output: str, exit_code: int = 0) -> tuple[str, bool]:
    """Extract a human-readable summary from raw pytest output.

    Returns (summary_string, success_bool).
    """
    lines = raw_output.split("\n")
    failure_lines = [l.strip() for l in lines if l.strip().startswith("FAILED")]
    summary = ""
    for line in reversed(lines):
        if "passed" in line or "failed" in line or "error" in line:
            summary = line.strip()
            break
    if not summary:
        summary = f"exit_code={exit_code}"
    success = exit_code == 0
    if failure_lines and not success:
        summary += "\n" + "\n".join(f"  {fl}" for fl in failure_lines)
    return summary, success


# ---------------------------------------------------------------------------
# run_shell
# ---------------------------------------------------------------------------

# Patterns for dangerous commands that should warn or block
_DANGEROUS_COMMANDS: list[tuple[str, str]] = [
    # (regex pattern, explanation)
    (r"\brm\s+-rf\b", "rm -rf: recursive force delete -- will permanently remove files"),
    (r"\bgit\s+push\s+.*--force\b", "git push --force: overwrites remote history"),
    (r"\bgit\s+push\s+.*-f\b", "git push -f: overwrites remote history"),
    (r"\bsudo\b", "sudo: requires elevated privileges"),
    (r"\bchmod\s+777\b", "chmod 777: makes files world-writable (security risk)"),
    (r"\bdd\s+if=", "dd: raw disk write -- can destroy data"),
    (r"\bmkfs\.", "mkfs: creates filesystems (destroys existing data)"),
    (r">\s*/dev/sd[a-z]", "redirect to /dev/sd*: raw disk write"),
    (r"\bformat\s+[A-Z]:\\?", "format drive: destroys all data on the drive"),
]

def _check_dangerous_command(command: str, force: bool) -> str | None:
    """Return a warning/block message for dangerous commands, or None if safe."""
    for pattern, explanation in _DANGEROUS_COMMANDS:
        if re.search(pattern, command, re.IGNORECASE):
            if force:
                return (
                    f"WARNING: DANGEROUS COMMAND: {explanation}\n"
                    f"The 'force=True' flag was set, so this command WILL execute."
                )
            else:
                return (
                    f"WARNING: DANGEROUS COMMAND BLOCKED: {explanation}\n"
                    f"This command was NOT executed. If you are absolutely sure, "
                    f"set force=True to bypass this safety check."
                )
    return None



@_register("task_status")
def _task_status(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    task_id = args.get("task_id", "")
    if not task_id:
        return ToolResult(success=False, content="Missing task_id parameter.")
    proc = _TASK_REGISTRY.get(task_id)
    if proc is None:
        return ToolResult(success=True, content=f"Task {task_id} not found (may have completed or never existed).")
    returncode = proc.poll()
    if returncode is None:
        return ToolResult(success=True, content=f"Task {task_id}: still running.")
    # Clean up completed tasks from the registry (may already be removed by auto-cleanup)
    _TASK_REGISTRY.pop(task_id, None)
    # Try to retrieve persisted test output for background runs
    output_msg = ""
    try:
        from memory.memory import MemoryStore
        import os as _os
        mem = MemoryStore(_os.path.join(_os.getcwd(), ".mini_agent_memory.db"), max_messages=500)
        test_out = mem.get_test_output()
        if test_out and test_out.strip():
            lines = test_out.split("\n")
            if len(lines) > 100:
                test_out = "\n".join(lines[:100]) + f"\n... (truncated - {len(lines)} total lines)"
            output_msg = f"\n\n--- Test Output ---\n{test_out}"
    except Exception:
        pass
    return ToolResult(success=True, content=f"Task {task_id}: completed with exit_code={returncode}.{output_msg}")


@_summarize("task_status")
def _task_status_summary(args: dict) -> str:
    return f"task_status({args.get('task_id', '?')})"


@_register("run_shell")
def _run_shell(args: dict, _wg: WriteSafetyGate, rg: ReadSafetyGate, on_output: callable = None) -> ToolResult:
    import sys as _sys
    import time as _time
    command = args["command"]
    force = args.get("force", False)
    timeout = min(int(args.get("timeout", 60)), 300)
    stdin_text = args.get("stdin", None)  # optional stdin to pipe to the process
    _sys.stderr.write(f"[shell] _run_shell start: {command[:100]}\n")
    _sys.stderr.flush()
    _t_start = _time.monotonic()
    # ACI upgrade: check for dangerous commands before executing
    danger_warning = _check_dangerous_command(command, force)
    if danger_warning and not force:
        return ToolResult(success=False, content=danger_warning)
    # If force=True, retain the warning to prepend to final output
    _danger_prefix = danger_warning + "\n\n" if danger_warning else ""
    # Windows: note when bash is unavailable (cmd.exe has different pipe/redirect syntax)
    _windows_cmd_note = ""
    if platform.system() == "Windows" and not _is_bash_available() and not force:
        _windows_cmd_note = (
            "\nNote: Running on Windows cmd.exe. "
            "Some shell commands (pipes, redirects, etc.) may behave differently than on Unix."
        )
    # Auto-backup files before any rm command (prevents permanent data loss)
    if force and re.search(r'\brm\b', command):
        from tools.file_ops import _backup_before_write
        import shlex
        try:
            tokens = shlex.split(command)
            idx = 1  # skip 'rm'
            while idx < len(tokens):
                token = tokens[idx]
                if token == '-r' or token == '-rf' or token == '-f':
                    idx += 1
                    continue
                if not token.startswith('-'):
                    _backup_before_write(token)
                idx += 1
        except Exception:
            pass  # best-effort, don't block the rm
    try:
        # Commands that need a real terminal (sudo, ssh, etc.) get spawned
        # in a separate terminal window so the user can interact with them.
        _INTERACTIVE_PATTERNS = [
            r"\bsudo\b", r"\bssh\b", r"\bsu\b", r"\bpasswd\b",
            r"\blogin\b", r"\bhg\s+commit\b", r"\bpkexec\b",
            r"\bgit\s+push\b", r"\bgit\s+pull\b", r"\bgit\s+clone\b",
        ]
        _interactive = (
            not args.get("background", False)
            and stdin_text is None
            and not platform.system() == "Windows"
            and any(re.search(p, command) for p in _INTERACTIVE_PATTERNS)
        )
        if _interactive:
            import shutil
            term = (shutil.which("xterm") or shutil.which("gnome-terminal")
                    or shutil.which("konsole") or shutil.which("kitty"))
            xte = shutil.which("x-terminal-emulator")
            if term is None and xte:
                if os.path.islink(xte):
                    link = os.readlink(xte)
                    if "cosmic" not in link.lower():
                        term = xte
                else:
                    term = xte
            if term:
                wrap = command + '; echo; read -p "[Enter to close]"'
                term_cmd = [term]
                if "gnome-terminal" in term:
                    term_cmd += ["--", "bash", "-c", wrap]
                elif "konsole" in term:
                    term_cmd += ["-e", "bash", "-c", wrap]
                elif "kitty" in term:
                    term_cmd += ["bash", "-c", wrap]
                else:
                    term_cmd += ["-e", "bash", "-c", wrap]
                proc = subprocess.run(term_cmd, cwd=rg.workspace_root,
                                      timeout=timeout,
                                      **(_WINDOWS_POPEN_KWARGS if _WINDOWS else {}))
                return ToolResult(success=(proc.returncode == 0),
                                  content=f"exit_code={proc.returncode}")

        # Default to DEVNULL so interactive prompts don't hang the TUI.
        stdin_kw = {"stdin": subprocess.DEVNULL}
        if stdin_text is not None:
            stdin_kw["stdin"] = subprocess.PIPE

        # On Windows, prevent console windows from flashing
        popen_kwargs = {}
        if _WINDOWS:
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        # Always use shell=True: cmd.exe on Windows, /bin/sh on Unix.
        # No bash wrapping -- it creates a fragile cmd->bash->command chain
        # and can cause process explosions when quoting is mishandled.
        proc = subprocess.Popen(
            command, shell=True,
            cwd=rg.workspace_root,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            **stdin_kw, **popen_kwargs,
        )

        _register_proc(proc)
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        background = args.get("background", False)
        if background:
            task_id = str(uuid.uuid4())[:8]
            _TASK_REGISTRY[task_id] = proc
            threading.Thread(target=_stream_reader, args=(proc.stdout, []), daemon=True).start()
            threading.Thread(target=_stream_reader, args=(proc.stderr, []), daemon=True).start()
            if stdin_text is not None and proc.stdin is not None:
                threading.Thread(target=lambda p, t: (p.stdin.write(t), p.stdin.close()),
                                 args=(proc, stdin_text), daemon=True).start()
            def _auto_cleanup(reg, tid, p):
                try:
                    if _WINDOWS:
                        p.stdout.close(); p.stderr.close()
                    p.wait()
                except (OSError, subprocess.SubprocessError):
                    pass
                finally:
                    reg.pop(tid, None)
                    _unregister_proc(p)
            threading.Thread(target=_auto_cleanup, args=(_TASK_REGISTRY, task_id, proc), daemon=True).start()
            return ToolResult(success=True,
                              content=f"Started background task {task_id}. Use task_status to check.")

        if on_output is not None:
            if stdin_text is not None and proc.stdin is not None:
                proc.stdin.write(stdin_text)
                proc.stdin.close()
            t_out = threading.Thread(target=_stream_reader,
                                     args=(proc.stdout, stdout_lines, True, on_output, ""), daemon=True)
            t_err = threading.Thread(target=_stream_reader,
                                     args=(proc.stderr, stderr_lines, True, on_output, "[stderr] "), daemon=True)
            t_out.start()
            t_err.start()

            from tools import _TOOL_CONTEXT
            _TOOL_CONTEXT._active_proc = proc

            if _WINDOWS:
                kill_fired = threading.Event()
                def _kill_timer():
                    kill_fired.set()
                    _kill_process_tree_windows(proc)
                timer = threading.Timer(timeout, _kill_timer)
                timer.daemon = True; timer.start()
                try:
                    # Poll t_out.join with short intervals so we can detect
                    # cancellation (via the module-level _CURRENT_CANCEL_EVENT).
                    # A single t_out.join(timeout=70) blocks the tool thread
                    # and prevents cancellation from working.
                    _poll_deadline = _time.monotonic() + timeout + 10
                    while t_out.is_alive() and _time.monotonic() < _poll_deadline:
                        # Check module-level cancel event
                        try:
                            from tools import _CURRENT_CANCEL_EVENT as _cce
                            if _cce is not None and _cce.is_set():
                                kill_fired.set()
                                _kill_process_tree_windows(proc)
                                break
                        except ImportError:
                            pass
                        t_out.join(timeout=0.1)
                    else:
                        # Normal completion or timeout -- join t_err briefly
                        t_err.join(timeout=5)
                finally:
                    timer.cancel()
                if kill_fired.is_set():
                    _unregister_proc(proc)
                    _TOOL_CONTEXT._active_proc = None
                    return ToolResult(success=False,
                                      content=f"Command timed out after {timeout}s (process tree killed)")
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            else:
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    t_out.join(timeout=2)
                    t_err.join(timeout=2)
                    _unregister_proc(proc)
                    _TOOL_CONTEXT._active_proc = None
                    return ToolResult(success=False, content=f"Command timed out after {timeout}s")
                finally:
                    _TOOL_CONTEXT._active_proc = None

            t_out.join(timeout=2)
            t_err.join(timeout=2)
            _TOOL_CONTEXT._active_proc = None
            stdout = "\n".join(stdout_lines)
            stderr = "\n".join(stderr_lines)
        else:
            from tools import _TOOL_CONTEXT
            _TOOL_CONTEXT._active_proc = proc

            if stdin_text is not None and proc.stdin is not None:
                proc.stdin.write(stdin_text)
                proc.stdin.close()

            if _WINDOWS:
                try:
                    stdout, stderr = _communicate_windows(proc, timeout)
                except subprocess.TimeoutExpired as exc:
                    _unregister_proc(proc)
                    _TOOL_CONTEXT._active_proc = None
                    partial = getattr(exc, "output", None) or ""
                    if partial:
                        return ToolResult(success=False,
                                          content=f"Command timed out after {timeout}s\n\n{partial}")
                    return ToolResult(success=False, content=f"Command timed out after {timeout}s")
            else:
                try:
                    out, err = proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out, err = proc.communicate()
                    _unregister_proc(proc)
                    _TOOL_CONTEXT._active_proc = None
                    return ToolResult(success=False, content=f"Command timed out after {timeout}s")
                stdout = out
                stderr = err

            _TOOL_CONTEXT._active_proc = None

        parts = [f"exit_code={proc.returncode}"]
        if stdout:
            lines_out = stdout.split("\n")
            if len(lines_out) > 500:
                stdout = "\n".join(lines_out[:500])
                stdout += f"\n... (truncated at 500 lines -- {len(lines_out)} total. "
                stdout += "Use read_file with offset/limit for the full log if needed.)"
            parts.append(f"stdout:\n{stdout}")
        elif proc.returncode == 0 and not stderr:
            # ACI upgrade: explicit empty-output message (SWE-agent pattern).
            # Silence is ambiguous -- the model needs to know the command ran OK.
            hint = "Command completed successfully (no output)."
            # Detect likely no-op patterns in python -c commands
            if "python" in command and " -c " in command:
                if "#" in command:
                    hint += " Hint: '#' in python -c comments out the rest of the line. Use ';' separators instead of comments, or use a multi-line script."
                elif any(kw in command for kw in (" if ", " try:", " for ", " while ", " with ", " def ", " class ")):
                    hint += " Hint: Compound statements (if/try/for/while/with/def/class) cannot follow ';' in python -c. Use newlines in a script instead."
            parts.append(hint)
        if stderr:
            err_output = stderr.rstrip()
            err_lines = err_output.split("\n")
            if len(err_lines) > 100:
                err_output = "\n".join(err_lines[:100])
                err_output += f"\n... (stderr truncated at 100 lines -- {len(err_lines)} total)"
            parts.append(f"stderr:\n{err_output}")
        content_out = "\n".join(parts)
        if _danger_prefix:
            content_out = _danger_prefix + content_out
        if _windows_cmd_note:
            content_out += _windows_cmd_note
        if proc.returncode == 127:
            content_out += "\nHint: Command not found. Check the spelling and that it is installed."
        _unregister_proc(proc)
        return ToolResult(success=proc.returncode == 0, content=content_out)
    except Exception as e:
        hint = "\nHint: Check the command and flag spelling. Try with --help first, or use search_files to find the right syntax."
        return ToolResult(success=False, content=f"Error running command: {e}{hint}{_windows_cmd_note}")


@_summarize("run_shell")
def _run_shell_summary(args: dict) -> str:
    cmd = args.get("command", "?")
    preview = cmd[:80]
    if len(cmd) > 80:
        preview += "..."
    force = args.get("force", False)
    if force:
        return f"run_shell[force] ({preview})"
    return f"run_shell({preview})"


# ---------------------------------------------------------------------------
# search_files
# ---------------------------------------------------------------------------

_SKIP_DIRS = {".git", ".hg", ".svn", "__pycache__", ".pytest_cache",
              "venv", ".venv", "node_modules", ".mypy_cache", ".tox",
              "dist", "build", ".eggs"}

# Binary / non-text extensions to skip during search
_BINARY_EXTS = {".pyc", ".pyo", ".so", ".o", ".a", ".dylib", ".dll",
                ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg",
                ".mp3", ".mp4", ".avi", ".mov", ".mkv", ".webm",
                ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
                ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                ".ttf", ".otf", ".woff", ".woff2", ".eot",
                ".db", ".sqlite", ".sqlite3", ".mdb",
                ".exe", ".bin", ".dat", ".pkl", ".pickle",
                # SQLite auxiliary files -- os.path.splitext splits on last dot
                "-wal", "-shm", "-journal",
                # Coverage / profiling binary files
                ".coverage",
                ".prof", ".gcda", ".gcno",
                # macOS resource forks
                ".rsrc",
                # No extension -- catches files like .DS_Store, .coverage (no dot variant)
                ".ds_store"}


def _is_binary_file(filepath: str) -> bool:
    """Check if a file is binary by reading the first 512 bytes.

    Returns True if the file contains null bytes or is unreadable.
    Used as a fast pre-filter in search_files to avoid reading
    SQLite WALs, coverage DBs, and other binary files that slip
    past extension-based filtering.
    """
    try:
        with open(filepath, "rb") as f:
            chunk = f.read(512)
        # Null byte in first 512 bytes -> binary (covers SQLite, ELF, Mach-O, etc.)
        return b"\x00" in chunk
    except (OSError, PermissionError):
        return True  # Can't read -> treat as binary, skip it


_SEARCH_MAX_RESULTS = 200


def _search_single_file(
    filepath: str, pattern: str, use_regex: bool, ignore_case: bool,
    offset: int = 0,
) -> ToolResult:
    """Search for pattern in a single file.  Used by _search_files(file_path=...)."""
    import re as _re
    if use_regex:
        flags = _re.IGNORECASE if ignore_case else 0
        try:
            compiled = _re.compile(pattern, flags)
        except _re.error as e:
            return ToolResult(success=False, content=f"Invalid regex: {e}")
        match_fn = lambda line: compiled.search(line) is not None
    elif ignore_case:
        lower_pattern = pattern.lower()
        match_fn = lambda line: lower_pattern in line.lower()
    else:
        match_fn = lambda line: pattern in line

    results: list[str] = []
    skipped = 0
    try:
        with open(filepath, "r", errors="replace", encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                if match_fn(line):
                    if skipped < offset:
                        skipped += 1
                        continue
                    results.append(f"{filepath}:{lineno}: {line.rstrip()}")
                    if len(results) >= _SEARCH_MAX_RESULTS:
                        break
    except (OSError, PermissionError) as e:
        return ToolResult(success=False, content=f"Error reading '{filepath}': {e}")

    if not results:
        msg = f"No matches for '{pattern}' in {filepath}"
        if offset:
            msg += f" (offset={offset})"
        return ToolResult(success=True, content=msg)
    return ToolResult(success=True, content="\n".join(results))


def _search_with_rg(root_dir: str, pattern: str, use_regex: bool, ignore_case: bool, offset: int) -> ToolResult:
    """Run ripgrep for fast file search, falling back to Python on failure."""
    import subprocess
    cmd = ["rg", "--no-heading", "--with-filename", "--line-number",
           "--max-count", str(_SEARCH_MAX_RESULTS + offset)]
    if not use_regex:
        cmd.append("--fixed-strings")
    if ignore_case:
        cmd.append("--ignore-case")
    cmd.extend(["--", pattern, root_dir])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                                **(_WINDOWS_POPEN_KWARGS if _WINDOWS else {}))
        # Check for rg errors (regex parse errors, etc.)
        if result.returncode != 0 and result.stderr.strip():
            err = result.stderr.strip().split("\n")[0]
            return ToolResult(success=False, content=f"Invalid regex: {err}")
        lines = result.stdout.splitlines()
        if not lines:
            return ToolResult(success=True, content=f"No matches for '{pattern}' in {root_dir}")
        if offset > 0:
            lines = lines[offset:]
        output = "\n".join(lines[:_SEARCH_MAX_RESULTS])
        if len(lines) > _SEARCH_MAX_RESULTS:
            output += f"\n... (showing first {_SEARCH_MAX_RESULTS} results. "
            output += "There may be more matches. Narrow your search with a more specific "
            output += "pattern, a subdirectory path, or use find_symbol for symbol lookups.)"
        return ToolResult(success=True, content=output)
    except (subprocess.TimeoutExpired, Exception):
        return ToolResult(success=False, content="rg search failed or timed out")


@_register("search_files")
def _search_files(args: dict, _wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:
    pattern = args["pattern"]
    path = args.get("path", ".")
    file_path = args.get("file_path", "")
    use_regex = args.get("regex", False)
    ignore_case = args.get("ignore_case", False)
    offset = max(0, int(args.get("offset", 0)))

    if file_path:
        # Single-file mode: skip the directory safety check, only validate the file
        file_safety = rg.check(file_path)
        if not file_safety.allowed:
            return ToolResult(
                success=False,
                content=f"Search blocked by safety layer: {file_safety.reason}",
            )
        resolved = file_safety.resolved_path
        if not os.path.isfile(resolved):
            return ToolResult(success=False, content=f"Not a file: {resolved}")
        return _search_single_file(resolved, pattern, use_regex, ignore_case, offset=offset)

    # Directory search mode: safety-check the search path
    safety_result = rg.check(path)
    if not safety_result.allowed:
        return ToolResult(
            success=False,
            content=f"Search blocked by safety layer: {safety_result.reason}",
        )

    # --- Ripgrep fast path: use rg if available ---
    import shutil as _shutil
    if _shutil.which("rg") and not file_path:
        return _search_with_rg(safety_result.resolved_path, pattern, use_regex, ignore_case, offset)

    if use_regex:
        import re
        flags = re.IGNORECASE if ignore_case else 0
        try:
            compiled = re.compile(pattern, flags)
        except re.error as e:
            return ToolResult(success=False, content=f"Invalid regex: {e}")
        match_fn = lambda line: compiled.search(line) is not None
    elif ignore_case:
        lower_pattern = pattern.lower()
        match_fn = lambda line: lower_pattern in line.lower()
    else:
        match_fn = lambda line: pattern in line

    results: list[str] = []
    skipped = 0
    file_count = 0
    try:
        for root, dirs, files in os.walk(safety_result.resolved_path):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
            for fname in sorted(files):
                # Skip known binary extensions
                ext = os.path.splitext(fname)[1].lower()
                if ext in _BINARY_EXTS:
                    continue
                file_count += 1
                if file_count % 500 == 0:
                    # Periodic yield -- prevents long-running searches from
                    # appearing hung, but the walk always completes.
                    pass
                fpath = os.path.join(root, fname)
                # Skip binary files (null bytes in first 512 bytes)
                if _is_binary_file(fpath):
                    continue
                try:
                    with open(fpath, "r", errors="replace", encoding="utf-8") as f:
                        for lineno, line in enumerate(f, 1):
                            if match_fn(line):
                                if skipped < offset:
                                    skipped += 1
                                    continue
                                results.append(f"{fpath}:{lineno}: {line.rstrip()}")
                                if len(results) >= _SEARCH_MAX_RESULTS:
                                    break
                except (OSError, PermissionError):
                    continue
                if len(results) >= _SEARCH_MAX_RESULTS:
                    break
            if len(results) >= _SEARCH_MAX_RESULTS:
                break
    except Exception as e:
        return ToolResult(success=False, content=f"Error searching: {e}")

    if not results:
        msg = f"No matches for '{pattern}' in {safety_result.resolved_path}"
        if offset:
            msg += f" (offset={offset})"
        return ToolResult(success=True, content=msg)
    output = "\n".join(results)
    if len(results) >= _SEARCH_MAX_RESULTS:
        output += f"\n... (capped at {_SEARCH_MAX_RESULTS} results)"
    return ToolResult(success=True, content=output)


@_summarize("search_files")
def _search_files_summary(args: dict) -> str:
    pattern = args.get("pattern", "?")
    p = args.get("path", ".")
    return f"search_files('{pattern}', {p})"


# ---------------------------------------------------------------------------
# run_tests
# ---------------------------------------------------------------------------

@_register("run_tests")
def _run_tests(args: dict, _wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:
    from tools import _TOOL_CONTEXT
    if getattr(_TOOL_CONTEXT, '_agent_depth', 0) > 0:
        return ToolResult(success=False, content="run_tests is restricted to the orchestrator. Sub-agents must not run tests.")
    target = args.get("path", "").strip()
    background = args.get("background", False)
    timeout = args.get("timeout", 120)

    global _PYTHON_CMD
    _retried = False

    def _build_cmd() -> list[str]:
        cmd = _get_python_cmd() + ["-m", "pytest", "-q", "--ignore=venv", "--ignore=eval", "--ignore=tests"]
        if target:
            cmd.append(target)
        return cmd

    cmd = _build_cmd()

    def _spawn(cmd):
        # On Windows, use shell=True (same as _run_shell) to avoid pipe-EOF
        # detection issues with CreateProcess and _communicate_windows.
        # On Unix, keep the list form (shell=False) for reliability.
        if _WINDOWS:
            cmd_str = subprocess.list2cmdline(cmd)
            try:
                return subprocess.Popen(
                    cmd_str, shell=True, cwd=rg.workspace_root,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except Exception as e:
                return ToolResult(success=False, content=f"Error starting pytest: {e}")
        else:
            try:
                return subprocess.Popen(
                    cmd, cwd=rg.workspace_root,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
            except Exception as e:
                return ToolResult(success=False, content=f"Error starting pytest: {e}")

    proc_or_err = _spawn(cmd)
    if isinstance(proc_or_err, ToolResult):
        return proc_or_err
    proc = proc_or_err

    if background:
        task_id = str(uuid.uuid4())[:8]
        _TASK_REGISTRY[task_id] = proc
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        threading.Thread(target=_stream_reader, args=(proc.stdout, stdout_lines), daemon=True).start()
        threading.Thread(target=_stream_reader, args=(proc.stderr, stderr_lines), daemon=True).start()
        def _persist_when_done():
            proc.wait()
            output = "".join(stdout_lines)
            if stderr_lines:
                output += "\n[stderr]\n" + "".join(stderr_lines)
            _persist_test_output(output)
            _TASK_REGISTRY.pop(task_id, None)  # auto-cleanup on completion
        threading.Thread(target=_persist_when_done, daemon=True).start()
        return ToolResult(
            success=True,
            content=f"Started background test run {task_id}. Use task_status to check.",
        )

    try:
        if _WINDOWS:
            out, err = _communicate_windows(proc, timeout)
        else:
            out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if not _WINDOWS:
            proc.kill()
            out, err = proc.communicate()
        else:
            out = getattr(exc, "output", None) or ""
            err = getattr(exc, "stderr", None) or ""
        output = (out + err).strip()
        if output:
            return ToolResult(success=False,
                              content=f"Tests timed out after {timeout}s\n\n{output}")
        return ToolResult(success=False, content=f"Tests timed out after {timeout}s")

    output = (out + err).strip()

    # Retry once if pytest module not found — the cached python may have
    # lost its pytest install (e.g. venv was rebuilt or PATH changed).
    if not _retried and "No module named pytest" in output:
        _retried = True
        _PYTHON_CMD = []  # invalidate cache, force re-scan
        cmd = _build_cmd()
        proc_or_err = _spawn(cmd)
        if isinstance(proc_or_err, ToolResult):
            return proc_or_err
        proc = proc_or_err
        try:
            if _WINDOWS:
                out, err = _communicate_windows(proc, timeout)
            else:
                out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, content=f"Tests timed out after {timeout}s")
        output = (out + err).strip()

    _persist_test_output(output)
    summary, success = _parse_pytest_output(output, proc.returncode)
    return ToolResult(success=success, content=summary)


@_summarize("run_tests")
def _run_tests_summary(args: dict) -> str:
    target = args.get("path", "").strip()
    if target:
        return f"run_tests({target})"
    return "run_tests(all)"


# ---------------------------------------------------------------------------
# verify -- lint + run tests for recently modified files
# ---------------------------------------------------------------------------

@_register("verify")
def _verify(args: dict, _wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:
    """Run lint + relevant tests for files modified this session.

    Uses _MODIFIED_FILES tracked by write_file and edit_file to determine
    which test files to run.  Falls back to running all tests if nothing
    has been modified yet.
    """
    from tools import _TOOL_CONTEXT
    if getattr(_TOOL_CONTEXT, '_agent_depth', 0) > 0:
        return ToolResult(success=False, content="verify is restricted to the orchestrator. Sub-agents must not run tests.")
    import subprocess
    import os as _os
    root = rg.workspace_root

    results: list[str] = []

    # Step 0: dead import detection (ruff if available, else pyflakes)
    import shutil
    if shutil.which("ruff"):
        try:
            r = subprocess.run(
                ["ruff", "check", "--select", "F401,F811",
                 "--output-format", "concise", root],
                capture_output=True, text=True, timeout=10,
                **(_WINDOWS_POPEN_KWARGS if _WINDOWS else {}),
            )
            if r.returncode == 0 and not r.stdout.strip():
                results.append("ruff: no unused/redefined imports found")
            elif r.stdout.strip():
                out = r.stdout.strip()[:500]
                results.append(f"ruff found issues:\n{out}")
        except subprocess.TimeoutExpired:
            results.append("ruff: timed out")
    elif shutil.which("pyflakes"):
        try:
            r = subprocess.run(
                ["pyflakes", root],
                capture_output=True, text=True, timeout=10,
                **(_WINDOWS_POPEN_KWARGS if _WINDOWS else {}),
            )
            stdout = r.stdout
            if (r.returncode == 0
                    and "undefined" not in stdout
                    and "unused import" not in stdout):
                results.append("pyflakes: no dead imports found")
            elif stdout.strip():
                out = stdout.strip()[:500]
                results.append(f"pyflakes found issues:\n{out}")
        except subprocess.TimeoutExpired:
            results.append("pyflakes: timed out")

    # Step 1: tests for modified files
    from tools import get_modified_files
    test_targets: list[str] = []
    mod_files = get_modified_files()
    if mod_files:
        seen = set()
        for fpath in mod_files:
            base = _os.path.basename(fpath)
            if base.startswith("test_"):
                test_targets.append(base)
            else:
                name = _os.path.splitext(base)[0]
                candidates = [
                    f"test_{name}.py",
                    f"tests/test_{name}.py",
                    f"test/test_{name}.py",
                ]
                parent = _os.path.basename(_os.path.dirname(fpath))
                if parent and parent != root:
                    candidates.append(f"test_{parent}.py")
                    candidates.append(f"tests/test_{parent}.py")
                for candidate in candidates:
                    if candidate not in seen:
                        if _os.path.exists(_os.path.join(root, candidate)):
                            seen.add(candidate)
                            test_targets.append(candidate)
                            break

    if not test_targets:
        test_targets.append(".")

    # Run lint + all test targets in parallel
    jobs: list = []
    # Lint job
    lint_cmd = _get_python_cmd() + ["-m", "flake8", "--count", "--select=E,F,W",
                 "--exclude=.git,__pycache__,venv,.venv,.egg-info,node_modules,build,dist", "."]
    try:
        lint_proc = subprocess.Popen(
            lint_cmd, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            **(_WINDOWS_POPEN_KWARGS if _WINDOWS else {}),
        )
        jobs.append(("lint", lint_proc))
    except Exception as e:
        results.append(f"Lint: error ({e})")

    # Test jobs
    for target in test_targets:
        try:
            proc = subprocess.Popen(
                _get_python_cmd() + ["-m", "pytest", target, "-q"],
                cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                **(_WINDOWS_POPEN_KWARGS if _WINDOWS else {}),
            )
            jobs.append(("test", (target, proc)))
        except Exception as e:
            results.append(f"Tests ({target}): error ({e})")

    # Wait for all jobs (ordered: lint first, then tests)
    for kind, payload in jobs:
        if kind == "lint":
            proc = payload
            try:
                out, err = proc.communicate(timeout=10)
                out = (out or "").strip()
                err = (err or "").strip()
                if proc.returncode == 0:
                    results.append("Lint: passed")
                elif "No module named" in err or "No module named" in out:
                    results.append("Lint: skipped (flake8 not installed)")
                else:
                    last = out.split("\n")[-1] if out else err.split("\n")[-1] if err else "failed"
                    results.append(f"Lint: {last}")
            except subprocess.TimeoutExpired:
                proc.kill(); proc.communicate()
                results.append("Lint: timed out")
        else:
            target, proc = payload
            try:
                out, err = proc.communicate(timeout=120)
                out = (out + err).strip()
                # Persist to DB
                _persist_test_output(out)
                summary, _ = _parse_pytest_output(out, proc.returncode)
                results.append(f"Tests ({target}): {summary}")
            except subprocess.TimeoutExpired:
                proc.kill(); proc.communicate()
                results.append(f"Tests ({target}): timed out")

    # Step 3: modified files summary
    if get_modified_files():
        results.append(f"Modified files: {len(get_modified_files())} files")

    all_ok = all("failed" not in r.lower() for r in results if "Tests" in r)
    return ToolResult(
        success=all_ok,
        content="\n".join(results),
    )


@_summarize("verify")
def _verify_summary(args: dict) -> str:
    return "verify()"


# ---------------------------------------------------------------------------
# diagnose_failures -- parse last test output and summarize failures
# ---------------------------------------------------------------------------

_FAILED_LINE_RE = re.compile(r"FAILED\s+(.+?\.py)::(.+?)(?:\s+-|\s*$)")


@_register("diagnose_failures")
def _diagnose_failures(args: dict, _wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:
    """Read the last test output from MemoryStore, parse FAILED lines,
    extract test function names and file paths, and return a structured
    summary with relevant source snippets."""
    from tools import _TOOL_CONTEXT
    if getattr(_TOOL_CONTEXT, '_agent_depth', 0) > 0:
        return ToolResult(success=False, content="diagnose_failures is restricted to the orchestrator. Sub-agents must not run tests.")
    import os as _os

    # Build MemoryStore path -- same default used by _persist_test_output
    db_path = _os.path.join(rg.workspace_root, ".mini_agent_memory.db")
    try:
        from memory.memory import MemoryStore
        store = MemoryStore(db_path, max_messages=500)
        output = store.get_test_output()
    except Exception as e:
        return ToolResult(
            success=False,
            content=f"Could not read test output from memory store ({db_path}): {e}",
        )

    if not output or not output.strip():
        return ToolResult(
            success=True,
            content="No test output found in memory store. Run tests first with run_tests.",
        )

    lines = output.split("\n")

    # Find the summary line (e.g. "1 passed, 2 failed")
    summary_line = ""
    for line in reversed(lines):
        stripped = line.strip()
        if any(kw in stripped for kw in ("passed", "failed", "error")):
            summary_line = stripped
            break

    # Parse FAILED lines
    failures: list[dict] = []
    for line in lines:
        m = _FAILED_LINE_RE.search(line)
        if m:
            file_path = m.group(1)
            func_path = m.group(2)  # e.g. "TestEditFile::test_func_name" or just "test_func_name"
            # Split on "::" to handle class-qualified method names
            func_parts = func_path.split("::")
            func_name = func_parts[-1]  # Last part is the actual function/method name
            failures.append({
                "file": file_path,
                "function": func_name,
                "qualified_function": func_path,
                "line": line.strip(),
            })

    if not failures:
        # No FAILED lines -- check if there's useful output at all
        if summary_line:
            return ToolResult(success=True, content=f"No FAILED lines parsed. Summary: {summary_line}")
        return ToolResult(success=True, content="No FAILED lines found in test output.")

    # Read source snippets for each failure
    snippets: list[str] = []
    for f in failures:
        fpath = f["file"]
        func = f["function"]
        # Resolve relative to workspace
        resolved = _os.path.join(rg.workspace_root, fpath)
        if not _os.path.isfile(resolved):
            # Try just the basename in workspace root
            alt = _os.path.join(rg.workspace_root, _os.path.basename(fpath))
            if _os.path.isfile(alt):
                resolved = alt
            else:
                snippets.append(f"\n--- {fpath}::{func} (file not found) ---")
                snippets.append(f"  FAILED: {f['line']}")
                continue

        # Extract lines around the function definition
        try:
            with open(resolved, encoding="utf-8", errors="replace") as fh:
                src_lines = fh.readlines()
        except Exception as e:
            snippets.append(f"\n--- {fpath}::{func} (error reading: {e}) ---")
            continue

        # Determine if this is a class method (qualified_function contains "::")
        qualified = f.get("qualified_function", func)
        class_name = ""
        if "::" in qualified:
            class_name = qualified.rsplit("::", 1)[0]  # e.g. "TestEditFile"

        # Find function/class + method definition
        func_lines: list[str] = []
        in_func = False
        in_class = False
        brace_depth = 0
        start_line = 0
        for i, sl in enumerate(src_lines):
            # Track class entry if we need it
            if class_name and re.search(rf"^\s*(async\s+)?class\s+{re.escape(class_name)}\s*[(:]", sl):
                in_class = True

            # Match "def func_name" or "    def func_name" etc.
            if not in_func and re.search(rf"^\s*(async\s+)?def\s+{re.escape(func)}\s*\(", sl):
                in_func = True
                start_line = i + 1
                # Include class context line if we're inside a class
                if class_name and not any(c in func_lines for c in [class_name]):
                    pass  # We'll just show the method, class context is implicit
                func_lines.append(sl.rstrip())
                continue
            if in_func:
                stripped_sl = sl.rstrip("\n")
                indent = len(sl) - len(sl.lstrip())
                first_indent = len(func_lines[0]) - len(func_lines[0].lstrip())
                if (not stripped_sl.strip() or
                    (indent <= first_indent and stripped_sl.strip()
                     and not stripped_sl.startswith("@")
                     and not stripped_sl.startswith("#")
                     and not stripped_sl.startswith('"""')
                     and not stripped_sl.startswith("'''"))):
                    if len(func_lines) > 1:
                        break
                    if not stripped_sl.strip():
                        func_lines.append(stripped_sl)
                        continue
                    break
                func_lines.append(stripped_sl)
                if len(func_lines) > 60:
                    func_lines.append("... (truncated at 60 lines)")
                    break

        snippet = "\n".join(func_lines) if func_lines else "(function body not found)"
        file_basename = _os.path.basename(resolved)
        snippets.append(f"\n--- {file_basename}::{func} (line ~{start_line}) ---")
        snippets.append(snippet)

    result_parts = [f"Test output summary: {summary_line or 'unknown'}"]
    result_parts.append(f"Failed tests: {len(failures)}")
    result_parts.append("\n".join(snippets))
    content = "\n".join(result_parts)

    return ToolResult(success=True, content=content)


@_summarize("diagnose_failures")
def _diagnose_failures_summary(args: dict) -> str:
    return "diagnose_failures()"

_ACTIVE_PROCS: set = set()
_ACTIVE_PROCS_LOCK = threading.Lock()

def _register_proc(proc):
    with _ACTIVE_PROCS_LOCK: _ACTIVE_PROCS.add((proc.pid, proc))

def _unregister_proc(proc):
    with _ACTIVE_PROCS_LOCK: _ACTIVE_PROCS.discard((proc.pid, proc))

def _kill_process_tree_windows(proc):
    """Kill the process tree rooted at *proc* as aggressively as possible.

    Tries three approaches in order:
    1. ``taskkill /F /T`` -- kills the whole tree including children.
    2. ``proc.kill()`` -- TerminateProcess (parent only, but may cause
      children to exit when pipes break).
    3. ``taskkill`` with just /F (no /T) -- last resort.

    Timeouts are short (4 s for taskkill) to avoid this function itself
    hanging and leaking processes.
    """
    pid = proc.pid
    # Method 1: taskkill /F /T (kills entire tree)
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                      capture_output=True, timeout=4)
    except Exception:
        pass
    # Method 2: TerminateProcess on the parent (may leave orphans, but
    #            breaking stdout/stderr pipes often causes children to exit)
    try:
        proc.kill()
    except Exception:
        pass
    # Method 3: taskkill /F (just the parent, no /T)
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                      capture_output=True, timeout=4)
    except Exception:
        pass

def _cleanup_all_procs():
    with _ACTIVE_PROCS_LOCK:
        procs = list(_ACTIVE_PROCS)
        _ACTIVE_PROCS.clear()
    for _pid, proc in procs:
        try:
            if _WINDOWS:
                _kill_process_tree_windows(proc)
            else:
                proc.kill()
        except Exception:
            pass

def _communicate_windows(proc, timeout):
    import time as _time
    if not _WINDOWS:
        raise RuntimeError("_communicate_windows called on non-Windows")
    out_lines, err_lines = [], []
    t_out = threading.Thread(target=_stream_reader, args=(proc.stdout, out_lines), daemon=True)
    t_err = threading.Thread(target=_stream_reader, args=(proc.stderr, err_lines), daemon=True)
    t_out.start(); t_err.start()
    kill_fired = threading.Event()
    def _kill():
        kill_fired.set()
        _kill_process_tree_windows(proc)
    timer = threading.Timer(timeout, _kill)
    timer.daemon = True; timer.start()
    try:
        # Poll t_out.join with short intervals to detect cancellation.
        # A single t_out.join(timeout=timeout+10) blocks the tool thread
        # and prevents cancellation from working.
        _deadline = _time.monotonic() + timeout + 10
        while t_out.is_alive() and _time.monotonic() < _deadline:
            try:
                from tools import _CURRENT_CANCEL_EVENT as _cce
                if _cce is not None and _cce.is_set():
                    kill_fired.set()
                    _kill_process_tree_windows(proc)
                    break
            except ImportError:
                pass
            t_out.join(timeout=0.1)
        else:
            t_err.join(timeout=5)
    finally:
        timer.cancel()
    if kill_fired.is_set():
        # proc.args is a list when shell=False, a string when shell=True
        _args = proc.args
        if isinstance(_args, list):
            _cmd = " ".join(_args)
        else:
            _cmd = str(_args or "unknown")
        raise subprocess.TimeoutExpired(
            cmd=_cmd,
            timeout=timeout,
            output="\n".join(out_lines),
            stderr="\n".join(err_lines),
        )
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    return "\n".join(out_lines), "\n".join(err_lines)


