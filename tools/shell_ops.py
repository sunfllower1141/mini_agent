#!/usr/bin/env python3
"""
shell_ops.py — shell, search, test, and git tools for mini_agent.

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

from safety import ReadSafetyGate, WriteSafetyGate
from tools import _register, _summarize, ToolResult, _TASK_REGISTRY


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
    On Unix: tries 'python3', then 'python'.
    Results are memoised in _PYTHON_CMD.
    """
    global _PYTHON_CMD
    if _PYTHON_CMD:
        return _PYTHON_CMD
    if platform.system() == "Windows":
        candidates = [["py", "-3"], ["python3"], ["python"]]
    else:
        candidates = [["python3"], ["python"]]
    for cmd in candidates:
        if shutil.which(cmd[0]):
            _PYTHON_CMD = cmd
            return _PYTHON_CMD
    # Ultimate fallback
    _PYTHON_CMD = ["python"]
    return _PYTHON_CMD


def _persist_test_output(output: str) -> None:
    """Save test run output to the memory DB for later inspection."""
    from tools import _TOOL_CONTEXT, CTX_SCRATCHPAD_PATH
    db_path = _TOOL_CONTEXT.scratchpad_path
    if not db_path:
        return
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
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

_DESTRUCTIVE_PATTERNS = [
    r"\brm\b",             # remove
    r"\brmdir\b",          # remove directory
    r"\bdd\b",             # disk destroyer
    r"\bmkfs\b",           # make filesystem
    r"\bmkswap\b",         # make swap
    r"\bchmod\s+777\b",    # world-writable
    r"\bchown\b",          # change owner
    r">.*/dev/",            # write directly to device
    r"\bformat\b",         # format disk
    r"\bwiped\b",          # wipe
    r"\bwipefs\b",         # wipe filesystem
    r"\bparted\b",         # partition editor
    r"\bfdisk\b",          # partition table
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",  # fork bomb
    r">/dev/null\s*&&\s*rm\b",  # rm disguised after suppression
    # Windows destructive patterns
    r"\bdel\s+/[fF]\b",       # del /f (force delete)
    r"\bformat\b",            # format disk (also matches Unix, already above)
    r"\bdiskpart\b",          # Windows disk partition tool
    r"\brmdir\s+/[sS]\b",     # rmdir /s (recursive remove directory)
    r"\brd\s+/[sS]\b",        # rd /s (same as rmdir /s, shorthand)
    r"\breg\s+delete\b",      # registry key deletion
]


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
    # Clean up completed tasks from the registry
    del _TASK_REGISTRY[task_id]
    # Try to retrieve persisted test output for background runs
    output_msg = ""
    try:
        from memory import MemoryStore
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


def _check_destructive(command: str) -> str | None:
    """Safety guards removed — all commands always allowed."""
    return None


