#!/usr/bin/env python3
"""
file_ops.py -- file/directory tools for mini_agent.

Tools: read_file, write_file, edit_file, list_directory, file_info
"""
from __future__ import annotations

import os
import platform
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

# Thread-local: current sub-agent task_id (set by agent_ops before tool execution)
import threading

from core.file_context_tracker import get_tracker
_current_agent_id: threading.local = threading.local()

_WINDOWS = platform.system() == "Windows"

# ---------------------------------------------------------------------------
# Windows-safe file read via _worker subprocess
# ---------------------------------------------------------------------------
# On Windows, ``open()`` / ``CreateFileW`` can block indefinitely inside
# kernel minifilter drivers (antivirus, backup agents, etc.).  Python
# threads have no way to kill a thread stuck in a kernel I/O call.
# The _worker subprocess isolates the I/O so the OS can kill it with
# TerminateProcess if it doesn't respond within the timeout.

_WORKER_READ_TIMEOUT = 30  # seconds for a single file read


def _read_file_windows_worker(
    resolved: str, offset: int, limit: int, line_numbers: bool,
) -> ToolResult:
    """Read a file via the _worker subprocess with a hard timeout.

    Falls back to direct open() if the worker fails for non-hang reasons.

    Uses _communicate_windows() on Windows to avoid proc.communicate() hangs.
    """
    if _WINDOWS:
        try:
            proc = subprocess.run(
                [
                    sys.executable, "-m", "tools._worker", "read",
                    resolved, str(offset), str(limit), str(line_numbers),
                ],
                capture_output=True,
                text=True,
                timeout=_WORKER_READ_TIMEOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            stdout, stderr = proc.stdout, proc.stderr
            import json
            data = json.loads(stdout.strip())
            if data.get("ok"):
                return ToolResult(success=True, content=data["content"])
            else:
                return ToolResult(
                    success=False,
                    content=data.get("content", "Worker read failed"),
                )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                content=f"File read timed out after {_WORKER_READ_TIMEOUT}s "
                        f"(possibly blocked by antivirus or filter driver). "
                        f"Try excluding the project directory from real-time scanning.",
            )
        except Exception:
            pass
    else:
        try:
            result = subprocess.run(
                [
                    sys.executable, "-m", "tools._worker", "read",
                    resolved, str(offset), str(limit), str(line_numbers),
                ],
                capture_output=True, text=True, timeout=_WORKER_READ_TIMEOUT,
            )
            import json
            data = json.loads(result.stdout.strip())
            if data.get("ok"):
                return ToolResult(success=True, content=data["content"])
            else:
                return ToolResult(
                    success=False,
                    content=data.get("content", "Worker read failed"),
                )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                content=f"File read timed out after {_WORKER_READ_TIMEOUT}s "
                        f"(possibly blocked by antivirus or filter driver). "
                        f"Try excluding the project directory from real-time scanning.",
            )
        except Exception:
            pass

    # Fallback: direct open (may hang on Windows but we already tried)
    return _read_file_direct(resolved, offset, limit, line_numbers)

