#!/usr/bin/env python3
"""
git_ops.py -- Windows-safe git tool wrappers for mini_agent.

Provides git_status, git_diff, git_log, git_add, git_commit tools that
wrap subprocess.run() with capture_output, text=True, and tight timeouts
to prevent hangs on Windows 11 (credential prompts, antivirus scanning,
pipe buffer deadlocks).

All tools are registered via @_register and activated via use_skill('git').
"""
from __future__ import annotations

import os
import platform
import subprocess as _sp
from typing import Any

from tools.result import ToolResult

# We use a delayed import of _register to avoid circular imports at module
# level.  The actual registration hooks run when tools/__init__.py imports
# this module (after _TOOL_DISPATCH is initialized).
_register = None  # type: Any


def _init_register():
    """Lazy-bind _register to avoid import-time circular dependency."""
    global _register
    if _register is None:
        from tools import _TOOL_DISPATCH, _TOOL_SUMMARIES

        def _reg(name: str):
            def decorator(fn):
                _TOOL_DISPATCH[name] = fn
                return fn

            return decorator

        def _sum(name: str):
            def decorator(fn):
                _TOOL_SUMMARIES[name] = fn
                return fn

            return decorator

        # Module-level registrations
        _register = _reg
        _register_summarize = _sum
        _do_register(_reg, _sum)


def _do_register(reg, sum_fn):
    """Register all git tools. Called once after lazy init."""

    @reg("git_status")
    def _git_status(
        args: dict, _wg: Any, _rg: Any
    ) -> ToolResult:
        """Run git status --short."""
        return _run_git(["--no-optional-locks", "status", "--short"], _rg)

    @sum_fn("git_status")
    def _git_status_summary(_args: dict) -> str:
        return "git_status()"

    @reg("git_diff")
    def _git_diff(
        args: dict, _wg: Any, _rg: Any
    ) -> ToolResult:
        """Run git diff with optional --staged flag."""
        staged = args.get("staged", False)
        cmd = ["diff"]
        if staged:
            cmd.append("--staged")
        file_path = args.get("path", "")
        if file_path:
            cmd.extend(["--", file_path])
        return _run_git(cmd, _rg)

    @sum_fn("git_diff")
    def _git_diff_summary(args: dict) -> str:
        staged = "staged " if args.get("staged") else ""
        path = args.get("path", "")
        return f"git_diff({staged}{path})"

    @reg("git_log")
    def _git_log(
        args: dict, _wg: Any, _rg: Any
    ) -> ToolResult:
        """Run git log --oneline."""
        n = min(int(args.get("n", 10)), 50)  # cap at 50
        cmd = ["log", "--oneline", f"-{n}"]
        return _run_git(cmd, _rg)

    @sum_fn("git_log")
    def _git_log_summary(args: dict) -> str:
        return f"git_log(n={args.get('n', 10)})"

    @reg("git_add")
    def _git_add(
        args: dict, _wg: Any, _rg: Any
    ) -> ToolResult:
        """Stage files for commit. Use paths='all' for git add -A, or specific paths."""
        paths = args.get("paths", "")
        if not paths:
            return ToolResult(
                success=False,
                content="Missing required 'paths' parameter. Use paths='all' for all, "
                "or a specific file path.",
            )
        if paths == "all":
            cmd = ["add", "-A"]
        else:
            # Support comma-separated or single path
            path_list = [p.strip() for p in str(paths).split(",") if p.strip()]
            if not path_list:
                return ToolResult(
                    success=False, content="No valid paths specified."
                )
            cmd = ["add"] + path_list
        return _run_git(cmd, _rg)

    @sum_fn("git_add")
    def _git_add_summary(args: dict) -> str:
        return f"git_add(paths={str(args.get('paths', ''))[:40]})"

    @reg("git_commit")
    def _git_commit(
        args: dict, _wg: Any, _rg: Any
    ) -> ToolResult:
        """Commit staged changes with a message."""
        message = str(args.get("message", "")).strip()
        if not message:
            return ToolResult(
                success=False,
                content="Missing required 'message' parameter for git_commit.",
            )
        # Escape quotes in message
        cmd = ["commit", "-m", message]
        return _run_git(cmd, _rg)

    @sum_fn("git_commit")
    def _git_commit_summary(args: dict) -> str:
        msg = str(args.get("message", ""))[:40]
        return f"git_commit(message='{msg}')"


# ---------------------------------------------------------------------------
# Core git wrapper -- safe on Windows
# ---------------------------------------------------------------------------

_GIT_TIMEOUT = 15  # seconds -- generous for most operations, prevents hangs
_WINDOWS = platform.system() == "Windows"


def _get_workspace(rg: Any) -> str:
    """Extract workspace path from read gate or fall back to cwd."""
    if rg is not None:
        ws = getattr(rg, "workspace_root", "")
        if ws:
            return ws
    return os.getcwd()


def _run_git(args_list: list[str], rg: Any) -> ToolResult:
    """Run a git command safely and return a ToolResult.

    On Windows, git can hang due to:
    - Credential Manager popups (https auth)
    - Antivirus scanning of git objects
    - Pipe buffer deadlocks with capture_output on large repos

    This wrapper uses:
    - subprocess.run with capture_output + text + timeout
    - GIT_TERMINAL_PROMPT=0 to suppress credential prompts
    - Explicit timeout to prevent hangs
    - Graceful error handling (returns failure instead of raising)
    """
    workspace = _get_workspace(rg)
    env = os.environ.copy()
    # Windows 11: Git Credential Manager (GCM) ignores GIT_TERMINAL_PROMPT.
    # GCM_INTERACTIVE=never + GIT_ASKPASS=echo prevent GUI prompts from hanging.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env["GIT_ASKPASS"] = "echo"

    # On Windows, CREATE_NO_WINDOW prevents a console window flash on each call
    popen_kwargs: dict = {}
    if _WINDOWS:
        popen_kwargs["creationflags"] = _sp.CREATE_NO_WINDOW

    try:
        r = _sp.run(
            ["git", "-C", workspace] + args_list,
            capture_output=True,
            text=True,
            encoding="utf-8", errors="replace",
            timeout=_GIT_TIMEOUT,
            env=env,
            stdin=_sp.DEVNULL,
            **popen_kwargs,
        )
    except _sp.TimeoutExpired:
        return ToolResult(
            success=False,
            content=f"Git command timed out after {_GIT_TIMEOUT}s: git {' '.join(args_list)}",
            hint="Try a more specific command (e.g., git_diff with a path) or check if the repo is very large.",
        )
    except FileNotFoundError:
        return ToolResult(
            success=False,
            content="Git is not installed or not found on PATH.",
            hint="Install Git for Windows from https://git-scm.com/download/win",
        )
    except OSError as e:
        return ToolResult(
            success=False,
            content=f"Git command failed (OS error): {e}",
        )

    output = ""
    if r.stdout and r.stdout.strip():
        output += r.stdout.strip()
    if r.stderr and r.stderr.strip():
        if output:
            output += "\n"
        output += r.stderr.strip()

    if not output:
        output = "(no output)"

    return ToolResult(
        success=(r.returncode == 0),
        content=output,
    )


# Trigger registration on import
_init_register()