@_register("run_shell")
def _run_shell(args: dict, _wg: WriteSafetyGate, rg: ReadSafetyGate, on_output: callable = None) -> ToolResult:
    command = args["command"]
    force = args.get("force", False)
    timeout = min(int(args.get("timeout", 60)), 300)
    stdin_text = args.get("stdin", None)  # optional stdin to pipe to the process
    if not force:
        block = _check_destructive(command)
        if block is not None:
            return ToolResult(success=False, content=block)
    # Windows: prefer bash for safer, more compatible command execution
    _windows_cmd_note = ""
    if platform.system() == "Windows":
        if _is_bash_available():
            bash = _get_shell_command()[0]
            command = f'{bash} -c "{command}"'
        elif not force:
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
        stdin_kw = {}
        if stdin_text is not None:
            stdin_kw["stdin"] = subprocess.PIPE
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=rg.workspace_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **stdin_kw,
        )

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        # Background mode: register and return immediately
        background = args.get("background", False)
        if background:
            task_id = str(uuid.uuid4())[:8]
            _TASK_REGISTRY[task_id] = proc
            # Drain stdout/stderr in daemon threads to prevent pipe-buffer deadlock
            threading.Thread(target=_stream_reader, args=(proc.stdout, []), daemon=True).start()
            threading.Thread(target=_stream_reader, args=(proc.stderr, []), daemon=True).start()
            # Write stdin in a daemon thread to avoid blocking
            if stdin_text is not None and proc.stdin is not None:
                threading.Thread(target=lambda p, t: (p.stdin.write(t), p.stdin.close()), args=(proc, stdin_text), daemon=True).start()
            return ToolResult(
                success=True,
                content=f"Started background task {task_id}. Use task_status to check.",
            )

        if on_output is not None:
            # Streaming mode: need threads to forward output in real-time
            # Write stdin before starting reader threads to avoid race
            if stdin_text is not None and proc.stdin is not None:
                proc.stdin.write(stdin_text)
                proc.stdin.close()
            t_out = threading.Thread(
                target=_stream_reader, args=(proc.stdout, stdout_lines, True, on_output, ""), daemon=True,
            )
            t_err = threading.Thread(
                target=_stream_reader, args=(proc.stderr, stderr_lines, True, on_output, "[stderr] "), daemon=True,
            )
            t_out.start()
            t_err.start()

            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                t_out.join(timeout=2)
                t_err.join(timeout=2)
                return ToolResult(success=False, content=f"Command timed out after {timeout}s")

            t_out.join(timeout=2)
            t_err.join(timeout=2)

            stdout = "\n".join(stdout_lines)
            stderr = "\n".join(stderr_lines)
        else:
            # No streaming: use communicate() to avoid thread overhead
            try:
                out, err = proc.communicate(input=stdin_text, timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
                return ToolResult(success=False, content=f"Command timed out after {timeout}s")
            stdout = out
            stderr = err

        parts = [f"exit_code={proc.returncode}"]
        if stdout:
            lines = stdout.split("\n")
            if len(lines) > 500:
                stdout = "\n".join(lines[:500])
                stdout += f"\n… (truncated at 500 lines — {len(lines)} total. "
                stdout += "Use read_file with offset/limit for the full log if needed.)"
            parts.append(f"stdout:\n{stdout}")
        if stderr:
            err_output = stderr.rstrip()
            err_lines = err_output.split("\n")
            if len(err_lines) > 100:
                err_output = "\n".join(err_lines[:100])
                err_output += f"\n… (stderr truncated at 100 lines — {len(err_lines)} total)"
            parts.append(f"stderr:\n{err_output}")
        content = "\n".join(parts)
        if _windows_cmd_note:
            content += _windows_cmd_note
        if proc.returncode == 127:
            content += "\nHint: Command not found. Check the spelling and that it is installed."
        return ToolResult(
            success=proc.returncode == 0,
            content=content,
        )
    except Exception as e:
        hint = "\nHint: Check the command and flag spelling. Try with --help first, or use search_files to find the right syntax."
        return ToolResult(success=False, content=f"Error running command: {e}{hint}{_windows_cmd_note}")


@_summarize("run_shell")
def _run_shell_summary(args: dict) -> str:
    cmd = args.get("command", "?")
    preview = cmd[:80]
    if len(cmd) > 80:
        preview += "…"
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
                ".exe", ".bin", ".dat", ".pkl", ".pickle"}


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
        with open(filepath, "r", errors="replace") as f:
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
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        lines = result.stdout.splitlines()
        if not lines:
            return ToolResult(success=True, content=f"No matches for '{pattern}' in {root_dir}")
        if offset > 0:
            lines = lines[offset:]
        output = "\n".join(lines[:_SEARCH_MAX_RESULTS])
        if len(lines) > _SEARCH_MAX_RESULTS:
            output += f"\n\u2026 (capped at {_SEARCH_MAX_RESULTS} results)"
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
                    # Periodic yield — prevents long-running searches from
                    # appearing hung, but the walk always completes.
                    pass
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", errors="replace") as f:
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
        output += f"\n… (capped at {_SEARCH_MAX_RESULTS} results)"
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
    cmd = _get_python_cmd() + ["-m", "pytest", "-q"]
    if target:
        cmd.append(target)

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=rg.workspace_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception as e:
        return ToolResult(success=False, content=f"Error starting pytest: {e}")

    # Background mode: register and return immediately
    if background:
        task_id = str(uuid.uuid4())[:8]
        _TASK_REGISTRY[task_id] = proc
        # Drain stdout/stderr in daemon threads to prevent pipe-buffer deadlock
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        threading.Thread(target=_stream_reader, args=(proc.stdout, stdout_lines), daemon=True).start()
        threading.Thread(target=_stream_reader, args=(proc.stderr, stderr_lines), daemon=True).start()
        # Persist output after the process completes
        def _persist_when_done():
            proc.wait()
            output = "".join(stdout_lines)
            if stderr_lines:
                output += "\n[stderr]\n" + "".join(stderr_lines)
            _persist_test_output(output)
        threading.Thread(target=_persist_when_done, daemon=True).start()
        return ToolResult(
            success=True,
            content=f"Started background test run {task_id}. Use task_status to check.",
        )

    # Foreground: use communicate() to avoid thread overhead
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        return ToolResult(success=False, content=f"Tests timed out after {timeout}s")

    output = (out + err).strip()
    # Persist to DB so agent can read failures without re-running
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
# verify — lint + run tests for recently modified files
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
    import subprocess, os as _os
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
# git
# ---------------------------------------------------------------------------

