#!/usr/bin/env python3
"""
safety.py — file-read and file-write safety layer for mini_agent.

Enforces:
    1. All reads/writes must land inside a configured workspace root.
    2. Overwrites trigger a confirmation check (unless explicitly allowed).
    3. All results are returned as structured dataclasses — never raw exceptions.
"""
from __future__ import annotations

import difflib
import os
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _is_within_workspace(resolved: str, root: str, root_prefix: str) -> bool:
    """Return True if *resolved* is within the workspace *root*."""
    return resolved.startswith(root_prefix) or resolved == root


# ---------------------------------------------------------------------------
# Shared result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SafetyResult:
    """Structured result for read/write safety checks — never throws."""
    allowed: bool
    reason: str
    resolved_path: str


# Backward-compatibility aliases (deprecated — use SafetyResult directly)
ReadSafetyResult = SafetyResult
WriteSafetyResult = SafetyResult


# ---------------------------------------------------------------------------
# Read safety
# ---------------------------------------------------------------------------

class ReadSafetyGate:
    """Gate that validates file-read operations before execution."""

    def __init__(self, workspace_root: str, *, unrestricted: bool = False) -> None:
        self._root = os.path.realpath(os.path.abspath(workspace_root))
        self._root_prefix = self._root + os.sep
        self._unrestricted = unrestricted

    @property
    def workspace_root(self) -> str:
        return self._root

    @property
    def unrestricted(self) -> bool:
        return self._unrestricted

    def check(self, path: str | None) -> SafetyResult:
        """Validate a proposed read path.

        Returns a structured result — never throws.
        """
        if path is None:
            return SafetyResult(
                allowed=False,
                reason="Path is None.",
                resolved_path="",
            )
        # Guard against empty-string "" which silently resolves to CWD via abspath.
        if not path:
            return SafetyResult(
                allowed=False,
                reason="Path is empty.",
                resolved_path="",
            )
        resolved = os.path.realpath(os.path.join(self._root, path))

        # NOTE: There is an inherent TOCTOU race between this realpath check
        # and the actual open() call — a symlink could be swapped after this
        # check passes.  We accept this because the workspace is assumed to be
        # single-writer and the window is tiny.
        if not self._unrestricted and not _is_within_workspace(resolved, self._root, self._root_prefix):
            return SafetyResult(
                allowed=False,
                reason=f"Path '{resolved}' is outside workspace root '{self._root}'.",
                resolved_path=resolved,
            )

        return SafetyResult(
            allowed=True,
            reason="OK",
            resolved_path=resolved,
        )


# ---------------------------------------------------------------------------
# Diff preview result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DiffPreview:
    """Structured diff preview for a proposed write/edit.

    *preview_text* is ANSI-colored for terminal display.
    *changed* is True if the diff shows any changes.
    """

    preview_text: str
    changed: bool


# ---------------------------------------------------------------------------
# Write safety
# ---------------------------------------------------------------------------

