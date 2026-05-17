#!/usr/bin/env python3
"""
file_ops.py — file/directory tools for mini_agent.

Tools: read_file, write_file, edit_file, list_directory, file_info
"""
from __future__ import annotations

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

# Cross-turn file content cache — avoids re-reading files whose mtime hasn't changed.
# Key: resolved path (str), Value: (content: str, mtime: float)
_FILE_CACHE: dict[str, tuple[str, float]] = {}


# ---------------------------------------------------------------------------
# Auto plan advancement — after a successful write/edit, check if any
# incomplete plan step’s keywords appear in the file path or edit content,
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
    resolved = safety_result.resolved_path

    # Apply offset and limit
    offset = args.get("offset", 0)
    if offset < 0:
        offset = 0
    limit = args.get("limit", _DEFAULT_READ_LINES)
    if limit < 1:
        limit = _DEFAULT_READ_LINES
    limit = min(limit, _ABSOLUTE_MAX_LINES)
    line_numbers = args.get("line_numbers", False)

    # Cross-turn cache: if file mtime hasn't changed, return cached content directly.
    # Only used when no offset/limit/line_numbers are specified (full reads).
    if offset == 0 and limit == _DEFAULT_READ_LINES and not line_numbers:
        try:
            current_mtime = os.path.getmtime(resolved)
            if resolved in _FILE_CACHE:
                cached_content, cached_mtime = _FILE_CACHE[resolved]
                if cached_mtime == current_mtime:
                    return ToolResult(success=True, content=cached_content)
        except OSError:
            pass  # fall through to normal read on stat error

    try:
        with open(resolved, "r") as f:
            # Use enumerate + early break to avoid reading the whole file
            collected: list[str] = []
            total_lines = 0
            for lineno, line in enumerate(f):
                total_lines = lineno + 1
                if lineno + 1 < offset:
                    continue
                if len(collected) < limit:
                    stripped = line.rstrip("\n")
                    if line_numbers:
                        stripped = f"{total_lines}: {stripped}"
                    collected.append(stripped)
                # Keep iterating to count total lines if we might need truncation message
                # but stop once we've gone well past what we need (limit + 1 is enough
                # to know whether we truncated)
                if len(collected) >= limit and lineno + 1 >= offset + limit:
                    break
    except Exception as e:
        hint = ""
        if isinstance(e, FileNotFoundError) or "No such file" in str(e):
            hint = "\nHint: Check the path spelling. Try list_directory to see available files."
        return ToolResult(success=False, content=f"Error reading '{resolved}': {e}{hint}")

    if offset > total_lines:
        return ToolResult(success=False, content=f"Offset {offset} exceeds file length ({total_lines} lines).")

    full_content = "\n".join(collected)
    lines_after_offset = total_lines - offset + 1

    # Cache full file content for cross-turn reuse (only when reading from offset 0)
    # We store the actual full content from disk for the cache invalidation pattern.
    if offset == 0:
        try:
            current_mtime = os.path.getmtime(resolved)
            _FILE_CACHE[resolved] = (full_content, current_mtime)
        except OSError:
            pass

    if lines_after_offset > limit:
        truncated = "\n".join(collected[:limit])
        msg = (
            f"{truncated}\n"
            f"… (truncated at {limit} lines — {lines_after_offset} total in selection. "
            f"Use a higher limit or offset to see more.)"
        )
        return ToolResult(success=True, content=msg)

    return ToolResult(success=True, content=full_content)


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
        # Invalidate cross-turn file cache
        _FILE_CACHE.pop(safety_result.resolved_path, None)
        # Keep symbol index fresh for newly written .py files
        if path.endswith(".py"):
            from tools.search_ops import _reindex_file
            _reindex_file(safety_result.resolved_path, wg.workspace_root)
        # Auto plan advancement
        _auto_advance_plan(safety_result.resolved_path, content)
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

_EditResult = tuple[str, ToolResult]  # (path, result)


def _fuzzy_find(content: str, search: str) -> tuple[int, int] | None:
    """Cascading 3-pass match for edit_file.
    1. Exact match. 2. Trailing-whitespace-tolerant. 3. Indentation-tolerant.
    """
    if not search or not content:
        return None
    idx = content.find(search)
    if idx != -1:
        return (idx, idx + len(search))
    content_lines = content.split('\n')
    search_lines = search.split('\n')
    if search_lines and search_lines[-1] == '':
        search_lines.pop()
    if not search_lines:
        return None
    for trim in ('right', 'all'):
        result = _line_match(content_lines, search_lines, trim, content)
        if result is not None:
            return result
    return None


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



