#!/usr/bin/env python3
"""
tools package -- tool definitions, execution, and structured results for mini_agent.

Every tool execution returns a ToolResult (never a raw exception).
All read and write paths route through the safety gates.
Shell commands and searches run sandboxed inside the workspace root.

Adding a new tool requires:
    1. A ``_<name>`` implementation function decorated with ``@_register("name")``.
    2. A ``_<name>_summary`` function decorated with ``@_summarize("name")``.
    3. An entry in ``TOOLS`` (the API schema sent to the LLM).

Submodules:
    file_ops    -- read_file, write_file, edit_file, list_directory, file_info,
                  write_scratchpad, diff, restore_file, plan, plan_status
    shell_ops   -- run_shell, task_status, search_files, run_tests, verify, git
    search_ops  -- find_symbol, find_usages, semantic_search, web_search, recall_turn
    agent_ops   -- spawn_agent, agent_status, collect_agent, collect_any,
                  agent_message, agent_read, agent_handoff, agent_inbox,
                  agent_subscribe, agent_extend, agent_cancel
    lsp         -- lsp_definition, lsp_references, lsp_hover, lsp_diagnostics

"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from core.safety import ReadSafetyGate, WriteSafetyGate
from tools.schema import TOOLS
from logging_setup import get_logger, log_tool_failure, log_tool_success, log_error_trace

_log = get_logger("tools")

# Hardcoded core schema for remember -- always present even if schema.py is missing
# Bootstrap guard: if the canonical "remember" schema is missing from schema.py
# (e.g. corrupted install), insert a minimal fallback so the tool still works.
if not any(td["function"]["name"] == "remember" for td in TOOLS):
    TOOLS.insert(0, {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Manually capture a learning or observation to project_knowledge for cross-session persistence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Short topic label for this learning."},
                    "detail": {"type": "string", "description": "The learning itself."},
                    "category": {"type": "string", "description": "Optional: category hint. Auto-detected if omitted."},
                },
                "required": ["topic", "detail"]
            }
        }
    })

# ---------------------------------------------------------------------------
# TOOL_SCHEMA_MAP -- O(1) name->schema lookup for execute_tool() validation
# ---------------------------------------------------------------------------

# Lazily-built dict mirroring TOOLS for O(1) lookup.  Invalidated when
# TOOLS length changes (skills appending tools between turns).
_TOOL_SCHEMA_MAP: dict[str, dict] = {}
_TOOL_SCHEMA_MAP_LEN: int = 0

def _get_tool_schema(name: str) -> dict | None:
    """Look up a tool's parameter schema from TOOLS at runtime.

    Builds an O(1) dict cache on first call; invalidates automatically
    when TOOLS grows (skills activate new tool groups).
    """
    global _TOOL_SCHEMA_MAP, _TOOL_SCHEMA_MAP_LEN
    if len(TOOLS) != _TOOL_SCHEMA_MAP_LEN:
        _TOOL_SCHEMA_MAP = {
            td["function"]["name"]: td["function"].get("parameters", {})
            for td in TOOLS
        }
        _TOOL_SCHEMA_MAP_LEN = len(TOOLS)
    return _TOOL_SCHEMA_MAP.get(name)

# ---------------------------------------------------------------------------
# Structured tool result (extracted to tools/result.py)
# ---------------------------------------------------------------------------
from tools.result import ToolResult  # noqa: E402, F401 -- re-exported for backward compat


# ---------------------------------------------------------------------------
# Tool dispatch registry
# ---------------------------------------------------------------------------

_TOOL_DISPATCH: dict[str, callable] = {}
_TOOL_SUMMARIES: dict[str, callable] = {}
# Cache: dispatch function signature inspection (P0.1 perf)
# Maps tool name -> bool (whether dispatch fn accepts on_output kwarg)
_DISPATCH_SIGNATURES: dict[str, bool] = {}

# ---------------------------------------------------------------------------
# Agent context -- extracted to tools/context.py (re-exported for backward compat)
# ---------------------------------------------------------------------------
from tools.context import (  # noqa: E402, F401
    AgentContext,
    _TOOL_CONTEXT_VAR,
    _ContextProxy,
    _TOOL_CONTEXT,
    CTX_SCRATCHPAD_PATH,
    CTX_SCRATCHPAD_UPDATED,
    CTX_TURN_HISTORY,
    CTX_PLAN_STEPS,
    CTX_PLAN_DONE,
    set_context,
)

# Session-level cache for read-only tools.  Persists across turns within a
# session, invalidated when files are modified.  Caps at _TOOL_CACHE_MAX_SIZE.
# Key: json.dumps([name, args], sort_keys=True).
# Value: (timestamp, ToolResult) with 30-second TTL.
_TOOL_CACHE: dict[str, tuple[float, "ToolResult"]] = {}
_TOOL_CACHE_MAX_SIZE = 256
_TOOL_CACHE_TTL: float = 3600.0  # seconds (1 hour) -- safety net; primary invalidation is write-driven
_TOOL_CACHE_PATH_MAP: dict[str, set[str]] = {}  # file path -> set of cache keys
_TOOL_CACHE_HITS: int = 0
_TOOL_CACHE_MISSES: int = 0

def get_tool_cache_stats() -> dict:
    """Return cache hit/miss/size stats for observability."""
    return {
        "size": len(_TOOL_CACHE),
        "max_size": _TOOL_CACHE_MAX_SIZE,
        "ttl_s": _TOOL_CACHE_TTL,
        "hits": _TOOL_CACHE_HITS,
        "misses": _TOOL_CACHE_MISSES,
        "hit_rate": (_TOOL_CACHE_HITS / max(_TOOL_CACHE_HITS + _TOOL_CACHE_MISSES, 1)),
    }

# Files modified by write/edit -- used by verify
_MODIFIED_FILES: set[str] = set()
_MODIFIED_FILES_LOCK = threading.Lock()

_TASK_REGISTRY: dict[str, subprocess.Popen] = {}  # background shell task registry

# Per-session tool usage tracking for dead-tool pruning.
# Incremented in execute_tool(); reset each session via reset_tool_usage().
# Dead-tool pruning drops tools with zero usage after a threshold turn,
# shrinking the API payload and stabilizing the KV-cache prefix.
_TOOL_USAGE_COUNT: dict[str, int] = {}
_TOOL_USAGE_LOCK = threading.Lock()

def reset_tool_usage() -> None:
    """Reset per-session tool usage counters (called at session init)."""
    with _TOOL_USAGE_LOCK:
        _TOOL_USAGE_COUNT.clear()

def get_tool_usage() -> dict[str, int]:
    """Return a snapshot of tool usage counts (thread-safe)."""
    with _TOOL_USAGE_LOCK:
        return dict(_TOOL_USAGE_COUNT)

def get_unused_tools(min_turns: int = 5) -> set[str]:
    """Return tool names that have never been called after *min_turns* turns.

    Only meaningful after the agent has had enough turns to establish patterns.
    Tools in CORE_TOOLS that are essential (read_file, write_file, etc.) are
    excluded from pruning.
    """
    from tools.skills import get_active_tool_names
    active = frozenset(get_active_tool_names())
    with _TOOL_USAGE_LOCK:
        used = frozenset(_TOOL_USAGE_COUNT)
    # Never prune these — they're essential scaffolding
    _UNPRUNABLE = frozenset({
        "read_file", "write_file", "edit_file", "run_shell",
        "search_files", "list_directory", "file_info", "find_symbol",
        "remember", "memory_core", "use_skill", "plan", "plan_status",
        "todo_write", "todo_read", "write_scratchpad",
    })
    unused = active - used - _UNPRUNABLE
    return unused

# ---------------------------------------------------------------------------
# File reservation system -- extracted to tools/reservations.py
# ---------------------------------------------------------------------------
from tools.reservations import (  # noqa: E402, F401
    reserve_file,
    release_file,
    release_all_files,
)

# Sub-agent runtime registry (lazy init in config.init_session)
_AGENT_RUNTIME = None  # AgentRuntime -- set by init_session
_CACHEABLE = frozenset({
    "read_file", "file_info", "list_directory",
    "search_files", "find_symbol", "semantic_search", "web_search",
    "lsp_definition", "lsp_references", "lsp_hover", "lsp_diagnostics",
})
_TOOL_TIMEOUT = 120  # P3.1: per-tool execution timeout (seconds)


def add_modified_file(path: str) -> None:
    """Record a file as modified (thread-safe). Used by write_file/edit_file."""
    with _MODIFIED_FILES_LOCK:
        _MODIFIED_FILES.add(path)
    # Incrementally update the codebase map so agent context stays current
    try:
        root = _TOOL_CONTEXT.workspace if _TOOL_CONTEXT else None
        if root and path.endswith((".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")):
            from core.codebase_map import update_file_in_map
            update_file_in_map(path, root)
    except Exception:
        pass  # best-effort; never block the write on map update failure


def get_modified_files() -> list[str]:
    """Return a snapshot of modified files (thread-safe)."""
    with _MODIFIED_FILES_LOCK:
        return sorted(_MODIFIED_FILES)


def _register(name: str):
    """Decorator: register an implementation function in the dispatch table."""
    def decorator(fn):
        _TOOL_DISPATCH[name] = fn
        return fn
    return decorator


def _summarize(name: str):
    """Decorator: register a summary function for verbose logging."""
    def decorator(fn):
        _TOOL_SUMMARIES[name] = fn
        return fn
    return decorator


# ---------------------------------------------------------------------------
def _write_session_handoff(args: dict, wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Auto-generate and write HANDOFF.md for session continuity."""
    workspace = _TOOL_CONTEXT.workspace
    if not workspace:
        return ToolResult(False, "", "no workspace available")
    start_head = getattr(_TOOL_CONTEXT, "_session_start_head", None)
    pending = (args.get("pending") or "").strip()
    notes = (args.get("notes") or "").strip()

    store = getattr(_TOOL_CONTEXT, "_memory_store", None)
    if store is None:
        # Fallback: use static method directly
        from memory.memory import MemoryStore
        try:
            path = MemoryStore.write_session_handoff(
                workspace, start_head=start_head,
                pending=pending, notes=notes,
            )
            return ToolResult(True, f"HANDOFF.md written to {path}")
        except OSError as e:
            return ToolResult(False, "", str(e))
    try:
        path = store.write_session_handoff(
            workspace, start_head=start_head,
            pending=pending, notes=notes,
        )
        return ToolResult(True, f"HANDOFF.md written to {path}")
    except OSError as e:
        return ToolResult(False, "", str(e))


