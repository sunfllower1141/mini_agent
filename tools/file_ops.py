#!/usr/bin/env python3
"""
file_ops.py -- file/directory tools for mini_agent.

Tools: read_file, write_file, edit_file, list_directory, file_info
"""
from __future__ import annotations

import os

import re
import stat as stat_module
import shutil
import subprocess
import sys
import time

from core.safety import DiffPreview, ReadSafetyGate, WriteSafetyGate
from tools import clear_tool_cache
from tools import _register, _summarize, ToolResult, _TOOL_CONTEXT
from tools.ast_ops import get_file_skeleton, get_function, get_symbol_range, replace_symbol

from tools.error_hints import _err, _hint
# Thread-local: current sub-agent task_id (set by agent_ops before tool execution)
import threading


_current_agent_id: threading.local = threading.local()

# Bogus path markers that indicate the model forgot to fill in a real path.
# These are never valid file paths and should be caught early with a clear
# error message rather than propagating to OS-level file operations.
_BOGUS_PATH_MARKERS: frozenset[str] = frozenset({"?", "", " ", "  ", "???", "...", "path", "file"})


def _validate_path(path: str, context: str = "path") -> str | None:
    """Return an error string if *path* is obviously bogus, otherwise None.

    Catches placeholder values like ``?`` that the model may emit when it
    fails to fill in a real file path.  These would otherwise pass schema
    validation (they are strings) and cause confusing OS-level errors.
    """
    stripped = path.strip() if isinstance(path, str) else ""
    if not stripped:
        return _err("REFUSED", f"{context} is empty, not a real path",
                   fix="use list_directory or search_files to find the real path")
    if stripped in _BOGUS_PATH_MARKERS:
        return _err("REFUSED", f"{context}='{stripped}' is a placeholder, not a real path",
                   fix="read a file or list files first")
    return None





def _read_file_direct(
    resolved: str, offset: int, limit: int, line_numbers: bool,
    hash_lines: bool = False,
    _content: str | None = None,
) -> ToolResult:
    """Direct file read -- used on Unix and as fallback on Windows.

    When hash_lines=True, uses AnchorStateManager for Dirac-style word
    anchors that persist across edits (unchanged lines keep their anchor).
    """
    all_lines: list[str] = []
    raw_content: str = ""
    try:
        if _content is not None:
            raw_content = _content
        else:
            with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                raw_content = f.read()
        all_lines = raw_content.split("\n")
        # Remove trailing empty line from split (consistent with old behavior)
        if all_lines and all_lines[-1] == "":
            all_lines.pop()
    except Exception as e:
        if isinstance(e, FileNotFoundError) or "No such file" in str(e):
            return ToolResult(success=False, content=_err("NOT_FOUND", f"{resolved}", "use list_directory"))
        return ToolResult(success=False, content=_err("READ", str(e)))

    total_lines = len(all_lines)

    if offset > 0 and offset >= total_lines:
        return ToolResult(success=False, content=_err("RANGE", f"offset={offset} > {total_lines}"))
    

    # --- Compute word anchors (Dirac-style persistent anchors) ---
    anchors: list[str] | None = None
    if hash_lines:
        from core.anchor_manager import AnchorStateManager
        task_id = getattr(_current_agent_id, "task_id", None)
        anchors = AnchorStateManager.reconcile(resolved, all_lines, task_id)

    # --- Slice and format ---
    sliced = all_lines[offset:offset + limit]
    collected: list[str] = []
    gutter_width = max(len(str(total_lines)), 1)
    for i, line in enumerate(sliced):
        lineno = offset + i + 1  # 1-based
        if hash_lines and anchors is not None:
            anchor = anchors[offset + i]
            collected.append(f"{lineno:>{gutter_width}} {anchor}\u2502{line}")
        elif line_numbers:
            collected.append(f"{lineno:>{gutter_width}} {line}")
        else:
            collected.append(line)

    # Actual lines remaining after offset
    lines_after_offset = total_lines - offset
    visible_count = len(sliced)

    # --- pi-style truncation with continuation hints ---
    # Hard caps: 2000 lines / 50KB (whichever hit first)
    HARD_LINE_CAP = 2000
    HARD_BYTE_CAP = 50 * 1024  # 50KB

    # Check if output exceeds caps
    full_content = "\n".join(collected)
    content_bytes = len(full_content.encode("utf-8"))
    truncated_by_lines = visible_count > HARD_LINE_CAP
    truncated_by_bytes = content_bytes > HARD_BYTE_CAP

    if truncated_by_lines or truncated_by_bytes:
        # Trim to fit within caps
        kept_lines: list[str] = []
        kept_bytes = 0
        for line in collected:
            line_bytes = len(line.encode("utf-8")) + (1 if kept_lines else 0)  # +1 for newline
            if len(kept_lines) >= HARD_LINE_CAP:
                break
            if kept_bytes + line_bytes > HARD_BYTE_CAP:
                break
            kept_lines.append(line)
            kept_bytes += line_bytes
        shown_lines = len(kept_lines)
        end_line = offset + shown_lines
        next_offset = end_line + 1
        # Continuation hint like pi: actionable offset for next chunk
        if next_offset <= total_lines:
            hint = (
                f"\n\n[Showing lines {offset + 1}-{end_line} of {total_lines}"
                f" ({'line' if truncated_by_lines else 'byte'} limit)."
                f" Use offset={next_offset} to continue.]"
            )
        else:
            hint = ""
        truncated = "\n".join(kept_lines) + hint
        return ToolResult(success=True, content=truncated)

    if lines_after_offset > limit:
        next_offset = offset + limit + 1
        truncated = "\n".join(collected[:limit])
        msg = (
            f"{truncated}\n"
            f"\n[Showing lines {offset + 1}-{offset + limit} of {total_lines}."
            f" Use offset={next_offset} to continue.]"
        )
        return ToolResult(success=True, content=msg)

    return ToolResult(success=True, content=full_content)

# ---------------------------------------------------------------------------
# Unicode & quote normalization maps (used by edit_file matching)
# ---------------------------------------------------------------------------

# Curly/smart quotes -> ASCII straight quotes
_QUOTE_NORMALIZE_MAP: dict[int, int | None] = {
    0x2018: ord("'"),   # ' left single
    0x2019: ord("'"),   # ' right single
    0x201A: ord("'"),   # , single low-9
    0x201B: ord("'"),   # ' single high-reversed
    0x201C: ord('"'),   # " left double
    0x201D: ord('"'),   # " right double
    0x201E: ord('"'),   # ,, double low-9
    0x201F: ord('"'),   # " double high-reversed
    0x2039: ord("'"),   # < single left-pointing angle
    0x203A: ord("'"),   # > single right-pointing angle
    0x00AB: ord('"'),   # << left-pointing double angle
    0x00BB: ord('"'),   # >> right-pointing double angle
}

# Unicode whitespace -> ASCII space (or None = remove)
_UNICODE_WHITESPACE_MAP: dict[int, int | None] = {
    0x00A0: ord(" "),   # non-breaking space
    0x2002: ord(" "),   # en space
    0x2003: ord(" "),   # em space
    0x2007: ord(" "),   # figure space
    0x2008: ord(" "),   # punctuation space
    0x2009: ord(" "),   # thin space
    0x200A: ord(" "),   # hair space
    0x202F: ord(" "),   # narrow non-breaking space
    0x205F: ord(" "),   # medium mathematical space
    0x3000: ord(" "),   # ideographic space
    0x00AD: None,       # soft hyphen -> remove
    0x200B: None,       # zero-width space -> remove
    0x200C: None,       # zero-width non-joiner -> remove
    0x200D: None,       # zero-width joiner -> remove
    0xFEFF: None,       # BOM / zero-width no-break space -> remove
    0x2060: None,       # word joiner -> remove
}

# Build fast translation tables (Python str.translate)
_QUOTE_TRANS_TABLE: dict[int, int] = {}
_UNICODE_WS_TRANS_TABLE: dict[int, int | None] = {}

def _normalize_quotes(s: str) -> str:
    """Convert curly/smart quotes to ASCII straight quotes."""
    return s.translate(_QUOTE_TRANS_TABLE)

def _normalize_unicode_whitespace(s: str) -> str:
    """Replace Unicode whitespace chars with ASCII space; remove zero-width chars."""
    return s.translate(_UNICODE_WS_TRANS_TABLE)

def _canonicalize_for_match(s: str) -> str:
    """Full canonicalization for matching: normalize Unicode ws, then quotes."""
    return _normalize_quotes(_normalize_unicode_whitespace(s))

# ---------------------------------------------------------------------------
# Read-before-edit tracking -- set of resolved_path values that have been
# read_file'd during this session.  Edit/replace operations check this to
# ensure the model has seen the current file content.
# ---------------------------------------------------------------------------

_READ_FILES: set[str] = set()

# ---------------------------------------------------------------------------
# ACI (Agent-Computer Interface) upgrade: syntax validation before applying
# edits.  Catch broken Python syntax before the edit cascades into a series
# of compounding failures.  This is the SWE-agent linter-in-edit pattern.
# ---------------------------------------------------------------------------

def _validate_python_syntax(content: str, filepath: str) -> str | None:
    """Return an error message if *content* is not valid Python, else None.

    Uses ``compile()`` for fast in-process validation. Only checks .py files.
    On error, returns a compact message with the offending line and context
    so the AI can self-correct the edit.
    """
    if not filepath.endswith(".py"):
        return None
    try:
        compile(content, filepath, "exec")
    except SyntaxError as e:
        lines = content.split("\n")
        lineno = e.lineno or 1
        # Show context: 2 lines before, the error line, 2 lines after
        ctx_start = max(0, lineno - 3)
        ctx_end = min(len(lines), lineno + 2)
        ctx_lines = []
        for i in range(ctx_start, ctx_end):
            prefix = ">>>" if i == lineno - 1 else "   "
            ctx_lines.append(f"{prefix} {i+1}: {lines[i][:120]}")
        ctx = "\n".join(ctx_lines)
        return (
            f"SyntaxError: {e.msg} at line {lineno}\n"
            f"{ctx}"
        )
    return None