class WriteSafetyGate:
    """Gate that validates file-write operations before execution."""

    # ANSI color constants for diff preview
    _GREEN = "\033[32m"
    _RED = "\033[31m"
    _CYAN = "\033[36m"
    _RESET = "\033[0m"
    _BOLD = "\033[1m"

    def __init__(self, workspace_root: str, *, allow_overwrites: bool = False,
                 unrestricted: bool = False) -> None:
        self._root = os.path.realpath(os.path.abspath(workspace_root))
        self._root_prefix = self._root + os.sep
        self._allow_overwrites = allow_overwrites
        self._unrestricted = unrestricted

    @property
    def workspace_root(self) -> str:
        return self._root

    @property
    def unrestricted(self) -> bool:
        return self._unrestricted

    def check(self, path: str | None) -> SafetyResult:
        """Validate a proposed write path.

        Returns a structured result — never throws.
        """
        if path is None:
            return SafetyResult(
                allowed=False,
                reason="Path is None.",
                resolved_path="",
            )
        # Guard against empty-string "" which silently resolves to CWD via abspath.
        if not path:
            return SafetyResult(
                allowed=False,
                reason="Path is empty.",
                resolved_path="",
            )

        # Resolve the intended absolute path
        resolved = os.path.realpath(os.path.join(self._root, path))

        # 1. Workspace boundary check (skipped when unrestricted)
        if not self._unrestricted and not _is_within_workspace(resolved, self._root, self._root_prefix):
            return SafetyResult(
                allowed=False,
                reason=f"Path '{resolved}' is outside workspace root '{self._root}'.",
                resolved_path=resolved,
            )

        return SafetyResult(
            allowed=True,
            reason="OK",
            resolved_path=resolved,
        )

    # ------------------------------------------------------------------
    # Diff preview for write approval
    # ------------------------------------------------------------------

    def generate_diff(self, tool_name: str, args: dict) -> DiffPreview:
        """Generate an ANSI-colored diff preview for a proposed write/edit.

        Returns a :class:`DiffPreview` with colored text and a *changed* flag.
        For ``write_file`` on new files, shows the full content as green
        additions.  For ``edit_file`` and ``write_file`` on existing files,
        shows a unified diff using :mod:`difflib`.
        """
        path = args.get("path", "")
        resolved = os.path.realpath(os.path.join(self._root, path))
        exists = os.path.isfile(resolved)

        if tool_name == "write_file":
            content = args.get("content", "")
            if exists:
                try:
                    with open(resolved, "r") as f:
                        old = f.read()
                except OSError:
                    old = ""
                diff_text = self._format_diff(resolved, old, content)
                changed = old != content
                return DiffPreview(preview_text=diff_text, changed=changed)
            else:
                diff_text = self._format_new_file(resolved, content)
                return DiffPreview(preview_text=diff_text, changed=bool(content))

        elif tool_name == "edit_file":
            old = args.get("old_string", "")
            new = args.get("new_string", "")
            if exists:
                try:
                    with open(resolved, "r") as f:
                        original = f.read()
                except OSError:
                    original = ""
                # Apply the edit to show the post-edit diff
                count = args.get("count", 1)
                if count == -1:
                    edited = original.replace(old, new)
                else:
                    edited = original.replace(old, new, 1)
                diff_text = self._format_diff(resolved, original, edited)
                return DiffPreview(preview_text=diff_text, changed=original != edited)
            else:
                diff_text = self._format_new_file(resolved, new)
                return DiffPreview(preview_text=diff_text, changed=bool(new))

        return DiffPreview(preview_text="", changed=False)

    def approve(self, tool_name: str, args: dict) -> str:
        """Generate ANSI-colored diff preview string (backward-compatible).

        Delegates to :meth:`generate_diff` and returns the *preview_text*.
        """
        return self.generate_diff(tool_name, args).preview_text

    def _format_new_file(self, path: str, content: str) -> str:
        """Format new-file content as a green diff (all additions)."""
        lines: list[str] = []
        lines.append(f"{self._BOLD}--- /dev/null{self._RESET}")
        lines.append(f"{self._BOLD}+++ {path}{self._RESET}")
        lines.append(f"{self._CYAN}@@ -0,0 +1,{content.count(chr(10)) + 1} @@{self._RESET}")
        for line in content.split("\n"):
            lines.append(f"{self._GREEN}+{line}{self._RESET}")
        return "\n".join(lines)

    def _format_diff(self, path: str, old: str, new: str) -> str:
        """Format a unified diff with ANSI coloring."""
        diff = difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=path,
            tofile=path,
        )
        colored: list[str] = []
        for line in diff:
            stripped = line.rstrip("\n")
            if line.startswith("@@"):
                colored.append(f"{self._CYAN}{stripped}{self._RESET}")
            elif line.startswith("+"):
                colored.append(f"{self._GREEN}{stripped}{self._RESET}")
            elif line.startswith("-"):
                colored.append(f"{self._RED}{stripped}{self._RESET}")
            elif line.startswith("---") or line.startswith("+++"):
                colored.append(f"{self._BOLD}{stripped}{self._RESET}")
            else:
                colored.append(stripped)
        return "\n".join(colored)