_GIT_SAFE: set[str] = {"status", "diff", "log", "init", "add", "commit", "show", "restore"}


def _git_run(cwd: str, *args: str) -> tuple[int, str, str]:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode, result.stdout, result.stderr


@_register("git")
def _git(args: dict, _wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:
    sub = args["subcommand"]
    extra = args.get("args", "")

    if sub not in _GIT_SAFE:
        return ToolResult(
            success=False,
            content=f"Unknown or unsafe git subcommand: '{sub}'. "
                    f"Allowed: {', '.join(sorted(_GIT_SAFE))}",
        )

    cwd = rg.workspace_root

    if sub == "status":
        rc, out, err = _git_run(cwd, "status", "--short")
        if rc != 0:
            return ToolResult(success=False, content=err or out)
        if not out.strip():
            return ToolResult(success=True, content="Working tree clean.")
        return ToolResult(success=True, content=out.rstrip())

    elif sub == "diff":
        rc, out, err = _git_run(cwd, "diff")
        if rc != 0:
            return ToolResult(success=False, content=err or out)
        if not out.strip():
            return ToolResult(success=True, content="No unstaged changes.")
        return ToolResult(success=True, content=out.rstrip())

    elif sub == "log":
        rc, out, err = _git_run(
            cwd, "log", "--oneline", "-n", "20", "--decorate",
        )
        if rc != 0 and "does not have any commits" not in err:
            return ToolResult(success=False, content=err or out)
        if not out.strip():
            return ToolResult(success=True, content="No commits yet.")
        return ToolResult(success=True, content=out.rstrip())

    elif sub == "init":
        rc, out, err = _git_run(cwd, "init")
        if rc != 0:
            return ToolResult(success=False, content=err or out)
        return ToolResult(success=True, content=out.strip() or "Repository initialized.")

    elif sub == "add":
        paths = extra.strip() if extra.strip() else "."
        rc, out, err = _git_run(cwd, "add", *paths.split())
        if rc != 0:
            return ToolResult(success=False, content=err or out)
        return ToolResult(success=True, content=f"Staged: {paths}")

    elif sub == "commit":
        if not extra.strip():
            return ToolResult(success=False, content="Commit requires a message in 'args'.")
        rc, out, err = _git_run(cwd, "commit", "-m", extra.strip())
        if rc != 0:
            return ToolResult(success=False, content=err or out)
        return ToolResult(success=True, content=out.strip() or "Committed.")

    elif sub == "show":
        if not extra.strip():
            return ToolResult(success=False, content="'show' requires a file path in 'args'.")
        rc, out, err = _git_run(cwd, "show", f"HEAD:{extra.strip()}")
        if rc != 0:
            return ToolResult(success=False, content=err or out)
        return ToolResult(success=True, content=out)

    elif sub == "restore":
        if not extra.strip():
            extra = "."
        restoring = extra.strip()
        rc, changed, _ = _git_run(cwd, "diff", "--name-only", "HEAD")
        if rc != 0:
            return ToolResult(success=False, content=changed or "Unable to list changed files.")
        files = changed.strip()
        rc, out, err = _git_run(cwd, "restore", *restoring.split())
        if rc != 0:
            return ToolResult(success=False, content=err or out)
        if files:
            return ToolResult(success=True, content=f"Restored: {files}")
        return ToolResult(success=True, content="Restored (no changes to revert).")


@_summarize("git")
def _git_summary(args: dict) -> str:
    sub = args.get("subcommand", "?")
    extra = args.get("args", "")
    if extra:
        return f"git {sub} {extra}"
    return f"git {sub}"


# ---------------------------------------------------------------------------
# diagnose_failures — parse last test output and summarize failures
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

    # Build MemoryStore path — same default used by _persist_test_output
    db_path = _os.path.join(rg.workspace_root, ".mini_agent_memory.db")
    try:
        from memory import MemoryStore
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
        # No FAILED lines — check if there's useful output at all
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
            with open(resolved) as fh:
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