# Build fast translation tables at import time
for _cp, _replacement in _QUOTE_NORMALIZE_MAP.items():
    _QUOTE_TRANS_TABLE[_cp] = _replacement

# Unicode ws table: map cp -> replacement (or delete if None via str.maketrans)
# str.translate with a dict can map to None to delete characters
_UNICODE_WS_TRANS_TABLE.update({cp: repl for cp, repl in _UNICODE_WHITESPACE_MAP.items() if repl is not None})
# Zero-width chars: map to None to delete
for _cp, _repl in _UNICODE_WHITESPACE_MAP.items():
    if _repl is None:
        _UNICODE_WS_TRANS_TABLE[_cp] = None


# ---------------------------------------------------------------------------
# Session undo -- backs up files before modification
# ---------------------------------------------------------------------------

_BACKUPS: dict[str, str] = {}  # resolved_path -> backup path



# Tracks files where edit_file recently failed -- used to detect write_file-as-fallback
# anti-pattern.  Cleared after each successful write/edit or on next read_file.
_RECENT_EDIT_FAILURES: set[str] = set()
# Track files deleted via rm (shell) to prevent delete+rewrite anti-pattern.
# When _write_file sees a path in this set, it blocks the write even if the
# file no longer exists on disk (because the model deleted it to bypass guards).
_RECENTLY_DELETED: set[str] = set()


def _track_deleted_file(path: str) -> None:
    """Record that a workspace file was just deleted (called from shell_ops)."""
    _RECENTLY_DELETED.add(os.path.realpath(path))


def _is_recently_deleted(path: str) -> bool:
    """Check if a path was recently deleted via rm."""
    return os.path.realpath(path) in _RECENTLY_DELETED



# Maximum file size for full reads before warning (50KB). Larger files should be
# read with offset/limit or via get_file_skeleton / get_function.
_MAX_FILE_READ_SIZE = 50 * 1024







# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Auto plan advancement -- after a successful write/edit, check if any
# incomplete plan step's keywords appear in the file path or edit content,
# and auto-complete it.
# ---------------------------------------------------------------------------

def _auto_advance_plan(file_path: str, edit_text: str = "") -> None:
    """Check plan steps against file_path and edit_text; auto-complete matches."""
    steps = getattr(_TOOL_CONTEXT, "_plan_steps", None)
    done = getattr(_TOOL_CONTEXT, "_plan_done", None)
    if not steps or done is None:
        return
    # Build set of words from the file path + edit text
    haystack = (file_path + " " + edit_text).lower()
    incomplete_indices = [i for i, _ in enumerate(steps) if i not in done]
    for idx in incomplete_indices:
        step_text = steps[idx].lower()
        # Tokenise the step into meaningful words (2+ chars, skip very common words)
        words = {w for w in step_text.split() if len(w) >= 4}
        if not words:
            # Fallback: use the whole step text as one token
            words = {step_text}
        if any(w in haystack for w in words):
            done.add(idx)
    _TOOL_CONTEXT._plan_done = done
    if incomplete_indices and any(i in done for i in incomplete_indices):
        _TOOL_CONTEXT._plan_last_advanced_turn = getattr(_TOOL_CONTEXT, "_turn_count", 0)

    # Persist to memory if any steps were auto-completed
    if incomplete_indices:
        try:
            from tools.agent_todos import _maybe_persist_plan
            _maybe_persist_plan()
        except ImportError:
            pass


def _backup_before_write(resolved_path: str) -> None:
    """Save a backup of *resolved_path* if it exists and hasn't already been backed up."""
    if resolved_path in _BACKUPS:
        return  # already backed up
    if not os.path.isfile(resolved_path):
        return  # nothing to back up
    backup_dir = os.path.join(os.path.dirname(resolved_path), ".mini_agent_backups")
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    fname = os.path.basename(resolved_path)
    backup_path = os.path.join(backup_dir, f"{fname}.{timestamp}.bak")
    shutil.copy2(resolved_path, backup_path)
    _BACKUPS[resolved_path] = backup_path


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------

# Default maximum lines returned by read_file when no limit is given.
_DEFAULT_READ_LINES = 300
# Absolute maximum (safety cap) -- never return more than this.
_ABSOLUTE_MAX_LINES = 1000


