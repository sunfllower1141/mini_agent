#!/usr/bin/env python3
"""
tools package — tool definitions, execution, and structured results for mini_agent.

Every tool execution returns a ToolResult (never a raw exception).
All read and write paths route through the safety gates.
Shell commands and searches run sandboxed inside the workspace root.

Adding a new tool requires:
    1. A ``_<name>`` implementation function decorated with ``@_register("name")``.
    2. A ``_<name>_summary`` function decorated with ``@_summarize("name")``.
    3. An entry in ``TOOLS`` (the API schema sent to the LLM).

Submodules:
    file_ops    — read_file, write_file, edit_file, list_directory, file_info,
                  write_scratchpad, diff, restore_file, plan, plan_status
    shell_ops   — run_shell, task_status, search_files, run_tests, verify, git
    search_ops  — find_symbol, find_usages, semantic_search, web_search, recall_turn
    agent_ops   — spawn_agent, agent_status, collect_agent, collect_any,
                  agent_message, agent_read, agent_handoff, agent_inbox,
                  agent_subscribe, agent_extend, agent_cancel
    lsp         — lsp_definition, lsp_references, lsp_hover, lsp_diagnostics

"""

import json
import re
import subprocess
import sys
import threading
from dataclasses import dataclass

from core.safety import ReadSafetyGate, WriteSafetyGate
from tools.schema import TOOLS
from logging_setup import get_logger, log_tool_failure, log_tool_success, log_error_trace

_log = get_logger("tools")

# Hardcoded core schema for remember — always present even if schema.py is missing
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
# TOOL_SCHEMA_MAP — O(1) name→schema lookup for execute_tool() validation
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
from tools.result import ToolResult  # noqa: E402, F401 — re-exported for backward compat


# ---------------------------------------------------------------------------
# Tool dispatch registry
# ---------------------------------------------------------------------------

_TOOL_DISPATCH: dict[str, callable] = {}
_TOOL_SUMMARIES: dict[str, callable] = {}
# Cache: dispatch function signature inspection (P0.1 perf)
# Maps tool name -> bool (whether dispatch fn accepts on_output kwarg)
_DISPATCH_SIGNATURES: dict[str, bool] = {}

# ---------------------------------------------------------------------------
# Agent context — extracted to tools/context.py (re-exported for backward compat)
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

# Per-turn cache for read-only tools. Cleared by run_agent_turn each turn.
# Key: (tool_name, sorted_args_json). Cached read_file/file_info/etc.
_TOOL_CACHE: dict[str, "ToolResult"] = {}

# Files modified by write/edit — used by verify
_MODIFIED_FILES: set[str] = set()
_MODIFIED_FILES_LOCK = threading.Lock()

_TASK_REGISTRY: dict[str, subprocess.Popen] = {}  # background shell task registry

# ---------------------------------------------------------------------------
# File reservation system — extracted to tools/reservations.py
# ---------------------------------------------------------------------------
from tools.reservations import (  # noqa: E402, F401
    reserve_file,
    release_file,
    release_all_files,
)

