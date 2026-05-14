#!/usr/bin/env python3
"""
file_ops.py — file/directory tools for mini_agent.

Tools: read_file, write_file, edit_file, list_directory, file_info
"""

import os
import stat as stat_module
import shutil
import time

from safety import ReadSafetyGate, WriteSafetyGate
from tools import clear_tool_cache
from tools import _register, _summarize, ToolResult, _TOOL_CONTEXT, CTX_SCRATCHPAD_PATH, CTX_SCRATCHPAD_UPDATED, CTX_PLAN_STEPS, CTX_PLAN_DONE
from tools import _FILE_RESERVATIONS

# Thread-local: current sub-agent task_id (set by agent_ops before tool execution)
import threading
_current_agent_id: threading.local = threading.local()


# ---------------------------------------------------------------------------
# Session undo — backs up files before modification
# ---------------------------------------------------------------------------

_BACKUPS: dict[str, str] = {}  # resolved_path -> backup path


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
# Absolute maximum (safety cap) — never return more than this.
_ABSOLUTE_MAX_LINES = 1000


@_register("read_file")
def _read_file(args: dict, _wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:
    path = args["path"]
    safety_result = rg.check(path)
    if not safety_result.allowed:
        return ToolResult(
            success=False,
            content=f"Read blocked by safety layer: {safety_result.reason}",
        )
    # Apply offset and limit
    offset = args.get("offset", 0)
    if offset < 0:
        offset = 0
    limit = args.get("limit", _DEFAULT_READ_LINES)
    if limit < 1:
        limit = _DEFAULT_READ_LINES
    limit = min(limit, _ABSOLUTE_MAX_LINES)
    line_numbers = args.get("line_numbers", False)

    try:
        with open(safety_result.resolved_path, "r") as f:
            # Use enumerate + early break to avoid reading the whole file
            collected: list[str] = []
            total_lines = 0
            for lineno, line in enumerate(f):
                total_lines = lineno + 1
                if lineno < offset:
                    continue
                if len(collected) < limit:
                    stripped = line.rstrip("\n")
                    if line_numbers:
                        stripped = f"{total_lines}: {stripped}"
                    collected.append(stripped)
                # Keep iterating to count total lines if we might need truncation message
                # but stop once we've gone well past what we need (limit + 1 is enough
                # to know whether we truncated)
                if len(collected) >= limit and lineno >= offset + limit:
                    break
    except Exception as e:
        hint = ""
        if isinstance(e, FileNotFoundError) or "No such file" in str(e):
            hint = "\nHint: Check the path spelling. Try list_directory to see available files."
        return ToolResult(success=False, content=f"Error reading '{safety_result.resolved_path}': {e}{hint}")

    if offset >= total_lines:
        return ToolResult(success=False, content=f"Offset {offset} exceeds file length ({total_lines} lines).")

    lines_after_offset = total_lines - offset
    if lines_after_offset > limit:
        truncated = "\n".join(collected[:limit])
        msg = (
            f"{truncated}\n"
            f"… (truncated at {limit} lines — {lines_after_offset} total in selection. "
            f"Use a higher limit or offset to see more.)"
        )
        return ToolResult(success=True, content=msg)

    return ToolResult(success=True, content="\n".join(collected))


@_summarize("read_file")
def _read_file_summary(args: dict) -> str:
    return f"read_file({args.get('path', '?')})"


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
    # File reservation check — prevent sub-agent collisions
    agent_id = getattr(_current_agent_id, "task_id", None)
    if agent_id is not None:
        from tools import _FILE_RESERVATIONS, _FILE_RESERVATIONS_LOCK
        with _FILE_RESERVATIONS_LOCK:
            existing = _FILE_RESERVATIONS.get(path)
        if existing is not None and existing != agent_id:
            return ToolResult(
                success=False,
                content=(
                    f"Write blocked: '{path}' is reserved by agent '{existing[:8]}'. "
                    f"Hint: Coordinate with the parent — only one agent should write to a file."
                ),
            )
    try:
        # Generate diff preview before writing
        diff = wg.generate_diff("write_file", args)
        parent = os.path.dirname(safety_result.resolved_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        _backup_before_write(safety_result.resolved_path)
        with open(safety_result.resolved_path, "w") as f:
            f.write(content)
        from tools import add_modified_file
        add_modified_file(safety_result.resolved_path)
        clear_tool_cache()
        # Keep symbol index fresh for newly written .py files
        if path.endswith(".py"):
            from tools.search_ops import _reindex_file
            _reindex_file(safety_result.resolved_path, wg.workspace_root)
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
        preview += "…"
    return f"write_file({path}, {len(content)}B → \"{preview}\")"


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------

@_register("edit_file")
def _edit_file(args: dict, wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    path = args["path"]
    old = args["old_string"]
    new = args["new_string"]
    count = args.get("count", 1)  # 1 = first occurrence, -1 = all occurrences
    safety_result = wg.check(path)
    if not safety_result.allowed:
        return ToolResult(
            success=False,
            content=f"Edit blocked by safety layer: {safety_result.reason}",
        )
    # File reservation check — prevent sub-agent collisions
    agent_id = getattr(_current_agent_id, "task_id", None)
    if agent_id is not None:
        from tools import _FILE_RESERVATIONS_LOCK
        with _FILE_RESERVATIONS_LOCK:
            existing = _FILE_RESERVATIONS.get(path)
        if existing is not None and existing != agent_id:
            return ToolResult(
                success=False,
                content=(
                    f"Edit blocked: '{path}' is reserved by agent '{existing[:8]}'. "
                    f"Hint: Coordinate with the parent — only one agent should edit a file."
                ),
            )
    try:
        with open(safety_result.resolved_path, "r") as f:
            original = f.read()
        # Generate diff preview before editing
        diff = wg.generate_diff("edit_file", args)
        _backup_before_write(safety_result.resolved_path)
        if old not in original:
            # Search for similar substrings to help the agent self-correct
            candidates: list[str] = []
            old_first_line = old.split("\n")[0].strip()
            for lineno, line in enumerate(original.split("\n"), 1):
                if old_first_line and old_first_line[:30] in line:
                    candidates.append(f"  line {lineno}: {line.rstrip()[:120]}")
                if len(candidates) >= 3:
                    break
            hint = (
                f"Edit failed: old_string not found in '{safety_result.resolved_path}'.\n"
                f"Hint: The string must match exactly — check whitespace, indentation, "
                f"and line endings. Try read_file first to verify the exact text."
            )
            if candidates:
                hint += "\nSimilar lines found (did you mean one of these?):\n" + "\n".join(candidates)
            return ToolResult(success=False, content=hint)

        if count == -1:
            # Replace all occurrences
            occurrences = original.count(old)
            updated = original.replace(old, new)
            replaced = occurrences
        elif count >= 1:
            # Replace first N occurrences
            updated = original.replace(old, new, count)
            replaced = min(count, original.count(old))
        else:
            return ToolResult(success=False, content=f"Invalid count: {count}. Use a positive integer or -1 (all).")

        with open(safety_result.resolved_path, "w") as f:
            f.write(updated)

        from tools import add_modified_file
        add_modified_file(safety_result.resolved_path)
        clear_tool_cache()

        # Short summary: no full diff on success (saves context tokens)
        added = updated.count("\n") - original.count("\n")
        label = f"{replaced} occurrence(s)" if replaced > 1 else "1 occurrence"
        return ToolResult(
            success=True,
            content=(
                f"OK: replaced {label} in {safety_result.resolved_path}"
                + (f" (+{added} lines)" if added > 0 else f" ({added} lines)" if added < 0 else "")
            ),
            diff_preview=diff.preview_text if diff.changed else None,
        )
    except Exception as e:
        return ToolResult(
            success=False,
            content=f"Error editing '{safety_result.resolved_path}': {e}",
        )


@_summarize("edit_file")
def _edit_file_summary(args: dict) -> str:
    path = args.get("path", "?")
    old = args.get("old_string", "")
    preview = old[:40].replace("\n", "\\n")
    if len(old) > 40:
        preview += "…"
    return f"edit_file({path}, \"{preview}\")"


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