@_register("read_file")
def _read_file(args: dict, _wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:
    """Read one or more files. Supports 'path' (single) and 'paths' (array, Dirac pattern).

    Hash-based re-read shortcut: if a file content hasn't changed since last read,
    returns a short "no changes" message instead of re-sending the full content to the API.
    """
    import hashlib

    # Accept 'paths' (array) or 'path' (single string) — Dirac multi-file pattern
    paths_raw = args.get("paths")
    if paths_raw is not None and isinstance(paths_raw, list):
        file_paths: list[str] = paths_raw
        is_multi = len(file_paths) > 1
    elif "path" in args:
        file_paths = [args["path"]]
        is_multi = False
    else:
        return ToolResult(
            success=False,
            content=_err("MISSING", "need 'path' or 'paths'"),
            hint="Valid parameters: path (string), paths (array), offset, limit, line_numbers, hash_lines",
        )

    # Reject obviously bogus paths (e.g. placeholder '?' instead of a real path)
    for p in file_paths:
        bogus_err = _validate_path(p)
        if bogus_err:
            return ToolResult(success=False, content=bogus_err)


    offset = args.get("offset", 0)
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    if offset < 0:
        offset = 0
    limit = args.get("limit", _DEFAULT_READ_LINES)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = _DEFAULT_READ_LINES
    if limit < 1:
        limit = _DEFAULT_READ_LINES
    limit = min(limit, _ABSOLUTE_MAX_LINES)
    line_numbers = args.get("line_numbers", False)
    if isinstance(line_numbers, str):
        line_numbers = line_numbers.lower() in ("true", "1", "yes")
    hash_lines = args.get("hash_lines", False)  # default off — only use when editing
    if isinstance(hash_lines, str):
        hash_lines = hash_lines.lower() in ("true", "1", "yes")

    results: list[str] = []
    any_failed = False

    for path in file_paths:
        safety_result = rg.check(path)
        if not safety_result.allowed:
            results.append(f"--- {path} ---\n{_err('BLOCKED', safety_result.reason)}")
            any_failed = True
            continue
        resolved = safety_result.resolved_path

        # --- File size guard (Dirac: warn on >50KB full reads) ---
        if offset == 0 and limit >= _DEFAULT_READ_LINES:
            try:
                fsize = os.path.getsize(resolved)
                if fsize > _MAX_FILE_READ_SIZE:
                    results.append(
                        f"--- {path} ---\n"
                        f"[WARNING] File is {fsize // 1024}KB, exceeds {_MAX_FILE_READ_SIZE // 1024}KB "
                        f"limit for full reads. Use offset/limit to read ranges, "
                        f"or get_file_skeleton / get_function for surgical reads."
                    )
                    continue
            except OSError:
                pass

        # --- Read file once (single disk I/O for hash + content) ---
        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                raw_content = f.read()
        except OSError as e:
            hint = ""
            if "No such file" in str(e):
                hint = _err("NOT_FOUND", f"{resolved}", "use list_directory")
            header = f"--- {path} ---\n" if is_multi else ""
            results.append(f"{header}{hint}" if hint else f"{header}Error reading '{resolved}': {e}")
            any_failed = True
            continue

        # --- Raw-content shortcut (no formatting, no offset/limit needed) ---
        if offset == 0 and limit >= _DEFAULT_READ_LINES and not line_numbers and not hash_lines:
            header = f"--- {path} ---\n" if is_multi else ""
            results.append(header + raw_content)
            _READ_FILES.add(resolved)
            continue

        # --- Actual formatted read (delegates to _read_file_direct with pre-read content) ---
        result = _read_file_direct(resolved, offset, limit, line_numbers, hash_lines=hash_lines, _content=raw_content)

        if not result.success:
            header = f"--- {path} ---\n" if is_multi else ""
            results.append(header + result.content)
            any_failed = True
            continue

        full_content = result.content

        _READ_FILES.add(resolved)

        # Clear edit-failure tracker -- agent is doing the right thing (re-reading)
        _RECENT_EDIT_FAILURES.discard(resolved)



        header = f"--- {path} ---\n" if is_multi else ""
        results.append(header + full_content)



    return ToolResult(success=not any_failed, content="\n\n".join(results))


@_summarize("read_file")
def _read_file_summary(args: dict) -> str:
    paths = args.get("paths") or [args.get("path", "?")]
    if isinstance(paths, list) and len(paths) <= 3:
        return f"read_file({', '.join(paths)})"
    elif isinstance(paths, list):
        return f"read_file({len(paths)} files)"
    return f"read_file({paths})"


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------

@_register("write_file")
def _write_file(args: dict, wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    path = args.get("path")
    if not path or not isinstance(path, str):
        return ToolResult(
            success=False,
            content="Missing required: path",
            hint="Valid parameters: path, content",
        )
    bogus_err = _validate_path(path)
    if bogus_err:
        return ToolResult(success=False, content=bogus_err)

    content = args.get("content")
    if content is None:
        return ToolResult(
            success=False,
            content="Missing required: content",
            hint="Valid parameters: path, content",
        )

    safety_result = wg.check(path)
    if not safety_result.allowed:
        return ToolResult(
            success=False,
            content=_err("BLOCKED", f"path outside workspace", f"use {wg.workspace_root}/... or force=True"),
        )
    # Read-before-edit enforcement (ACI upgrade): reject writes to
    # .py files that haven't been read_file'd this session, unless
    # the file doesn't exist yet (new file creation is allowed).
    _resolved = safety_result.resolved_path
    if _resolved.endswith(".py") and os.path.isfile(_resolved) and _resolved not in _READ_FILES:
        return ToolResult(
            success=False,
            content=_err("GUARD", f"read_file('{_resolved}') first, then write"),
        )
    # write_file only creates new files; use edit_file for existing files
    if os.path.isfile(_resolved):
        return ToolResult(
            success=False,
            content=_err("EXISTS", f"file already exists; use edit_file to modify",
                         f"edit_file('{path}', ...)"),
        )
    # Block delete+rewrite anti-pattern: if this file was just deleted via rm
    # to bypass the EXISTS guard, refuse the write.
    if _is_recently_deleted(_resolved):
        return ToolResult(
            success=False,
            content=_err("BLOCKED", f"{path} was deleted via rm to bypass edit_file -- restore the file and use edit_file instead",
                         "use git checkout or restore_file to recover the original file"),
        )
    # File reservation check -- prevent sub-agent collisions
    agent_id = getattr(_current_agent_id, "task_id", None)
    if agent_id is not None:
        from tools import reserve_file
        ok, msg = reserve_file(path, agent_id)
        if not ok:
            return ToolResult(success=False, content=msg)
    try:
        # Generate diff preview before writing
        diff = wg.generate_diff("write_file", args)
        parent = os.path.dirname(safety_result.resolved_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        _backup_before_write(safety_result.resolved_path)
        # --- ACI upgrade: syntax validation for .py files ---
        # Only gate if the existing file was already valid Python. If the file
        # doesn't even compile now (e.g. prose in a .py test fixture), skip.
        syntax_error = None
        if safety_result.resolved_path.endswith(".py"):
            try:
                with open(safety_result.resolved_path, "r", encoding="utf-8") as _f:
                    _prev = _f.read()
                compile(_prev, safety_result.resolved_path, "exec")
            except (FileNotFoundError, SyntaxError):
                pass  # No existing file, or existing content isn't valid Python
            else:
                syntax_error = _validate_python_syntax(content, safety_result.resolved_path)
        if syntax_error:
            return ToolResult(
                success=False,
                content=(
                    f"Syntax validation failed -- file NOT written.\n"
                    f"{syntax_error}"
                ),
            )
        with open(safety_result.resolved_path, "w", encoding="utf-8") as f:
            f.write(content)
        from tools import add_modified_file
        add_modified_file(safety_result.resolved_path)
        clear_tool_cache()
        
        # Invalidate anchor state for this file (Dirac-style)
        try:
            from core.anchor_manager import AnchorStateManager
            AnchorStateManager.clear_state(safety_result.resolved_path)
        except Exception:
            pass
        # Track as read for read-before-edit enforcement (agent wrote it, knows content)
        _READ_FILES.add(safety_result.resolved_path)
        # Keep symbol index fresh for newly written .py files
        if path.endswith(".py"):
            from tools.search_ops import _reindex_file
            _reindex_file(safety_result.resolved_path, wg.workspace_root)
        # Auto plan advancement (file path only -- full content is too noisy)
        _auto_advance_plan(safety_result.resolved_path)
        # Diagnostic: detect write_file-as-fallback anti-pattern.
        # If edit_file just failed on this same file, warn that edit_file should
        # have been retried with fresh hash_lines=True instead.
        _fallback_warning = ""
        if safety_result.resolved_path in _RECENT_EDIT_FAILURES:
            _RECENT_EDIT_FAILURES.discard(safety_result.resolved_path)
            _fallback_warning = (
                "\n[WARNING] write_file used on a file where edit_file recently failed. "
                "Prefer re-running read_file(hash_lines=True) then retrying edit_file "
                "with fresh anchors."
            )
        return ToolResult(
            success=True,
            content=f"OK: wrote {len(content)} bytes to {safety_result.resolved_path}{_fallback_warning}",
            diff_preview=diff.preview_text if diff.changed else None,
        )
    except Exception as e:
        return ToolResult(
            success=False,
            content=f"Error writing '{safety_result.resolved_path}': {e}",
        )


@_summarize("write_file")
def _write_file_summary(args: dict) -> str:
    path = args.get("path", "?")
    content = args.get("content", "")
    preview = content[:60].replace("\n", "\\n")
    if len(content) > 60:
        preview += "..."
    return f"write_file({path}, {len(content)}B -> \"{preview}\")"


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------


def _normalize_line(s: str) -> str:
    """Collapse whitespace: Unicode ws->space, tabs->spaces, strip, collapse multiple spaces."""
    s = _normalize_unicode_whitespace(s)
    return ' '.join(s.replace('\t', '    ').split())



def _find_closest_lines(content_lines: list[str], search_lines: list[str]) -> dict | None:
    """Find the closest matching region in the file for diagnostic diff.

    Uses normalized content comparison (pass 4 style) with a sliding window.
    Returns {'line': int, 'lines': list[str], 'diff_hint': str} or None.
    """
    n_search = len(search_lines)
    n_content = len(content_lines)
    if n_search == 0 or n_content < n_search:
        return None

    norm_search = [_normalize_line(s) for s in search_lines]
    best_score = -1
    best_idx = 0

    # Score each window: count how many lines match (after normalization)
    for i in range(n_content - n_search + 1):
        window = content_lines[i:i + n_search]
        norm_window = [_normalize_line(w) for w in window]
        score = sum(1 for a, b in zip(norm_search, norm_window) if a == b)
        if score > best_score:
            best_score = score
            best_idx = i

    match_ratio = best_score / n_search if n_search > 0 else 0

    # Build a diff hint showing what's different
    diff_parts = []
    norm_content_window = [_normalize_line(l) for l in content_lines[best_idx:best_idx + n_search]]
    for j in range(n_search):
        if norm_search[j] != norm_content_window[j]:
            diff_parts.append(
                f"line {j+1}: expected '{norm_search[j][:40]}' "
                f"got '{norm_content_window[j][:40]}'"
            )

    return {
        'line': best_idx + 1,
        'lines': content_lines[best_idx:best_idx + n_search],
        'diff_hint': '; '.join(diff_parts[:5]) if diff_parts else '',
        'match_ratio': match_ratio,
        'matched_lines': best_score,
    }


def _fuzzy_find(content: str, search: str) -> tuple[int, int] | None:
    """Cascading 5-pass match for edit_file.

    1. Exact substring match.
    2. Quote-normalized match (curly->straight quotes).
    3. Trailing-whitespace-tolerant.
    4. Indentation-tolerant (full strip).
    5. Normalized-content fuzzy match (Unicode ws->space, tabs->spaces, collapsed whitespace)
       with confidence scoring (requires >=95% normalized line matches).
    """
    if not search or not content:
        return None

    # -- Line-ending normalization: CRLF -> LF -------
    content_lf = content.replace('\r\n', '\n').replace('\r', '\n')
    search_lf = search.replace('\r\n', '\n').replace('\r', '\n')

    # Pass 1: exact substring (against LF-normalized content)
    idx = content_lf.find(search_lf)
    if idx != -1:
        # Map back to original content offsets (CR removal may shift)
        return _map_lf_offset_to_original(content, search_lf, idx)

    content_lines = content_lf.split('\n')
    search_lines = search_lf.split('\n')
    if search_lines and search_lines[-1] == '':
        search_lines.pop()
    if not search_lines:
        return None

    # Pass 2: quote normalization -- try matching after normalizing curly quotes
    result = _quote_normalized_match(content_lf, search_lf, content)
    if result is not None:
        return result

    # Pass 3-4: trailing-whitespace-tolerant, then full indent-tolerant
    for trim in ('right', 'all'):
        result = _line_match(content_lines, search_lines, trim, content_lf)
        if result is not None:
            return _map_lf_region_to_original(content, result[0], result[1])

    # Pass 5: normalize whitespace on every line, then try to match
    # with confidence scoring (>=95% threshold)
    return _fuzzy_find_closest(content_lf, search_lines, content_lines, content)


def _map_lf_offset_to_original(
    original: str, search: str, lf_idx: int,
) -> tuple[int, int]:
    """Map an LF-normalized match offset back to original content offsets."""
    # Walk original content counting chars; skip CR bytes
    orig_pos = 0
    lf_pos = 0
    while lf_pos < lf_idx and orig_pos < len(original):
        if original[orig_pos] == '\r':
            orig_pos += 1
            if orig_pos < len(original) and original[orig_pos] == '\n':
                orig_pos += 1
            lf_pos += 1  # \r alone maps to \n
        else:
            orig_pos += 1
            lf_pos += 1
    start = orig_pos
    # Now find end -- search_len chars in LF space
    remaining = len(search)
    while remaining > 0 and orig_pos < len(original):
        if original[orig_pos] == '\r':
            orig_pos += 1
            if orig_pos < len(original) and original[orig_pos] == '\n':
                orig_pos += 1
        else:
            orig_pos += 1
        remaining -= 1
    return (start, orig_pos)


def _map_lf_region_to_original(
    original: str, lf_start: int, lf_end: int,
) -> tuple[int, int]:
    """Map an LF-normalized region [lf_start, lf_end) back to original offsets."""
    start = _map_lf_offset_to_original(original, "x" * (lf_end - lf_start), lf_start)[0]
    _, end = _map_lf_offset_to_original(original, "x" * (lf_end - lf_start), lf_start)
    return (start, end)


def _quote_normalized_match(
    content_lf: str, search_lf: str, original: str,
) -> tuple[int, int] | None:
    """Pass 2: try matching after normalizing curly/smart quotes to ASCII.

    Returns (start, end) in *original* content offsets.
    """
    norm_content = _normalize_quotes(content_lf)
    norm_search = _normalize_quotes(search_lf)
    idx = norm_content.find(norm_search)
    if idx != -1:
        # Map the normalized offset back through the LF content to original
        # Since quote normalization doesn't change string length (1 cp -> 1 byte
        # in these cases), the offsets are the same as LF offsets.
        return _map_lf_offset_to_original(original, search_lf, idx)
    return None


def _preserve_indentation(
    old_str: str, new_str: str, file_region: str,
) -> str:
    """Preserve the file's indentation style when applying a replacement.

    Captures the leading whitespace of each line in the matched file region
    and applies the same indentation *relative changes* to the new_string lines.
    If old_str has N lines with indentation I?...I? and new_str has M lines with
    indentation J?...J?, then for each new line k at position k in the new block:
      - if k < N: apply (J? - I?) offset relative to file's I?
      - if k >= N: apply (J??? - I???) offset relative to file's last I

    This handles the common case where the model outputs refactored code with
    spaces instead of tabs (or vice versa) and we want to match the file's style.
    """
    old_lines = old_str.split('\n')
    new_lines = new_str.split('\n')
    file_lines = file_region.split('\n')

    # Extract leading whitespace from each line
    def _leading_ws(s: str) -> str:
        m = re.match(r'^([ \t]*)', s)
        return m.group(1) if m else ''

    old_indents = [_leading_ws(l) for l in old_lines]
    new_indents = [_leading_ws(l) for l in new_lines]
    file_indents = [_leading_ws(l) for l in file_lines]

    # If all old indents are empty or single-line, no preservation needed
    if not any(old_indents) or len(old_lines) <= 1:
        return new_str

    result_lines: list[str] = []
    for k, new_line in enumerate(new_lines):
        new_ws = new_indents[k] if k < len(new_indents) else ''
        new_content = new_line[len(new_ws):]  # rest of line after indentation

        if k < len(old_indents) and k < len(file_indents):
            old_ws = old_indents[k]
            file_ws = file_indents[k]
            # Compute the relative indentation change from old->new
            if old_ws:
                # New wanted more/less indentation relative to old baseline
                if new_ws.startswith(old_ws):
                    # New has old prefix + extra: apply extra to file's indent
                    extra = new_ws[len(old_ws):]
                    result_lines.append(file_ws + extra + new_content)
                elif old_ws.startswith(new_ws):
                    # New wants less indent than old: reduce file's indent
                    remove = len(old_ws) - len(new_ws)
                    if len(file_ws) >= remove:
                        result_lines.append(file_ws[remove:] + new_content)
                    else:
                        result_lines.append(new_content)
                else:
                    # Totally different indent style: use file's indent + relative diff
                    # Count indent "levels" (tabs=1 level, 2+ spaces=1 level)
                    old_levels = _count_indent_levels(old_ws)
                    new_levels = _count_indent_levels(new_ws)
                    level_diff = new_levels - old_levels
                    new_file_levels = _count_indent_levels(file_ws) + level_diff
                    new_file_indent = _indent_from_levels(new_file_levels, file_ws)
                    result_lines.append(new_file_indent + new_content)
            else:
                # Old had no indent; apply new indent relative to file's indent
                if new_ws:
                    result_lines.append(file_ws + new_ws + new_content)
                else:
                    result_lines.append(file_ws + new_content)
        elif k < len(new_indents):
            # Extra lines beyond old: use last old->file diff
            last_idx = len(old_indents) - 1
            if last_idx >= 0 and last_idx < len(file_indents):
                old_last = old_indents[last_idx]
                file_last = file_indents[last_idx]
                level_diff = _count_indent_levels(new_indents[k]) - _count_indent_levels(old_last) if old_last else _count_indent_levels(new_indents[k])
                new_levels = _count_indent_levels(file_last) + level_diff
                result_lines.append(_indent_from_levels(new_levels, file_last) + new_content)
            else:
                result_lines.append(new_line)
        else:
            result_lines.append(new_line)

    return '\n'.join(result_lines)


def _count_indent_levels(ws: str) -> int:
    """Count indentation levels: each tab = 1 level, each 2 spaces = 1 level."""
    if not ws:
        return 0
    if '\t' in ws:
        return ws.count('\t')
    space_count = len(ws)
    # Treat each 2 spaces as 1 level (Python standard), with remainder as partial
    levels = space_count // 2
    return levels


def _indent_from_levels(levels: int, reference_ws: str) -> str:
    """Generate indentation string from level count, matching reference style."""
    if levels <= 0:
        return ''
    if '\t' in (reference_ws or ''):
        return '\t' * levels
    return ' ' * (levels * 2)


def _fuzzy_find_closest(
    content_lf: str,
    search_lines: list[str],
    content_lines: list[str],
    original: str,
    confidence_threshold: float = 0.95,
) -> tuple[int, int] | None:
    """Pass 5: normalize all whitespace on every line, sliding-window match.

    Normalizes both search and content lines by collapsing whitespace
    (Unicode ws->space, tabs->spaces, strip, collapse multiple spaces).
    Requires a unique match -- if multiple windows match, returns None.
    Also enforces a confidence threshold: the best match must have >=95% of
    normalized lines matching exactly.  If below threshold, returns None
    so the caller can report the near-miss with a score.

    Returns None on ambiguous or low-confidence matches.
    """
    norm_search = [_normalize_line(s) for s in search_lines]
    n_search = len(search_lines)
    n_content = len(content_lines)
    if n_search == 0 or n_content < n_search:
        return None

    match_start = None
    best_score = -1
    best_idx = 0

    for i in range(n_content - n_search + 1):
        window = content_lines[i:i + n_search]
        norm_window = [_normalize_line(w) for w in window]
        score = sum(1 for a, b in zip(norm_search, norm_window) if a == b)
        if score > best_score:
            best_score = score
            best_idx = i
            match_start = None  # reset ambiguity
        if norm_window == norm_search:
            if match_start is not None:
                return None  # ambiguous -- multiple exact normalized matches
            match_start = i

    # If we have a unique exact normalized match, use it regardless of score
    if match_start is not None:
        start_byte = sum(len(line) + 1 for line in content_lines[:match_start])
        end_byte = start_byte + sum(
            len(line) + 1 for line in content_lines[match_start:match_start + n_search]
        )
        if end_byte > start_byte and content_lf[end_byte - 1:end_byte] == '\n':
            end_byte -= 1
        return _map_lf_region_to_original(original, start_byte, end_byte)

    # No exact normalized match -- check confidence threshold
    confidence = best_score / n_search if n_search > 0 else 0.0
    if confidence < confidence_threshold:
        return None  # below threshold, let caller report near-miss

    # Above threshold but not exact -- use best match
    # (this handles near-perfect matches with minor whitespace differences)
    start_byte = sum(len(line) + 1 for line in content_lines[:best_idx])
    end_byte = start_byte + sum(
        len(line) + 1 for line in content_lines[best_idx:best_idx + n_search]
    )
    if end_byte > start_byte and content_lf[end_byte - 1:end_byte] == '\n':
        end_byte -= 1
    return _map_lf_region_to_original(original, start_byte, end_byte)


def _line_match(content_lines, search_lines, trim, content=''):
    normalize = str.rstrip if trim == 'right' else str.strip
    n_search = len(search_lines)
    n_content = len(content_lines)
    norm_search = [normalize(s) for s in search_lines]
    match_start = None
    for i in range(n_content - n_search + 1):
        window = content_lines[i:i + n_search]
        if [normalize(w) for w in window] == norm_search:
            if match_start is not None:
                return None
            match_start = i
    if match_start is None:
        return None
    start_byte = sum(len(line) + 1 for line in content_lines[:match_start])
    end_byte = start_byte + sum(len(line) + 1 for line in content_lines[match_start:match_start + n_search])
    if end_byte > start_byte and content[end_byte - 1:end_byte] == '\n':
        end_byte -= 1
    return (start_byte, end_byte)






# ---------------------------------------------------------------------------
# edit_file -- hash-anchored editing, single-edit and batch (Hashlines pattern from Akay/Howard Chen)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Pi-style exact text matching for edits (no anchors needed)
# ---------------------------------------------------------------------------

# Unicode normalization map for fuzzy text matching (pi-style)
# Smart quotes, Unicode dashes, special spaces → ASCII equivalents
_UNICODE_NORMALIZE_MAP: dict[int, int | None] = {
    # Smart single quotes → '
    0x2018: ord("'"), 0x2019: ord("'"), 0x201A: ord("'"), 0x201B: ord("'"),
    # Smart double quotes → "
    0x201C: ord('"'), 0x201D: ord('"'), 0x201E: ord('"'), 0x201F: ord('"'),
    # Dashes/hyphens → -
    0x2010: ord('-'), 0x2011: ord('-'), 0x2012: ord('-'),
    0x2013: ord('-'), 0x2014: ord('-'), 0x2015: ord('-'), 0x2212: ord('-'),
    # Special spaces → regular space
    0x00A0: ord(' '), 0x2002: ord(' '), 0x2003: ord(' '), 0x2004: ord(' '),
    0x2005: ord(' '), 0x2006: ord(' '), 0x2007: ord(' '), 0x2008: ord(' '),
    0x2009: ord(' '), 0x200A: ord(' '), 0x202F: ord(' '), 0x205F: ord(' '),
    0x3000: ord(' '),
}


def _normalize_for_match(text: str) -> str:
    """Normalize text for fuzzy matching (pi-style).
    
    - Strip trailing whitespace from each line
    - Normalize smart quotes, Unicode dashes, special spaces to ASCII
    - Normalize Unicode NFKC
    """
    import unicodedata
    # Per-line trailing whitespace strip
    lines = [line.rstrip() for line in text.split("\n")]
    result = "\n".join(lines)
    # NFKC normalization
    result = unicodedata.normalize("NFKC", result)
    # Smart quotes, dashes, spaces → ASCII
    return result.translate(_UNICODE_NORMALIZE_MAP)


def _edit_file_text_match(resolved: str, edits: list[dict], display_path: str) -> ToolResult:
    """Apply edits using pi-style exact text matching (oldText → newText).
    
    Each edit.oldText must match a unique, non-overlapping region in the file.
    Edits are applied bottom-up (reverse order by match position) so earlier
    edits don't affect the positions of later ones.
    
    No read-before-edit requirement — the model edits from what it already saw.
    """
    # Read the file
    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            original = f.read()
    except FileNotFoundError:
        return ToolResult(success=False, content=_err("NOT_FOUND", resolved, "use list_directory"))
    except Exception as e:
        return ToolResult(success=False, content=_err("READ", str(e)))

    # Detect line endings (preserve them like pi does)
    line_ending = "\r\n" if "\r\n" in original else "\n"
    # Normalize to LF for matching
    normalized = original.replace("\r\n", "\n")
    fuzzy_normalized = _normalize_for_match(normalized)

    # --- Validate all edits first (against original file, not incremental) ---
    matches: list[dict] = []
    for i, edit in enumerate(edits):
        old_text = edit.get("oldText", "")
        new_text = edit.get("newText", "")
        edit_type = edit.get("edit_type", "replace")
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            return ToolResult(
                success=False,
                content=_err("INVALID", f"edit[{i}] oldText/newText must be strings"),
            )
        # Normalize oldText for fuzzy matching
        fuzzy_old = _normalize_for_match(old_text)
        # Find match in normalized content
        match_idx = fuzzy_normalized.find(fuzzy_old)
        if match_idx == -1:
            # Try exact match (no normalization)
            match_idx = normalized.find(old_text)
            if match_idx == -1:
                # Build helpful error: show where in the file the text is similar
                first_line = old_text.split("\n")[0][:40]
                hint = (
                    f"oldText not found in file. The file may have changed since you last read it. "
                    f"Re-read the file, then copy the EXACT text you want to replace. "
                    f"First line of your oldText: '{first_line}...'"
                )
                return ToolResult(
                    success=False,
                    content=_err("NOT_FOUND", f"edit[{i}] oldText not found in {display_path}", hint),
                )
        # Check for duplicate matches (oldText must be unique)
        second_match = fuzzy_normalized.find(fuzzy_old, match_idx + max(len(fuzzy_old), 1))
        if second_match != -1:
            return ToolResult(
                success=False,
                content=_err("DUPLICATE", f"edit[{i}] oldText matches multiple locations in {display_path}",
                             "Add more surrounding context to make oldText unique"),
            )
        # Handle insert mode: newText goes before/after oldText
        if edit_type == "insert_after":
            replacement = old_text + "\n" + new_text if new_text else old_text
        elif edit_type == "insert_before":
            replacement = new_text + "\n" + old_text if new_text else old_text
        else:
            replacement = new_text
        matches.append({
            "idx": match_idx,
            "old_len": len(old_text),
            "new_text": replacement,
            "edit_idx": i,
        })

    # Check for overlapping matches
    sorted_matches = sorted(matches, key=lambda m: m["idx"])
    for j in range(len(sorted_matches) - 1):
        a = sorted_matches[j]
        b = sorted_matches[j + 1]
        a_end = a["idx"] + a["old_len"]
        if a_end > b["idx"]:
            return ToolResult(
                success=False,
                content=_err("OVERLAP",
                             f"edit[{a['edit_idx']}] and edit[{b['edit_idx']}] overlap in {display_path}",
                             "Merge overlapping edits into a single edit"),
            )

    # --- Apply edits bottom-up (reverse position order) ---
    result = normalized
    for m in reversed(sorted_matches):
        result = result[:m["idx"]] + m["new_text"] + result[m["idx"] + m["old_len"]:]

    # Restore line endings
    if line_ending == "\r\n":
        result = result.replace("\n", "\r\n")

    # Write the file
    try:
        _backup_before_write(resolved)
        with open(resolved, "w", encoding="utf-8", errors="replace") as f:
            f.write(result)
    except Exception as e:
        return ToolResult(success=False, content=_err("WRITE", str(e)))

    # Generate a simple diff preview
    from difflib import unified_diff
    diff_lines = list(unified_diff(
        original.splitlines(keepends=True),
        result.splitlines(keepends=True),
        fromfile=display_path,
        tofile=display_path,
    ))
    diff_preview = "".join(diff_lines[:30])  # cap at 30 lines
    if len(diff_lines) > 30:
        diff_preview += f"\n... ({len(diff_lines) - 30} more diff lines)"

    # Success
    _RECENT_EDIT_FAILURES.discard(resolved)
    from tools import add_modified_file
    add_modified_file(resolved)
    return ToolResult(
        success=True,
        content=f"Applied {len(edits)} edit(s) to {display_path}",
        diff_preview=diff_preview,
    )


@_register("edit_file")  # primary name
@_register("edit_lines")  # backward-compat alias
def _edit_lines(args: dict, wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Replace line ranges using word anchors for reliable first-attempt edits.

    Each edit specifies {from, from_hash, to, to_hash, new_text}.
    The file is re-read fresh; word anchors are recomputed and validated
    before any edit is applied.  Unchanged lines keep their anchors
    across edits (Dirac-style persistence).  Edits are applied bottom-up
    so line numbers in the edits array can refer to the pre-edit file.

    On any anchor mismatch the ENTIRE batch is rejected with a precise error.
    """
    

    path = args.get("path")
    if not path or not isinstance(path, str):
        return ToolResult(
            success=False,
            content="Missing required: path",
            hint="Valid parameters: path, edits",
        )
    bogus_err = _validate_path(path)
    if bogus_err:
        return ToolResult(success=False, content=bogus_err)

    edits = args.get("edits")
    if not edits:
        return ToolResult(
            success=False,
            content="Missing required: edits",
            hint="Valid parameters: edits, path",
        )


    safety_result = wg.check(path)
    if not safety_result.allowed:
        return ToolResult(
            success=False,
            content=f"Edit blocked by safety layer: {safety_result.reason}",
        )
    resolved = safety_result.resolved_path

    if not isinstance(edits, list) or not edits:
        return ToolResult(success=False, content="'edits' must be a non-empty list.")

    # Track this edit attempt for write_file-as-fallback detection.
    # Cleared on success; persists on failure so _write_file can warn.
    _RECENT_EDIT_FAILURES.add(resolved)

    # --- Detect edit style: pi-style (oldText/newText) vs anchor-style (from/from_hash) ---
    has_old_text = all("oldText" in e and "newText" in e for e in edits)
    has_anchors = all("from" in e and "from_hash" in e for e in edits)

    if has_old_text:
        # --- Pi-style exact text matching (primary path, no anchors needed) ---
        return _edit_file_text_match(resolved, edits, path)

    if not has_anchors:
        return ToolResult(
            success=False,
            content="edits must use either oldText/newText (pi-style text match) or from/from_hash (anchor-style). Mixed edits not supported.",
        )

    # --- Anchor-style editing (existing path, requires read-before-edit) ---
    # Read-before-edit enforcement
    if resolved not in _READ_FILES:
        return ToolResult(
            success=False,
            content=_err("GUARD", f"read_file('{resolved}', hash_lines=True) first"),
        )

    # File reservation check
    agent_id = getattr(_current_agent_id, "task_id", None)
    if agent_id is not None:
        from tools import reserve_file
        ok, msg = reserve_file(path, agent_id)
        if not ok:
            return ToolResult(success=False, content=msg)

    # Read original for anchor-based editing
    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            original = f.read()
    except Exception as e:
        return ToolResult(success=False, content=_err("READ", str(e)))


    lines = original.split("\n")
    # Use Dirac-style word anchors (persistent across edits)
    from core.anchor_manager import AnchorStateManager
    task_id = getattr(_current_agent_id, "task_id", None)
    anchors = AnchorStateManager.reconcile(resolved, lines, task_id)

    # --- Validate all word anchors first ---
    for i, edit in enumerate(edits):
        edit_type = edit.get("edit_type", "replace")
        is_insert = edit_type in ("insert_after", "insert_before")
        # For replace edits, default 'to' and 'to_hash' to 'from'/'from_hash'
        if not is_insert:
            edit.setdefault("to", edit.get("from"))
            edit.setdefault("to_hash", edit.get("from_hash"))
        # For insert edits, only validate 'from' anchor; 'to' is ignored
        endpoints = [("from", "from")] if is_insert else [("from", "from"), ("to", "to")]
        for endpoint, label in endpoints:
            line_num = edit.get(endpoint)
            claimed_anchor = edit.get(f"{label}_hash")
            if line_num is None or claimed_anchor is None:
                return ToolResult(
                    success=False,
                    content=_err("MISSING", f"edit[{i}] needs '{endpoint}' and '{label}_hash'"),
                )
            # 1-indexed -> 0-indexed
            idx = line_num - 1
            if idx < 0 or idx >= len(lines):
                return ToolResult(
                    success=False,
                    content=(
                        _err("RANGE", f"edit[{i}] {label}={line_num} > {len(lines)} lines")
                    ),
                )
            actual_anchor = anchors[idx]
            # Strip content suffix if model provided "anchor│content" format.
            _BOX = "\u2502"
            anchor_to_check = claimed_anchor
            content_claim = ""
            if _BOX in claimed_anchor:
                delimiter_idx = claimed_anchor.index(_BOX)
                anchor_to_check = claimed_anchor[:delimiter_idx]
                content_claim = claimed_anchor[delimiter_idx + 1:]

            # Content-based auto-recovery: if the model included the line content
            # in the hash claim (as read_file(hash_lines=True) naturally produces),
            # we can verify correctness even when anchors were refreshed by hot-reload
            # or a chained edit.  This eliminates the need for write_file fallbacks.
            if content_claim:
                if content_claim == lines[idx]:
                    pass  # Content matches -- stale anchor is harmless, proceed
                else:
                    return ToolResult(
                        success=False,
                        content=(
                            _err("CONTENT", f"edit[{i}] {label}={line_num} content changed since last read",
                                 "Re-read the file with read_file(hash_lines=True), then use the CURRENT anchors and content. "
                                 "DO NOT delete the file — that is blocked. Use read+edit, not delete+rewrite.")
                        ),
                    )
            elif anchor_to_check != actual_anchor:
                return ToolResult(
                    success=False,
                    content=(
                        _err("ANCHOR", f"edit[{i}] {label}={line_num} anchor mismatch",
                             "Re-read the file with read_file(hash_lines=True), then use the EXACT anchors shown. "
                             "DO NOT delete the file and rewrite it — that loses git history and is blocked.")
                    ),
                )
    # --- Capture edit positions for output (before any edits) ---
    edit_details: list[dict] = []
    for i, edit in enumerate(edits):
        edit_type = edit.get("edit_type", "replace")
        is_insert = edit_type in ("insert_after", "insert_before")
        f = edit["from"] - 1
        t = edit.get("to", edit["from"]) - 1  # inserts: to defaults to from
        edit_details.append({
            "from_line": edit["from"],
            "to_line": edit["from"] if is_insert else edit["to"],
            "from_anchor_actual": anchors[f],
        })

    # --- Apply edits bottom-up (reverse order by line number) ---
    sorted_edits = sorted(enumerate(edits), key=lambda x: x[1]["from"], reverse=True)
    updated_lines = list(lines)

    for orig_idx, edit in sorted_edits:
        edit_type = edit.get("edit_type", "replace")
        from_line = edit["from"] - 1  # 0-indexed
        to_line = edit.get("to", edit["from"]) - 1  # inserts: to defaults to from
        new_text = edit["new_text"]
        new_lines = new_text.split("\n")

        if edit_type == "insert_after":
            # Dedup: if new_text's first line matches the anchor line, strip it.
            # LLMs often include the anchor line in new_text for insert edits,
            # which would duplicate it. This auto-corrects that common mistake.
            if new_lines and new_lines[0] == lines[from_line]:
                new_lines = new_lines[1:]
            # Insert after the anchor line
            splice_at = from_line + 1
            updated_lines[splice_at:splice_at] = new_lines
        elif edit_type == "insert_before":
            # Dedup: if new_text's last line matches the anchor line, strip it.
            if new_lines and new_lines[-1] == lines[from_line]:
                new_lines = new_lines[:-1]
            # Insert before the anchor line
            splice_at = from_line
            updated_lines[splice_at:splice_at] = new_lines
        else:
            # replace (default): validate and replace range
            if from_line > to_line:
                return ToolResult(
                    success=False,
                    content=(
                        f"edit_file: edit[{orig_idx}] from={from_line + 1} > to={to_line + 1}. "
                        f"'from' must be <= 'to'."
                    ),
                )
            updated_lines[from_line:to_line + 1] = new_lines
    updated = "\n".join(updated_lines)

    # --- Syntax validation for .py files ---
    syntax_error = None
    if resolved.endswith(".py"):
        try:
            compile(original, resolved, "exec")
        except SyntaxError:
            pass  # Existing content isn't valid Python -- skip gate
        else:
            syntax_error = _validate_python_syntax(updated, resolved)
    if syntax_error:
        return ToolResult(
            success=False,
            content=(
                f"Syntax validation failed -- edit NOT applied.\n"
                f"{syntax_error}\n"
                f"File unchanged. Re-read with hash_lines=True and retry."
            ),
        )

    # --- Diff preview (compute before write so we have original vs updated) ---
    from core.safety import DiffPreview
    diff_text = wg._format_diff(resolved, original, updated)
    diff = DiffPreview(preview_text=diff_text, changed=original != updated)

    # --- Write ---
    try:
        _backup_before_write(resolved)
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(updated)
    except Exception as e:
        return ToolResult(success=False, content=_err("WRITE", str(e)))

    from tools import add_modified_file
    add_modified_file(resolved)
    clear_tool_cache()
    

    if path.endswith(".py"):
        from tools.search_ops import _reindex_file
        _reindex_file(resolved, wg.workspace_root)

    _auto_advance_plan(resolved)

    # --- Build compact output matching edit_file format with hash indicator ---
    # --- Build compact output matching edit_file format with hash indicator ---

    total_added = len(updated_lines) - len(lines)
    edit_label = "edits" if len(edits) != 1 else "edit"

    # Line range: use first edit's from_line; if multi-line show range
    first_from = edit_details[0]["from_line"]
    if len(edit_details) == 1 and edit_details[0]["from_line"] == edit_details[0]["to_line"]:
        line_info = f" (line {first_from})"
    else:
        last_to = edit_details[-1]["to_line"]
        if first_from == last_to:
            line_info = f" (line {first_from})"
        else:
            line_info = f" (lines {first_from}\u2013{last_to})"

    # Delta string (plain text)
    delta_str = ""
    if total_added > 0:
        delta_str = f" (+{total_added} lines)"
    elif total_added < 0:
        delta_str = f" ({total_added} lines)"

    # Anchor verification indicator (plain text)
    anchor_label = "anchors" if len(edits) != 1 else "anchor"
    first_anchor = edit_details[0]["from_anchor_actual"]
    anchor_indicator = f"  [anchor \u2713: {first_anchor}]"
    if len(edit_details) > 1:
        last_anchor = edit_details[-1]["from_anchor_actual"]
        anchor_indicator = f"  [anchors \u2713: {first_anchor}\u2026{last_anchor}]"


    # Edit succeeded -- clear the failure tracker so _write_file won't warn
    _RECENT_EDIT_FAILURES.discard(resolved)
    return ToolResult(
        success=True,
        content=(
            f"OK: applied {len(edits)} {edit_label} to {resolved}"
            f"{line_info}{delta_str}"
            f"\n  {anchor_indicator.strip()}"

        ),
        diff_preview=diff.preview_text if diff.changed else None,
    )


@_summarize("edit_file")
@_summarize("edit_lines")
def _edit_lines_summary(args: dict) -> str:
    path = args.get("path", "?")
    edits = args.get("edits", [])
    if not edits:
        return f"edit_file({path}, 0 edits)"

    parts = []
    for edit in edits:
        edit_type = edit.get("edit_type", "replace")
        from_line = edit.get("from")
        from_hash = edit.get("from_hash", "")
        to_line = edit.get("to", from_line)
        to_hash = edit.get("to_hash", "")
        new_text = edit.get("new_text", "")
        new_lines = new_text.count("\n")
        plus = f" +{new_lines}L" if new_lines else ""

        if edit_type in ("insert_after", "insert_before"):
            dir_ = "↓" if edit_type == "insert_after" else "↑"
            parts.append(f"{dir_}L{from_line}[{from_hash}]{plus}")
        else:
            if from_line == to_line:
                parts.append(f"L{from_line}[{from_hash}]{plus}")
            else:
                parts.append(f"L{from_line}[{from_hash}]→L{to_line}[{to_hash}]{plus}")

    details = ", ".join(parts)
    return f"edit_file({path}, {len(edits)} edit{'s' if len(edits) != 1 else ''}: {details})"


# ---------------------------------------------------------------------------
# list_directory
# ---------------------------------------------------------------------------

@_register("list_directory")
def _list_directory(args: dict, _wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:
    path = args.get("path")
    if not path or not isinstance(path, str):
        return ToolResult(
            success=False,
            content="Missing required: path",
            hint="Valid parameters: path",
        )
    bogus_err = _validate_path(path)
    if bogus_err:
        return ToolResult(success=False, content=bogus_err)

    safety_result = rg.check(path)
    if not safety_result.allowed:
        return ToolResult(
            success=False,
            content=f"List blocked by safety layer: {safety_result.reason}",
        )
    try:
        rows: list[str] = []
        with os.scandir(safety_result.resolved_path) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                prefix = "d" if entry.is_dir(follow_symlinks=False) else "f"
                rows.append(f"  [{prefix}] {entry.name}")
        if not rows:
            content = f"{safety_result.resolved_path}  (empty)"
        else:
            content = f"{safety_result.resolved_path}\n" + "\n".join(rows)
        return ToolResult(success=True, content=content)
    except Exception as e:
        return ToolResult(success=False, content=_err("LIST", str(e)))


@_summarize("list_directory")
def _list_directory_summary(args: dict) -> str:
    return f"list_directory({args.get('path', '?')})"


# ---------------------------------------------------------------------------
# file_info
# ---------------------------------------------------------------------------

@_register("file_info")
def _file_info(args: dict, _wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:
    path = args.get("path")
    if not path or not isinstance(path, str):
        return ToolResult(
            success=False,
            content="Missing required: path",
            hint="Valid parameters: path",
        )
    bogus_err = _validate_path(path)
    if bogus_err:
        return ToolResult(success=False, content=bogus_err)

    safety_result = rg.check(path)
    if not safety_result.allowed:
        return ToolResult(
            success=False,
            content=f"File info blocked by safety layer: {safety_result.reason}",
        )
    resolved = safety_result.resolved_path
    try:
        st = os.stat(resolved)
        parts = [
            f"path: {resolved}",
            f"size: {st.st_size} bytes",
            f"mode: {stat_module.filemode(st.st_mode)}",
            f"modified: {time.ctime(st.st_mtime)}",
        ]
        if stat_module.S_ISDIR(st.st_mode):
            parts.append("type: directory")
            # Gather child count and total recursive size
            child_count = 0
            total_size = 0
            try:
                with os.scandir(resolved) as entries:
                    for entry in entries:
                        child_count += 1
                        try:
                            total_size += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            pass
            except PermissionError:
                pass
            parts.append(f"children: {child_count}")
            parts.append(f"total_children_size: {total_size} bytes")
        else:
            parts.append("type: file")
        return ToolResult(success=True, content="\n".join(parts))
    except FileNotFoundError:
        return ToolResult(success=True, content=f"path: {resolved}\nexists: no")
    except Exception as e:
        return ToolResult(success=False, content=_err("STAT", str(e)))


@_summarize("file_info")
def _file_info_summary(args: dict) -> str:
    return f"file_info({args.get('path', '?')})"




@_register("init")
@_summarize("init")
def _init_rules(args: dict, _wg, read_gate: ReadSafetyGate) -> ToolResult:
    """Analyze the workspace and auto-generate .mini_agent.rules + .mini_agent.toml
    and seed project_knowledge with auto-detected learnings."""
    try:
        import subprocess
        import time
        workspace = read_gate.workspace_root
        rules_path = os.path.join(workspace, ".mini_agent.rules")
        toml_path = os.path.join(workspace, ".mini_agent.toml")
        created: list[str] = []
        knowledge: list[tuple[str, str, str, int]] = []  # (summary, category, detail, importance)

        # --- Recursive scan for Python files ---
        py_files_all: list[str] = []
        test_files: list[str] = []
        for root, dirs, files in os.walk(workspace):
            # Skip hidden dirs, venvs, node_modules, __pycache__
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in
                       ('node_modules', 'venv', '.venv', '__pycache__', 'dist', 'build', '.git')]
            for f in files:
                if f.endswith('.py'):
                    full = os.path.join(root, f)
                    py_files_all.append(full)
                    if f.startswith('test_') or f.endswith('_test.py'):
                        test_files.append(full)

        py_files = sorted(py_files_all)

        # --- .mini_agent.rules ---
        rules = [
            f"# Auto-generated by /init on {time.strftime('%Y-%m-%d')}",
            f"# Workspace: {workspace}",
            "", "## Code Style",
            "- Use type hints on all public functions.",
            "- Prefer dataclasses for structured data.",
            "- No magic numbers; use named constants.",
            "- Keep modules small and single-purpose.",
            "", "## Testing",
            "- Run tests with: python -m pytest -q",
            "", "## Module Map",
        ]
        for pf in py_files[:25]:
            rules.append(f"  {os.path.basename(pf)}  # auto-detected")
        with open(rules_path, "w", encoding="utf-8") as f:
            f.write("\n".join(rules))
        created.append(f".mini_agent.rules ({len(rules)} lines, {len(py_files)} modules)")

        # --- .mini_agent.toml (if missing) ---
        if not os.path.isfile(toml_path):
            toml = [
                "# Auto-generated by /init on " + time.strftime('%Y-%m-%d'),
                "",
                "[agent]",
                "# model = \"deepseek-v4-pro\"",
                "# max_messages = 500",
                "# max_tokens = 200000",
                "# stream = false",
                "# allow_overwrites = false",
                "# unrestricted = false",
            ]
            with open(toml_path, "w", encoding="utf-8") as f:
                f.write("\n".join(toml))
            created.append(".mini_agent.toml (template)")
        else:
            created.append(".mini_agent.toml (already exists, skipped)")

        # --- Auto-detect workspace learnings for project_knowledge ---
        # 1. Module count
        if py_files:
            knowledge.append((
                f"Workspace has {len(py_files)} Python module(s)",
                "workspace", f"Total .py files: {len(py_files)}. Test files: {len(test_files)}.",
                2,
            ))
        if test_files:
            knowledge.append((
                f"{len(test_files)} test file(s) detected",
                "testing", f"Test files: {', '.join(os.path.basename(t) for t in test_files[:10])}.",
                3,
            ))

        # 2. Import-based framework detection (sample first 20 files)
        frameworks: dict[str, str] = {}
        known_frameworks = {
            'fastapi': 'web', 'flask': 'web', 'django': 'web', 'starlette': 'web',
            'pytest': 'testing', 'unittest': 'testing',
            'torch': 'ml', 'tensorflow': 'ml', 'jax': 'ml', 'transformers': 'ml',
            'pandas': 'data', 'numpy': 'data', 'polars': 'data',
            'click': 'cli', 'typer': 'cli', 'argparse': 'cli',
            'sqlalchemy': 'database', 'sqlite3': 'database',
            'pydantic': 'validation', 'dataclasses': 'data',
            'rich': 'ui', 'textual': 'ui',
        }
        sample = py_files[:min(20, len(py_files))]
        for pf in sample:
            try:
                with open(pf, encoding="utf-8", errors="replace") as f:
                    content = f.read(4096)
                for line in content.split('\n')[:80]:
                    line_stripped = line.strip()
                    if line_stripped.startswith(('import ', 'from ')):
                        for kw, cat in known_frameworks.items():
                            if kw in line_stripped and kw not in frameworks:
                                frameworks[kw] = cat
            except Exception:
                pass
        for framework, cat in sorted(frameworks.items()):
            knowledge.append((
                f"Uses {framework} ({cat})",
                "dependencies", f"Detected import of {framework} in workspace source.",
                2,
            ))

        # 3. Git repo detection
        if os.path.isdir(os.path.join(workspace, ".git")):
            try:
                result = subprocess.run(
                    ["git", "branch", "--show-current"],
                    cwd=workspace, capture_output=True, text=True, timeout=3,
                )
                branch = result.stdout.strip()
                git_info = f"branch: {branch}" if branch else "git repo detected"
            except Exception:
                git_info = "git repo detected"
            knowledge.append((
                f"Git repository: {git_info}",
                "workspace", "Project is version-controlled with git.",
                2,
            ))

        # 4. Language detection (look for non-Python files)
        other_exts: set[str] = set()
        for root, dirs, files in os.walk(workspace):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in
                       ('node_modules', 'venv', '.venv', '__pycache__', 'dist', 'build', '.git')]
            for f in files:
                _, ext = os.path.splitext(f)
                if ext and ext != '.py' and ext not in ('.pyc', '.pyo', '.pyd', '.so', '.dylib'):
                    other_exts.add(ext)
            if len(other_exts) >= 10:
                break
        if other_exts:
            knowledge.append((
                f"Multi-language: {', '.join(sorted(other_exts)[:10])}",
                "workspace", f"Non-Python file types detected: {', '.join(sorted(other_exts))}.",
                1,
            ))

        # --- Store knowledge to project_knowledge table ---
        from tools import _TOOL_CONTEXT
        memory_store = getattr(_TOOL_CONTEXT, '_memory_store', None)
        if memory_store and knowledge:
            stored = 0
            for summary, category, detail, importance in knowledge:
                existing = memory_store.find_knowledge(category, summary)
                if existing:
                    memory_store.bump_knowledge(existing["id"])
                else:
                    memory_store.add_knowledge(summary, category, detail, importance)
                stored += 1
            created.append(f"{stored} project learnings")

        return ToolResult(success=True,
            content=f"Initialized workspace: {', '.join(created)}.")
    except Exception as e:
        return ToolResult(success=False, content=_err("INIT", str(e)))


# ---------------------------------------------------------------------------
# get_file_skeleton
# ---------------------------------------------------------------------------

@_register("get_file_skeleton")
def _get_file_skeleton(args: dict, wg: WriteSafetyGate, rg: ReadSafetyGate,
                      on_output=None, approve_callback=None, cancel_event=None) -> ToolResult:
    """Extract the structural skeleton of one or more files."""
    paths = args.get("paths", [])
    if isinstance(paths, str):
        paths = [paths]
    if not paths:
        return ToolResult(success=False, content=_err("MISSING", "paths"))

    include_anchors = args.get("include_anchors", True)
    is_subagent = getattr(_TOOL_CONTEXT, "_is_subagent", False)
    task_id = getattr(_current_agent_id, "task_id", None)

    results = []
    for rel_path in paths:
        safety = rg.check(rel_path)
        if not safety.allowed:
            results.append(f"--- {rel_path} ---\n{_err('BLOCKED', safety.reason)}")
            continue
        try:
            # Pre-read file content (one disk read) and cache it
            try:
                with open(safety.resolved_path, "r", encoding="utf-8", errors="replace") as _f:
                    source = _f.read()
            except OSError:
                source = None  # fall through; get_file_skeleton will read it
            _READ_FILES.add(safety.resolved_path)
            skeleton = get_file_skeleton(
                safety.resolved_path,
                include_anchors=include_anchors,
                show_call_graph=True,
                task_id=task_id,
                _source=source,
            )
            # Detect "not found" / error cases from the skeleton text
            error_prefixes = ("No definitions found", "Empty file", "Unsupported file type",
                             "Could not read file", "Could not parse")
            if skeleton.startswith(error_prefixes):
                results.append(f"--- {rel_path} ---\nError: {skeleton}")
            else:
                results.append(f"--- {rel_path} ---\n{skeleton}")
        except Exception as e:
            results.append(f"--- {rel_path} ---\n{_err('ERROR', str(e))}")

    if not results:
        return ToolResult(success=False, content=_err("NOT_FOUND", "no definitions"))

    # Check if all results are errors
    all_errors = all(
        "Error:" in r.split("\n", 1)[1] if "\n" in r else "Error:" in r
        for r in results
    ) if results else False

    content = "\n\n".join(results)
    if all_errors:
        return ToolResult(success=False, content=content)
    return ToolResult(success=True, content=content)


@_summarize("get_file_skeleton")
def _get_file_skeleton_summary(args: dict) -> str:
    paths = args.get("paths", [])
    if isinstance(paths, str):
        paths = [paths]
    if not paths:
        return "get_file_skeleton(0 files)"
    # Show first 3 paths, with count if more
    shown = ", ".join(paths[:3])
    if len(paths) <= 3:
        return f"get_file_skeleton({shown})"
    return f"get_file_skeleton({shown} +{len(paths)-3} more)"


# ---------------------------------------------------------------------------
# get_function
# ---------------------------------------------------------------------------

@_register("get_function")
def _get_function(args: dict, wg: WriteSafetyGate, rg: ReadSafetyGate,
                  on_output=None, approve_callback=None, cancel_event=None) -> ToolResult:
    """Extract complete implementation of specific functions."""
    paths = args.get("paths", [])
    if isinstance(paths, str):
        paths = [paths]
    function_names = args.get("function_names", [])
    if isinstance(function_names, str):
        function_names = [function_names]

    if not paths:
        return ToolResult(success=False, content=_err("MISSING", "paths"))
    if not function_names:
        return ToolResult(success=False, content=_err("MISSING", "function_names"))

    include_anchors = args.get("include_anchors", True)
    task_id = getattr(_current_agent_id, "task_id", None)

    results = []
    all_found = []
    for rel_path in paths:
        safety = rg.check(rel_path)
        if not safety.allowed:
            results.append(f"--- {rel_path} ---\n{_err('BLOCKED', safety.reason)}")
            continue
        try:
            # Pre-read file content (one disk read) and cache it
            try:
                with open(safety.resolved_path, "r", encoding="utf-8", errors="replace") as _f:
                    source = _f.read()
            except OSError:
                source = None  # fall through; get_function will read it
            _READ_FILES.add(safety.resolved_path)
            content, found = get_function(
                safety.resolved_path,
                function_names,
                include_anchors=include_anchors,
                task_id=task_id,
                _source=source,
            )
            # Separate "found" results from "not found" / error messages
            if found:
                if content:
                    results.append(content)
                all_found.extend(found)
            else:
                # No functions found in this file -- normal when searching across multiple files
                results.append(f"--- {rel_path} ---\n{content}")
        except Exception as e:
            results.append(f"--- {rel_path} ---\n{_err('ERROR', str(e))}")

    if not results:
        return ToolResult(
            success=False,
            content=_err("NOT_FOUND", f"functions {function_names}")
        )

    # Check if all results are error lines (no functions found)
    all_errors = all(
        r.startswith("---") and "Error:" in r.split("\n", 1)[1] if "\n" in r else False
        for r in results
    ) if results else False

    # Report which functions were NOT found (if any)
    not_found = [fn for fn in function_names if fn not in all_found]
    content = "\n\n".join(results)
    if not_found:
        content += f"\n\nNote: {len(not_found)} function(s) not found: {', '.join(not_found)}"

    if all_errors:
        return ToolResult(success=False, content=content)
    return ToolResult(success=True, content=content)


@_summarize("get_function")
def _get_function_summary(args: dict) -> str:
    paths = args.get("paths", [])
    if isinstance(paths, str):
        paths = [paths]
    fns = args.get("function_names", [])
    if isinstance(fns, str):
        fns = [fns]
    # Show paths + functions
    path_str = ", ".join(paths[:3])
    if len(paths) > 3:
        path_str += f" +{len(paths)-3} more"
    fn_str = ", ".join(fns[:5])
    if len(fns) > 5:
        fn_str += f" +{len(fns)-5} more"
    return f"get_function({fn_str})" if not paths else f"get_function({fn_str} in {path_str})"


# ---------------------------------------------------------------------------
# replace_symbol
# ---------------------------------------------------------------------------

@_register("replace_symbol")
def _replace_symbol(args: dict, wg: WriteSafetyGate, rg: ReadSafetyGate,
                    on_output=None, approve_callback=None, cancel_event=None) -> ToolResult:
    """Replace symbol(s) using AST-precise byte ranges. Supports both single and batch."""
    replacements = args.get("replacements", [])
    path = args.get("path", "")
    symbol = args.get("symbol", "")
    text = args.get("text", "")
    sym_type = args.get("type", "function")

    # Build replacement list: batch takes priority, then single
    if replacements:
        # Dirac-style batch replacements
        if not isinstance(replacements, list):
            return ToolResult(success=False, content="Error: 'replacements' must be an array.")
    elif path and symbol and text:
        # Legacy single replacement
        replacements = [{"path": path, "symbol": symbol, "text": text, "type": sym_type}]
    else:
        return ToolResult(success=False, content="Error: Provide either 'replacements' array or 'path'+'symbol'+'text'.")

    results = []
    for i, repl in enumerate(replacements):
        repl_path = repl.get("path", "")
        repl_symbol = repl.get("symbol", "")
        repl_text = repl.get("text", "")
        repl_type = repl.get("type", "function")

        if not repl_path or not repl_symbol or not repl_text:
            results.append(f"[{i+1}/{len(replacements)}] Error: Missing required fields for '{repl_symbol or '?'}'")
            continue

        safety = wg.check(repl_path)
        if not safety.allowed:
            results.append(f"[{i+1}/{len(replacements)}] Access denied: {safety.reason}")
            continue

        # Read-before-edit enforcement
        if safety.resolved_path not in _READ_FILES:
            results.append(
                _err("GUARD", f"[{i+1}/{len(replacements)}] read_file('{safety.resolved_path}') first")
            )
            continue

        try:
            _backup_before_write(safety.resolved_path)

            # Read original for diff preview
            try:
                with open(safety.resolved_path, "r", encoding="utf-8", errors="replace") as f:
                    original = f.read()
            except OSError:
                original = ""

            result_msg = replace_symbol(safety.resolved_path, repl_symbol, repl_text, symbol_type=repl_type)
            clear_tool_cache()
            # Cache will be updated after diff read below
            if repl_path.endswith(".py"):
                from tools.search_ops import _reindex_file
                _reindex_file(safety.resolved_path, wg.workspace_root)
            _auto_advance_plan(safety.resolved_path, repl_text)

            # Generate diff preview
            try:
                with open(safety.resolved_path, "r", encoding="utf-8", errors="replace") as f:
                    updated = f.read()
            except OSError:
                updated = repl_text

            

            diff_text = wg._format_diff(safety.resolved_path, original, updated)
            diff_changed = original != updated
            diff_preview = diff_text if diff_changed else None

            results.append({
                "msg": f"[{i+1}/{len(replacements)}] {result_msg}",
                "diff_preview": diff_preview,
            })
        except Exception as e:
            results.append(f"[{i+1}/{len(replacements)}] Error replacing '{repl_symbol}': {e}")

    # Build content string and combine diff previews
    content_lines = []
    combined_diffs = []
    for r in results:
        if isinstance(r, dict):
            content_lines.append(r.get("msg", ""))
            if r.get("diff_preview"):
                combined_diffs.append(r["diff_preview"])
        else:
            content_lines.append(r)

    content = "\n".join(content_lines)
    diff_preview = "\n\n".join(combined_diffs) if combined_diffs else None
    return ToolResult(success=True, content=content, diff_preview=diff_preview)


@_summarize("replace_symbol")
def _replace_symbol_summary(args: dict) -> str:
    replacements = args.get("replacements", [])
    if replacements:
        # Batch mode — show symbol@path pairs
        items = []
        for r in replacements[:3]:
            sym = r.get("symbol", "?")
            p = r.get("path", "?")
            items.append(f"{sym}@{p}")
        shown = ", ".join(items)
        if len(replacements) <= 3:
            return f"replace_symbol({shown})"
        return f"replace_symbol({shown} +{len(replacements)-3} more)"
    # Legacy single mode
    return f"replace_symbol({args.get('symbol', '?')}@{args.get('path', '?')})"


# ---------------------------------------------------------------------------
# get_symbol_range
# ---------------------------------------------------------------------------


@_register("get_symbol_range")
def _get_symbol_range(args: dict, wg: WriteSafetyGate, rg: ReadSafetyGate,
                    on_output=None, approve_callback=None, cancel_event=None) -> ToolResult:
    """Get the precise AST byte range of a symbol."""
    path = args.get("path", "")
    symbol = args.get("symbol", "")
    sym_type = args.get("type")

    if not path or not symbol:
        return ToolResult(
            success=False,
            content="Error: Missing required parameters 'path' and 'symbol'.",
        )

    safety = rg.check(path)
    if not safety.allowed:
        return ToolResult(success=False, content=_err("BLOCKED", safety.reason))

    try:
        # Pre-read file content (one disk read) and cache it
        try:
            with open(safety.resolved_path, "r", encoding="utf-8", errors="replace") as _f:
                source = _f.read()
        except OSError:
            source = None  # fall through; get_symbol_range will read it
        _READ_FILES.add(safety.resolved_path)
        result = get_symbol_range(safety.resolved_path, symbol, type=sym_type, _source=source)
        if result is None:
            return ToolResult(
                success=False,
                content=f"Symbol '{symbol}' not found or unsupported file type in {path}.",
            )

        return ToolResult(
            success=True,
            content=(
                f"--- {path} :: {result['nameText']} ---\n"
                f"startIndex: {result['startIndex']}\n"
                f"endIndex: {result['endIndex']}\n"
                f"startLine: {result['startLine']}\n"
                f"(Use these byte offsets with replace_symbol for precise replacement.)"
            ),
        )
    except Exception as e:
        return ToolResult(success=False, content=_err("ERROR", str(e)))


@_summarize("get_symbol_range")
def _get_symbol_range_summary(args: dict) -> str:
    return f"get_symbol_range({args.get('symbol', '?')} in {args.get('path', '?')})"