def _apply_single_edit(
    path: str,
    old: str,
    new: str,
    count: int,
    preview: bool,
    wg: WriteSafetyGate,
    args: dict,
) -> _EditResult:
    """Apply an edit to a single file. Returns (path, ToolResult)."""
    safety_result = wg.check(path)
    if not safety_result.allowed:
        return (path, ToolResult(
            success=False,
            content=f"Edit blocked by safety layer: {safety_result.reason}",
        ))
    # File reservation check — prevent sub-agent collisions
    agent_id = getattr(_current_agent_id, "task_id", None)
    if agent_id is not None:
        from tools import _FILE_RESERVATIONS_LOCK
        with _FILE_RESERVATIONS_LOCK:
            existing = _FILE_RESERVATIONS.get(path)
        if existing is not None and existing != agent_id:
            return (path, ToolResult(
                success=False,
                content=(
                    f"Edit blocked: '{path}' is reserved by agent '{existing[:8]}'. "
                    f"Hint: Coordinate with the parent — only one agent should edit a file."
                ),
            ))
    resolved = safety_result.resolved_path
    try:
        with open(resolved, "r") as f:
            original = f.read()
        diff = wg.generate_diff("edit_file", args)
        _backup_before_write(resolved)
        match = _fuzzy_find(original, old)
        if match is None:
            # Search for similar substrings to help the agent self-correct
            candidates: list[str] = []
            old_first_line = old.split("\n")[0].strip()
            for lineno, line in enumerate(original.split("\n"), 1):
                if old_first_line and old_first_line[:30] in line:
                    candidates.append(f"  line {lineno}: {line.rstrip()[:120]}")
                if len(candidates) >= 3:
                    break
            hint = (
                f"Edit failed: old_string not found in '{resolved}'.\n"
                f"Hint: The string must match exactly — check whitespace, indentation, "
                f"and line endings. Try read_file first to verify the exact text."
            )
            if candidates:
                hint += "\nSimilar lines found (did you mean one of this?):\n" + "\n".join(candidates)
            if old_first_line:
                try:
                    memory = getattr(_TOOL_CONTEXT, "_memory_store", None)
                    if memory is not None:
                        memory.add_knowledge(
                            category="pattern",
                            summary=f"edit_file mismatch: {old_first_line[:80]}",
                            detail=f"File: {resolved}. Could not find exact match for old_string.",
                        )
                except Exception as exc:
                    print(f"  ⚠ backup skipped: {exc}", file=sys.stderr, flush=True)
            return (path, ToolResult(success=False, content=hint))

        if count == -1:
            occurrences = original.count(old)
            updated = original.replace(old, new)
            replaced = occurrences
        elif count >= 1:
            start, end = match
            updated = original[:start] + new + original[end:]
            replaced = 1
        else:
            return (path, ToolResult(success=False, content=f"Invalid count: {count}. Use a positive integer or -1 (all)."))

        if preview:
            raw_diff = wg._format_diff(resolved, original, updated)
            return (path, ToolResult(
                success=True,
                content=f"Preview: proposed edit to {resolved}\n{raw_diff}",
            ))

        with open(resolved, "w") as f:
            f.write(updated)

        from tools import add_modified_file
        add_modified_file(resolved)
        clear_tool_cache()
        _FILE_CACHE.pop(resolved, None)
        # Keep symbol index fresh for edited .py files
        if path.endswith(".py"):
            from tools.search_ops import _reindex_file
            _reindex_file(resolved, wg.workspace_root)

        # Auto plan advancement
        _auto_advance_plan(resolved, old)

        added = updated.count("\n") - original.count("\n")
        label = f"{replaced} occurrence(s)" if replaced > 1 else "1 occurrence"
        return (path, ToolResult(
            success=True,
            content=(
                f"OK: replaced {label} in {resolved}"
                + (f" (+{added} lines)" if added > 0 else f" ({added} lines)" if added < 0 else "")
            ),
            diff_preview=diff.preview_text if diff.changed else None,
        ))
    except Exception as e:
        return (path, ToolResult(
            success=False,
            content=f"Error editing '{resolved}': {e}",
        ))


@_register("edit_file")
def _edit_file(args: dict, wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    old = args["old_string"]
    new = args["new_string"]
    count = args.get("count", 1)
    preview = args.get("preview", False)
    paths = args.get("paths", None)

    if paths is not None:
        # Batch edit: apply same old→new to all paths
        if not isinstance(paths, list) or not paths:
            return ToolResult(
                success=False,
                content="'paths' must be a non-empty list of file paths.",
            )
        results: list[_EditResult] = []
        for p in paths:
            result = _apply_single_edit(p, old, new, count, preview, wg, {**args, "path": p})
            results.append(result)
        all_ok = all(r.success for _, r in results)
        lines: list[str] = []
        failures: list[str] = []
        for p, r in results:
            first_line = r.content.split("\n")[0]
            if r.success:
                lines.append(f"  [OK] {p}: {first_line}")
            else:
                lines.append(f"  [FAIL] {p}: {first_line}")
                failures.append(p)
        summary = "Batch edit results:\n" + "\n".join(lines)
        if all_ok:
            return ToolResult(success=True, content=summary)
        else:
            return ToolResult(
                success=False,
                content=summary + f"\n\nFailed paths: {failures}",
            )
    else:
        path = args["path"]
        result = _apply_single_edit(path, old, new, count, preview, wg, args)
        return result[1]


@_summarize("edit_file")
def _edit_file_summary(args: dict) -> str:
    path = args.get("path", "?")
    old = args.get("old_string", "")
    old_preview = old[:40].replace("\n", "\\n")
    if len(old) > 40:
        old_preview += "…"
    preview_flag = args.get("preview", False)
    suffix = " [preview]" if preview_flag else ""
    return f"edit_file({path}, \"{old_preview}\"){suffix}"


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
        import fnmatch, glob, subprocess, time
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
        with open(rules_path, "w") as f:
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
            with open(toml_path, "w") as f:
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
                with open(pf) as f:
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


@_summarize("init")
def _init_rules_summary(args: dict) -> str:
    return "init: generate .mini_agent.rules from workspace"