def _read_file_direct(
    resolved: str, offset: int, limit: int, line_numbers: bool,
    hash_lines: bool = False,
) -> ToolResult:
    """Direct file read -- used on Unix and as fallback on Windows.

    When hash_lines=True, uses AnchorStateManager for Dirac-style word
    anchors that persist across edits (unchanged lines keep their anchor).
    """
    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            all_lines = [line.rstrip("\n") for line in f]
    except Exception as e:
        hint = ""
        if isinstance(e, FileNotFoundError) or "No such file" in str(e):
            hint = "\nHint: Check the path spelling. Try list_directory to see available files."
        return ToolResult(success=False, content=f"Error reading '{resolved}': {e}{hint}")

    total_lines = len(all_lines)

    if offset > 0 and offset >= total_lines:
        return ToolResult(success=False, content=f"Offset {offset} exceeds file length ({total_lines} lines).")

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
            collected.append(f"{lineno:>{gutter_width}} {anchor}\u2502 {line}")
        elif line_numbers:
            collected.append(f"{lineno:>{gutter_width}} {line}")
        else:
            collected.append(line)

    # Actual lines remaining after offset
    lines_after_offset = total_lines - offset

    if lines_after_offset > limit:
        truncated = "\n".join(collected[:limit])
        msg = (
            f"{truncated}\n"
            f"... (truncated at {limit} lines -- {lines_after_offset} total in selection. "
            f"Use a higher limit or offset to see more.)"
        )
        return ToolResult(success=True, content=msg)

    full_content = "\n".join(collected)

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

    Uses ``compile()`` for fast in-process validation.  Only checks .py files.
    """
    if not filepath.endswith(".py"):
        return None
    try:
        compile(content, filepath, "exec")
    except SyntaxError as e:
        # Build a helpful pointer line
        lines = content.split("\n")
        lineno = e.lineno or 1
        pointer = f"  line {lineno}: {lines[lineno - 1][:100] if lineno <= len(lines) else '?'}"
        return (
            f"SyntaxError: {e.msg} at line {lineno}\n"
            f"{pointer}"
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

# Cross-turn file content cache -- avoids re-reading files whose mtime hasn't changed.
# Key: resolved path (str), Value: (content: str, mtime: float)
# Capped at _FILE_CACHE_MAX entries; oldest entries are evicted (LRU via insertion order).
_FILE_CACHE: dict[str, tuple[str, float]] = {}
_FILE_CACHE_MAX = 50
_CACHE_DISK_READS = 0  # Diagnostic: counts cache-miss disk reads in edit paths

# Per-task file content hashes — used to short-circuit re-reads when content hasn't
# changed since last read (Dirac pattern: "no changes since your last read").
# Key: "{resolved_path}#anchored" or "{resolved_path}#plain", Value: content hash.
_FILE_HASHES: dict[str, str] = {}

# Maximum file size for full reads before warning (50KB). Larger files should be
# read with offset/limit or via get_file_skeleton / get_function.
_MAX_FILE_READ_SIZE = 50 * 1024

def _cache_file_content(resolved_path: str, _source: str | None = None) -> None:
    """Populate _FILE_CACHE with file content (LRU eviction).

    Args:
        resolved_path: Absolute file path (cache key).
        _source: Pre-read file content. When provided, skips the disk read.
    """
    try:
        if resolved_path in _FILE_CACHE:
            return  # already cached
        current_mtime = os.path.getmtime(resolved_path)
        # Evict oldest if at capacity
        if len(_FILE_CACHE) >= _FILE_CACHE_MAX:
            _FILE_CACHE.pop(next(iter(_FILE_CACHE)), None)
        if _source is not None:
            _FILE_CACHE[resolved_path] = (_source, current_mtime)
        else:
            with open(resolved_path, "r", encoding="utf-8", errors="replace") as _f:
                _FILE_CACHE[resolved_path] = (_f.read(), current_mtime)
    except OSError:
        pass


def _get_cached_content(resolved_path: str) -> str | None:
    """Return cached file content if mtime matches, otherwise None."""
    cached = _FILE_CACHE.get(resolved_path)
    if cached is None:
        return None
    content, cached_mtime = cached
    try:
        if os.path.getmtime(resolved_path) == cached_mtime:
            return content
    except OSError:
        pass
    # Stale cache -- evict and return None
    _FILE_CACHE.pop(resolved_path, None)
    return None


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
            content="Missing required parameter: provide either 'path' (string) or 'paths' (array of strings).",
            hint="Valid parameters: path (string), paths (array), offset, limit, line_numbers, hash_lines",
        )

    offset = args.get("offset", 0)
    if offset < 0:
        offset = 0
    limit = args.get("limit", _DEFAULT_READ_LINES)
    if limit < 1:
        limit = _DEFAULT_READ_LINES
    limit = min(limit, _ABSOLUTE_MAX_LINES)
    line_numbers = args.get("line_numbers", False)
    hash_lines = args.get("hash_lines", True)

    results: list[str] = []
    any_failed = False

    for path in file_paths:
        safety_result = rg.check(path)
        if not safety_result.allowed:
            results.append(f"--- {path} ---\n[BLOCKED] Read blocked by safety layer: {safety_result.reason}")
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

        # --- Hash-based re-read shortcut (Dirac: "no changes since your last read") ---
        cache_key = f"{resolved}#{'anchored' if hash_lines else 'plain'}"
        try:
            with open(resolved, "rb") as f:
                raw = f.read()
            current_hash = hashlib.md5(raw).hexdigest()
        except OSError:
            current_hash = None

        if current_hash is not None:
            last_hash = _FILE_HASHES.get(cache_key)
            if last_hash == current_hash and offset == 0 and limit >= _DEFAULT_READ_LINES:
                header = f"--- {path} ---\n" if is_multi else ""
                results.append(f"{header}no changes have been made to the file since your last read (Hash: {current_hash})")
                _READ_FILES.add(resolved)
                continue

        # --- Cross-turn mtime cache (disk I/O bypass) ---
        if offset == 0 and limit == _DEFAULT_READ_LINES and not line_numbers and not hash_lines:
            try:
                current_mtime = os.path.getmtime(resolved)
                if resolved in _FILE_CACHE:
                    cached_content, cached_mtime = _FILE_CACHE[resolved]
                    if cached_mtime == current_mtime:
                        _FILE_HASHES[cache_key] = current_hash
                        header = f"--- {path} ---\n" if is_multi else ""
                        results.append(header + cached_content)
                        _READ_FILES.add(resolved)
                        continue
            except OSError:
                pass

        # --- Actual read ---
        if False:  # _WINDOWS bypassed
            result = _read_file_windows_worker(resolved, offset, limit, line_numbers)
        else:
            result = _read_file_direct(resolved, offset, limit, line_numbers, hash_lines=hash_lines)

        if not result.success:
            header = f"--- {path} ---\n" if is_multi else ""
            results.append(header + result.content)
            any_failed = True
            continue

        full_content = result.content

        # --- Update caches ---
        if current_hash is not None:
            _FILE_HASHES[cache_key] = current_hash

        if offset == 0 and "... (truncated at " not in full_content and not hash_lines and not line_numbers:
            try:
                current_mtime = os.path.getmtime(resolved)
                if len(_FILE_CACHE) >= _FILE_CACHE_MAX and resolved not in _FILE_CACHE:
                    _FILE_CACHE.pop(next(iter(_FILE_CACHE)), None)
                _FILE_CACHE[resolved] = (full_content, current_mtime)
            except OSError:
                pass

        _READ_FILES.add(resolved)

        # Track for stale-context detection (FileContextTracker)
        try:
            tracker = get_tracker(getattr(_current_agent_id, "task_id", ""))
            tracker.mark_file_read(resolved)
        except Exception:
            pass

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
    path = args["path"]
    content = args["content"]
    safety_result = wg.check(path)
    if not safety_result.allowed:
        return ToolResult(
            success=False,
            content=(
                f"Write blocked by safety layer: {safety_result.reason}\n"
                f"Hint: Use a path inside the workspace ({wg.workspace_root}) or enable unrestricted mode."
            ),
        )
    # Read-before-edit enforcement (ACI upgrade): reject writes to
    # .py files that haven't been read_file'd this session, unless
    # the file doesn't exist yet (new file creation is allowed).
    _resolved = safety_result.resolved_path
    if _resolved.endswith(".py") and os.path.isfile(_resolved) and _resolved not in _READ_FILES:
        return ToolResult(
            success=False,
            content=(
                f"Read-before-edit guard: '{_resolved}' has not been read this session. "
                f"Read the file with read_file first so you have the current content "
                f"before writing. This prevents accidental overwrites of recent changes."
            ),
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
                    f"Syntax validation failed -- file NOT written to prevent broken code.\n"
                    f"{syntax_error}"
                ),
            )
        with open(safety_result.resolved_path, "w", encoding="utf-8") as f:
            f.write(content)
        from tools import add_modified_file
        add_modified_file(safety_result.resolved_path)
        clear_tool_cache()
        # Invalidate cross-turn file cache
        _FILE_CACHE.pop(safety_result.resolved_path, None)
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
        return ToolResult(
            success=True,
            content=f"OK: wrote {len(content)} bytes to {safety_result.resolved_path}",
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


@_register("edit_file")
def _edit_file(args: dict, wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    # --- Hash-anchored mode (primary) ---
    from_line = args.get("from")
    from_hash = args.get("from_hash")
    if from_line is not None and from_hash is not None:
        # Build a single-edit edit_lines call
        edit_type = args.get("edit_type", "replace")
        if edit_type in ("insert_after", "insert_before"):
            to_line = from_line  # not used for insert; set same as from for validation
            to_hash = from_hash
        else:
            to_line = args.get("to", from_line)
            to_hash = args.get("to_hash", from_hash)
        new_text = args.get("new_text", "")
        preview = args.get("preview", False)
        path = args["path"]

        edit_args = {
            "path": path,
            "edits": [{
                "from": from_line,
                "from_hash": from_hash,
                "to": to_line,
                "to_hash": to_hash,
                "new_text": new_text,
                "edit_type": edit_type,
            }],
        }
        if preview:
            edit_args["preview"] = preview

        # Delegate to edit_lines for hash-anchored editing
        return _edit_lines(edit_args, wg, _rg)

    # --- Hash-anchored mode is now the only mode ---
    return ToolResult(
        success=False,
        content=(
            "edit_file: hash-anchored editing is now the only mode. "
            "Provide (from, from_hash) at minimum. "
            "Use read_file(hash_lines=True) first to get anchors. "
            "Full params: (from, from_hash, to, to_hash, new_text, edit_type, preview)."
        ),
    )


@_summarize("edit_file")
def _edit_file_summary(args: dict) -> str:
    path = args.get("path", "?")
    from_line = args.get("from")
    from_hash = args.get("from_hash")
    if from_line is not None and from_hash is not None:
        to_line = args.get("to", from_line)
        preview_flag = args.get("preview", False)
        suffix = " [preview]" if preview_flag else ""
        if from_line == to_line:
            return f"edit_file({path}, line {from_line}, hash {from_hash}){suffix}"
        else:
            return f"edit_file({path}, lines {from_line}-{to_line}){suffix}"
    return f"edit_file({path}, missing hash params)"


# ---------------------------------------------------------------------------
# edit_lines -- hash-anchored editing (Hashlines pattern from Akay/Howard Chen)
# ---------------------------------------------------------------------------

@_register("edit_lines")
def _edit_lines(args: dict, wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Replace line ranges using word anchors for reliable first-attempt edits.

    Each edit specifies {from, from_hash, to, to_hash, new_text}.
    The file is re-read fresh; word anchors are recomputed and validated
    before any edit is applied.  Unchanged lines keep their anchors
    across edits (Dirac-style persistence).  Edits are applied bottom-up
    so line numbers in the edits array can refer to the pre-edit file.

    On any anchor mismatch the ENTIRE batch is rejected with a precise error.
    """
    path = args["path"]
    edits = args["edits"]

    safety_result = wg.check(path)
    if not safety_result.allowed:
        return ToolResult(
            success=False,
            content=f"Edit blocked by safety layer: {safety_result.reason}",
        )
    resolved = safety_result.resolved_path

    if not isinstance(edits, list) or not edits:
        return ToolResult(success=False, content="'edits' must be a non-empty list.")

    # --- Read-before-edit enforcement ---
    if resolved not in _READ_FILES:
        return ToolResult(
            success=False,
            content=(
                f"Read '{resolved}' first with read_file(hash_lines=True) to get anchors"
            ),
        )

    # File reservation check
    agent_id = getattr(_current_agent_id, "task_id", None)
    if agent_id is not None:
        from tools import reserve_file
        ok, msg = reserve_file(path, agent_id)
        if not ok:
            return ToolResult(success=False, content=msg)

    # Always read raw file content from disk (never use _FILE_CACHE, which may
    # contain formatted/anchor-prefixed content from hash_lines reads).
    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            original = f.read()
    except Exception as e:
        return ToolResult(success=False, content=f"Error reading '{resolved}': {e}")


    lines = original.split("\n")
    # Use Dirac-style word anchors (persistent across edits)
    from core.anchor_manager import AnchorStateManager
    task_id = getattr(_current_agent_id, "task_id", None)
    anchors = AnchorStateManager.reconcile(resolved, lines, task_id)

    # --- Validate all word anchors first ---
    for i, edit in enumerate(edits):
        edit_type = edit.get("edit_type", "replace")
        is_insert = edit_type in ("insert_after", "insert_before")
        # For insert edits, only validate 'from' anchor; 'to' is ignored
        endpoints = [("from", "from")] if is_insert else [("from", "from"), ("to", "to")]
        for endpoint, label in endpoints:
            line_num = edit.get(endpoint)
            claimed_anchor = edit.get(f"{label}_hash")
            if line_num is None or claimed_anchor is None:
                return ToolResult(
                    success=False,
                    content=f"edit_lines: edit[{i}] missing '{endpoint}' or '{label}_hash'.",
                )
            # 1-indexed -> 0-indexed
            idx = line_num - 1
            if idx < 0 or idx >= len(lines):
                return ToolResult(
                    success=False,
                    content=(
                        f"edit_lines: edit[{i}] {label}={line_num} is out of range "
                        f"(file has {len(lines)} lines)."
                    ),
                )
            actual_anchor = anchors[idx]
            # Strip content suffix if model provided "anchor│content" format
            _BOX = "\u2502"
            anchor_to_check = claimed_anchor
            content_claim = ""
            if _BOX in claimed_anchor:
                delimiter_idx = claimed_anchor.index(_BOX)
                anchor_to_check = claimed_anchor[:delimiter_idx]
                content_claim = claimed_anchor[delimiter_idx + 1:].lstrip()
            if anchor_to_check != actual_anchor:
                return ToolResult(
                    success=False,
                    content=(
                        f"edit_lines: edit[{i}] {label} line {line_num} anchor mismatch -- "
                        f"stale anchor '{anchor_to_check}' vs file content. "
                        f"Re-run read_file(hash_lines=True) to get current anchors."
                    ),
                )
            # Content cross-check: if model provided content, verify it matches
            if content_claim and content_claim != lines[idx]:
                return ToolResult(
                    success=False,
                    content=(
                        f"edit_lines: edit[{i}] {label} content mismatch at line {line_num}. "
                        f"Re-run read_file(hash_lines=True) to get current content."
                    ),
                )
    # --- Capture edit positions for output (before any edits) ---
    edit_details: list[dict] = []
    for i, edit in enumerate(edits):
        edit_type = edit.get("edit_type", "replace")
        is_insert = edit_type in ("insert_after", "insert_before")
        f = edit["from"] - 1
        t = edit["to"] - 1
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
        to_line = edit["to"] - 1      # 0-indexed (same as from_line for inserts)
        new_text = edit["new_text"]
        new_lines = new_text.split("\n")

        if edit_type == "insert_after":
            # Insert after the anchor line
            splice_at = from_line + 1
            updated_lines[splice_at:splice_at] = new_lines
        elif edit_type == "insert_before":
            # Insert before the anchor line
            splice_at = from_line
            updated_lines[splice_at:splice_at] = new_lines
        else:
            # replace (default): validate and replace range
            if from_line > to_line:
                return ToolResult(
                    success=False,
                    content=(
                        f"edit_lines: edit[{orig_idx}] from={from_line + 1} > to={to_line + 1}. "
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
                f"Syntax validation failed -- edit NOT applied to prevent broken code.\n"
                f"{syntax_error}\n"
                f"The file is unchanged."
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
        return ToolResult(success=False, content=f"Error writing '{resolved}': {e}")

    from tools import add_modified_file
    add_modified_file(resolved)
    clear_tool_cache()
    # Update cache with new content so chained edits skip disk I/O
    try:
        _FILE_CACHE[resolved] = (updated, os.path.getmtime(resolved))
    except OSError:
        _FILE_CACHE.pop(resolved, None)

    if path.endswith(".py"):
        from tools.search_ops import _reindex_file
        _reindex_file(resolved, wg.workspace_root)

    _auto_advance_plan(resolved)

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

    return ToolResult(
        success=True,
        content=(
            f"OK: applied {len(edits)} {edit_label} to {resolved}"
            f"{line_info}{delta_str}"
            f"\n  {anchor_indicator.strip()}"
            f"  [\u26a0 anchors preserved for unchanged lines \u2013 chain edits without re-reading]"
        ),
        diff_preview=diff.preview_text if diff.changed else None,
    )


@_summarize("edit_lines")
def _edit_lines_summary(args: dict) -> str:
    path = args.get("path", "?")
    edits = args.get("edits", [])
    if not edits:
        return f"edit_lines({path}, 0 edits)"

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
    return f"edit_lines({path}, {len(edits)} edit{'s' if len(edits) != 1 else ''}: {details})"


# ---------------------------------------------------------------------------
# list_directory
# ---------------------------------------------------------------------------

@_register("list_directory")
def _list_directory(args: dict, _wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:
    path = args["path"]
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
        return ToolResult(success=False, content=f"Error listing '{safety_result.resolved_path}': {e}")


@_summarize("list_directory")
def _list_directory_summary(args: dict) -> str:
    return f"list_directory({args.get('path', '?')})"


# ---------------------------------------------------------------------------
# file_info
# ---------------------------------------------------------------------------

@_register("file_info")
def _file_info(args: dict, _wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:
    path = args["path"]
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
        return ToolResult(success=False, content=f"Error stating '{resolved}': {e}")


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
        return ToolResult(success=False, content=f"/init failed: {e}")


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
        return ToolResult(success=False, content="Error: Missing required parameter 'paths'.")

    include_anchors = args.get("include_anchors", True)
    is_subagent = getattr(_TOOL_CONTEXT, "_is_subagent", False)
    task_id = getattr(_current_agent_id, "task_id", None)

    results = []
    for rel_path in paths:
        safety = rg.check(rel_path)
        if not safety.allowed:
            results.append(f"--- {rel_path} ---\nAccess denied: {safety.reason}")
            continue
        try:
            # Pre-read file content (one disk read) and cache it
            try:
                with open(safety.resolved_path, "r", encoding="utf-8", errors="replace") as _f:
                    source = _f.read()
                _cache_file_content(safety.resolved_path, _source=source)
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
            results.append(f"--- {rel_path} ---\nError: {e}")

    if not results:
        return ToolResult(success=False, content=f"No definitions found in any of the provided files: {paths}")

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
        return ToolResult(success=False, content="Error: Missing required parameter 'paths'.")
    if not function_names:
        return ToolResult(success=False, content="Error: Missing required parameter 'function_names'.")

    include_anchors = args.get("include_anchors", True)
    task_id = getattr(_current_agent_id, "task_id", None)

    results = []
    all_found = []
    for rel_path in paths:
        safety = rg.check(rel_path)
        if not safety.allowed:
            results.append(f"--- {rel_path} ---\nAccess denied: {safety.reason}")
            continue
        try:
            # Pre-read file content (one disk read) and cache it
            try:
                with open(safety.resolved_path, "r", encoding="utf-8", errors="replace") as _f:
                    source = _f.read()
                _cache_file_content(safety.resolved_path, _source=source)
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
                # No functions found in this file -- report as error line
                results.append(f"--- {rel_path} ---\nError: {content}")
        except Exception as e:
            results.append(f"--- {rel_path} ---\nError: {e}")

    if not results:
        return ToolResult(
            success=False,
            content=f"No functions found matching {function_names} in any of the provided files."
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
                f"[{i+1}/{len(replacements)}] Edit blocked: '{safety.resolved_path}' has not been read yet. "
                f"Use read_file first."
            )
            continue

        try:
            _backup_before_write(safety.resolved_path)

            # Read original for diff preview (use cache when available)
            original = _get_cached_content(safety.resolved_path)
            if original is None:
                global _CACHE_DISK_READS
                _CACHE_DISK_READS += 1
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

            # Update cache with new content so chained edits skip disk I/O
            try:
                _FILE_CACHE[safety.resolved_path] = (updated, os.path.getmtime(safety.resolved_path))
            except OSError:
                _FILE_CACHE.pop(safety.resolved_path, None)

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
        return ToolResult(success=False, content=f"Access denied: {safety.reason}")

    try:
        # Pre-read file content (one disk read) and cache it
        try:
            with open(safety.resolved_path, "r", encoding="utf-8", errors="replace") as _f:
                source = _f.read()
            _cache_file_content(safety.resolved_path, _source=source)
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
        return ToolResult(success=False, content=f"Error: {e}")


@_summarize("get_symbol_range")
def _get_symbol_range_summary(args: dict) -> str:
    return f"get_symbol_range({args.get('symbol', '?')} in {args.get('path', '?')})"