_TOOL_DISPATCH["write_session_handoff"] = _write_session_handoff
_TOOL_SUMMARIES["write_session_handoff"] = (
    lambda args: "write_session_handoff()"
)

# -- discord_search: search Discord server message history --
def _discord_search(args: dict, wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:
    """Search Discord server channel history for a keyword via REST API."""
    import requests as _requests

    token = _TOOL_CONTEXT.discord_token
    guild_id = _TOOL_CONTEXT.discord_guild_id
    if not token or not guild_id:
        return ToolResult(False, "", "Discord not connected (no guild/token in context).")

    query = (args.get("query") or "").strip()
    if not query:
        return ToolResult(False, "", "Missing required 'query' parameter.")

    limit = min(int(args.get("limit", 15)), 30)
    query_lower = query.lower()

    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    base = "https://discord.com/api/v10"

    results: list[dict] = []

    try:
        # Get all text channels
        resp = _requests.get(f"{base}/guilds/{guild_id}/channels", headers=headers, timeout=10)
        if resp.status_code != 200:
            return ToolResult(False, "", f"Discord API error: {resp.status_code} {resp.text[:200]}")
        channels = resp.json()
    except Exception as e:
        return ToolResult(False, "", f"Failed to list channels: {e}")

    for ch in channels:
        if ch.get("type") != 0:  # GUILD_TEXT only
            continue
        if len(results) >= limit:
            break
        try:
            resp = _requests.get(
                f"{base}/channels/{ch['id']}/messages?limit=100",
                headers=headers, timeout=10,
            )
            if resp.status_code != 200:
                continue
            messages = resp.json()
            for m in messages:
                content = m.get("content", "")
                if query_lower in content.lower():
                    results.append({
                        "channel": ch.get("name", f"#{ch['id']}"),
                        "author": m["author"]["username"],
                        "timestamp": m.get("timestamp", ""),
                        "content": content[:400],
                        "jump_url": f"https://discord.com/channels/{guild_id}/{ch['id']}/{m['id']}",
                    })
                    if len(results) >= limit:
                        break
        except Exception:
            continue

    if not results:
        return ToolResult(True, f"No Discord messages found matching **{query}**.")

    out_lines = [f"**{len(results)} Discord match(es) for \"{query}\":**\n"]
    for r in results:
        ts = r["timestamp"][:16].replace("T", " ") if r["timestamp"] else "?"
        out_lines.append(
            f"**#{r['channel']}** — {r['author']} ({ts})\n"
            f">>> {r['content'][:300]}\n"
            f"🔗 {r['jump_url']}\n"
        )

    return ToolResult(True, "\n".join(out_lines))

_TOOL_DISPATCH["discord_search"] = _discord_search
_TOOL_SUMMARIES["discord_search"] = lambda args: f"discord_search({args.get('query', '?')})"

# -- use_skill gate: lazy tool loading --
from tools.skills import USE_SKILL_SCHEMA, SKILL_LIST_SCHEMA, SKILL_VIEW_SCHEMA, _use_skill, _skill_list, _skill_view  # noqa: E402

_TOOL_DISPATCH["use_skill"] = _use_skill
_TOOL_SUMMARIES["use_skill"] = lambda args: f"use_skill({args.get('name', '?')})"
_TOOL_DISPATCH["skill_list"] = _skill_list
_TOOL_SUMMARIES["skill_list"] = lambda args: "skill_list()"
_TOOL_DISPATCH["skill_view"] = _skill_view
_TOOL_SUMMARIES["skill_view"] = lambda args: f"skill_view({args.get('name', '?')})"
# Inject schemas so they're always in TOOLS (core tools, always visible)
TOOLS.append(USE_SKILL_SCHEMA)
TOOLS.append(SKILL_LIST_SCHEMA)
TOOLS.append(SKILL_VIEW_SCHEMA)


def clear_tool_cache() -> None:
    """Clear the per-turn tool cache. Called at the start of each agent turn.

    Since the cache is now session-level (invalidated by writes, not turns),
    this is still a no-op in production (writes call clear_tool_cache but
    actual invalidation happens via write-driven _reindex_file and
    _FILE_CACHE eviction).  The in-memory _TOOL_CACHE dict is kept for
    intra-turn deduplication within a single execute_tool call.
    """
    pass  # session-level cache -- invalidation is write-driven, not turn-driven


def _repair_json(raw: str) -> tuple[object, bool]:
    """Attempt to repair common LLM-generated JSON malformations.

    Returns (parsed_value, was_repaired).  If all repair attempts fail the
    original raw string is re-raised via json.loads so callers see a standard
    JSONDecodeError.

    Repairs attempted (in order, each retried independently, then combinations):
    1. Trailing commas before ``]`` or ``}``
    2. Single-quoted strings -> double quotes
    3. Unquoted object keys
    4. 1+2, 1+3, 2+3, 1+2+3 (combinations)
    """

    # Helper: apply unquoted-key fix only outside strings
    def _fix_unquoted_keys(text: str) -> str:
        """Quote bare keys but skip content inside double-quoted strings."""
        result: list[str] = []
        i = 0
        while i < len(text):
            if text[i] == '"':
                # Find end of string (handle backslash escapes)
                j = i + 1
                while j < len(text):
                    if text[j] == '\\' and j + 1 < len(text):
                        j += 2
                        continue
                    if text[j] == '"':
                        j += 1
                        break
                    j += 1
                result.append(text[i:j])
                i = j
            else:
                # Accumulate consecutive non-quoted chars into one segment
                j = i
                while j < len(text) and text[j] != '"':
                    j += 1
                result.append(text[i:j])
                i = j
        # Only apply regex to segments at even indices (outside strings).
        # Use [A-Za-z_]\w* instead of \w+ to avoid matching numeric keys
        # like '1:' which would produce '"1":}' instead of leaving '1:' alone.
        for idx in range(0, len(result), 2):
            result[idx] = re.sub(r'([A-Za-z_]\w*)(\s*:)', r'"\1"\2', result[idx])
        return ''.join(result)

    # Individual fixes
    fix1 = re.sub(r',\s*([}\]])', r'\1', raw)

    fix2 = raw
    if "'" in raw:
        fix2 = raw.replace("'", '"')

    fix3 = raw
    if not raw.strip().startswith('['):
        fix3 = _fix_unquoted_keys(raw)

    # Combinations -- apply fixes in sequence on copies
    def _apply_combo(base: str, *indices: int) -> str:
        s = base
        for i in indices:
            if i == 1:
                s = re.sub(r',\s*([}\]])', r'\1', s)
            elif i == 2:
                s = s.replace("'", '"')
            elif i == 3:
                if not s.strip().startswith('['):
                    s = _fix_unquoted_keys(s)
        return s

    attempts: list[str] = [
        fix1,
        fix2,
        fix3,
        _apply_combo(raw, 1, 2),
        _apply_combo(raw, 1, 3),
        _apply_combo(raw, 2, 3),
        _apply_combo(raw, 1, 2, 3),
    ]

    for attempt in attempts:
        if attempt == raw:
            continue
        try:
            return json.loads(attempt), True
        except (json.JSONDecodeError, ValueError):
            continue

    # Last resort: try the original
    return json.loads(raw), False


# ---------------------------------------------------------------------------
# Error hints & failure learning (extracted to tools/error_hints.py)
# ---------------------------------------------------------------------------
from tools.error_hints import (  # noqa: E402, F401
    _TOOL_PARAM_CACHE,
    _ERROR_HINTS,
    _build_error_hint,
    _fingerprint_error,
    _FAILURE_PATTERNS,
    _learn_from_failure,
)


# Module-level cancel event so tool functions (e.g. _run_shell) can
# detect cancellation and stop blocking on subprocess I/O.  Set by
# execute_tool() before dispatching; cleared after the thread completes.
# This is NOT a ContextVar -- it's shared across all threads on purpose.
_CURRENT_CANCEL_EVENT: threading.Event | None = None

def execute_tool(
    tool_call: dict,
    write_gate: WriteSafetyGate,
    read_gate: ReadSafetyGate,
    on_output: callable = None,
    approve_callback: callable = None,
    cancel_event: threading.Event | None = None,
) -> ToolResult:
    """Execute a single tool call.  All read/write paths go through safety gates.

    Read-only tools (read_file, file_info, etc.) are cached within a turn
    so repeated reads of the same file hit the cache instead of disk.

    If *on_output* is provided, it is called with (tool_name, line_str) for
    real-time output streaming (currently only run_shell uses this).

    Malformed JSON arguments are repaired automatically (trailing commas,
    single quotes, unquoted keys) before parsing.  On failure a *hint* is
    attached to the ToolResult so the LLM can self-correct.
    """
    fn = tool_call["function"]
    name = fn["name"]
    raw_args = fn["arguments"]
    try:
        args, _repaired = _repair_json(raw_args)
    except json.JSONDecodeError as exc:
        hint = _build_error_hint(name, exc)
        return ToolResult(
            success=False,
            content=f"Malformed JSON in tool arguments: {exc}",
            hint=hint,
        )

    # --- strip _pipe meta-field before validation AND cache check (tool piping) ---
    # Must happen BEFORE cache key is computed, otherwise piped calls
    # get a different key and never hit the cache.
    pipe_config = None
    if isinstance(args, dict):
        pipe_config = args.pop("_pipe", None)

    # Check cache for read-only tools (skip if on_output is streaming)
    cache_key = ""
    if on_output is None and name in _CACHEABLE:
        cache_key = json.dumps([name, args], sort_keys=True)
        cached = _TOOL_CACHE.get(cache_key)
        if cached is not None:
            ts, result = cached
            if time.monotonic() - ts < _TOOL_CACHE_TTL:
                global _TOOL_CACHE_HITS
                _TOOL_CACHE_HITS += 1
                return result
            # Expired -- evict from cache and path map
            _TOOL_CACHE.pop(cache_key, None)
            for p, keys in list(_TOOL_CACHE_PATH_MAP.items()):
                keys.discard(cache_key)
                if not keys:
                    del _TOOL_CACHE_PATH_MAP[p]
        global _TOOL_CACHE_MISSES
        _TOOL_CACHE_MISSES += 1

    # --- schema validation: check parameter names against tool definition ---
    if isinstance(args, dict):
        tool_schema = _get_tool_schema(name)
        if tool_schema:
            valid_params = set(tool_schema.get("properties", {}).keys())
            required_params = set(tool_schema.get("required", []))
            provided = set(args.keys())
            unknown = provided - valid_params
            missing = required_params - provided
            if unknown or missing:
                hint_parts = []
                if unknown:
                    hint_parts.append(
                        f"Unknown parameter(s): {', '.join(sorted(unknown))}")
                if missing:
                    hint_parts.append(
                        f"Missing required: {', '.join(sorted(missing))}")
                hint_parts.append(
                    f"Valid parameters: {', '.join(sorted(valid_params))}")
                return ToolResult(
                    success=False,
                    content=f"Invalid arguments: {'; '.join(hint_parts[:2])}",
                    hint="\n".join(hint_parts),
                )

    dispatch = _TOOL_DISPATCH.get(name)
    if dispatch is None:
        known = sorted(td["function"]["name"] for td in TOOLS)
        return ToolResult(
            success=False,
            content=f"Unknown tool: {name}",
            hint=f"Tool '{name}' is not recognized. Available tools: {', '.join(known)}. Please use one of these.",
        )

    # Approval gate for write/destructive tools
    if approve_callback is not None and name in ("write_file", "edit_file", "run_shell"):
        if not approve_callback(name, args):
            return ToolResult(
                success=False,
                content=f"{name} not approved by user.",
                hint=f"Tool '{name}' requires user approval and was denied. Consider an alternative approach or ask the user to approve.",
            )

    # Pass on_output to the tool if it accepts it (P0.1: cached signature check)
    accepts_on_output = _DISPATCH_SIGNATURES.get(name)
    if accepts_on_output is None:
        import inspect
        accepts_on_output = "on_output" in inspect.signature(dispatch).parameters
        _DISPATCH_SIGNATURES[name] = accepts_on_output

    # P3.1: Per-tool execution timeout via background thread
    # Using threading.Thread instead of ThreadPoolExecutor avoids
    # creating/destroying a pool for every single tool call.
    _result_container: list[ToolResult | Exception] = []

    def _run_and_capture():
        _t0 = _time.monotonic()
        try:
            if accepts_on_output:
                _result_container.append(dispatch(args, write_gate, read_gate, on_output=on_output))
            else:
                _result_container.append(dispatch(args, write_gate, read_gate))
        except Exception as exc:
            _result_container.append(exc)
        _elapsed = _time.monotonic() - _t0
        # (dispatch timing collected but no longer logged to console)

    import sys as _sys
    import time as _time
    _turn = getattr(_TOOL_CONTEXT, '_turn_count', 0)
    _sys.stderr.write(f"[turn {_turn}] dispatching '{name}' (timeout={_TOOL_TIMEOUT}s)\n")
    _sys.stderr.flush()
    _t_dispatch_start = _time.monotonic()
    _t_start = _time.monotonic()
    # Set the module-level cancel event so tool functions like _run_shell
    # can detect cancellation and stop blocking on subprocess I/O.
    global _CURRENT_CANCEL_EVENT
    _prev_cancel = _CURRENT_CANCEL_EVENT
    _CURRENT_CANCEL_EVENT = cancel_event
    try:
        t = threading.Thread(target=_run_and_capture, daemon=True)
        t.start()
        if cancel_event is not None:
            # Poll with short intervals so the user can cancel during streaming.
            # A single t.join(timeout=120) blocks the SSE parsing loop and makes
            # the UI appear hung -- especially on Windows where the first open()
            # call in a new thread can be delayed by antivirus filter drivers.
            _poll_interval = 0.1  # 100 ms
            _deadline = _time.monotonic() + _TOOL_TIMEOUT
            _last_hb = _t_dispatch_start
            while t.is_alive() and _time.monotonic() < _deadline:
                if cancel_event.is_set():
                    _sys.stderr.write(f"[turn {_turn}] cancelled '{name}' thread (elapsed={_time.monotonic() - _t_dispatch_start:.2f}s)\n")
                    _sys.stderr.flush()
                    # Kill any active process trees immediately to unblock
                    # tool threads waiting on subprocess I/O (e.g. _run_shell
                    # blocking on t_out.join).  This prevents orphaned bash.exe
                    # / cmd.exe from accumulating.
                    try:
                        from tools.shell_ops import _cleanup_all_procs
                        _cleanup_all_procs()
                    except Exception:
                        pass
                    # Don't return yet -- give the thread a brief grace period
                    # to finish (50 ms).  We can't kill Python threads safely.
                    t.join(timeout=0.05)
                    break
                t.join(timeout=_poll_interval)
                # Heartbeat: log every 5s so we can tell if thread is stuck
                _now = _time.monotonic()
                if _now - _last_hb >= 5.0:
                    _sys.stderr.write(f"[turn {_turn}] '{name}' still running ({_now - _t_dispatch_start:.1f}s elapsed)...\n")
                    _sys.stderr.flush()
                    _last_hb = _now
        else:
            t.join(timeout=_TOOL_TIMEOUT)
    finally:
        _CURRENT_CANCEL_EVENT = _prev_cancel
    if t.is_alive():
        # Thread still running after timeout -- it's stuck.
        # We cannot safely kill a Python thread, so return a timeout result.
        # The daemon thread will continue running but will be terminated
        # when the process exits.
        #
        # On Windows, kill any orphaned subprocess trees to prevent
        # process multiplication (e.g. thousands of bash.exe).
        try:
            from tools.shell_ops import _cleanup_all_procs
            _cleanup_all_procs()
        except Exception:
            pass
        return ToolResult(
            success=False,
            content=f"Tool '{name}' timed out after {_TOOL_TIMEOUT}s.",
            hint=_build_error_hint(name, error_msg=f"timed out after {_TOOL_TIMEOUT}s"),
        )

    raw = _result_container[0]
    if isinstance(raw, Exception):
        raise raw
    result = raw

    # --- console: success / failure status ---
    _turn = getattr(_TOOL_CONTEXT, '_turn_count', 0)
    if result.success:
        _sys.stderr.write(f"[turn {_turn}] '{name}' OK\n")
    else:
        _sys.stderr.write(f"[turn {_turn}] '{name}' ERR -- {result.content[:120]}\n")
    _sys.stderr.flush()

    # Normalize: every failed result gets a _build_error_hint so the LLM
    # always sees the same structure (tool name, error, valid params, retry
    # nudge).  If the tool already set a hint that adds unique context,
    # append it to the standard hint instead of discarding it.
    if not result.success:
        standard_hint = _build_error_hint(name, error_msg=result.content)
        if result.hint and result.hint != standard_hint:
            # Merge: standard first, then the tool-specific hint as extra context
            result.hint = standard_hint + "\nAdditional info: " + result.hint
        else:
            result.hint = standard_hint

    # --- Auto-learn from tool failures (pattern detection + escalation) ---
    if not result.success:
        log_tool_failure(name, result.content)
        _learn_from_failure(name, result)
    else:
        log_tool_success(name)
        # Only record success when this tool has known failure patterns;
        # skips unnecessary DB queries for the vast majority of successful calls.
        in_memory = _TOOL_CONTEXT.__dict__.get("_failure_patterns", {})
        if any(k.startswith(name + ":") for k in in_memory):
            pattern_store = getattr(_TOOL_CONTEXT, "_failure_pattern_store", None)
            if pattern_store is not None:
                try:
                    pattern_store.record_success(name, args)
                except Exception:
                    _log.warning("FailurePatternStore.record_success failed", exc_info=True)
            # Clear in-memory counters on success -- the agent recovered
            _TOOL_CONTEXT.__dict__.pop("_failure_patterns", None)

    # --- Cache invalidation: invalidate cache entries for modified files ---
    # instead of clearing the entire cache.  This preserves session-level
    # caching for unmodified files across turns while ensuring fresh reads
    # for just-edited files.  Also covers restore_file.
    _WRITE_TOOLS = frozenset({"write_file", "edit_file", "restore_file"})
    if result.success and name in _WRITE_TOOLS:
        file_path = args.get("path", "") if isinstance(args, dict) else ""
        if file_path and file_path in _TOOL_CACHE_PATH_MAP:
            for key in list(_TOOL_CACHE_PATH_MAP.get(file_path, ())):
                _TOOL_CACHE.pop(key, None)
            _TOOL_CACHE_PATH_MAP.pop(file_path, None)

    # --- Post-edit auto-verification: run LSP diagnostics after file writes ---
    # LSP connections use subprocess pipes + per-connection locks, so they are
    # thread-safe.  Tool dispatch is synchronous (one tool at a time), so two
    # LSP calls never race on the same connection.
    if result.success and name in ("write_file", "edit_file"):
        try:
            file_path = args.get("path", "")
            if file_path:
                from tools.lsp import _lsp_diagnostics
                diag_result = _lsp_diagnostics({"file_path": file_path}, write_gate, read_gate)
                if diag_result.success and diag_result.content:
                    result.content += "\n\n[auto-verify] LSP diagnostics:\n" + diag_result.content[:500]
        except Exception:
            _log.warning("auto-verify LSP diagnostics failed for %s", file_path, exc_info=True)

    # Cache successful read-only results (only when not streaming).
    # Session-level LRU with TTL: evict oldest entry when over _TOOL_CACHE_MAX_SIZE.
    if cache_key and result.success:
        if cache_key not in _TOOL_CACHE and len(_TOOL_CACHE) >= _TOOL_CACHE_MAX_SIZE:
            # Evict oldest entry (Python 3.7+ dicts are insertion-ordered)
            _TOOL_CACHE.pop(next(iter(_TOOL_CACHE)), None)
        _TOOL_CACHE[cache_key] = (time.monotonic(), result)
        # Track file-path-to-cache-key mapping for targeted invalidation
        file_path = args.get("path", "") if isinstance(args, dict) else ""
        if file_path:
            _TOOL_CACHE_PATH_MAP.setdefault(file_path, set()).add(cache_key)

    # Increment per-session tool usage counter for dead-tool pruning
    # (count even failed calls — they indicate the agent tried to use the tool)
    with _TOOL_USAGE_LOCK:
        _TOOL_USAGE_COUNT[name] = _TOOL_USAGE_COUNT.get(name, 0) + 1

    return result


def tool_summary(tc: dict) -> str:
    """Return a compact one-line summary of a tool call for display."""
    fn = tc["function"]
    name = fn["name"]
    try:
        args = json.loads(fn["arguments"])
    except Exception as exc:
        _log.warning("tool_summary JSON parse failed for '%s': %s", name, exc)
        args = {}

    summarize = _TOOL_SUMMARIES.get(name)
    if summarize is None:
        return f"{name}(...)"
    return summarize(args)


# ---------------------------------------------------------------------------
# Import submodules to trigger @_register / @_summarize side effects.
#
# These imports run at package-load time to populate _TOOL_DISPATCH (dispatch
# handlers) and _TOOL_SUMMARIES (compact log summaries).  The skills system
# separately controls which tool *schemas* are sent to the LLM — so
# skill-gated tools have registered dispatch handlers but their schemas
# won't appear in TOOLS until the skill is activated via use_skill.
# ---------------------------------------------------------------------------

from tools import file_ops        # noqa: E402, F401  -- read_file, write_file, edit_file, etc.
from tools import shell_ops       # noqa: E402, F401  -- run_shell, search_files, run_tests, etc.
from tools import search_ops      # noqa: E402, F401  -- find_symbol, web_search, semantic_search
from tools import browser_ops     # noqa: E402, F401  -- browser automation (web skill)
from tools import desktop_ops     # noqa: E402, F401  -- desktop automation (desktop skill)
from tools import macos_ops       # noqa: E402, F401  -- macOS-specific APIs (desktop skill)
from tools import agent_spawn     # noqa: E402, F401  -- spawn_agent (agents skill)
from tools import agent_collect   # noqa: E402, F401  -- agent_status, collect_agent, collect_any
from tools import agent_ops       # noqa: E402, F401  -- agent_extend, agent_cancel, wait_for_agent, etc.
from tools import memory_core     # noqa: E402, F401  -- memory_core, session_search (core)
from tools import agent_todos     # noqa: E402, F401  -- todo_write, todo_read (planning skill)
from tools import agent_patterns  # noqa: E402, F401  -- fan_out, pipeline, barrier (agents skill)
from tools import agent_messages  # noqa: E402, F401  -- typed inter-agent messaging (agents skill)
from tools import lsp             # noqa: E402, F401  -- LSP tools (lsp skill)
from tools.search_ops import build_symbol_index  # noqa: E402, F401
from tools.mcp_client import get_mcp_manager, init_mcp_servers, shutdown_mcp  # noqa: E402, F401

# ---------------------------------------------------------------------------
# mcp_discover / mcp_call -- MCP client tools
# ---------------------------------------------------------------------------


@_register("mcp_discover")
def _mcp_discover(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """List all tools from all connected MCP servers."""
    manager = get_mcp_manager()
    return manager.discover()


@_summarize("mcp_discover")
def _mcp_discover_summary(_args: dict) -> str:
    return "mcp_discover()"


@_register("mcp_call")
def _mcp_call(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Call a tool on a specific MCP server."""
    server = args.get("server", "")
    tool = args.get("tool", "")
    arguments = args.get("arguments", {})
    if not server:
        return ToolResult(
            success=False,
            content="Missing required parameter: 'server' (MCP server name).",
            hint="Use mcp_discover to see available servers, then mcp_call with a server and tool name.",
        )
    if not tool:
        return ToolResult(
            success=False,
            content="Missing required parameter: 'tool' (tool name to call).",
            hint="Use mcp_discover to see available tools on each server.",
        )
    manager = get_mcp_manager()
    return manager.call(server, tool, arguments)


@_summarize("mcp_call")
def _mcp_call_summary(args: dict) -> str:
    server = args.get("server", "?")
    tool = args.get("tool", "?")
    return f"mcp_call({server}/{tool})"


# ---------------------------------------------------------------------------
# atexit cleanup -- tear down browser, LSP, MCP, background tasks on exit
# ---------------------------------------------------------------------------

import atexit as _atexit


def _cleanup_resources() -> None:
    """Best-effort cleanup of tool resources on process exit."""
    # Kill any background shell tasks
    for tid, proc in list(_TASK_REGISTRY.items()):
        try:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)
        except Exception:
            pass
    _TASK_REGISTRY.clear()

    # Close browser (Playwright)
    try:
        from tools.browser_ops import _close_browser as _close_browser_fn
        _close_browser_fn()
    except Exception:
        pass

    # Shutdown LSP connections
    try:
        from tools.lsp import shutdown_lsp as _shutdown_lsp_fn
        _shutdown_lsp_fn()
    except Exception:
        pass

    # Shutdown MCP servers
    try:
        shutdown_mcp()
    except Exception:
        pass


_atexit.register(_cleanup_resources)