# Sub-agent runtime registry (lazy init in config.init_session)
_AGENT_RUNTIME = None  # AgentRuntime — set by init_session
_CACHEABLE = frozenset({
    "read_file", "file_info", "list_directory",
    "search_files", "semantic_search", "web_search",
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

# ── use_skill gate: lazy tool loading ──
from tools.skills import USE_SKILL_SCHEMA, _use_skill  # noqa: E402

_TOOL_DISPATCH["use_skill"] = _use_skill
_TOOL_SUMMARIES["use_skill"] = lambda args: f"use_skill({args.get('name', '?')})"
# Inject schema so it's always in TOOLS (it's a core tool, always visible)
TOOLS.append(USE_SKILL_SCHEMA)


def clear_tool_cache() -> None:
    """Clear the per-turn tool cache. Called at the start of each agent turn."""
    _TOOL_CACHE.clear()


# ---------------------------------------------------------------------------
# JSON repair (extracted to tools/json_repair.py)
# ---------------------------------------------------------------------------
from tools.json_repair import repair_json as _repair_json  # noqa: E402, F401

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


def execute_tool(
    tool_call: dict,
    write_gate: WriteSafetyGate,
    read_gate: ReadSafetyGate,
    on_output: callable = None,
    approve_callback: callable = None,
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
        if cache_key in _TOOL_CACHE:
            return _TOOL_CACHE[cache_key]

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

    # --- Circuit breaker: reject tool calls that have been made 3+ times ---
    # with the same tool name and identical arguments in the recent window.
    tool_call_key = f"{name}:{json.dumps(args, sort_keys=True)}"
    recent_keys = getattr(_TOOL_CONTEXT, "_recent_tool_keys", None)
    if recent_keys is not None:
        from collections import Counter
        recent_list = list(recent_keys)
        if len(recent_list) >= 3:
            counts = Counter(recent_list)
            if counts.get(tool_call_key, 0) >= 3:
                return ToolResult(
                    success=False,
                    content=(
                        f"⛔ CIRCUIT BREAKER TRIPPED: '{name}' with these exact arguments "
                        f"has been called {counts[tool_call_key]} times in the last "
                        f"{len(recent_list)} tool calls.\n\n"
                        f"This is a HARD STOP. The same call keeps failing identically.\n"
                        f"Do NOT retry with the same arguments. Instead:\n"
                        f"  1. Read relevant files with read_file to understand current state\n"
                        f"  2. Check your assumptions — are you using the correct tool? correct path? correct parameter names?\n"
                        f"  3. Try a fundamentally different approach\n"
                        f"  4. Call remember() to capture what you've learned from these failures\n"
                        f"  5. If stuck, use web_search for help or ask the user to clarify"
                    ),
                ).with_typed_error(
                    "circuit_breaker",
                    retry_budget=0,
                    suggested_action="Stop retrying. Read files, check assumptions, try a different approach.",
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
        try:
            if accepts_on_output:
                _result_container.append(dispatch(args, write_gate, read_gate, on_output=on_output))
            else:
                _result_container.append(dispatch(args, write_gate, read_gate))
        except Exception as exc:
            _result_container.append(exc)

    t = threading.Thread(target=_run_and_capture, daemon=True)
    t.start()
    t.join(timeout=_TOOL_TIMEOUT)
    if t.is_alive():
        # Thread still running after timeout — it's stuck.
        # We cannot safely kill a Python thread, so return a timeout result.
        # The daemon thread will continue running but will be terminated
        # when the process exits.
        return ToolResult(
            success=False,
            content=f"Tool '{name}' timed out after {_TOOL_TIMEOUT}s.",
            hint=_build_error_hint(name, error_msg=f"timed out after {_TOOL_TIMEOUT}s"),
        )

    raw = _result_container[0]
    if isinstance(raw, Exception):
        raise raw
    result = raw

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
            # Clear in-memory counters on success — the agent recovered
            _TOOL_CONTEXT.__dict__.pop("_failure_patterns", None)

    # --- Cache invalidation: clear read cache on any write so subsequent ---
    # reads within the same turn see fresh content.  Also covers restore_file.
    _WRITE_TOOLS = frozenset({"write_file", "edit_file", "restore_file"})
    if result.success and name in _WRITE_TOOLS:
        _TOOL_CACHE.clear()

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

    # Cache successful read-only results (only when not streaming)
    if cache_key and result.success:
        _TOOL_CACHE[cache_key] = result

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
        return f"{name}(…)"
    return summarize(args)


# ---------------------------------------------------------------------------
# Import submodules to trigger @_register / @_summarize side effects
# ---------------------------------------------------------------------------

from tools import file_ops    # noqa: E402, F401
from tools import shell_ops   # noqa: E402, F401
from tools import search_ops  # noqa: E402, F401
from tools import browser_ops  # noqa: E402, F401  # browser automation tools
from tools import desktop_ops # noqa: E402, F401  # desktop automation tools
from tools import macos_ops   # noqa: E402, F401  # intensive macOS API integrations
from tools import agent_ops   # noqa: E402, F401
from tools import agent_todos  # noqa: E402, F401
from tools import agent_patterns  # noqa: E402, F401
from tools import agent_messages  # noqa: E402, F401
from tools import lsp         # noqa: E402, F401
from tools.search_ops import build_symbol_index  # noqa: E402, F401
from tools.mcp_client import get_mcp_manager, init_mcp_servers, shutdown_mcp  # noqa: E402, F401

# ---------------------------------------------------------------------------
# mcp_discover / mcp_call — MCP client tools
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
# atexit cleanup — tear down browser, LSP, MCP, background tasks on exit
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
            _log.debug("cleanup: background task %s kill failed", tid, exc_info=True)
    _TASK_REGISTRY.clear()

    # Close browser (Playwright)
    try:
        from tools.browser_ops import _close_browser as _close_browser_fn
        _close_browser_fn()
    except Exception:
        _log.debug("cleanup: browser close failed", exc_info=True)

    # Shutdown LSP connections
    try:
        from tools.lsp import shutdown_lsp as _shutdown_lsp_fn
        _shutdown_lsp_fn()
    except Exception:
        _log.debug("cleanup: LSP shutdown failed", exc_info=True)

    # Shutdown MCP servers
    try:
        shutdown_mcp()
    except Exception:
        _log.debug("cleanup: MCP shutdown failed", exc_info=True)


_atexit.register(_cleanup_resources)
